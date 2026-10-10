from dotenv import load_dotenv
import asyncio
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, MessageNotModifiedError

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

    @client.on(events.NewMessage(chats=source))
    async def on_new(event):
        try:
            original = event.message
            destination_reply_id = None

            if original.reply_to and original.reply_to.reply_to_msg_id:
                source_reply_id = original.reply_to.reply_to_msg_id
                destination_reply_id = id_map.get(source_reply_id)

            # Copy the message content and media without forwarding attribution.
            sent = await client.send_message(
                target,
                original,
                reply_to=destination_reply_id,
            )

            with map_lock:
                id_map[original.id] = sent.id
            save_map()

            print(f"Copied message {original.id} -> {sent.id}")

        except FloodWaitError as exc:
            print(f"Telegram rate limit: wait {exc.seconds} seconds")
            await asyncio.sleep(exc.seconds)
        except Exception as exc:
            print(f"Message copy failed: {exc}")

    @client.on(events.MessageEdited(chats=source))
    async def on_edit(event):
        try:
            edited = event.message
            destination_id = id_map.get(edited.id)

            if destination_id is None:
                # Message was posted before the last restart, or never copied.
                print(f"Edit on {edited.id} skipped: no mapped copy")
                return

            # Edits text (or media caption) and keeps formatting.
            await client.edit_message(
                target,
                destination_id,
                text=edited.raw_text or "",
                formatting_entities=edited.entities,
                link_preview=bool(getattr(edited.media, "webpage", None)),
            )

            print(f"Edited message {edited.id} -> {destination_id}")

        except MessageNotModifiedError:
            # Nothing changed on our side (e.g. a reaction or media-only edit)
            pass
        except FloodWaitError as exc:
            print(f"Telegram rate limit: wait {exc.seconds} seconds")
            await asyncio.sleep(exc.seconds)
        except Exception as exc:
            print(f"Message edit failed: {exc}")

    @client.on(events.MessageDeleted(chats=source))
    async def on_delete(event):
        try:
            source_ids = list(event.deleted_ids)
            destination_ids = [
                id_map[i] for i in source_ids if i in id_map
            ]

            if not destination_ids:
                print(f"Delete of {source_ids} skipped: no mapped copies")
                return

            await client.delete_messages(target, destination_ids)

            # Forget the deleted messages so the map doesn't keep growing
            with map_lock:
                for i in source_ids:
                    id_map.pop(i, None)
            save_map()

            print(f"Deleted copies {destination_ids} for source {source_ids}")

        except FloodWaitError as exc:
            print(f"Telegram rate limit: wait {exc.seconds} seconds")
            await asyncio.sleep(exc.seconds)
        except Exception as exc:
            print(f"Message delete failed: {exc}")

    try:
        print("Listening for new, edited and deleted messages...")
        await client.run_until_disconnected()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    asyncio.run(main())