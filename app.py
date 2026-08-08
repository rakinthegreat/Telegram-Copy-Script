"""
app.py — Main entry point.

Startup sequence:
  1. Launch Flask health-check server on a background thread (keeps Render alive)
  2. Connect Telethon userbot
  3. Build (or reload) topic map source → destination
  4. [If COPY_HISTORY=true] Copy all past messages, topic by topic, resumably
  5. Register live NewMessage handler and block forever

UptimeRobot pings /health every 5 min → service never sleeps on Render free tier.
"""
import asyncio
import logging
import threading
import time
from typing import Optional

from flask import Flask, jsonify
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import ForumTopic

import config
import state
import topics as topic_mgr
from copier import copy_album, copy_message

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Flask health server ────────────────────────────────────────────────────────
flask_app = Flask(__name__)
_start_time = time.time()


@flask_app.route("/")
@flask_app.route("/health")
def health():
    uptime = int(time.time() - _start_time)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    return jsonify({
        "status": "running",
        "uptime": f"{h:02d}:{m:02d}:{s:02d}",
        "copy_history": config.COPY_HISTORY,
    }), 200


def _start_flask() -> None:
    flask_app.run(host="0.0.0.0", port=config.PORT, use_reloader=False)


# ── History copy ───────────────────────────────────────────────────────────────

async def _copy_history(
    client: TelegramClient,
    source_entity,
    dest_entity,
    topic_map: dict,
    progress: dict,
) -> None:
    """
    Copy all historical messages from each source topic to its mirror topic.
    Resumes from the last successfully copied message ID if interrupted.
    """
    source_topics = await topic_mgr.fetch_all_topics(client, source_entity)

    for topic in source_topics:
        if not isinstance(topic, ForumTopic):
            continue

        src_id = topic.id
        dest_id = topic_map.get(src_id)
        if dest_id is None:
            logger.warning("No dest mapping for topic '%s' (id=%d), skipping.", topic.title, src_id)
            continue

        min_id = progress.get(src_id, 0)
        logger.info(
            "▶ History: '%s'  src_topic=%d → dest_topic=%d  (resuming from msg_id=%d)",
            topic.title, src_id, dest_id, min_id,
        )

        # Collect ALL messages for this topic in chronological order
        messages = []
        async for msg in client.iter_messages(
            source_entity,
            reply_to=src_id,
            min_id=min_id,
            reverse=True,      # oldest first
            limit=None,
        ):
            messages.append(msg)

        logger.info("   %d messages to copy in '%s'", len(messages), topic.title)
        if not messages:
            continue

        i = 0
        batch_counter = 0
        while i < len(messages):
            msg = messages[i]
            try:
                if msg.grouped_id:
                    # Collect all album parts consecutively in the sorted list
                    album = [msg]
                    j = i + 1
                    while j < len(messages) and messages[j].grouped_id == msg.grouped_id:
                        album.append(messages[j])
                        j += 1
                    await copy_album(client, album, dest_entity, dest_id)
                    progress[src_id] = album[-1].id
                    i = j
                else:
                    await copy_message(client, msg, dest_entity, dest_id)
                    progress[src_id] = msg.id
                    i += 1

                batch_counter += 1
                # Save checkpoint every 20 messages
                if batch_counter % 20 == 0:
                    await state.save_progress(client, progress)
                    logger.info("   checkpoint: %d/%d messages copied", i, len(messages))

                await asyncio.sleep(config.DELAY_BETWEEN_MSGS)

            except Exception as exc:
                logger.error("   Error on message %d: %s — skipping.", msg.id, exc)
                i += 1

        # Final save for this topic
        await state.save_progress(client, progress)
        logger.info("   ✅ Done: '%s'", topic.title)

    logger.info("🎉 Full history copy complete!")


# ── Live album buffering ───────────────────────────────────────────────────────
# Albums arrive as N separate NewMessage events with the same grouped_id.
# We buffer them and flush after a short window.

_album_buf: dict[int, list] = {}
_album_tasks: dict[int, asyncio.Task] = {}


async def _flush_album(
    client: TelegramClient,
    grouped_id: int,
    dest_entity,
    dest_topic_id: int,
) -> None:
    """Wait briefly then send all buffered album parts as one album."""
    await asyncio.sleep(2.5)  # wait for all parts to arrive
    messages = _album_buf.pop(grouped_id, [])
    _album_tasks.pop(grouped_id, None)
    if not messages:
        return
    messages.sort(key=lambda m: m.id)
    try:
        await copy_album(client, messages, dest_entity, dest_topic_id)
    except Exception as exc:
        logger.error("Live album copy failed (grouped_id=%d): %s", grouped_id, exc)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    # 1. Flask health server
    threading.Thread(target=_start_flask, daemon=True).start()
    logger.info("Health server started on port %d", config.PORT)

    # 2. Telethon client
    client = TelegramClient(
        StringSession(config.SESSION_STRING),
        config.API_ID,
        config.API_HASH,
    )
    await client.start()
    me = await client.get_me()
    logger.info("Logged in as: %s (id=%d)", me.first_name, me.id)

    # 3. Resolve chat entities
    source_entity = await client.get_entity(config.SOURCE_CHAT_ID)
    dest_entity = await client.get_entity(config.DEST_CHAT_ID)
    logger.info("Source : %s", source_entity.title)
    logger.info("Dest   : %s", dest_entity.title)

    # 4. Build / reload topic map
    topic_map = await state.load_topic_map(client)
    if not topic_map:
        logger.info("No saved topic map — building now (this may take a minute)…")
        topic_map = await topic_mgr.build_topic_map(client, source_entity, dest_entity)
    else:
        logger.info("Topic map loaded (%d topics)", len(topic_map))

    # 5. History copy
    if config.COPY_HISTORY:
        progress = await state.load_progress(client)
        await _copy_history(client, source_entity, dest_entity, topic_map, progress)

    # 6. Live sync handler
    @client.on(events.NewMessage(chats=source_entity))
    async def on_new_message(event):
        msg = event.message

        # Determine which topic this message belongs to
        src_topic_id = 1  # default: General
        rt = msg.reply_to
        if rt is not None:
            top = getattr(rt, "reply_to_top_id", None)
            if top:
                src_topic_id = top
            elif getattr(rt, "forum_topic", False):
                src_topic_id = getattr(rt, "reply_to_msg_id", 1) or 1

        dest_topic_id = topic_map.get(src_topic_id, topic_map.get(1, 1))

        try:
            if msg.grouped_id:
                # Buffer album parts; flush after window
                gid = msg.grouped_id
                _album_buf.setdefault(gid, []).append(msg)
                if gid not in _album_tasks:
                    loop = asyncio.get_event_loop()
                    _album_tasks[gid] = loop.create_task(
                        _flush_album(client, gid, dest_entity, dest_topic_id)
                    )
            else:
                await copy_message(client, msg, dest_entity, dest_topic_id)

        except Exception as exc:
            logger.error("Live copy error (msg=%d): %s", msg.id, exc)

    logger.info("🚀 Live sync active — listening for new messages…")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
