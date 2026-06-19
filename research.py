"""
Keyword research engine for ytauto.

Pipeline (runs in order, all failures are non-fatal):
  1. Google News RSS  — real-time trending tech/AI topics (no API key needed)
  2. YouTube Trending  — mostPopular Science & Technology chart
  3. YouTube Search Scoring — for each seed, search YouTube and score by
                               (views of top videos) vs (result count)
                               high views + low competition = opportunity
  4. YouTube Autocomplete  — public suggest endpoint
  5. Google Trends (pytrends) — 7-day rising queries (slow, optional)
  6. Agent Memory Boost  — multiply score by get_keyword_boost()
  7. Claude AI Idea Generator — Claude reads the gathered trends and generates
                                 5-10 fresh modern video ideas for the niche
  8. Used-keyword deduplication — never suggest the same keyword twice in
                                   30 days (tracked in used_keywords.json)
  9. Fallback list — hand-curated high-CPM evergreen topics if pipeline
                     returns too few results
"""
import datetime
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
import urllib.parse
import urllib.request

from config import (
    ANTHROPIC_API_KEY, CHANNEL_NICHE, CLAUDE_MODEL,
    OUTPUT_DIR, YOUTUBE_API_KEY,
)

logger    = logging.getLogger(__name__)
_BASE     = Path(__file__).parent
_YEAR     = str(datetime.datetime.now().year)

# ── Used-keyword log (rolling 30-day dedup) ────────────────────────────────────
_USED_KW_FILE = _BASE / "used_keywords.json"


def _load_used() -> dict[str, str]:
    """Returns {keyword_lower: iso_date_used}."""
    if not _USED_KW_FILE.exists():
        return {}
    try:
        return json.loads(_USED_KW_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_used(used: dict) -> None:
    _USED_KW_FILE.write_text(json.dumps(used, indent=2, ensure_ascii=False), encoding="utf-8")


def _mark_used(keywords: list[str]) -> None:
    used = _load_used()
    today = datetime.date.today().isoformat()
    for kw in keywords:
        used[kw.lower().strip()] = today
    # Prune entries older than 60 days
    cutoff = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()
    used = {k: v for k, v in used.items() if v >= cutoff}
    _save_used(used)


def _recently_used(keyword: str, days: int = 30) -> bool:
    used    = _load_used()
    cutoff  = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    entry   = used.get(keyword.lower().strip(), "")
    return entry >= cutoff if entry else False


# ── Seed topics ────────────────────────────────────────────────────────────────
_DEFAULT_SEED_TOPICS = [
    "artificial intelligence", "AI tools 2025", "machine learning tutorial",
    "ChatGPT tips", "Python automation", "AI productivity",
    "how to use AI", "tech explained", "AI side hustle", "Claude AI",
    "Google Gemini tips", "AI coding tools", "make money with AI",
]


def _load_seed_topics() -> list[str]:
    try:
        import channel_settings
        return channel_settings.get("seed_topics") or _DEFAULT_SEED_TOPICS
    except Exception:
        return _DEFAULT_SEED_TOPICS


# ── Niche filter ───────────────────────────────────────────────────────────────
_NICHE_WORDS   = {"ai", "gpt", "llm", "api", "ml"}
_NICHE_PHRASES = {
    "artificial intelligence", "chatgpt", "claude", "gemini",
    "machine learning", "deep learning", "python", "automation", "tutorial",
    "how to", "productivity", "tools", "coding", "programming",
    "software", "technology", "midjourney", "stable diffusion",
    "copilot", "perplexity", "workflow", "make money", "business",
    "no-code", "nocode", "zapier", "chatbot", "prompt", "openai",
    "tech", "ai tool", "tech explained", "n8n", "langchain", "ollama",
    "local ai", "open source ai", "cursor", "windsurf", "vibe coding",
    "agent", "agentic", "rag", "vector", "fine-tune", "hugging face",
    "side hustle", "passive income", "freelance", "solopreneur",
}


def _is_on_niche(text: str) -> bool:
    t     = text.lower()
    words = set(re.findall(r'\b\w+\b', t))
    return any(w in words for w in _NICHE_WORDS) or any(p in t for p in _NICHE_PHRASES)


# ── Slug / assembled check ────────────────────────────────────────────────────
def _slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"\s+", "_", text.strip())
    return text[:60]


