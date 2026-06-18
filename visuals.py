"""
Downloads landscape video clips from the Pexels Videos API.

Uses a two-pass search:
  1. Exact keyword query.
  2. If < CLIPS_PER_KEYWORD results, retry with the first two words as a
     broader fallback query.
"""
import logging
import time
from pathlib import Path
from typing import Optional

import requests

from config import CLIPS_PER_KEYWORD, PEXELS_API_KEY

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.pexels.com/videos/search"
_TIMEOUT = 120  # seconds per clip download


def download_clips(keyword: str, clips_dir: Path) -> list[Path]:
    """
    Download up to CLIPS_PER_KEYWORD MP4 clips for *keyword* into *clips_dir*.
    Already-downloaded clips are skipped (idempotent).
    Returns list of available clip paths.
    """
    clips_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(clips_dir.glob("clip_*.mp4"))

    if len(existing) >= CLIPS_PER_KEYWORD:
        logger.info(f"All {CLIPS_PER_KEYWORD} clips already present, skipping download.")
        return existing[:CLIPS_PER_KEYWORD]

    already = {p.name for p in existing}
    downloaded: list[Path] = list(existing)

    # Build query list: exact → broad fallback
    words = keyword.split()
    queries = [keyword]
    if len(words) > 2:
        queries.append(" ".join(words[:2]))

    raw_videos: list[dict] = []
    for q in queries:
        raw_videos = _search_pexels(q, per_page=CLIPS_PER_KEYWORD * 2)
        if raw_videos:
            break

    if not raw_videos:
        logger.warning(f"Pexels returned no videos for '{keyword}'")
        return downloaded

    clip_idx = len(downloaded)
    for video in raw_videos:
        if len(downloaded) >= CLIPS_PER_KEYWORD:
            break
        dest_name = f"clip_{clip_idx:02d}.mp4"
        if dest_name in already:
            clip_idx += 1
            continue

        url = _best_url(video)
        if not url:
            continue

        dest = clips_dir / dest_name
        try:
            _stream_download(url, dest)
            downloaded.append(dest)
            logger.info(f"  ↓ {dest_name}  (id={video['id']})")
        except Exception as exc:
            logger.warning(f"  Clip {clip_idx} download failed: {exc}")
        clip_idx += 1
        time.sleep(0.25)

    logger.info(f"{len(downloaded)} clips ready in {clips_dir}")
    return downloaded[:CLIPS_PER_KEYWORD]


def _search_pexels(query: str, per_page: int) -> list[dict]:
    try:
        resp = requests.get(
            _SEARCH_URL,
            headers={"Authorization": PEXELS_API_KEY},
            params={
                "query": query,
                "per_page": per_page,
                "orientation": "landscape",
                "size": "medium",
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("videos", [])
    except requests.RequestException as exc:
        logger.error(f"Pexels search failed for '{query}': {exc}")
        return []


def _best_url(video: dict) -> Optional[str]:
    """
    Select the best MP4 URL from a Pexels video object.
    Prefers ≤ 1 080 p to save bandwidth; returns the highest resolution within that.
    """
    files = video.get("video_files", [])
    best_url = None
    best_width = 0
    for f in files:
        link = f.get("link", "")
        width = f.get("width", 0)
        if ".mp4" not in link.lower():
            continue
        if 0 < width <= 1920 and width > best_width:
            best_url = link
            best_width = width
    return best_url


def _stream_download(url: str, dest: Path) -> None:
    with requests.get(url, stream=True, timeout=_TIMEOUT) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65_536):
                f.write(chunk)
