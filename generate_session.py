"""
generate_session.py — Run this ONCE locally to generate your SESSION_STRING.

Usage:
    python generate_session.py

It will ask for your phone number, send a Telegram login code,
then print a long SESSION_STRING. Copy that string into your
Render environment variables (or .env file for local testing).

You must have your API_ID and API_HASH from https://my.telegram.org first.
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession


def main():
    print("=" * 60)
    print("  Telegram Copy Bot — Session Generator")
    print("=" * 60)
    print()
    print("Get your API credentials from: https://my.telegram.org")
    print()

    api_id = int(input("Enter API_ID  : ").strip())
    api_hash = input("Enter API_HASH : ").strip()

    print()
    print("Opening Telegram login (you'll receive a code in the app)…")
    print()

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_string = client.session.save()

    print()
    print("=" * 60)
    print("  ✅ SUCCESS — Copy the string below into your Render")
    print("     environment variables as SESSION_STRING")
    print("=" * 60)
    print()
    print(session_string)
    print()
    print("⚠️  Keep this string secret. Anyone with it can log in as you.")


if __name__ == "__main__":
    main()