def _already_assembled(keyword: str) -> bool:
    return (OUTPUT_DIR / _slugify(keyword) / "video.mp4").exists()


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 1 — Google News RSS (real-time trending, no API key needed)
# ─────────────────────────────────────────────────────────────────────────────

_GOOGLE_NEWS_FEEDS = [
    "https://news.google.com/rss/search?q=artificial+intelligence&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=AI+tools&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=ChatGPT+2025&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=machine+learning&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=AI+productivity&hl=en-US&gl=US&ceid=US:en",
]


def _fetch_google_news_trends() -> list[tuple[str, float]]:
    """
    Parse Google News RSS feeds for current tech/AI headlines.
    Converts headlines into YouTube-style keyword ideas.
    Returns (keyword, score) — score 70-95 based on freshness.
    """
    results: list[tuple[str, float]] = []
    today   = datetime.date.today()

    for feed_url in _GOOGLE_NEWS_FEEDS:
        try:
            req  = urllib.request.Request(feed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_data = resp.read().decode("utf-8", errors="ignore")
            root  = ET.fromstring(xml_data)
            items = root.findall(".//item")
            for item in items[:8]:
                title_el = item.find("title")
                pub_el   = item.find("pubDate")
                if title_el is None or not title_el.text:
                    continue
                headline = title_el.text.strip()
                # Parse age for freshness bonus
                age_days = 7
                if pub_el is not None and pub_el.text:
                    try:
                        from email.utils import parsedate_to_datetime
                        pub_dt  = parsedate_to_datetime(pub_el.text)
                        age_days = max(0, (today - pub_dt.date()).days)
                    except Exception:
                        pass
                # Convert headline to a YouTube search intent keyword
                kw = _headline_to_keyword(headline)
                if not kw or not _is_on_niche(kw):
                    continue
                freshness_bonus = max(0, 25 - age_days * 3)
                score = 70.0 + freshness_bonus
                results.append((kw, score))
            time.sleep(0.3)
        except Exception as exc:
            logger.debug(f"Google News RSS failed: {exc}")

    logger.info(f"Google News: {len(results)} trending keyword ideas")
    return results


def _headline_to_keyword(headline: str) -> str:
    """
    Convert a news headline to a YouTube-style searchable keyword.
    e.g. "OpenAI Releases GPT-5: Everything You Need to Know" →
         "openai gpt-5 explained everything you need to know"
    """
    h = headline.lower()
    # Remove source attribution (e.g. " - TechCrunch")
    h = re.sub(r"\s*[-–|]\s*\w[\w\s]+$", "", h)
    # Remove special characters except hyphens
    h = re.sub(r"[^\w\s-]", " ", h)
    h = re.sub(r"\s+", " ", h).strip()
    if 15 < len(h) < 80:
        return h
    # If too long, take first 70 chars up to a word boundary
    if len(h) >= 80:
        h = h[:75].rsplit(" ", 1)[0]
        return h if len(h) > 15 else ""
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 2 — YouTube Trending (mostPopular chart)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_youtube_trending() -> list[tuple[str, float]]:
    """Top Science & Technology trending videos → keyword ideas."""
    if not YOUTUBE_API_KEY:
        return []
    try:
        from googleapiclient.discovery import build
        yt   = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        resp = yt.videos().list(
            part="snippet,statistics",
            chart="mostPopular",
            regionCode="US",
            videoCategoryId="28",
            maxResults=30,
        ).execute()
        results = []
        for item in resp.get("items", []):
            title   = item["snippet"]["title"]
            vstats  = item.get("statistics", {})
            views   = int(vstats.get("viewCount", 0))
            likes   = int(vstats.get("likeCount", 0))
            # Engagement quality bonus
            eng_bonus = min(20, likes / max(views, 1) * 1000)
            clean     = _clean_title(title)
            if 15 < len(clean) < 80 and _is_on_niche(clean):
                score = 80.0 + eng_bonus + min(15, views / 50000)
                results.append((clean, round(score, 1)))
        logger.info(f"YouTube trending: {len(results)} on-niche ideas")
        return results
    except Exception as exc:
        logger.warning(f"YouTube trending fetch failed: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 3 — YouTube Search Opportunity Scoring
# ─────────────────────────────────────────────────────────────────────────────

def _score_youtube_opportunity(keyword: str) -> float:
    """
    Search YouTube for the keyword. Score based on:
    - Average views of top 5 videos (high = topic has an audience)
    - Result count (low = less competition)
    Returns 0-100 opportunity score.
    """
    if not YOUTUBE_API_KEY:
        return 50.0
    try:
        from googleapiclient.discovery import build
        yt   = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        resp = yt.search().list(
            q=keyword, part="id,snippet", type="video",
            order="relevance", maxResults=5,
            publishedAfter=(datetime.datetime.utcnow() -
                            datetime.timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ).execute()
        items = resp.get("items", [])
        total_results = int(resp.get("pageInfo", {}).get("totalResults", 0))
        if not items:
            return 30.0
        vid_ids = [i["id"]["videoId"] for i in items if i["id"].get("videoId")]
        if not vid_ids:
            return 30.0
        # Get stats for those videos
        stats_resp = yt.videos().list(
            part="statistics", id=",".join(vid_ids)
        ).execute()
        views_list = [
            int(v.get("statistics", {}).get("viewCount", 0))
            for v in stats_resp.get("items", [])
        ]
        avg_views = sum(views_list) / len(views_list) if views_list else 0
        # Opportunity: high avg views = proven demand; low competition = easier to rank
        demand_score      = min(50, avg_views / 5000)       # caps at 50k avg views
        competition_score = max(0, 50 - total_results / 5000)  # caps at 250k results
        return round(demand_score + competition_score, 1)
    except Exception as exc:
        logger.debug(f"YT opportunity score failed for '{keyword}': {exc}")
        return 50.0


def _fetch_youtube_search_ideas(seeds: list[str]) -> list[tuple[str, float]]:
    """
    For each seed, search recent YouTube videos and extract high-performing
    title patterns as keyword ideas. Recent high-view = proven trend.
    """
    if not YOUTUBE_API_KEY:
        return []
    results: list[tuple[str, float]] = []
    try:
        from googleapiclient.discovery import build
        yt        = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        cutoff    = (datetime.datetime.utcnow() - datetime.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for seed in seeds[:8]:
            try:
                resp = yt.search().list(
                    q=seed, part="id,snippet", type="video",
                    order="viewCount", maxResults=8,
                    publishedAfter=cutoff,
                ).execute()
                vid_ids = [i["id"]["videoId"] for i in resp.get("items", [])
                           if i["id"].get("videoId")]
                if not vid_ids:
                    continue
                stats_resp = yt.videos().list(
                    part="statistics,snippet", id=",".join(vid_ids)
                ).execute()
                for v in stats_resp.get("items", []):
                    views = int(v.get("statistics", {}).get("viewCount", 0))
                    if views < 5000:
                        continue
                    title = v["snippet"]["title"]
                    clean = _clean_title(title)
                    if 15 < len(clean) < 80 and _is_on_niche(clean):
                        # Score proportional to views (capped so outliers don't dominate)
                        score = 60.0 + min(35, views / 10000)
                        results.append((clean, round(score, 1)))
                time.sleep(0.3)
            except Exception as exc:
                logger.debug(f"YT search ideas for '{seed}': {exc}")
    except Exception as exc:
        logger.warning(f"YouTube search ideas failed: {exc}")
    logger.info(f"YouTube search ideas: {len(results)} candidates")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 4 — YouTube Autocomplete
# ─────────────────────────────────────────────────────────────────────────────

def _youtube_autocomplete(seeds: list[str]) -> list[tuple[str, float]]:
    """YouTube search suggestions via the public suggest endpoint."""
    results = []
    for seed in seeds[:8]:
        try:
            q   = urllib.parse.quote_plus(seed)
            url = f"https://suggestqueries.google.com/complete/search?client=firefox&q={q}&ds=yt"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data        = json.loads(resp.read().decode("utf-8"))
                suggestions = data[1] if isinstance(data, list) and len(data) > 1 else []
                for i, s in enumerate(suggestions[:6]):
                    if isinstance(s, str) and 15 < len(s) < 80 and _is_on_niche(s):
                        results.append((s, 85.0 - i * 5))
            time.sleep(0.35)
        except Exception as exc:
            logger.debug(f"Autocomplete failed for '{seed}': {exc}")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 5 — Google Trends via pytrends (optional, slow)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_pytrends_candidates(seeds: list[str]) -> list[tuple[str, float]]:
    candidates: list[tuple[str, float]] = []
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl="en-US", tz=360)
        for seed in seeds[:4]:          # limit to 4 to avoid rate limits
            try:
                pt.build_payload([seed], timeframe="now 7-d", geo="US")
                related = pt.related_queries()
                rising  = related.get(seed, {}).get("rising")
                if rising is not None and not rising.empty:
                    for _, row in rising.head(5).iterrows():
                        candidates.append((str(row["query"]), float(row["value"])))
                time.sleep(2.0)
            except Exception as exc:
                logger.warning(f"pytrends '{seed}': {exc}")
        logger.info(f"pytrends: {len(candidates)} candidates")
    except Exception as exc:
        logger.debug(f"pytrends unavailable: {exc}")
    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 6 — Claude AI Idea Generator
# ─────────────────────────────────────────────────────────────────────────────

def _generate_ai_ideas(
    trending_topics: list[str],
    memory_insights: dict,
    seed_topics: list[str],
    count: int = 8,
) -> list[tuple[str, float]]:
    """
    Send gathered trends + memory insights to Claude.
    Ask it to generate fresh, specific, modern video ideas for the channel.
    Returns (keyword, score) pairs — scored 95 (AI ideas get highest priority).
    """
    if not ANTHROPIC_API_KEY:
        return []
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        best_day  = memory_insights.get("best_upload_day", "")
        avg_views = memory_insights.get("avg_views", 0)
        best_len  = memory_insights.get("best_script_length", "")

        trending_str = "\n".join(f"- {t}" for t in trending_topics[:20]) or "No trends data"
        memory_str   = (
            f"Channel avg views/video: {avg_views:,}\n"
            f"Best performing upload day: {best_day or 'unknown'}\n"
            f"Best script length: {best_len or 'unknown'}\n"
        )

        prompt = f"""You are a YouTube channel strategist specializing in {CHANNEL_NICHE}.

CURRENT TRENDING TOPICS (from Google News + YouTube, last 48 hours):
{trending_str}

CHANNEL PERFORMANCE MEMORY:
{memory_str}

YOUR TASK:
Generate exactly {count} unique, specific YouTube video title/keyword ideas that:
1. Are TRENDING RIGHT NOW in {_YEAR} — reference current tools, models, events
2. Have HIGH search demand but MODERATE competition (not oversaturated)
3. Match a "how to", "best X", "I tested", "X explained", or "make money with X" format
4. Target HIGH CPM niches: AI tools, make money online, automation, coding, productivity
5. Are DIFFERENT from each other — cover different angles/formats
6. Sound natural as a YouTube search query (not a news headline)
7. Are 40-75 characters long — specific enough to rank but broad enough for views

OUTPUT FORMAT — return ONLY a JSON array of strings, nothing else:
["keyword idea 1", "keyword idea 2", ...]

DO NOT include any explanation, markdown, or text outside the JSON array."""

        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        # Log cost
        try:
            from cost_tracker import log_usage
            log_usage("research", CLAUDE_MODEL,
                      message.usage.input_tokens, message.usage.output_tokens)
        except Exception:
            pass

        raw = message.content[0].text.strip()
        # Extract JSON array
        match = re.search(r'\[.*?\]', raw, re.DOTALL)
        if not match:
            return []
        ideas = json.loads(match.group())
        results = []
        for idea in ideas:
            if isinstance(idea, str) and 15 < len(idea) < 90:
                # AI-generated ideas get top score
                results.append((idea.strip(), 95.0))
        logger.info(f"Claude AI ideas: generated {len(results)} keywords")
        return results
    except Exception as exc:
        logger.warning(f"Claude idea generation failed: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean_title(title: str) -> str:
    """Normalize a YouTube title into a clean keyword string."""
    clean = re.sub(r'\[.*?\]|\(.*?\)', '', title)
    clean = re.sub(r'[^\w\s-]', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip().lower()
    return clean


# ─────────────────────────────────────────────────────────────────────────────
# Fallback keywords — high-CPM evergreen (used only when sources return too few)
# ─────────────────────────────────────────────────────────────────────────────

FALLBACK_KEYWORDS = [
    "best AI tools to make money online in __YEAR__",
    "how to use ChatGPT to make $1000 a month",
    "AI side hustles that actually pay in __YEAR__",
    "how to start an AI automation business from scratch",
    "make money online with AI tools beginners guide",
    "ChatGPT vs Claude vs Gemini which AI is best __YEAR__",
    "best free AI tools better than paid alternatives __YEAR__",
    "I tested 10 AI writing tools here are the results",
    "how to use ChatGPT to write better code faster",
    "complete Claude AI tutorial for beginners __YEAR__",
    "automate your work with AI and save 10 hours a week",
    "no-code AI automation tools for small business __YEAR__",
    "how to build an AI workflow for content creation",
    "learn Python with AI assistance __YEAR__ complete guide",
    "how AI is changing software development in __YEAR__",
    "best way to learn AI and machine learning in __YEAR__",
    "prompt engineering masterclass complete beginners guide",
    "how to use AI to learn any new skill 10x faster",
    "local AI models you can run free on your computer",
    "open source AI tools better than ChatGPT in __YEAR__",
    "n8n automation tutorial complete beginners guide __YEAR__",
    "how to build an AI agent without coding in __YEAR__",
    "Ollama tutorial run AI models locally for free __YEAR__",
    "cursor AI coding tutorial complete beginners guide",
    "how to use Claude AI for coding and productivity __YEAR__",
    "Google Gemini tips and tricks you need to know __YEAR__",
    "AI tools that will replace your job and what to do",
    "how I make passive income with AI content creation",
    "best AI productivity system to 10x your output __YEAR__",
    "vibe coding tutorial build apps with AI no experience",
]

# How many candidates to keep before competition-filter (cap API calls)
_MAX_CANDIDATES = 60
COMPETITION_THRESHOLD = 300_000


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def get_trending_keywords(count: int = 5) -> list[str]:
    """
    Return *count* trending, high-value, non-repeated keyword ideas.

    Priority order:
      1. Claude AI-generated ideas (score 95)  ← always freshest
      2. YouTube search opportunity winners    ← proven demand
      3. Google News trending topics           ← real-time
      4. YouTube trending chart               ← high engagement
      5. YouTube autocomplete                 ← search intent
      6. pytrends rising queries              ← slower but signal-rich
      7. Fallback list                        ← guaranteed quality floor
    """
    seed_topics = _load_seed_topics()
    logger.info("Research: starting multi-source keyword pipeline…")

    # ── Collect raw candidates ────────────────────────────────────────────────
    candidates: list[tuple[str, float]] = []

    # Source 1 — Google News RSS (fast, always fresh)
    candidates.extend(_fetch_google_news_trends())

    # Source 2 — YouTube Trending chart
    candidates.extend(_fetch_youtube_trending())

    # Source 3 — YouTube Search Ideas (recent high-view videos)
    candidates.extend(_fetch_youtube_search_ideas(seed_topics))

    # Source 4 — YouTube Autocomplete
    ac_results = _youtube_autocomplete(seed_topics[:6])
    candidates.extend(ac_results)
    logger.info(f"Autocomplete: {len(ac_results)} candidates")

    # Source 5 — Google Trends (optional — may be slow)
    candidates.extend(_fetch_pytrends_candidates(seed_topics))

    # ── Deduplicate keeping highest score ─────────────────────────────────────
    seen: dict[str, float] = {}
    for kw, score in candidates:
        kw_norm = kw.lower().strip()
        if kw_norm not in seen or score > seen[kw_norm]:
            seen[kw_norm] = score

    # ── Apply agent memory boost ──────────────────────────────────────────────
    try:
        from agent_memory import get_keyword_boost
        boosted = {kw: round(score * get_keyword_boost(kw), 2)
                   for kw, score in seen.items()}
    except Exception:
        boosted = dict(seen)

    # ── Source 6 — Claude AI idea generation ─────────────────────────────────
    # Pass the top trending topics so Claude knows what's hot right now
    top_trending = [kw for kw, _ in sorted(boosted.items(), key=lambda x: -x[1])[:20]]
    try:
        from agent_memory import get_insights
        memory_insights = get_insights()
    except Exception:
        memory_insights = {}

    ai_ideas = _generate_ai_ideas(top_trending, memory_insights, seed_topics, count=count + 3)
    for kw, score in ai_ideas:
        kw_norm = kw.lower().strip()
        # AI ideas override — always use their score (95) even if already seen
        boosted[kw_norm] = max(boosted.get(kw_norm, 0), score)

    # ── Sort and filter ───────────────────────────────────────────────────────
    ranked = sorted(boosted.items(), key=lambda x: -x[1])[:_MAX_CANDIDATES]
    logger.info(f"Research: {len(ranked)} total candidates after scoring")

    filtered: list[str] = []
    for kw, score in ranked:
        if len(filtered) >= count:
            break
        if _already_assembled(kw):
            logger.debug(f"  ~ '{kw}' already assembled, skip")
            continue
        if _recently_used(kw, days=30):
            logger.debug(f"  ~ '{kw}' used within 30 days, skip")
            continue
        # Light competition check (only for non-AI-generated to save quota)
        if score < 90:
            result_count = _youtube_result_count(kw)
            if result_count is not None and result_count > COMPETITION_THRESHOLD:
                logger.info(f"  x '{kw}' too competitive ({result_count:,})")
                continue
        filtered.append(kw)

    # ── Pad with fallbacks if needed ─────────────────────────────────────────
    year = _YEAR
    fb_candidates = [fb.replace("__YEAR__", year) for fb in FALLBACK_KEYWORDS]
    fb_idx = 0
    while len(filtered) < count:
        if fb_idx >= len(fb_candidates):
            break
        fb = fb_candidates[fb_idx]
        fb_idx += 1
        if fb in filtered or _already_assembled(fb) or _recently_used(fb, days=7):
            continue
        filtered.append(fb)

    result = filtered[:count]
    _mark_used(result)

    logger.info(f"Research complete. Final keywords ({len(result)}): {result}")
    return result


def _youtube_result_count(keyword: str) -> Optional[int]:
    if not YOUTUBE_API_KEY:
        return None
    try:
        from googleapiclient.discovery import build
        yt   = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        resp = yt.search().list(
            q=keyword, part="id", type="video", maxResults=1
        ).execute()
        return int(resp.get("pageInfo", {}).get("totalResults", 0))
    except Exception as exc:
        logger.warning(f"YT result count failed for '{keyword}': {exc}")
        return None
