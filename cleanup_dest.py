"""
cleanup_dest.py — Delete ALL forum topics from the destination group (except General).
Run this when the dest group has stale/duplicate topics from failed runs.

After running this, also run reset_state.py, then app.py for a clean start.
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetForumTopicsRequest, DeleteTopicHistoryRequest, EditForumTopicRequest
from telethon.tl.types import ForumTopic
import config

GENERAL_TOPIC_ID = 1

async def main():
    client = TelegramClient(StringSession(config.SESSION_STRING), config.API_ID, config.API_HASH)
    await client.start()

    dest_input = await client.get_input_entity(config.DEST_CHAT_ID)

    # Fetch all topics from destination
    all_topics = []
    offset_date, offset_id, offset_topic = None, 0, 0
    while True:
        result = await client(GetForumTopicsRequest(
            peer=dest_input,
            q="",
            offset_date=offset_date,
            offset_id=offset_id,
            offset_topic=offset_topic,
            limit=100,
        ))
        batch = [t for t in result.topics if isinstance(t, ForumTopic)]
        all_topics.extend(batch)
        if len(batch) < 100:
            break
        last = batch[-1]
        offset_topic = last.id
        offset_date = getattr(last, "date", None)

    non_general = [t for t in all_topics if t.id != GENERAL_TOPIC_ID]
    print(f"Found {len(all_topics)} topics total, {len(non_general)} to delete (keeping General).")

    if not non_general:
        print("Nothing to delete.")
        await client.disconnect()
        return

    for topic in non_general:
        try:
            # Delete all messages in the topic thread first
            await client(DeleteTopicHistoryRequest(
                peer=dest_input,
                top_msg_id=topic.id,
            ))
            print(f"  Deleted topic '{topic.title}' (id={topic.id})")
        except Exception as e:
            print(f"  FAILED to delete '{topic.title}' (id={topic.id}): {e}")

    print(f"\nDone. Destination group cleaned.")
    print("Now run: python reset_state.py  then  python app.py")
    await client.disconnect()

asyncio.run(main())
