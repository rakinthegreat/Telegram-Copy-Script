"""
state.py — Persistent state storage using a private Telegram channel.

Why a Telegram channel instead of disk?
  Render's free tier has an ephemeral filesystem — any file written to disk
  is lost when the service restarts or redeploys. Using a private Telegram
  channel as a key-value store gives us free, permanent persistence with
  zero extra accounts or services.

State stored:
  - TOPIC_MAP  : mapping of source topic IDs → destination topic IDs
  - PROGRESS   : last successfully copied message ID per source topic
"""
import json
import logging
from typing import Optional

from telethon import TelegramClient

import config

logger = logging.getLogger(__name__)

# Unique markers so we can find our state messages among other messages
_TOPIC_MAP_MARKER = "TGCOPY_TOPIC_MAP:"
_PROGRESS_MARKER = "TGCOPY_PROGRESS:"


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _find_message(client: TelegramClient, marker: str):
    """Scan the state channel for a message starting with `marker`."""
    async for msg in client.iter_messages(config.STATE_CHANNEL_ID, limit=100):
        if msg.text and msg.text.startswith(marker):
            return msg
    return None


async def _write(client: TelegramClient, marker: str, data: dict) -> None:
    """Write (or update) a state message in the state channel."""
    content = marker + json.dumps({str(k): v for k, v in data.items()}, separators=(",", ":"))
    existing = await _find_message(client, marker)
    if existing:
        await existing.edit(content)
    else:
        await client.send_message(config.STATE_CHANNEL_ID, content)


async def _read(client: TelegramClient, marker: str) -> dict:
    """Read a state message from the state channel. Returns {} if not found."""
    msg = await _find_message(client, marker)
    if not msg:
        return {}
    try:
        raw = msg.text[len(marker):]
        return {int(k): int(v) for k, v in json.loads(raw).items()}
    except Exception as e:
        logger.error("Failed to parse state '%s': %s", marker, e)
        return {}


# ── Public API ────────────────────────────────────────────────────────────────

async def load_topic_map(client: TelegramClient) -> dict:
    """Return {source_topic_id: dest_topic_id}. Empty dict if none saved."""
    data = await _read(client, _TOPIC_MAP_MARKER)
    logger.info("Loaded topic map: %d topics", len(data))
    return data


async def save_topic_map(client: TelegramClient, topic_map: dict) -> None:
    """Persist the full topic map to the state channel."""
    await _write(client, _TOPIC_MAP_MARKER, topic_map)
    logger.debug("Saved topic map (%d entries)", len(topic_map))


async def load_progress(client: TelegramClient) -> dict:
    """Return {source_topic_id: last_copied_msg_id}. Empty dict if none saved."""
    data = await _read(client, _PROGRESS_MARKER)
    logger.info("Loaded progress: %d topics tracked", len(data))
    return data


async def save_progress(client: TelegramClient, progress: dict) -> None:
    """Persist copy progress to the state channel."""
    await _write(client, _PROGRESS_MARKER, progress)
    logger.debug("Saved progress (%d topics)", len(progress))
