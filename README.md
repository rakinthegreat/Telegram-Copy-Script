# Telegram Copy Bot

A Python userbot that mirrors a **private Telegram forum group** (with topics) to
another private group — including full history copy and live incremental sync.

Bypasses forward restrictions and save restrictions by **downloading and re-uploading**
every message instead of forwarding it.

Hosted free on [Render](https://render.com), kept alive 24/7 by [UptimeRobot](https://uptimerobot.com).

---

## Features

- ✅ Copies all message types: text, photos, videos, documents, audio, stickers (static & animated), voice notes, video notes, polls, albums
- ✅ Bypasses forward restrictions and media save restrictions
- ✅ Mirrors forum **topic structure** (creates matching topics in destination)
- ✅ Full history copy on first run, then live incremental sync
- ✅ Resumable — safely restarts from the last checkpoint if interrupted
- ✅ Flood-wait aware — automatically sleeps when Telegram asks it to
- ✅ Zero-cost deployment (Render free + UptimeRobot free)
- ✅ State stored in a private Telegram channel (no database needed)

---

## How It Works

```
Source Group (with topics)          Destination Group
  ├── Topic: Announcements    →       ├── Topic: Announcements
  ├── Topic: General          →       ├── Topic: General
  └── Topic: Resources        →       └── Topic: Resources
         │
         ▼
  [Userbot downloads to RAM]
         │
         ▼
  [Re-uploads to destination topic]
  (no "forwarded from" header, no restrictions)
```

---

## Prerequisites

- Python 3.11+
- A Telegram **user account** (not a bot account)
- Admin rights in both source group (to read messages) and destination group (to manage topics)
- A separate **private channel** that you own (for state storage)

---

## Step 1 — Get Telegram API Credentials

1. Go to [https://my.telegram.org](https://my.telegram.org)
2. Log in with your phone number
3. Click **API development tools**
4. Create a new application (any name/platform)
5. Copy your `API_ID` (a number) and `API_HASH` (a 32-char string)

---

## Step 2 — Find Your Chat IDs

Forward a message from each group/channel to [@userinfobot](https://t.me/userinfobot).
It will reply with the chat ID (a negative number like `-1001234567890`).

You need IDs for:
- **SOURCE_CHAT_ID** — the source forum group
- **DEST_CHAT_ID** — the destination group  
- **STATE_CHANNEL_ID** — a private channel you create just for this bot's state

---

## Step 3 — Generate Your Session String (run once, locally)

```bash
# Clone/download this project first
pip install telethon python-dotenv

python generate_session.py
```

Follow the prompts. Enter your phone number and the code Telegram sends you.
At the end it prints a long `SESSION_STRING`. **Copy it — you'll need it in Step 5.**

> ⚠️ Keep this string secret. It's equivalent to your Telegram password.

---

## Step 4 — Deploy to Render

1. Push this project to a GitHub repository
2. Go to [render.com](https://render.com) → **New** → **Web Service**
3. Connect your GitHub repo
4. Render will auto-detect `render.yaml` and configure the service
5. Click **Create Web Service**

Alternatively, use manual settings:
| Setting | Value |
|---|---|
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python app.py` |
| Plan | **Free** |

---

## Step 5 — Set Environment Variables on Render

In your Render service dashboard → **Environment** tab, add:

| Variable | Value |
|---|---|
| `API_ID` | Your API ID from my.telegram.org |
| `API_HASH` | Your API Hash from my.telegram.org |
| `SESSION_STRING` | The string from Step 3 |
| `SOURCE_CHAT_ID` | e.g. `-1001234567890` |
| `DEST_CHAT_ID` | e.g. `-1009876543210` |
| `STATE_CHANNEL_ID` | e.g. `-1001112223334` |
| `COPY_HISTORY` | `true` (copy history) or `false` (live only) |
| `DELAY_BETWEEN_MSGS` | `1.5` (safe default) |

Click **Save** — Render will redeploy automatically.

---

## Step 6 — Set Up UptimeRobot (Free 24/7)

1. Create a free account at [uptimerobot.com](https://uptimerobot.com)
2. Click **Add New Monitor**
3. Settings:
   - **Monitor Type:** HTTP(s)
   - **Friendly Name:** Telegram Copy Bot
   - **URL:** `https://your-app-name.onrender.com/health`
   - **Monitoring Interval:** 5 minutes
4. Click **Create Monitor**

UptimeRobot now pings your service every 5 minutes, preventing Render from sleeping it.

---

## Local Development

```bash
# Copy and fill in environment variables
cp .env.example .env
# Edit .env with your actual values

# Install dependencies
pip install -r requirements.txt

# Run
python app.py
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `API_ID` | — | **Required.** From my.telegram.org |
| `API_HASH` | — | **Required.** From my.telegram.org |
| `SESSION_STRING` | — | **Required.** From generate_session.py |
| `SOURCE_CHAT_ID` | — | **Required.** Source forum group ID |
| `DEST_CHAT_ID` | — | **Required.** Destination group ID |
| `STATE_CHANNEL_ID` | — | **Required.** Private channel for state |
| `COPY_HISTORY` | `true` | Copy existing messages on startup |
| `DELAY_BETWEEN_MSGS` | `1.5` | Seconds between sends (flood protection) |
| `HISTORY_BATCH_SIZE` | `200` | Messages fetched per API call |
| `PORT` | `10000` | Flask server port |

---

## Troubleshooting

**FloodWaitError** — Telegram is rate-limiting you. Increase `DELAY_BETWEEN_MSGS` to `3.0` or higher.

**Topic not found in destination** — Make sure the destination group has **Topics** enabled (Group Settings → Topics). You must be admin with "Manage Topics" permission.

**Session expired** — Re-run `generate_session.py` and update `SESSION_STRING` in Render.

**Messages missing** — Check Render logs. The bot may have restarted during history copy; it will automatically resume from the last checkpoint on next start.

---

## Project Structure

```
├── app.py                # Main entry point
├── copier.py             # Message download + re-upload logic
├── topics.py             # Forum topic discovery and creation
├── state.py              # Persistent state via Telegram channel
├── config.py             # Environment variable configuration
├── generate_session.py   # One-time session string generator
├── requirements.txt
├── Procfile
├── render.yaml
└── .env.example
```
