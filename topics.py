"""
topics.py — Forum topic discovery, creation, and ID mapping.

Telegram forum topics are message threads. Every topic has a numeric ID
equal to the message ID of the topic-creation service message.
The "General" topic always has ID = 1.
"""
import logging

from telethon import TelegramClient
from telethon.tl.functions.messages import GetForumTopicsRequest, CreateForumTopicRequest
from telethon.tl.types import ForumTopic

import state
import config

logger = logging.getLogger(__name__)

GENERAL_TOPIC_ID = 1


async def fetch_all_topics(client: TelegramClient, entity) -> list[ForumTopic]:
    """
    Return all ForumTopic objects from a forum supergroup.
    Paginates automatically if there are more than 100 topics.
    """
    # Raw API needs InputPeerChannel, not a bare Channel object
    input_entity = await client.get_input_entity(entity)

    topics: list[ForumTopic] = []
    offset_date = None
    offset_id = 0
    offset_topic = 0

    while True:
        result = await client(GetForumTopicsRequest(
            peer=input_entity,
            q="",
            offset_date=offset_date,
            offset_id=offset_id,
            offset_topic=offset_topic,
            limit=100,
        ))
        batch = [t for t in result.topics if isinstance(t, ForumTopic)]
        topics.extend(batch)
        logger.debug("Fetched %d topics (total so far: %d)", len(batch), len(topics))

        if len(batch) < 100:
            break  # No more pages

        last = batch[-1]
        offset_topic = last.id
        offset_date = getattr(last, "date", None)
        offset_id = 0

    logger.info("Total topics in source: %d", len(topics))
    return topics


async def _create_topic(client: TelegramClient, dest_entity, topic: ForumTopic) -> int:
    """
    Create a matching topic in the destination group.
    Returns the new topic's ID.
    """
    # Raw API requires InputPeerChannel, not a bare Channel
    input_peer = await client.get_input_entity(dest_entity)

    kwargs: dict = dict(peer=input_peer, title=topic.title)
    if getattr(topic, "icon_color", None) is not None:
        kwargs["icon_color"] = topic.icon_color
    if getattr(topic, "icon_emoji_id", None):
        kwargs["icon_emoji_id"] = topic.icon_emoji_id

    result = await client(CreateForumTopicRequest(**kwargs))

    # Extract new topic ID from the Updates object
    for upd in result.updates:
        # UpdateNewChannelMessage carries the new msg id = topic id
        if hasattr(upd, "message") and hasattr(upd.message, "id"):
            return upd.message.id
        if hasattr(upd, "id") and not hasattr(upd, "user_id"):
            return upd.id

    raise RuntimeError(f"Cannot extract topic ID after creating '{topic.title}'")


async def build_topic_map(
    client: TelegramClient, source_entity, dest_entity
) -> dict:
    """
    Build {source_topic_id: dest_topic_id} by:
    1. Listing all source topics
    2. Creating matching topics in destination (skipping already-mapped ones)
    3. Saving the map to the state channel after each new topic

    The General topic (id=1) is always mapped 1→1 (it exists in all forum groups).
    """
    topic_map = await state.load_topic_map(client)
    if topic_map:
        logger.info("Partial topic map loaded (%d entries); resuming build…", len(topic_map))

    source_topics = await fetch_all_topics(client, source_entity)

    for topic in source_topics:
        src_id = topic.id

        if src_id in topic_map:
            logger.info("  skip '%s' (already mapped → %d)", topic.title, topic_map[src_id])
            continue

        if src_id == GENERAL_TOPIC_ID:
            # General topic exists in every forum group
            topic_map[src_id] = GENERAL_TOPIC_ID
            logger.info("  mapped General (1 → 1)")
        else:
            try:
                dest_id = await _create_topic(client, dest_entity, topic)
                topic_map[src_id] = dest_id
                logger.info("  created '%s': %d → %d", topic.title, src_id, dest_id)
            except Exception as exc:
                logger.error("  FAILED to create topic '%s': %s", topic.title, exc)
                continue

        # Persist after every topic so a restart doesn't redo work
        await state.save_topic_map(client, topic_map)

    logger.info("Topic map complete: %d topics", len(topic_map))
    return topic_map
