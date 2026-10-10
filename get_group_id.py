
import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("Enter your Telegram API ID: "))
API_HASH = input("Enter your Telegram API HASH: ")
SESSION_STRING = input("Paste your SESSION STRING: ")

with TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH
) as client:
    print("\nYour Telegram groups and channels:\n")

    for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            print(f"Name: {dialog.name}")
            print(f"Numeric ID: {dialog.id}")
            print("-" * 40)