"""
Identifies low-competition trending keywords for the channel.

Strategy:
  1. Pull rising queries from pytrends for several seed topics.
  2. Fetch YouTube autocomplete suggestions (free, no API key needed).
  3. Extract keyword ideas from trending YouTube tech/AI videos (needs YOUTUBE_API_KEY).
  4. De-duplicate and sort by trend score.
  5. Optionally filter by YouTube search result count (requires YOUTUBE_API_KEY).
  6. Pad with hand-curated fallbacks if sources return too few results.
"""
import logging
import re
import time
from typing import Optional

from config import OUTPUT_DIR, YOUTUBE_API_KEY, CHANNEL_NICHE

logger = logging.getLogger(__name__)

SEED_TOPICS = [
    "artificial intelligence",
    "AI tools",
    "machine learning tutorial",
    "ChatGPT tips",
    "Python automation",
    "AI productivity",
    "how to use AI",
    "tech explained",
]

# Hard-coded evergreen fallbacks — always original, always useful
# High-CPM monetizable topics: AI tools, make money, business automation,
# investing, software tutorials ($15-40 CPM niches)
FALLBACK_KEYWORDS = [
    # High CPM: AI tools for business/money
    "best AI tools to make money online in 2025",
    "how to use ChatGPT to make $1000 a month",
    "AI side hustles that actually pay in 2025",
    "how to start an AI automation business from scratch",
    "make money online with AI tools beginners guide",
    "best AI tools for freelancers to earn more money",
    "how to use AI to grow your business fast",
    "AI tools that replace expensive software in 2025",
    # High CPM: comparisons and reviews
    "ChatGPT vs Claude vs Gemini which AI is best 2025",
    "best free AI tools better than paid alternatives 2025",
    "I tested 10 AI writing tools here are the results",
    "best AI image generators compared honest review 2025",
    "top AI coding assistants compared for developers 2025",
    "best AI video generators that actually work in 2025",
    # High CPM: tutorials on specific tools
    "how to use ChatGPT to write better code faster",
    "complete Claude AI tutorial for beginners 2025",
    "how to use Perplexity AI for research and studying",
    "Microsoft Copilot complete beginner guide 2025",
    "how to use Google Gemini for productivity 2025",
    "Midjourney vs DALL-E vs Stable Diffusion comparison 2025",
    # Productivity and automation
    "automate your work with AI and save 10 hours a week",
    "how to build an AI workflow for content creation",
    "no-code AI automation tools for small business 2025",
    "how to use Zapier and AI to automate your business",
    "AI productivity system that changed how I work",
    # Learning and skills
    "learn Python in 30 days with AI assistance",
    "how AI is changing software development in 2025",
    "best way to learn AI and machine learning in 2025",
    "prompt engineering masterclass for beginners",
    "how to use AI to learn any new skill 10x faster",
    # Evergreen tech tutorials
    "Python automation tutorial complete beginners guide",
    "machine learning explained simply for beginners",
    "how to build a chatbot without coding in 2025",
    "local AI models you can run free on your computer",
    "open source AI tools better than ChatGPT in 2025",
    # Viral-format topics
    "AI tools I wish I knew sooner complete guide",
    "stop using Google for research use these AI tools instead",
    "the truth about AI replacing jobs in 2025",
    "how I use AI to work 4 hours a day remotely",
    "AI tools that will make you rich if you start now",
]

# YouTube search result count below which we consider a keyword low-competition
COMPETITION_THRESHOLD = 200_000


def _slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s]+", "_", text.strip())
    return text[:60]


def _already_assembled(keyword: str) -> bool:
    """Return True if this keyword already has a video.mp4 on disk."""
    return (OUTPUT_DIR / _slugify(keyword) / "video.mp4").exists()


# Short terms (2-4 chars) need exact word-boundary matching to avoid false hits
# e.g. "ai" in "paint" or "gpt" in "egypt" — must match as whole words only
_NICHE_WORDS = {"ai", "gpt", "llm", "api", "ml"}

# Longer phrases are safe as substrings
_NICHE_PHRASES = {
    "artificial intelligence", "chatgpt", "claude", "gemini",
    "machine learning", "deep learning", "python", "automation", "tutorial",
    "how to", "productivity", "tools", "coding", "programming",
    "software", "technology", "midjourney", "stable diffusion",
    "copilot", "perplexity", "workflow", "make money", "business",
    "no-code", "nocode", "zapier", "chatbot", "prompt", "openai",
    "tech tutorial", "ai tool", "tech explained",
}


def _is_on_niche(text: str) -> bool:
    t = text.lower()
    words = set(re.findall(r'\b\w+\b', t))
    return any(w in words for w in _NICHE_WORDS) or any(p in t for p in _NICHE_PHRASES)


def _youtube_autocomplete(seeds: list[str]) -> list[tuple[str, float]]:
    """Fetch YouTube search suggestions using the public autocomplete endpoint."""
    import urllib.request
    import urllib.parse
    import json as _json
    results = []
    for seed in seeds[:6]:
        try:
            q = urllib.parse.quote_plus(seed)
            url = f"https://suggestqueries.google.com/complete/search?client=firefox&q={q}&ds=yt"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
                suggestions = data[1] if isinstance(data, list) and len(data) > 1 else []
                for i, s in enumerate(suggestions[:5]):
                    if isinstance(s, str) and 15 < len(s) < 80 and _is_on_niche(s):
                        results.append((s, 90.0 - i * 5))
            time.sleep(0.4)
        except Exception as exc:
            logger.debug(f"Autocomplete failed for '{seed}': {exc}")
    return results


