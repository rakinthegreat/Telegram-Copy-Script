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

# Unique prefixes so we can find our state messages among other messages
# Now supports pair-specific markers
_TOPIC_MAP_PREFIX = "TGCOPY_TOPIC_MAP_"
_PROGRESS_PREFIX = "TGCOPY_PROGRESS_"
_LEGACY_TOPIC_MAP = "TGCOPY_TOPIC_MAP:"
_LEGACY_PROGRESS = "TGCOPY_PROGRESS:"

def _get_marker(prefix: str, src_id: int, dest_id: int) -> str:
    return f"{prefix}{src_id}_{dest_id}:"



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
        try:
            await existing.edit(content)
        except Exception as e:
            # MessageNotModifiedError: content unchanged — perfectly fine
            if "not modified" in str(e).lower():
                return
            raise
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

async def load_topic_map(client: TelegramClient, src_id: int, dest_id: int) -> dict:
    """Return {source_topic_id: dest_topic_id}. Empty dict if none saved."""
    marker = _get_marker(_TOPIC_MAP_PREFIX, src_id, dest_id)
    data = await _read(client, marker)
    logger.info("Loaded topic map for pair %d:%d (%d topics)", src_id, dest_id, len(data))
    return data


async def save_topic_map(client: TelegramClient, src_id: int, dest_id: int, topic_map: dict) -> None:
    """Persist the full topic map to the state channel."""
    marker = _get_marker(_TOPIC_MAP_PREFIX, src_id, dest_id)
    await _write(client, marker, topic_map)
    logger.debug("Saved topic map for pair %d:%d (%d entries)", src_id, dest_id, len(topic_map))


async def load_progress(client: TelegramClient, src_id: int, dest_id: int) -> dict:
    """Return {source_topic_id: last_copied_msg_id}. Empty dict if none saved."""
    marker = _get_marker(_PROGRESS_PREFIX, src_id, dest_id)
    data = await _read(client, marker)
    logger.info("Loaded progress for pair %d:%d (%d topics tracked)", src_id, dest_id, len(data))
    return data


async def save_progress(client: TelegramClient, src_id: int, dest_id: int, progress: dict) -> None:
    """Persist the copy progress dictionary to the state channel."""
    marker = _get_marker(_PROGRESS_PREFIX, src_id, dest_id)
    await _write(client, marker, progress)
    logger.debug("Saved progress for pair %d:%d", src_id, dest_id)


async def migrate_legacy_state(client: TelegramClient, first_src_id: int, first_dest_id: int) -> None:
    """
    Look for legacy state markers (from when the bot only supported a single pair).
    If found, migrate them to the new pair-specific format for the first pair.
    """
    legacy_prog_msg = await _find_message(client, _LEGACY_PROGRESS)
    if legacy_prog_msg:
        # Read old
        prog_data = await _read(client, _LEGACY_PROGRESS)
        if prog_data:
            # Save new
            await save_progress(client, first_src_id, first_dest_id, prog_data)
            logger.info("✅ Migrated legacy PROGRESS to pair %d:%d", first_src_id, first_dest_id)
            # Delete old
            await legacy_prog_msg.delete()

    legacy_map_msg = await _find_message(client, _LEGACY_TOPIC_MAP)
    if legacy_map_msg:
        # Read old
        map_data = await _read(client, _LEGACY_TOPIC_MAP)
        if map_data:
            # Save new
            await save_topic_map(client, first_src_id, first_dest_id, map_data)
            logger.info("✅ Migrated legacy TOPIC MAP to pair %d:%d", first_src_id, first_dest_id)
            # Delete old
            await legacy_map_msg.delete()
