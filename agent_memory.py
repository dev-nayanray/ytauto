"""
Agent learning memory for ytauto.

Records per-video performance after each YouTube stats refresh.
Derives actionable insights: best topics, best upload times, best script length.
These insights feed back into research.py keyword ranking and upload scheduling.
"""
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BASE       = Path(__file__).parent
MEMORY_FILE = _BASE / "agent_memory.json"


# ── Persistence ───────────────────────────────────────────────────────────────

def _load() -> dict:
    if not MEMORY_FILE.exists():
        return {"videos": {}, "topic_scores": {}, "insights": {}, "updated_at": ""}
    try:
        return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"videos": {}, "topic_scores": {}, "insights": {}, "updated_at": ""}


def _save(mem: dict) -> None:
    mem["updated_at"] = datetime.utcnow().isoformat()
    MEMORY_FILE.write_text(json.dumps(mem, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Recording ─────────────────────────────────────────────────────────────────

def record_video(
    slug: str,
    keyword: str,
    views: int,
    likes: int,
    comments: int,
    duration_s: int,
    upload_date: str,   # "YYYY-MM-DD"
    script_words: int = 0,
    thumbnail_style: int = -1,
) -> None:
    """Record or update performance data for one video."""
    mem = _load()
    videos: dict = mem.setdefault("videos", {})

    entry = videos.get(slug, {})
    entry.update({
        "slug":            slug,
        "keyword":         keyword,
        "views":           views,
        "likes":           likes,
        "comments":        comments,
        "duration_s":      duration_s,
        "upload_date":     upload_date,
        "script_words":    script_words,
        "thumbnail_style": thumbnail_style,
        "engagement_rate": round((likes + comments) / max(views, 1), 4),
        "recorded_at":     datetime.utcnow().isoformat(),
    })

    # Upload hour/day (best-effort from upload_date)
    try:
        dt = datetime.fromisoformat(upload_date)
        entry["upload_hour"] = dt.hour
        entry["upload_day"]  = dt.strftime("%A")
    except Exception:
        pass

    videos[slug] = entry
    mem["videos"] = videos

    # Recompute derived insights after each recording
    mem["topic_scores"] = _compute_topic_scores(videos)
    mem["insights"]     = _compute_insights(videos)
    _save(mem)


def record_batch(video_stats: list[dict]) -> int:
    """
    Batch-record from YouTube API results.
    video_stats: list of dicts with keys: slug, keyword, views, likes, comments,
                 duration_s, upload_date  (all optional/0 if missing)
    Returns number of videos recorded.
    """
    count = 0
    for v in video_stats:
        try:
            slug = v.get("slug", "")
            if not slug:
                continue
            record_video(
                slug=slug,
                keyword=v.get("keyword", slug.replace("_", " ")),
                views=int(v.get("views", 0)),
                likes=int(v.get("likes", 0)),
                comments=int(v.get("comments", 0)),
                duration_s=int(v.get("duration_s", 0)),
                upload_date=v.get("upload_date") or v.get("published_at", "")[:10],
                script_words=v.get("script_words", 0),
            )
            count += 1
        except Exception as exc:
            logger.debug(f"record_batch skip {v.get('slug')}: {exc}")
    return count


# ── Topic scoring ─────────────────────────────────────────────────────────────

def _extract_topics(keyword: str) -> list[str]:
    """Pull meaningful single/double words from a keyword string."""
    kw = keyword.lower()
    words = re.findall(r"\b[a-z]{3,}\b", kw)
    stop  = {"the","and","for","with","how","you","are","this","that","from",
              "your","have","will","can","use","make","best","free","what",
              "into","more","also","about","using","tutorial","beginners",
              "complete","guide","explain","explained","simply","simple",
              "working","actually","honest","truth","results"}
    return [w for w in words if w not in stop][:5]


def _compute_topic_scores(videos: dict) -> dict:
    """Average views per topic word across all recorded videos."""
    topic_views: dict[str, list[int]] = defaultdict(list)
    for v in videos.values():
        kw     = v.get("keyword", "")
        views  = int(v.get("views", 0))
        topics = _extract_topics(kw)
        for t in topics:
            topic_views[t].append(views)

    scores: dict[str, dict] = {}
    for topic, view_list in topic_views.items():
        scores[topic] = {
            "avg_views":   round(sum(view_list) / len(view_list)),
            "max_views":   max(view_list),
            "count":       len(view_list),
            "total_views": sum(view_list),
        }
    return scores


# ── Insights ──────────────────────────────────────────────────────────────────

def _compute_insights(videos: dict) -> dict:
    if not videos:
        return {}

    all_views   = [int(v.get("views", 0)) for v in videos.values()]
    all_likes   = [int(v.get("likes", 0)) for v in videos.values()]
    all_eng     = [float(v.get("engagement_rate", 0)) for v in videos.values()]

    # Best upload day
    day_views: dict[str, list] = defaultdict(list)
    for v in videos.values():
        day = v.get("upload_day")
        if day:
            day_views[day].append(int(v.get("views", 0)))
    best_day = max(day_views, key=lambda d: sum(day_views[d]) / len(day_views[d])) if day_views else None
    best_day_avg = round(sum(day_views[best_day]) / len(day_views[best_day])) if best_day else 0

    # Best script length bracket
    length_buckets: dict[str, list] = {"short(<800)": [], "medium(800-1400)": [], "long(>1400)": []}
    for v in videos.values():
        w = int(v.get("script_words", 0))
        vw = int(v.get("views", 0))
        if 0 < w < 800:
            length_buckets["short(<800)"].append(vw)
        elif 800 <= w <= 1400:
            length_buckets["medium(800-1400)"].append(vw)
        elif w > 1400:
            length_buckets["long(>1400)"].append(vw)
    best_length = None
    best_length_avg = 0
    for bracket, vlist in length_buckets.items():
        if vlist:
            avg = sum(vlist) / len(vlist)
            if avg > best_length_avg:
                best_length_avg = avg
                best_length = bracket

    # Top 5 videos
    sorted_vids = sorted(videos.values(), key=lambda v: int(v.get("views", 0)), reverse=True)
    top_videos  = [{"slug": v["slug"], "keyword": v.get("keyword",""), "views": v.get("views",0)} for v in sorted_vids[:5]]

    return {
        "total_videos":     len(videos),
        "avg_views":        round(sum(all_views) / len(all_views)) if all_views else 0,
        "max_views":        max(all_views) if all_views else 0,
        "total_views":      sum(all_views),
        "avg_likes":        round(sum(all_likes) / len(all_likes)) if all_likes else 0,
        "avg_engagement":   round(sum(all_eng) / len(all_eng), 4) if all_eng else 0,
        "best_upload_day":  best_day,
        "best_day_avg_views": best_day_avg,
        "best_script_length": best_length,
        "top_videos":       top_videos,
    }


# ── Public read API ───────────────────────────────────────────────────────────

def get_all() -> dict:
    return _load()


def get_insights() -> dict:
    return _load().get("insights", {})


def get_topic_scores() -> dict:
    return _load().get("topic_scores", {})


def get_top_topics(n: int = 10) -> list[dict]:
    """Return top N topics sorted by avg views."""
    scores = get_topic_scores()
    ranked = sorted(scores.items(), key=lambda x: x[1]["avg_views"], reverse=True)
    return [{"topic": t, **s} for t, s in ranked[:n]]


def get_recommendations(n: int = 5) -> list[dict]:
    """
    Return content recommendations based on learned patterns.
    Each recommendation has: title, reason, confidence (low/medium/high)
    """
    mem      = _load()
    insights = mem.get("insights", {})
    topics   = get_top_topics(20)
    recs: list[dict] = []

    # Rec: best performing topics to focus on
    if topics:
        top3 = [t["topic"] for t in topics[:3]]
        recs.append({
            "type":       "topic_focus",
            "title":      f"Focus on topics: {', '.join(top3)}",
            "reason":     f"Your top keywords average {topics[0]['avg_views']:,} views",
            "confidence": "high" if topics[0]["avg_views"] > 100 else "medium",
            "action":     f"Add '{top3[0]}' related keywords to your queue",
        })

    # Rec: best upload day
    best_day = insights.get("best_upload_day")
    if best_day:
        avg = insights.get("best_day_avg_views", 0)
        recs.append({
            "type":       "upload_timing",
            "title":      f"Upload on {best_day}s for best initial reach",
            "reason":     f"Your {best_day} uploads average {avg:,} views",
            "confidence": "medium",
            "action":     f"Set scheduler to run on {best_day}",
        })

    # Rec: script length
    best_len = insights.get("best_script_length")
    if best_len:
        recs.append({
            "type":       "script_length",
            "title":      f"Optimal script length: {best_len}",
            "reason":     "Videos in this bracket get the most views for your channel",
            "confidence": "medium",
            "action":     "Adjust TARGET_WORD_COUNT in config.py",
        })

    # Rec: low engagement alert
    avg_eng = insights.get("avg_engagement", 0)
    if 0 < avg_eng < 0.02:
        recs.append({
            "type":       "engagement_alert",
            "title":      "Low engagement rate — improve CTAs in scripts",
            "reason":     f"Avg engagement is {avg_eng:.1%} (target: >2%)",
            "confidence": "high",
            "action":     "Add stronger subscribe CTAs and comment questions",
        })

    # Rec: volume boost
    total_vids = insights.get("total_videos", 0)
    if total_vids < 20:
        recs.append({
            "type":       "volume",
            "title":      "Publish more consistently to accelerate growth",
            "reason":     f"You have {total_vids} videos — channels need 30+ to gain algorithmic momentum",
            "confidence": "high",
            "action":     "Increase --count to 2 or 3 per run",
        })

    return recs[:n]


def get_keyword_boost(keyword: str) -> float:
    """
    Returns a multiplier (1.0 = neutral, >1.0 = boosted) for a candidate keyword
    based on how similar keywords have performed historically.
    Used by research.py to re-rank candidates.
    """
    topics = _extract_topics(keyword)
    if not topics:
        return 1.0
    scores = get_topic_scores()
    boosts = [scores[t]["avg_views"] for t in topics if t in scores]
    if not boosts:
        return 1.0
    avg_boost = sum(boosts) / len(boosts)
    # Normalize: channel avg views = baseline
    insights = get_insights()
    channel_avg = insights.get("avg_views", 1) or 1
    multiplier = avg_boost / channel_avg
    return round(max(0.5, min(3.0, multiplier)), 2)
