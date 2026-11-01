from dotenv import load_dotenv
import asyncio
import copy
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from flask import Flask
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    MediaCaptionTooLongError,
    MessageNotModifiedError,
    RPCError,
)
from telethon.tl.types import MessageEntityBold, MessageMediaWebPage

load_dotenv()

# Make logs show up immediately on Render
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def parse_chat(value):
    """Numeric IDs become ints; @usernames and links stay as strings."""
    value = str(value).strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def split_targets(value):
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        raise RuntimeError('"targets" must be a list (or comma-separated text)')
    return [parse_chat(v) for v in items if str(v).strip()]


def load_routes():
    """Read the routes: each one copies a source channel to its own targets.

    Preferred: set ROUTES to JSON, e.g.

      ROUTES=[
        {"name": "Client A", "source": -1001111111111,
         "targets": ["@client_a_channel", "-1002222222222"]},
        {"name": "Client B", "source": "@client_b_source",
         "targets": ["@client_b_channel"], "account": "2"},
        {"name": "Client C", "source": -1003333333333,
         "targets": ["@client_c"], "enabled": false}
      ]

    "account" picks which Telegram account runs the route ("main" if left
    out). Extra accounts come from SESSION_STRING_<NAME> variables, e.g.
    SESSION_STRING_2 is used by routes with "account": "2".

    Without ROUTES, the old settings SOURCE_CHAT_ID + TARGET_CHANNEL
    (comma-separated) still work as a single route on the main account.
    """
    raw = os.environ.get("ROUTES", "").strip()

    if not raw:
        return [{
            "name": "default",
            "source": parse_chat(os.environ["SOURCE_CHAT_ID"]),
            "targets": split_targets(os.environ["TARGET_CHANNEL"]),
            "account": "main",
            "enabled": True,
        }]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ROUTES is not valid JSON: {exc}") from exc

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        raise RuntimeError("ROUTES must be a non-empty JSON list of routes")

    routes = []
    for number, item in enumerate(data, 1):
        if not isinstance(item, dict) or "source" not in item or "targets" not in item:
            raise RuntimeError(f'Route #{number} needs both "source" and "targets"')
        routes.append({
            "name": str(item.get("name") or f"route {number}"),
            "source": parse_chat(item["source"]),
            "targets": split_targets(item["targets"]),
            "account": str(item.get("account") or "main").strip().lower(),
            "enabled": item.get("enabled", True) is not False,
        })
    return routes


def load_accounts():
    """{account name: session string}. 'main' is SESSION_STRING; every
    SESSION_STRING_<NAME> variable adds another account called <NAME>."""
    accounts = {"main": os.environ["SESSION_STRING"].strip()}
    prefix = "SESSION_STRING_"
    for key, value in os.environ.items():
        name = key[len(prefix):].strip().lower() if key.startswith(prefix) else ""
        if name and name != "main" and value.strip():
            accounts[name] = value.strip()
    return accounts


# Read credentials from Render environment variables
# (the same API_ID / API_HASH are used for every account)
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

ROUTES = load_routes()

# Text added at the top of copies of forwarded posts.
FORWARD_LABEL = os.environ.get("FORWARD_LABEL", "↪️ Forwarded from")

# Keep-alive: Render free web services spin down after 15 minutes without
# inbound traffic. Pinging our own public URL every 10 minutes prevents that.
# RENDER_EXTERNAL_URL is set automatically by Render.
KEEPALIVE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "600"))

MAP_FILE = Path("reply_map.json")
map_lock = threading.Lock()

# Names of the accounts that are up and listening (shown at "/").
running_accounts = []


def map_key(source_id, target_id):
    return f"{source_id}:{target_id}"


