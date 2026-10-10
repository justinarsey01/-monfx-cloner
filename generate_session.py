
import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("Enter your Telegram API ID: "))
api_hash = input("Enter your Telegram API HASH: ")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nYour SESSION_STRING is:\n")
    print(client.session.save())
    print("\nKeep this value private.")