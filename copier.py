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
import os
import tempfile
from io import BytesIO
from typing import Union

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
    Build the reply_to kwarg to route a message into a specific forum topic.
    Telethon 1.44 only accepts a plain int for reply_to in send_message/send_file.
    It wraps it internally as InputReplyToMessage(reply_to_msg_id=N) which is
    enough to route into the correct forum thread.
    """
    if dest_topic_id and dest_topic_id != 1:
        return {"reply_to": dest_topic_id}
    # General topic (id=1): messages go there by default, no reply_to needed
    return {}


# Files larger than this are streamed to a temp file instead of RAM.
# 100 MB keeps peak memory well under Render's 512 MB free-tier limit.
_LARGE_FILE_THRESHOLD = 100 * 1024 * 1024  # 100 MB

# Type alias: either a BytesIO (in-RAM) or a str path (on-disk temp file)
_FileObj = Union[BytesIO, str]


async def _download(
    client: TelegramClient, message, filename: str | None
) -> tuple[_FileObj | None, bool]:
    """
    Download message media.
    - Small files (≤100 MB): BytesIO with .name set → zero disk, fast.
    - Large files (>100 MB): named temp file on disk → zero extra RAM.

    Returns (file_obj, is_temp_path) where:
      is_temp_path=False → file_obj is BytesIO
      is_temp_path=True  → file_obj is a str path; caller MUST delete it.
    """
    if not message.media:
        return None, False

    # Peek at declared size (only available on documents, not photos)
    declared_size: int = 0
    if isinstance(message.media, MessageMediaDocument):
        declared_size = getattr(message.media.document, "size", 0) or 0

    if declared_size > _LARGE_FILE_THRESHOLD:
        # Stream to a named temp file so the correct extension is preserved
        ext = os.path.splitext(filename or "")[1] or ".bin"
        fd, tmp_path = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        await client.download_media(message, file=tmp_path)
        logger.info(
            "Large file (%d MB) → temp disk: %s",
            declared_size // 1024 // 1024, tmp_path,
        )
        return tmp_path, True

    # Small file → RAM
    buf = BytesIO()
    await client.download_media(message, file=buf)
    buf.seek(0)
    if filename:
        buf.name = filename
    return buf, False


def _cleanup(file_obj: _FileObj, is_temp: bool) -> None:
    """Delete the temp file if one was used."""
    if is_temp and isinstance(file_obj, str) and os.path.exists(file_obj):
        try:
            os.unlink(file_obj)
        except OSError:
            pass


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
        if config.MEDIA_ONLY:
            return  # skip polls in media-only mode
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
        if message.text and not config.MEDIA_ONLY:
            await _call(client.send_message(
                dest_chat,
                message=message.text,
                formatting_entities=message.entities,
                **route,
            ))
        return

    # ── All media types ───────────────────────────────────────────────────────
    filename, extra = _classify(message)
    file_obj, is_temp = await _download(client, message, filename)

    if file_obj is None:
        logger.warning("Could not download media for message %d, skipping.", message.id)
        return

    try:
        await _call(client.send_file(
            dest_chat,
            file=file_obj,
            caption=message.text or "",
            formatting_entities=message.entities,
            **route,
            **extra,
        ))
    finally:
        _cleanup(file_obj, is_temp)


async def copy_album(
    client: TelegramClient, messages: list, dest_chat, dest_topic_id: int
) -> None:
    """
    Copy a grouped album (multiple photos/videos with the same grouped_id)
    as a single album in the destination.
    """
    route = _reply_to_kwarg(dest_topic_id)

    files = []
    temps = []  # track temp paths for cleanup
    for msg in messages:
        filename, _ = _classify(msg)
        file_obj, is_temp = await _download(client, msg, filename)
        if file_obj:
            files.append(file_obj)
            if is_temp:
                temps.append(file_obj)

    if not files:
        return

    caption = next((m.text for m in messages if m.text), "") or ""
    entities = next((m.entities for m in messages if m.entities), None)

    try:
        await _call(client.send_file(
            dest_chat,
            file=files,
            caption=caption,
            formatting_entities=entities,
            **route,
        ))
    finally:
        for path in temps:
            _cleanup(path, True)


# ── Internal helper needed for poll SendMediaRequest ─────────────────────────

def _make_reply_to(topic_id: int):
    """Build a ReplyToMessage for raw SendMediaRequest (used for polls)."""
    from telethon.tl.types import InputReplyToMessage
    return InputReplyToMessage(
        reply_to_msg_id=topic_id,
        top_msg_id=topic_id,
    )
