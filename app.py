"""
app.py — Main entry point.

Startup sequence:
  1. Launch Flask health-check server on a background thread (keeps Render alive)
  2. Connect Telethon userbot
  3. Migrate legacy state if present
  4. Build (or reload) topic map for all pairs
  5. [If COPY_HISTORY=true] Copy all past messages, time-slicing across pairs
  6. Register live NewMessage handler and block forever

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

# Suppress ONLY the noisy internal updates logs (like "Got difference for channel...")
# but keep download/upload logs visible!
logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)

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
    src_id: int,
    dest_id: int,
    source_entity,
    dest_entity,
    topic_map: dict,
    progress: dict,
) -> bool:
    """
    Copy historical messages for a specific pair.
    Returns True if this pair is fully caught up, False if it hit the time limit.
    """
    start_time = time.time()
    time_limit_sec = config.TIME_SLICE_HOURS * 3600

    is_forum = getattr(source_entity, "forum", False)
    if is_forum:
        source_topics = await topic_mgr.fetch_all_topics(client, source_entity)
    else:
        # Fake a single topic for standard groups/channels
        class DummyTopic:
            id = 1
            title = "Main Chat"
        source_topics = [DummyTopic()]

    for topic in source_topics:
        if not getattr(topic, "title", None):
            continue

        topic_src_id = topic.id
        if is_forum:
            topic_dest_id = topic_map.get(topic_src_id)
            if topic_dest_id is None:
                logger.warning("No dest mapping for topic '%s' (id=%d), skipping.", topic.title, topic_src_id)
                continue
        else:
            topic_dest_id = None # standard group, no reply_to routing

        min_id = progress.get(topic_src_id, 0)
        logger.info(
            "▶ History: '%s'  src_topic=%s → dest_topic=%s  (resuming from msg_id=%d)",
            topic.title, topic_src_id, topic_dest_id, min_id,
        )

        # Collect ALL messages for this topic in chronological order
        messages = []
        iter_kwargs = {
            "entity": source_entity,
            "min_id": min_id,
            "reverse": True,  # oldest first
            "limit": None,
        }
        if is_forum and topic_src_id != 1:
            iter_kwargs["reply_to"] = topic_src_id

        async for msg in client.iter_messages(**iter_kwargs):
            messages.append(msg)

        logger.info("   %d messages to copy in '%s'", len(messages), topic.title)
        if not messages:
            continue

        i = 0
        batch_counter = 0
        while i < len(messages):
            if time.time() - start_time >= time_limit_sec:
                await state.save_progress(client, src_id, dest_id, progress)
                logger.info("⏳ Time slice of %.2f hours reached for pair %d:%d", config.TIME_SLICE_HOURS, src_id, dest_id)
                return False

            msg = messages[i]
            try:
                if msg.grouped_id:
                    # Collect all album parts consecutively in the sorted list
                    album = [msg]
                    j = i + 1
                    while j < len(messages) and messages[j].grouped_id == msg.grouped_id:
                        album.append(messages[j])
                        j += 1
                    await copy_album(client, album, dest_entity, topic_dest_id)
                    progress[topic_src_id] = album[-1].id
                    i = j
                else:
                    await copy_message(client, msg, dest_entity, topic_dest_id)
                    progress[topic_src_id] = msg.id
                    i += 1

                batch_counter += 1
                # Save checkpoint every 20 messages
                if batch_counter % 20 == 0:
                    await state.save_progress(client, src_id, dest_id, progress)
                    logger.info("   checkpoint: %d/%d messages copied", i, len(messages))

                await asyncio.sleep(config.DELAY_BETWEEN_MSGS)

            except Exception as exc:
                logger.error("   Error on message %d: %s — skipping.", msg.id, exc)
                i += 1

        # Final save for this topic
        await state.save_progress(client, src_id, dest_id, progress)
        logger.info("   ✅ Done: '%s'", topic.title)

    logger.info("🎉 Full history copy complete for pair %d:%d!", src_id, dest_id)
    return True


# ── Live album buffering ───────────────────────────────────────────────────────

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

    # Force telethon to cache all chats we are part of so get_entity doesn't fail
    logger.info("Fetching dialogs to cache entities...")
    await client.get_dialogs()

    # 3. Migrate Legacy State (if any)
    if config.CHAT_PAIRS:
        first_src, first_dest = config.CHAT_PAIRS[0]
        await state.migrate_legacy_state(client, first_src, first_dest)

    # Resolve entities and load states for all pairs
    entities = {}
    all_topic_maps = {}
    all_progress = {}

    for src_id, dest_id in config.CHAT_PAIRS:
        logger.info("Initializing pair %d → %d", src_id, dest_id)
        src_entity = await client.get_entity(src_id)
        dst_entity = await client.get_entity(dest_id)
        entities[src_id] = src_entity
        entities[dest_id] = dst_entity

        is_src_forum = getattr(src_entity, "forum", False)
        is_dest_forum = getattr(dst_entity, "forum", False)

        topic_map = {}
        if is_src_forum and is_dest_forum:
            topic_map = await state.load_topic_map(client, src_id, dest_id)
            if not topic_map:
                logger.info("No saved topic map for %d:%d — building now…", src_id, dest_id)
                topic_map = await topic_mgr.build_topic_map(client, src_entity, dst_entity, src_id, dest_id)
                # save it using the state function since build_topic_map used the legacy one inside
                await state.save_topic_map(client, src_id, dest_id, topic_map)
            else:
                logger.info("Topic map loaded (%d topics) for %d:%d", len(topic_map), src_id, dest_id)
        elif not is_src_forum:
            logger.info("Source %d is not a forum. Skipping topic map.", src_id)
        elif not is_dest_forum:
            logger.info("Dest %d is not a forum. Messages will be copied without topic routing.", dest_id)
        
        all_topic_maps[(src_id, dest_id)] = topic_map
        all_progress[(src_id, dest_id)] = await state.load_progress(client, src_id, dest_id)

    # 4. History copy with Time Slicing
    if config.COPY_HISTORY:
        caught_up_pairs = set()
        
        while len(caught_up_pairs) < len(config.CHAT_PAIRS):
            for src_id, dest_id in config.CHAT_PAIRS:
                if (src_id, dest_id) in caught_up_pairs:
                    continue
                
                logger.info("=== Switching to History Sync for Pair %d → %d ===", src_id, dest_id)
                finished = await _copy_history(
                    client=client,
                    src_id=src_id,
                    dest_id=dest_id,
                    source_entity=entities[src_id],
                    dest_entity=entities[dest_id],
                    topic_map=all_topic_maps[(src_id, dest_id)],
                    progress=all_progress[(src_id, dest_id)]
                )
                if finished:
                    caught_up_pairs.add((src_id, dest_id))
        
        logger.info("🎉 All pairs have finished history copying! Switching to LIVE SYNC.")

    # 5. Live sync handler
    source_ids = [pair[0] for pair in config.CHAT_PAIRS]
    pair_map = {pair[0]: pair[1] for pair in config.CHAT_PAIRS}
    
    @client.on(events.NewMessage(chats=source_ids))
    async def on_new_message(event):
        msg = event.message
        # In Telethon, event.chat_id is the integer ID of the chat where it occurred
        src_id = event.chat_id
        dest_id = pair_map.get(src_id)
        
        if not dest_id:
            return

        src_entity = entities.get(src_id)
        dest_entity = entities.get(dest_id)
        topic_map = all_topic_maps.get((src_id, dest_id), {})

        is_src_forum = getattr(src_entity, "forum", False)
        is_dest_forum = getattr(dest_entity, "forum", False)

        dest_topic_id = None
        if is_src_forum and is_dest_forum:
            src_topic_id = 1
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
                if msg.grouped_id not in _album_buf:
                    _album_buf[msg.grouped_id] = []
                    _album_tasks[msg.grouped_id] = asyncio.create_task(
                        _flush_album(client, msg.grouped_id, dest_entity, dest_topic_id)
                    )
                _album_buf[msg.grouped_id].append(msg)
            else:
                await copy_message(client, msg, dest_entity, dest_topic_id)
        except Exception as exc:
            logger.error("Live copy failed: %s", exc)

    logger.info("🚀 Listening for new messages...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
