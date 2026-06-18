"""
YouTube channel rank tracker and SEO optimizer.

For each uploaded video:
  • Searches YouTube for the video's target keyword and finds where our
    video appears in the first 50 results.
  • Stores rank history in output_dir/rank_history.json.
  • Generates improvement suggestions based on title/description/tags analysis.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config import OUTPUT_DIR, YOUTUBE_API_KEY

logger = logging.getLogger(__name__)


def check_video_rank(video_id: str, keyword: str) -> int | None:
    """
    Search YouTube for *keyword* and return the 1-based position of *video_id*
    in results (up to 50). Returns None if not found or API unavailable.
    """
    if not YOUTUBE_API_KEY:
        logger.warning("YOUTUBE_API_KEY not set — rank check skipped")
        return None
    try:
        from googleapiclient.discovery import build
        yt   = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        resp = yt.search().list(
            q=keyword, part="id", type="video",
            maxResults=50, relevanceLanguage="en",
        ).execute()
        items = resp.get("items", [])
        for pos, item in enumerate(items, start=1):
            if item.get("id", {}).get("videoId") == video_id:
                logger.info(f"Rank check: '{keyword}' → position {pos}")
                return pos
        logger.info(f"Rank check: '{keyword}' → not in top {len(items)}")
        return None
    except Exception as exc:
        logger.warning(f"Rank check failed: {exc}")
        return None


def record_rank(output_dir: Path, keyword: str, rank: int | None) -> None:
    """Append a rank entry to output_dir/rank_history.json."""
    hist_path = output_dir / "rank_history.json"
    history: list[dict] = []
    if hist_path.exists():
        try:
            history = json.loads(hist_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    history.append({
        "date":    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "keyword": keyword,
        "rank":    rank,
    })
    hist_path.write_text(json.dumps(history, indent=2), encoding="utf-8")


def generate_optimization_tips(output_dir: Path) -> list[str]:
    """
    Analyse video metadata and return a list of actionable improvement tips.
    """
    tips: list[str] = []

    seo_path = output_dir / "seo.json"
    if not seo_path.exists():
        return tips

    try:
        seo = json.loads(seo_path.read_text(encoding="utf-8"))
    except Exception:
        return tips

    title = seo.get("title", "")
    desc  = seo.get("description", "")
    tags  = seo.get("tags", [])

    # Title checks
    if len(title) < 40:
        tips.append("Title is short (<40 chars) — add more descriptive keywords for better search matching")
    if len(title) > 70:
        tips.append("Title is long (>70 chars) — YouTube truncates after ~70; move key info to the front")
    if not any(c.isupper() for c in title):
        tips.append("No uppercase in title — use 1-2 CAPS power words to boost CTR (e.g. 'PROVEN', 'BEST')")
    _year = str(datetime.now().year)
    if _year not in title and str(datetime.now().year - 1) not in title:
        tips.append(f"Add year '{_year}' to title — year tags significantly improve search relevance and CTR")

    # Description checks
    if len(desc) < 200:
        tips.append("Description is very short — aim for 400-700 words; longer descriptions rank better")
    if "subscribe" not in desc.lower():
        tips.append("Add a subscribe CTA in description — 'Subscribe for weekly AI tutorials'")
    if "http" not in desc:
        tips.append("No links in description — add channel link and related video links for viewer retention")

    # Tags checks
    if len(tags) < 10:
        tips.append(f"Only {len(tags)} tags — use all 15 allowed; more tags = more search entry points")
    if any(len(t) > 30 for t in tags):
        tips.append("Some tags exceed 30 chars — YouTube may ignore long tags; keep each under 30 chars")

    # Thumbnail check
    thumb_path = output_dir / "thumbnail.jpg"
    if not thumb_path.exists():
        tips.append("No thumbnail found — always use a custom thumbnail; it can increase CTR by 3-5x")

    # Rank-based tips
    hist_path = output_dir / "rank_history.json"
    if hist_path.exists():
        try:
            history = json.loads(hist_path.read_text(encoding="utf-8"))
            latest = history[-1].get("rank") if history else None
            if latest is None:
                tips.append("Video not in top 50 results — consider updating title/description to better match keyword")
            elif latest > 10:
                tips.append(f"Ranking #{latest} — add more keyword variations in description to improve to top 10")
            elif latest <= 5:
                tips.append(f"Excellent! Ranking #{latest} — maintain by adding end screens and pinned comment with keyword")
        except Exception:
            pass

    return tips


def _compute_seo_score(output_dir: Path, rank: int | None) -> int:
    """Score 0-100 based on title, description, tags, thumbnail, and rank."""
    score = 0
    seo_path = output_dir / "seo.json"
    if not seo_path.exists():
        return 0
    try:
        seo = json.loads(seo_path.read_text(encoding="utf-8"))
    except Exception:
        return 0

    title = seo.get("title", "")
    desc  = seo.get("description", "")
    tags  = seo.get("tags", [])

    # Title (25 pts)
    if 40 <= len(title) <= 70:
        score += 15
    elif len(title) > 20:
        score += 8
    if any(c.isupper() for c in title):
        score += 5
    if str(datetime.now().year) in title or str(datetime.now().year - 1) in title:
        score += 5

    # Description (30 pts)
    if len(desc) >= 400:
        score += 20
    elif len(desc) >= 200:
        score += 12
    elif len(desc) >= 50:
        score += 5
    if "subscribe" in desc.lower():
        score += 5
    if "http" in desc:
        score += 5

    # Tags (20 pts)
    tag_score = min(20, len(tags) * 2)
    score += tag_score

    # Thumbnail (10 pts)
    if (output_dir / "thumbnail.jpg").exists():
        score += 10

    # Rank (15 pts)
    if rank is not None:
        if rank <= 3:
            score += 15
        elif rank <= 10:
            score += 12
        elif rank <= 20:
            score += 8
        elif rank <= 50:
            score += 4

    return min(100, score)


def run_full_audit(output_dir: Path, keyword: str, video_id: str | None = None) -> dict:
    """
    Run rank check + generate tips for a video. Returns a dict with results.
    """
    rank = None
    if video_id:
        rank = check_video_rank(video_id, keyword)
        record_rank(output_dir, keyword, rank)

    suggestions = generate_optimization_tips(output_dir)
    seo_score   = _compute_seo_score(output_dir, rank)
    return {
        "keyword":    keyword,
        "video_id":   video_id,
        "rank":       rank,
        "suggestions": suggestions,
        "tips":        suggestions,   # backwards compat alias
        "seo_score":  seo_score,
        "audited_at": datetime.now(timezone.utc).isoformat(),
    }


def get_all_ranks() -> list[dict]:
    """Return latest rank entry for every uploaded video."""
    results = []
    if not OUTPUT_DIR.exists():
        return results
    for d in sorted(OUTPUT_DIR.iterdir()):
        if not d.is_dir():
            continue
        hist_path = d / "rank_history.json"
        if not hist_path.exists():
            continue
        try:
            history = json.loads(hist_path.read_text(encoding="utf-8"))
            if history:
                results.append(history[-1])
        except Exception:
            pass
    return results
