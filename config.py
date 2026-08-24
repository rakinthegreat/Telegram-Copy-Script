"""
config.py — All configuration loaded from environment variables.
Copy .env.example to .env and fill in your values for local development.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _required(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise EnvironmentError(
            f"❌ Missing required environment variable: {key}\n"
            f"   See .env.example for instructions."
        )
    return val


# ── Telegram API credentials (from https://my.telegram.org) ──────────────────
API_ID: int = int(_required("API_ID"))
API_HASH: str = _required("API_HASH")

# ── Telethon StringSession (generated once via generate_session.py) ───────────
SESSION_STRING: str = _required("SESSION_STRING")

# ── Chat Pairs (Format: source1:dest1,source2:dest2) ──────────────────────────
def _parse_pairs(raw: str) -> list[tuple[int, int]]:
    pairs = []
    for pair in raw.split(","):
        if ":" in pair:
            src, dst = pair.split(":")
            pairs.append((int(src.strip()), int(dst.strip())))
    if not pairs:
        raise EnvironmentError("❌ CHAT_PAIRS is empty or invalid. Format: src1:dest1,src2:dest2")
    return pairs

CHAT_PAIRS: list[tuple[int, int]] = _parse_pairs(_required("CHAT_PAIRS"))

# Private channel used to persist topic map + copy progress across restarts
STATE_CHANNEL_ID: int = int(_required("STATE_CHANNEL_ID"))

# ── Behaviour ─────────────────────────────────────────────────────────────────
# Copy all existing history on startup (then switch to live mode)
COPY_HISTORY: bool = os.environ.get("COPY_HISTORY", "true").lower() == "true"

# Hours to spend copying a single pair before rotating to the next
TIME_SLICE_HOURS: float = float(os.environ.get("TIME_SLICE_HOURS", "0.5"))

# If true: skip text-only messages and polls — copy only media (photos, videos,
# documents, stickers, voice, etc.)  Default: false (copy everything)
MEDIA_ONLY: bool = os.environ.get("MEDIA_ONLY", "false").lower() == "true"

# Maximum file size to download/copy in MB. Files larger than this will be skipped.
MAX_FILE_SIZE_MB: int = int(os.environ.get("MAX_FILE_SIZE_MB", "300"))

# Seconds to wait between each sent message to avoid Telegram flood limits
DELAY_BETWEEN_MSGS: float = float(os.environ.get("DELAY_BETWEEN_MSGS", "1.5"))

# Maximum messages to buffer per history fetch (lower = less RAM on free tier)
HISTORY_BATCH_SIZE: int = int(os.environ.get("HISTORY_BATCH_SIZE", "200"))

# ── Server ────────────────────────────────────────────────────────────────────
# Port for the Flask health-check server (Render sets this automatically)
PORT: int = int(os.environ.get("PORT", "10000"))
