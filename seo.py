"""
Generates YouTube SEO metadata via Claude.

Claude is instructed to return strict JSON; this module validates the
structure and enforces YouTube character limits before persisting to seo.json.
"""
import json
import logging
import re
from pathlib import Path
from typing import TypedDict

import anthropic

from config import ANTHROPIC_API_KEY, CHANNEL_NICHE, CLAUDE_MODEL

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 100
MAX_TAGS = 15
MAX_HASHTAGS = 3


class SeoData(TypedDict):
    title: str
    description: str
    tags: list[str]
    hashtags: list[str]


def generate_seo(keyword: str, script_path: Path, output_path: Path) -> SeoData:
    """
    Generate SEO metadata for *keyword* (uses script excerpt for context).
    Returns a SeoData dict and writes it to *output_path* as JSON.
    Skips generation if *output_path* already exists.
    """
    if output_path.exists():
        logger.info(f"SEO cache hit → {output_path}")
        with open(output_path, encoding="utf-8") as f:
            return json.load(f)

    # Provide the first 2 000 chars of the script as context
    script_excerpt = ""
    if script_path.exists():
        script_excerpt = script_path.read_text(encoding="utf-8")[:2_000]

    logger.info(f"Generating SEO metadata for '{keyword}'…")

    prompt = f"""You are a YouTube SEO and growth specialist for a {CHANNEL_NICHE} channel.
Your titles consistently achieve 8-12% CTR. You know YouTube's algorithm deeply.

Video topic: "{keyword}"

Script excerpt (first 2 000 chars):
{script_excerpt}

Return ONLY a JSON object — no markdown, no explanation, no code fences.

{{
  "title": "...",
  "description": "...",
  "tags": ["tag1", ..., "tag15"],
  "hashtags": ["#Tag1", "#Tag2", "#Tag3"]
}}

─── TITLE RULES (most important) ───────────────────────────────────────────────
Write the title using ONE of these proven high-CTR formulas:
  • Numbers: "7 AI Tools That [Benefit] in 2025 (I Tested All of Them)"
  • Contrast: "Why [Common Belief] Is WRONG — [Real Truth]"
  • How-to + benefit: "How to [Outcome] in [Short Time] (Even If You're a Beginner)"
  • Secret reveal: "The AI Tool Nobody Talks About (But You Need to Know)"
  • I tested: "I Tried [X] — Here's the Honest Truth"
  • Curiosity gap: "What Happens When You [Do Unusual Thing With AI]"
- Under 70 characters (ideal), never over 100
- Must contain the primary keyword naturally
- Use CAPS on 1-2 emotional/power words only (not every word)
- No vague clickbait — the title must accurately describe what the viewer gets

─── DESCRIPTION RULES ───────────────────────────────────────────────────────────
Write 500-700 words structured as:
  Line 1-2: Powerful hook (these show before "Show More" — make them compelling)
  Line 3+: What you'll learn (3-5 bullet points with ✅ emoji)
  Then: Timestamps section with [TIMESTAMPS] placeholder
  Then: "🔔 Subscribe for weekly AI tutorials that actually teach you something real."
  Then: "📌 More videos you'll love:" with [RELATED_LINKS] placeholder
  Then: Natural keyword integration (mention keyword 3-4 times)
  End with: [CHANNEL_LINK]

─── TAGS RULES ──────────────────────────────────────────────────────────────────
Exactly 15 tags mixing:
  - 3 broad terms (e.g. "artificial intelligence", "AI tools", "tech tutorial")
  - 7 specific mid-tail (e.g. "best ai tools 2025", "chatgpt tips")
  - 5 long-tail exact-match (e.g. "how to use ai tools for beginners 2025")
  Each tag under 30 characters. No special chars except spaces and hyphens.

─── HASHTAGS ─────────────────────────────────────────────────────────────────────
Exactly 3 hashtags. Capitalise each word. Format: #AITools #HowTo #MachineLearning
"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1_024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    data = _parse_json(raw, keyword)
    data = _enforce_limits(data)
    data = _inject_timestamps(data, script_path)  # add this line

    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"SEO metadata saved → {output_path}")
    return data


def _inject_timestamps(data: SeoData, script_path: Path) -> SeoData:
    """Replace [TIMESTAMPS] placeholder with real chapter markers derived from script word count."""
    if "[TIMESTAMPS]" not in data.get("description", ""):
        return data
    if not script_path.exists():
        data["description"] = data["description"].replace("[TIMESTAMPS]", "")
        return data

    text = script_path.read_text(encoding="utf-8")
    # Strip [VISUAL:...] cues from word count
    clean = re.sub(r'\[VISUAL:[^\]]*\]', '', text)
    words = clean.split()

    # Build chapters every ~200 words
    WORDS_PER_MINUTE = 150
    chapters = []
    chapter_size = 200
    cumulative_seconds = 0

    # Intro is always 0:00
    chapters.append(("0:00", "Intro"))

    i = 0
    chapter_num = 1
    chapter_names = ["What You'll Learn", "Getting Started", "Deep Dive", "Key Tips", "Advanced Strategies", "Common Mistakes", "Best Practices", "Final Thoughts"]

    while i + chapter_size < len(words):
        i += chapter_size
        cumulative_seconds = int((i / WORDS_PER_MINUTE) * 60)
        mins = cumulative_seconds // 60
        secs = cumulative_seconds % 60
        name = chapter_names[min(chapter_num - 1, len(chapter_names) - 1)]
        chapters.append((f"{mins}:{secs:02d}", name))
        chapter_num += 1
        if chapter_num > len(chapter_names):
            break

    # Add outro
    total_secs = int((len(words) / WORDS_PER_MINUTE) * 60) - 15
    if total_secs > 0:
        m, s = divmod(max(0, total_secs), 60)
        chapters.append((f"{m}:{s:02d}", "Recap & Key Takeaways"))

    timestamp_block = "\n📌 Chapters:\n" + "\n".join(f"{ts} {name}" for ts, name in chapters)
    data["description"] = data["description"].replace("[TIMESTAMPS]", timestamp_block)
    return data


def _parse_json(raw: str, keyword: str) -> SeoData:
    """Parse Claude's response, stripping any accidental markdown fences."""
    text = raw
    if text.startswith("```"):
        # Strip opening fence (```json or ```)
        text = text[text.find("\n") + 1 :]
    if text.endswith("```"):
        text = text[: text.rfind("```")].rstrip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error(f"Claude returned invalid JSON for SEO ({exc}). Using fallback.")
        return _fallback(keyword)


def _enforce_limits(data: SeoData) -> SeoData:
    data["tags"] = data.get("tags", [])[:MAX_TAGS]
    data["hashtags"] = data.get("hashtags", [])[:MAX_HASHTAGS]
    title = data.get("title", "")
    if len(title) > MAX_TITLE_CHARS:
        data["title"] = title[: MAX_TITLE_CHARS - 1] + "…"
    return data


def _fallback(keyword: str) -> SeoData:
    words = keyword.split()
    return SeoData(
        title=keyword[:70],
        description=(
            f"In this video we explore {keyword} — everything you need to know "
            f"explained clearly with real examples.\n\nSubscribe for weekly Tech "
            f"and AI tutorials. [CHANNEL_LINK]"
        ),
        tags=(words + ["AI", "tech", "tutorial", "how to", "explained"])[:MAX_TAGS],
        hashtags=["#AI", "#Tech", "#Tutorial"],
    )
