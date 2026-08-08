"""
copier.py — Core message copy logic.

Strategy: download everything into BytesIO (RAM), then re-upload.
This bypasses:
  • noforwards flag (forward restrictions)
  • Media protection / save restrictions
  • "Save to Gallery" blocks

Supports: text, photo, video, document, audio, voice, video note,
          sticker (static & animated), GIF, poll, albums (grouped media).

KEY INSIGHT: Telethon infers media type from the filename extension on the
BytesIO buffer. Without a correct `buf.name`, everything becomes a document.
"""
import asyncio
import logging
from io import BytesIO

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import SendMediaRequest
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    InputMediaPoll,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaPoll,
)

import config

logger = logging.getLogger(__name__)


# ── Flood-wait retry wrapper ──────────────────────────────────────────────────

async def _call(coro):
    """Execute a coroutine; on FloodWait, sleep and retry automatically."""
    while True:
        try:
            return await coro
        except FloodWaitError as e:
            wait = e.seconds + 2
            logger.warning("FloodWait: sleeping %ds…", wait)
            await asyncio.sleep(wait)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reply_to_kwarg(dest_topic_id: int) -> dict:
    """
    Build the reply_to kwarg needed to route a message into a specific topic.
    For the General topic (id=1) no kwarg is needed.
    """
    if dest_topic_id and dest_topic_id != 1:
        return {"reply_to": dest_topic_id}
    return {}


async def _download(client: TelegramClient, message) -> BytesIO | None:
    """Download message media into memory. Returns None if no media."""
    if not message.media:
        return None
    buf = BytesIO()
    await client.download_media(message, file=buf)
    buf.seek(0)
    return buf


def _mime_to_ext(mime: str) -> str:
    """Map a MIME type to a file extension Telethon will recognise."""
    return {
        "image/jpeg":       ".jpg",
        "image/jpg":        ".jpg",
        "image/png":        ".png",
        "image/webp":       ".webp",
        "image/gif":        ".gif",
        "video/mp4":        ".mp4",
        "video/webm":       ".webm",
        "audio/ogg":        ".ogg",
        "audio/mpeg":       ".mp3",
        "audio/mp4":        ".m4a",
        "application/pdf":  ".pdf",
    }.get(mime, ".bin")


def _classify(message) -> tuple[str | None, dict]:
    """
    Inspect the message media and return:
      (filename_with_ext, extra_send_file_kwargs)

    The filename extension is the key signal Telethon uses to determine
    whether to send as photo / video / sticker / document.
    Returns (None, {}) for photos — handled separately.
    """
    # ── Photo ─────────────────────────────────────────────────────────────────
    if isinstance(message.media, MessageMediaPhoto):
        return "photo.jpg", {"force_document": False}

    if not isinstance(message.media, MessageMediaDocument):
        return None, {}

    doc = message.media.document
    mime: str = doc.mime_type or ""
    attrs = {type(a).__name__: a for a in doc.attributes}

    # ── Static sticker (.webp) ────────────────────────────────────────────────
    if "DocumentAttributeSticker" in attrs:
        return "sticker.webp", {"force_document": False}

    # ── Animated sticker (.tgs) ───────────────────────────────────────────────
    if "DocumentAttributeAnimated" in attrs:
        # TGS files must be sent as documents (Telethon can't re-wrap them)
        return "sticker.tgs", {"force_document": True}

    # ── Video sticker (.webm) ─────────────────────────────────────────────────
    if mime == "video/webm" and "DocumentAttributeVideo" in attrs:
        va: DocumentAttributeVideo = attrs["DocumentAttributeVideo"]
        if getattr(va, "nosound", False):
            return "sticker.webm", {"force_document": False}

    # ── Video note (round bubble) ─────────────────────────────────────────────
    if "DocumentAttributeVideo" in attrs:
        va = attrs["DocumentAttributeVideo"]
        if va.round_message:
            return "video_note.mp4", {"video_note": True}
        # Regular video
        return "video.mp4", {}

    # ── Voice message ─────────────────────────────────────────────────────────
    if "DocumentAttributeAudio" in attrs:
        aa: DocumentAttributeAudio = attrs["DocumentAttributeAudio"]
        if aa.voice:
            ext = ".ogg" if "ogg" in mime else ".mp3"
            return f"voice{ext}", {"voice_note": True}
        # Regular audio file
        ext = _mime_to_ext(mime)
        name = attrs["DocumentAttributeFilename"].file_name if "DocumentAttributeFilename" in attrs else f"audio{ext}"
        return name, {}

    # ── Generic document — preserve original filename ─────────────────────────
    if "DocumentAttributeFilename" in attrs:
        return attrs["DocumentAttributeFilename"].file_name, {}

    # ── Fallback: infer extension from MIME ───────────────────────────────────
    return f"file{_mime_to_ext(mime)}", {}


# ── Public API ────────────────────────────────────────────────────────────────

async def copy_message(
    client: TelegramClient, message, dest_chat, dest_topic_id: int
) -> None:
    """
    Copy a single (non-album) message to dest_chat, routing it into
    the correct forum topic if specified.
    """
    route = _reply_to_kwarg(dest_topic_id)

    # ── Poll ──────────────────────────────────────────────────────────────────
    if isinstance(message.media, MessageMediaPoll):
        try:
            await _call(client(SendMediaRequest(
                peer=dest_chat,
                media=InputMediaPoll(poll=message.media.poll),
                message="",
                **({} if not route else {"reply_to": _make_reply_to(dest_topic_id)}),
            )))
        except Exception as e:
            logger.warning("Could not copy poll (id=%d): %s", message.id, e)
        return

    # ── Text only ─────────────────────────────────────────────────────────────
    if not message.media:
        if message.text:
            await _call(client.send_message(
                dest_chat,
                message=message.text,
                formatting_entities=message.entities,
                **route,
            ))
        return

    # ── All media types ───────────────────────────────────────────────────────
    buf = await _download(client, message)
    if buf is None:
        logger.warning("Could not download media for message %d, skipping.", message.id)
        return

    filename, extra = _classify(message)
    if filename:
        buf.name = filename  # ← critical: tells Telethon the file type

    await _call(client.send_file(
        dest_chat,
        file=buf,
        caption=message.text or "",
        formatting_entities=message.entities,
        **route,
        **extra,
    ))


async def copy_album(
    client: TelegramClient, messages: list, dest_chat, dest_topic_id: int
) -> None:
    """
    Copy a grouped album (multiple photos/videos with the same grouped_id)
    as a single album in the destination.
    """
    route = _reply_to_kwarg(dest_topic_id)

    files = []
    for msg in messages:
        buf = await _download(client, msg)
        if buf:
            filename, _ = _classify(msg)
            if filename:
                buf.name = filename  # ← set extension per item
            files.append(buf)

    if not files:
        return

    caption = next((m.text for m in messages if m.text), "") or ""
    entities = next((m.entities for m in messages if m.entities), None)

    await _call(client.send_file(
        dest_chat,
        file=files,
        caption=caption,
        formatting_entities=entities,
        **route,
    ))


# ── Internal helper needed for poll SendMediaRequest ─────────────────────────

def _make_reply_to(topic_id: int):
    """Build a ReplyToMessage object for the raw SendMediaRequest."""
    from telethon.tl.types import InputReplyToMessage
    return InputReplyToMessage(reply_to_msg_id=topic_id)
