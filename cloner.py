from dotenv import load_dotenv
import asyncio
import json
import os
import threading
from pathlib import Path

from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
load_dotenv()
# Read credentials from Render environment variables
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

# Prefer numeric source chat ID, e.g. -1001234567890.
# Set SOURCE_CHAT_ID in Render after obtaining the correct ID.
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHANNEL = os.environ["TARGET_CHANNEL"]

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

    print(f"Telegram account connected.")
    print(f"Watching source: {source.title if hasattr(source, 'title') else source.id}")
    print(f"Copying to target: {target.id}")

    @client.on(events.NewMessage(chats=source))
    async def handler(event):
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

    try:
        print("Listening for new messages...")
        await client.run_until_disconnected()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(main())