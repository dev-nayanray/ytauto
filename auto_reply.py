"""
Auto-reply to YouTube comments using Claude.

Fetches recent top-level comments on uploaded videos, generates
a helpful/engaging reply via Claude, and posts it back to YouTube.
Tracks replied comments in output_dir/replied_comments.json to avoid duplicates.
"""
import json
import logging
from pathlib import Path

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)

_REPLY_SYSTEM = """\
You are a friendly, knowledgeable YouTube content creator specialising in AI and Tech tutorials.
You reply to comments on your videos in a way that:
  • Is warm, genuine, and helpful — never robotic or generic
  • Adds value — answers questions, gives extra tips, or shares related insight
  • Encourages engagement — ends with a question or invites them to try something
  • Is 1-3 sentences max — concise and conversational
  • Never uses emojis excessively (max 1 per reply)
  • Never says "Great question!" or "Thanks for watching!" as openers
"""


def reply_to_comments(video_id: str, output_dir: Path, max_replies: int = 5) -> int:
    """
    Fetch up to *max_replies* unresponded comments on *video_id* and reply.
    Returns the number of replies posted.
    """
    from upload import get_youtube_service

    replied_file = output_dir / "replied_comments.json"
    replied: set[str] = set()
    if replied_file.exists():
        try:
            replied = set(json.loads(replied_file.read_text(encoding="utf-8")))
        except Exception:
            pass

    yt = get_youtube_service()

    # Fetch recent top-level comments
    try:
        resp = yt.commentThreads().list(
            part="snippet",
            videoId=video_id,
            order="relevance",
            maxResults=20,
            textFormat="plainText",
        ).execute()
    except Exception as exc:
        logger.warning(f"Could not fetch comments for {video_id}: {exc}")
        return 0

    items = resp.get("items", [])
    posted = 0

    for item in items:
        if posted >= max_replies:
            break

        comment_id = item["id"]
        if comment_id in replied:
            continue

        snippet = item["snippet"]["topLevelComment"]["snippet"]
        # Skip if channel already replied (check reply count + authorChannelId)
        if item["snippet"].get("totalReplyCount", 0) > 0:
            replied.add(comment_id)
            continue

        author  = snippet.get("authorDisplayName", "there")
        text    = snippet.get("textDisplay", "").strip()
        if not text or len(text) < 5:
            continue

        # Generate reply with Claude
        try:
            reply_text = _generate_reply(author, text)
        except Exception as exc:
            logger.warning(f"Claude reply generation failed: {exc}")
            continue

        # Post the reply
        try:
            yt.comments().insert(
                part="snippet",
                body={
                    "snippet": {
                        "parentId": comment_id,
                        "textOriginal": reply_text,
                    }
                },
            ).execute()
            replied.add(comment_id)
            posted += 1
            logger.info(f"Replied to comment by {author}: {reply_text[:60]}…")
        except Exception as exc:
            logger.warning(f"Failed to post reply: {exc}")

    replied_file.write_text(json.dumps(list(replied), indent=2), encoding="utf-8")
    logger.info(f"Auto-reply: {posted} new replies posted for video {video_id}")
    return posted


def _generate_reply(author: str, comment: str) -> str:
    """Use Claude to generate a contextual reply to a YouTube comment."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=150,
        system=_REPLY_SYSTEM,
        messages=[{
            "role": "user",
            "content": f'Comment from "{author}": {comment}\n\nWrite a reply:',
        }],
    )
    return msg.content[0].text.strip()
