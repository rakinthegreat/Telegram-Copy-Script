"""
reset_state.py — Clears the saved topic map and progress from the state channel.
Run this when you want to start fresh (e.g. after fixing a bug mid-run).
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
import config

_MARKERS = ("TGCOPY_TOPIC_MAP:", "TGCOPY_PROGRESS:")

async def main():
    client = TelegramClient(StringSession(config.SESSION_STRING), config.API_ID, config.API_HASH)
    await client.start()
    deleted = 0
    async for msg in client.iter_messages(config.STATE_CHANNEL_ID, limit=100):
        if msg.text and any(msg.text.startswith(m) for m in _MARKERS):
            await msg.delete()
            print(f"Deleted: {msg.text[:60]}...")
            deleted += 1
    print(f"\n Cleared {deleted} state message(s). Fresh run ready.")
    await client.disconnect()

asyncio.run(main())
