import asyncio
import json
import os
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from flask import Flask
import threading

API_ID = 36172139
API_HASH = "48c2263f359396145f5f9df9ffa07909"
FROM_INVITE = "https://t.me/+1_4KcZxZuMJkYTE8"
TO_CHANNEL = "https://t.me/monfxtradingofficial"
MAP_FILE = "reply_map.json"

if os.path.exists(MAP_FILE):
    with open(MAP_FILE, 'r') as f:
        id_map = json.load(f)
else:
    id_map = {}

id_map = {int(k): int(v) for k, v in id_map.items()}

def save_map():
    with open(MAP_FILE, 'w') as f:
        json.dump(id_map, f)

app = Flask(__name__)

@app.route('/')
def home():
    return f"Cloner running - {len(id_map)} mapped"

threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()

client = TelegramClient('monfx_session', API_ID, API_HASH)

async def main():
    while True:
        try:
            await client.start()
            from_group = await client.get_entity(FROM_INVITE)
            print(f"Watching: {from_group.title}")

            @client.on(events.NewMessage(chats=from_group))
            async def handler(event):
                try:
                    reply_to_dest_id = None
                    if event.message.is_reply:
                        replied_id = event.message.reply_to.reply_to_msg_id
                        if replied_id in id_map:
                            reply_to_dest_id = id_map[replied_id]

                    if reply_to_dest_id:
                        sent = await client.send_message(TO_CHANNEL, event.message, reply_to=reply_to_dest_id)
                    else:
                        sent = await client.send_message(TO_CHANNEL, event.message)

                    id_map[event.message.id] = sent.id
                    save_map()
                    print(f"Copied {event.message.id} -> {sent.id}")
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    print(f"Error: {e}")

            await client.run_until_disconnected()
        except Exception as e:
            print(f"Restarting: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
