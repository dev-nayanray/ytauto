"""
Keyword queue — persistent list of keywords to process in the next pipeline run.
keywords_queue.json in project root is the source of truth.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

QUEUE_FILE = Path(__file__).parent / "keywords_queue.json"


def load_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_queue(queue: list[dict]) -> None:
    QUEUE_FILE.write_text(json.dumps(queue, indent=2), encoding="utf-8")


def add_keywords(keywords: list[str], added_by: str = "user") -> int:
    """Append keywords to the queue, skipping duplicates. Returns count added."""
    queue = load_queue()
    existing = {item["keyword"].lower() for item in queue}
    added = 0
    for kw in keywords:
        kw = kw.strip()
        if kw and kw.lower() not in existing:
            queue.append({
                "keyword": kw,
                "added_at": datetime.now(timezone.utc).isoformat(),
                "added_by": added_by,
            })
            existing.add(kw.lower())
            added += 1
    if added:
        save_queue(queue)
    return added


def pop_keywords(n: int) -> list[str]:
    """Remove and return up to n keywords from the front of the queue."""
    queue = load_queue()
    popped = queue[:n]
    save_queue(queue[n:])
    return [item["keyword"] for item in popped]


def remove_keyword(keyword: str) -> bool:
    """Remove one keyword by exact match (case-insensitive). Returns True if removed."""
    queue = load_queue()
    new_q = [item for item in queue if item["keyword"].lower() != keyword.lower()]
    if len(new_q) == len(queue):
        return False
    save_queue(new_q)
    return True