def _fetch_trending_keywords() -> list[tuple[str, float]]:
    """Extract on-niche keyword ideas from trending YouTube tech/AI videos."""
    if not YOUTUBE_API_KEY:
        return []
    try:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        resp = yt.videos().list(
            part="snippet",
            chart="mostPopular",
            regionCode="US",
            videoCategoryId="28",  # Science & Technology
            maxResults=25,
        ).execute()
        results = []
        for item in resp.get("items", []):
            title = item["snippet"]["title"]
            clean = re.sub(r'\[.*?\]|\(.*?\)', '', title)
            clean = re.sub(r'[^\w\s]', ' ', clean)
            clean = re.sub(r'\s+', ' ', clean).strip().lower()
            # Only include if relevant to AI/tech/productivity niche
            if 15 < len(clean) < 80 and _is_on_niche(clean):
                results.append((clean, 85.0))
        logger.info(f"YouTube trending: {len(results)} on-niche keyword ideas")
        return results
    except Exception as exc:
        logger.warning(f"YouTube trending fetch failed: {exc}")
        return []


def get_trending_keywords(count: int = 5) -> list[str]:
    """
    Return *count* low-competition trending keywords.
    Prefers keywords that have NOT been assembled yet so each run
    generates fresh content.  Falls back gracefully on any error.
    """
    logger.info("Starting keyword research…")

    candidates: list[tuple[str, float]] = []

    # Source 1: pytrends
    candidates.extend(_fetch_pytrends_candidates())

    # Source 2: YouTube autocomplete
    autocomplete_results = _youtube_autocomplete(SEED_TOPICS[:6])
    candidates.extend(autocomplete_results)
    logger.info(f"YouTube autocomplete: {len(autocomplete_results)} candidates")

    # Source 3: YouTube trending videos
    trending = _fetch_trending_keywords()
    candidates.extend(trending)

    # De-duplicate preserving highest score
    seen: dict[str, float] = {}
    for kw, score in candidates:
        kw_lower = kw.lower().strip()
        if kw_lower not in seen or score > seen[kw_lower]:
            seen[kw_lower] = score

    unique = sorted(seen.items(), key=lambda x: x[1], reverse=True)

    # Filter: skip assembled, check competition
    filtered: list[str] = []
    for kw, _ in unique:
        if len(filtered) >= count:
            break
        if _already_assembled(kw):
            logger.info(f"  ~ '{kw}' already assembled, skipping")
            continue
        result_count = _youtube_result_count(kw)
        if result_count is None or result_count < COMPETITION_THRESHOLD:
            filtered.append(kw)
        else:
            logger.info(f"  x '{kw}' too competitive: {result_count:,}")

    # Pad with fallbacks
    fb_idx = 0
    while len(filtered) < count and fb_idx < len(FALLBACK_KEYWORDS) * 2:
        fb = FALLBACK_KEYWORDS[fb_idx % len(FALLBACK_KEYWORDS)]
        fb_idx += 1
        if fb in filtered or _already_assembled(fb):
            continue
        filtered.append(fb)

    # Last resort
    for fb in FALLBACK_KEYWORDS:
        if len(filtered) >= count:
            break
        if fb not in filtered:
            filtered.append(fb)

    result = filtered[:count]
    logger.info(f"Final keywords ({len(result)}): {result}")
    return result


def _fetch_pytrends_candidates() -> list[tuple[str, float]]:
    """Return (keyword, trend_score) pairs from pytrends rising queries."""
    candidates: list[tuple[str, float]] = []
    try:
        from pytrends.request import TrendReq  # lazy import — fails gracefully

        pt = TrendReq(hl="en-US", tz=360)

        for seed in SEED_TOPICS:
            try:
                pt.build_payload([seed], timeframe="now 7-d", geo="US")
                related = pt.related_queries()
                rising = related.get(seed, {}).get("rising")
                if rising is not None and not rising.empty:
                    for _, row in rising.head(5).iterrows():
                        candidates.append((str(row["query"]), float(row["value"])))
                time.sleep(1.5)  # avoid Google rate-limiting
            except Exception as exc:
                logger.warning(f"pytrends seed '{seed}' failed: {exc}")

        logger.info(f"pytrends returned {len(candidates)} raw candidates")
    except Exception as exc:
        logger.warning(f"pytrends unavailable ({exc}); using fallback keywords only")

    return candidates


def _youtube_result_count(keyword: str) -> Optional[int]:
    """
    Return estimated YouTube search result count for *keyword*.
    Returns None if YOUTUBE_API_KEY is not set or the request fails.
    """
    if not YOUTUBE_API_KEY:
        return None
    try:
        from googleapiclient.discovery import build

        yt = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        resp = (
            yt.search()
            .list(q=keyword, part="id", type="video", maxResults=1)
            .execute()
        )
        return int(resp.get("pageInfo", {}).get("totalResults", 0))
    except Exception as exc:
        logger.warning(f"YouTube competition check failed for '{keyword}': {exc}")
        return None
