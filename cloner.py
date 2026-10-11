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

# Read credentials from Render environment variables
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

# Prefer numeric source chat ID, e.g. -1001234567890.
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHANNEL = os.environ["TARGET_CHANNEL"]

# Text added at the top of copies of forwarded posts.
FORWARD_LABEL = os.environ.get("FORWARD_LABEL", "↪️ Forwarded from")

# Keep-alive: Render free web services spin down after 15 minutes without
# inbound traffic. Pinging our own public URL every 10 minutes prevents that.
# RENDER_EXTERNAL_URL is set automatically by Render.
KEEPALIVE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "600"))

MAP_FILE = Path("reply_map.json")
map_lock = threading.Lock()

if MAP_FILE.exists():
    try:
        id_map = {
            int(k): int(v)
            for k, v in json.loads(MAP_FILE.read_text()).items()
        }
    except (ValueError, OSError, json.JSONDecodeError):
        id_map = {}
else:
    id_map = {}


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
        "mapped_messages": len(id_map)
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


async def main():
    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
    )

    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telegram session is invalid or expired. "
            "Generate a new SESSION_STRING."
        )

    source = await client.get_entity(SOURCE_CHAT_ID)
    target = await client.get_entity(TARGET_CHANNEL)

    print("Telegram account connected.")
    print(f"Watching source: {source.title if hasattr(source, 'title') else source.id}")
    print(f"Copying to target: {target.id}")

    # Handle one event at a time, in arrival order. This keeps copies in the
    # right order, makes sure a reply's parent is already mapped, and means an
    # edit or delete never races ahead of the copy it refers to.
    send_lock = asyncio.Lock()

    @client.on(events.NewMessage(chats=source))
    async def on_new(event):
        original = event.message
        kind = type(original.media).__name__ if original.media else "text"
        forwarded = ", forwarded" if original.fwd_from else ""
        print(f"Received message {original.id} ({kind}{forwarded})")

        try:
            async with send_lock:
                reply_id = None
                if original.reply_to and original.reply_to.reply_to_msg_id:
                    reply_id = id_map.get(original.reply_to.reply_to_msg_id)

                try:
                    sent = await retry_on_flood(
                        lambda: copy_message(client, target, original, reply_id)
                    )
                except RPCError as exc:
                    # The message being replied to may no longer exist in the
                    # target (e.g. it was deleted). Post without the reply
                    # rather than losing the message.
                    if reply_id and "MESSAGE_ID_INVALID" in (exc.message or ""):
                        print(
                            f"Reply target {reply_id} missing in target; "
                            f"copying {original.id} without reply"
                        )
                        sent = await retry_on_flood(
                            lambda: copy_message(client, target, original, None)
                        )
                    else:
                        raise

                if sent is None:
                    print(f"Message {original.id} skipped: nothing to copy")
                    return

                with map_lock:
                    id_map[original.id] = sent.id
                save_map()

                print(f"Copied message {original.id} -> {sent.id}")

        except Exception as exc:
            print(f"Message copy FAILED for {original.id}: {exc!r}")

    @client.on(events.MessageEdited(chats=source))
    async def on_edit(event):
        edited = event.message
        try:
            async with send_lock:
                destination_id = id_map.get(edited.id)

                if destination_id is None:
                    # Message was posted before the last restart, or never copied.
                    print(f"Edit on {edited.id} skipped: no mapped copy")
                    return

                text, entities = await build_content(edited)

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

                print(f"Edited message {edited.id} -> {destination_id}")

        except MessageNotModifiedError:
            # Nothing changed on our side (e.g. a reaction or media-only edit)
            pass
        except Exception as exc:
            print(f"Message edit FAILED for {edited.id}: {exc!r}")

    @client.on(events.MessageDeleted(chats=source))
    async def on_delete(event):
        source_ids = list(event.deleted_ids)
        try:
            async with send_lock:
                destination_ids = [
                    id_map[i] for i in source_ids if i in id_map
                ]

                if not destination_ids:
                    print(f"Delete of {source_ids} skipped: no mapped copies")
                    return

                await retry_on_flood(
                    lambda: client.delete_messages(target, destination_ids)
                )

                # Forget the deleted messages so the map doesn't keep growing
                with map_lock:
                    for i in source_ids:
                        id_map.pop(i, None)
                save_map()

                print(f"Deleted copies {destination_ids} for source {source_ids}")

        except Exception as exc:
            print(f"Message delete FAILED for {source_ids}: {exc!r}")

    try:
        print("Listening for new, edited and deleted messages...")
        await client.run_until_disconnected()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    asyncio.run(main())