def load_saved_map():
    """Load {"<source_id>:<target_id>": {source_msg_id: copied_msg_id}}."""
    if not MAP_FILE.exists():
        return {}
    try:
        raw = json.loads(MAP_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

    try:
        if raw and not all(
            isinstance(k, str) and ":" in k and isinstance(v, dict)
            for k, v in raw.items()
        ):
            print(
                "Saved message map is from an older version; starting fresh "
                "(older posts won't be matched for replies, edits or deletes)."
            )
            return {}
        return {
            k: {int(a): int(b) for a, b in mapping.items()}
            for k, mapping in raw.items()
        }
    except (ValueError, TypeError, AttributeError):
        return {}


# id_map[map_key(source_id, target_id)][source_msg_id] = copied_msg_id
id_map = load_saved_map()


def save_map():
    with map_lock:
        temp_file = MAP_FILE.with_suffix(".tmp")
        temp_file.write_text(json.dumps(id_map))
        temp_file.replace(MAP_FILE)


app = Flask(__name__)


@app.get("/")
def home():
    return {
        "status": "online",
        "accounts": list(running_accounts),
        "route_targets": len(id_map),
        "mapped_messages": sum(len(m) for m in id_map.values()),
    }


@app.get("/health")
def health():
    return {"status": "ok"}, 200


def run_web():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


def keep_alive():
    if not KEEPALIVE_URL:
        print("RENDER_EXTERNAL_URL not set; keep-alive pings disabled.")
        return

    url = f"{KEEPALIVE_URL}/health"
    print(f"Keep-alive pinging {url} every {KEEPALIVE_INTERVAL}s")

    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                resp.read()
        except Exception as exc:
            print(f"Keep-alive ping failed: {exc}")


# ---------------------------------------------------------------------------
# Message content helpers
# ---------------------------------------------------------------------------

def utf16_len(text):
    # Telegram entity offsets/lengths are measured in UTF-16 code units.
    return len(text.encode("utf-16-le")) // 2


def name_of(chat):
    return (
        getattr(chat, "title", None)
        or getattr(chat, "username", None)
        or str(getattr(chat, "id", chat))
    )


async def forward_source_name(message):
    """Name of the original author/channel of a forwarded message, or None."""
    fwd = message.fwd_from
    if not fwd:
        return None

    # Hidden senders only expose a plain name.
    if getattr(fwd, "from_name", None):
        return fwd.from_name

    try:
        forward = message.forward
        entity = None
        if forward:
            entity = await forward.get_chat() or await forward.get_sender()
        if entity:
            return utils.get_display_name(entity) or "Unknown source"
    except Exception as exc:
        print(f"Could not resolve forward source: {exc}")

    return "Unknown source"


async def build_content(message):
    """Return (text, entities) for the copy, adding a 'Forwarded from' header
    when the source message was itself forwarded."""
    text = message.raw_text or ""
    entities = list(message.entities or [])

    source_name = await forward_source_name(message)
    if not source_name:
        return text, entities

    header = f"{FORWARD_LABEL} {source_name}"
    prefix = header + ("\n\n" if text else "")
    shift = utf16_len(prefix)

    shifted = [MessageEntityBold(offset=0, length=utf16_len(header))]
    for entity in entities:
        moved = copy.copy(entity)
        moved.offset += shift
        shifted.append(moved)

    return prefix + text, shifted


async def retry_on_flood(action, attempts=3):
    """Run action(); if Telegram rate-limits us, wait and try again instead of
    silently dropping the message."""
    for attempt in range(1, attempts + 1):
        try:
            return await action()
        except FloodWaitError as exc:
            if attempt == attempts:
                raise
            print(
                f"Rate limited: waiting {exc.seconds}s "
                f"(attempt {attempt}/{attempts})"
            )
            await asyncio.sleep(exc.seconds + 1)


async def copy_message(client, target, original, reply_to):
    """Copy a message (text + media) to the target without forward attribution.
    Returns the sent message, or None if there was nothing to copy."""
    text, entities = await build_content(original)

    media = original.media
    if isinstance(media, MessageMediaWebPage):
        media = None  # link previews are regenerated from the text

    if not media and not text:
        return None  # service message (pin, join, etc.)

    async def send(body, body_entities):
        if media:
            return await client.send_file(
                target,
                media,
                caption=body,
                formatting_entities=body_entities,
                reply_to=reply_to,
                buttons=original.reply_markup,
            )
        return await client.send_message(
            target,
            body,
            formatting_entities=body_entities,
            reply_to=reply_to,
            buttons=original.reply_markup,
        )

    try:
        return await send(text, entities)
    except MediaCaptionTooLongError:
        # Adding the "Forwarded from" line pushed the caption over the limit.
        print(
            f"Caption too long with forward label on {original.id}; "
            "sending without the label"
        )
        return await send(
            original.raw_text or "",
            list(original.entities or []),
        )


async def find_entity(client, spec, state):
    """client.get_entity(), but if a numeric ID isn't known to this session
    yet (a fresh session starts with no chats cached), load the account's chat
    list once and try again."""
    try:
        return await client.get_entity(spec)
    except ValueError:
        if state["dialogs_loaded"]:
            raise
        state["dialogs_loaded"] = True
        print("Channel not cached yet; loading this account's chat list once...")
        await client.get_dialogs()
        return await client.get_entity(spec)


async def resolve_routes(client, specs):
    """Look up every route's source and targets. Anything that can't be found
    is skipped and logged, so one bad entry never stops the other clients."""
    routes = []
    state = {"dialogs_loaded": False}

    for spec in specs:
        name = spec["name"]

        try:
            source = await find_entity(client, spec["source"], state)
        except Exception as exc:
            print(f"[{name}] could not resolve source {spec['source']!r}: "
                  f"{exc!r} - route skipped")
            continue

        targets = []
        for target_spec in spec["targets"]:
            try:
                entity = await find_entity(client, target_spec, state)
            except Exception as exc:
                print(f"[{name}] could not resolve target {target_spec!r}: "
                      f"{exc!r} - target skipped")
                continue

            if entity.id == source.id:
                print(f"[{name}] target {target_spec!r} is the source channel "
                      "itself - skipped (would loop forever)")
                continue
            if any(t.id == entity.id for t in targets):
                print(f"[{name}] target {target_spec!r} listed twice - "
                      "ignoring the duplicate")
                continue

            targets.append(entity)
            id_map.setdefault(map_key(source.id, entity.id), {})

        if not targets:
            print(f"[{name}] no valid targets - route skipped")
            continue

        routes.append(SimpleNamespace(name=name, source=source, targets=targets))

    return routes


async def run_account(account, session_string, specs, started):
    """Log in one Telegram account and run the routes assigned to it."""
    tag = f"account {account}"
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

    try:
        await client.connect()

        if not await client.is_user_authorized():
            print(f"[{tag}] session is invalid or expired - generate a new "
                  "session string. This account was not started.")
            return

        try:
            me = await client.get_me()
            print(f"[{tag}] connected as {utils.get_display_name(me)}")
        except Exception:
            print(f"[{tag}] connected")

        routes = await resolve_routes(client, specs)
        if not routes:
            print(f"[{tag}] no usable routes - account not started")
            return

        # Handle one event at a time per account, in arrival order. This keeps
        # copies in the right order, makes sure a reply's parent is already
        # mapped, and means an edit or delete never races ahead of its copy.
        # Each account has its own queue, so accounts don't slow each other.
        send_lock = asyncio.Lock()

        async def copy_to_target(route, target, original):
            target_map = id_map[map_key(route.source.id, target.id)]
            label = f"{route.name} > {name_of(target)}"
            try:
                reply_id = None
                if original.reply_to and original.reply_to.reply_to_msg_id:
                    reply_id = target_map.get(original.reply_to.reply_to_msg_id)

                try:
                    sent = await retry_on_flood(
                        lambda: copy_message(client, target, original, reply_id)
                    )
                except RPCError as exc:
                    # The message being replied to may no longer exist in this
                    # target (e.g. it was deleted). Post without the reply
                    # rather than losing the message.
                    if reply_id and "MESSAGE_ID_INVALID" in (exc.message or ""):
                        print(
                            f"Reply target {reply_id} missing in {label}; "
                            f"copying {original.id} without reply"
                        )
                        sent = await retry_on_flood(
                            lambda: copy_message(client, target, original, None)
                        )
                    else:
                        raise

                if sent is None:
                    print(f"Message {original.id} skipped for {label}: "
                          "nothing to copy")
                    return

                with map_lock:
                    target_map[original.id] = sent.id
                save_map()

                print(f"Copied message {original.id} -> {sent.id} [{label}]")

            except Exception as exc:
                print(f"Message copy FAILED for {original.id} [{label}]: {exc!r}")

        async def edit_in_target(route, target, edited, text, entities):
            label = f"{route.name} > {name_of(target)}"
            destination_id = id_map[map_key(route.source.id, target.id)].get(
                edited.id
            )

            if destination_id is None:
                # Posted before the last restart, or never copied here.
                print(f"Edit on {edited.id} skipped for {label}: no mapped copy")
                return

            try:
                # Edits text (or media caption) and keeps formatting.
                await retry_on_flood(
                    lambda: client.edit_message(
                        target,
                        destination_id,
                        text=text,
                        formatting_entities=entities,
                        link_preview=bool(getattr(edited.media, "webpage", None)),
                    )
                )
                print(f"Edited message {edited.id} -> {destination_id} [{label}]")
            except MessageNotModifiedError:
                # Nothing changed on our side (e.g. a reaction or media-only edit)
                pass
            except Exception as exc:
                print(f"Message edit FAILED for {edited.id} [{label}]: {exc!r}")

        async def delete_in_target(route, target, source_ids):
            label = f"{route.name} > {name_of(target)}"
            target_map = id_map[map_key(route.source.id, target.id)]
            destination_ids = [
                target_map[i] for i in source_ids if i in target_map
            ]

            if not destination_ids:
                print(f"Delete of {source_ids} skipped for {label}: "
                      "no mapped copies")
                return

            try:
                await retry_on_flood(
                    lambda: client.delete_messages(target, destination_ids)
                )

                # Forget the deleted messages so the map doesn't keep growing
                with map_lock:
                    for i in source_ids:
                        target_map.pop(i, None)
                save_map()

                print(f"Deleted copies {destination_ids} for source "
                      f"{source_ids} [{label}]")
            except Exception as exc:
                print(f"Message delete FAILED for {source_ids} [{label}]: {exc!r}")

        def register(route):
            # A separate function per route, so each handler keeps its route.

            @client.on(events.NewMessage(chats=route.source))
            async def on_new(event):
                original = event.message
                kind = type(original.media).__name__ if original.media else "text"
                forwarded = ", forwarded" if original.fwd_from else ""
                print(f"[{route.name}] Received message {original.id} "
                      f"({kind}{forwarded})")

                try:
                    async with send_lock:
                        # Each target is independent: a failure in one never
                        # blocks the others.
                        for target in route.targets:
                            await copy_to_target(route, target, original)
                except Exception as exc:
                    print(f"[{route.name}] Message copy FAILED for "
                          f"{original.id}: {exc!r}")

            @client.on(events.MessageEdited(chats=route.source))
            async def on_edit(event):
                edited = event.message
                try:
                    async with send_lock:
                        text, entities = await build_content(edited)
                        for target in route.targets:
                            await edit_in_target(route, target, edited, text, entities)
                except Exception as exc:
                    print(f"[{route.name}] Message edit FAILED for "
                          f"{edited.id}: {exc!r}")

            @client.on(events.MessageDeleted(chats=route.source))
            async def on_delete(event):
                source_ids = list(event.deleted_ids)
                try:
                    async with send_lock:
                        for target in route.targets:
                            await delete_in_target(route, target, source_ids)
                except Exception as exc:
                    print(f"[{route.name}] Message delete FAILED for "
                          f"{source_ids}: {exc!r}")

        for route in routes:
            print(f"[{route.name}] ({tag}) watching {name_of(route.source)} -> "
                  + ", ".join(name_of(t) for t in route.targets))
            register(route)

        started.append(account)
        running_accounts.append(account)
        print(f"[{tag}] listening for new, edited and deleted messages...")
        await client.run_until_disconnected()

    except Exception as exc:
        print(f"[{tag}] stopped with error: {exc!r}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def main():
    accounts = load_accounts()

    # Group the enabled routes by the account that runs them.
    by_account = {}
    for spec in ROUTES:
        name = spec["name"]

        if not spec["enabled"]:
            print(f"[{name}] disabled - skipping")
            continue
        if spec["account"] not in accounts:
            print(f"[{name}] unknown account {spec['account']!r} "
                  f"(known: {', '.join(sorted(accounts))}) - route skipped")
            continue

        by_account.setdefault(spec["account"], []).append(spec)

    if not by_account:
        raise RuntimeError(
            "No usable routes. Check ROUTES (or SOURCE_CHAT_ID and "
            "TARGET_CHANNEL) and the account names."
        )

    # Accounts run side by side; one failing doesn't stop the others.
    started = []
    await asyncio.gather(*(
        run_account(account, accounts[account], specs, started)
        for account, specs in by_account.items()
    ))

    if not started:
        raise RuntimeError(
            "No account could start. See the lines above: an expired session, "
            "or channels the account hasn't joined."
        )


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    asyncio.run(main())