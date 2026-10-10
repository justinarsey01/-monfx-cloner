from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = 36172139
API_HASH = "48c2263f359396145f5f9df9ffa07909"

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print(client.session.save())