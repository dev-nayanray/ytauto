"""
Generates an original, retention-optimised YouTube script via Claude.

The script uses [VISUAL: …] cues to guide editing; voice.py strips
these before TTS so they never appear in the narration.
"""
import logging
from pathlib import Path

import anthropic

from config import ANTHROPIC_API_KEY, CHANNEL_NICHE, CLAUDE_MODEL, TARGET_WORD_COUNT

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are an elite YouTube scriptwriter who specialises in viral Tech and AI content.
Your scripts consistently achieve 60%+ audience retention and drive subscriptions.

Your writing style:
  • Open with a SHOCK — a counterintuitive fact, a surprising number, or a bold claim that makes viewers stop scrolling.
  • Use "curiosity loops" — every 90 seconds, hint at something bigger coming later to prevent drop-off.
  • Write like a knowledgeable friend, not a lecturer. Short sentences. Contractions. Energy.
  • Every claim is specific: real tool names, real numbers, real examples.
  • Pattern interrupts every 2–3 minutes to reset viewer attention.
  • Built-in mid-video CTA at the 40% mark (feels natural, not forced).
  • Strong close that makes subscribing feel obvious and valuable.

Include [VISUAL: brief description] inline cues — stripped before TTS, guide the editor.
The first word must NOT be "Hey", "Hi", "Welcome", or "Today".\
"""

_USER_TEMPLATE = """\
Write a complete YouTube narration script on this topic:
"{keyword}"

Channel niche: {niche}
Target audience: busy people who want real, actionable AI knowledge — not hype.
Target spoken length: ≈{word_count} words ({minutes} min at 150 wpm).

─── VIRAL STRUCTURE ───────────────────────────────────────────────────────────

1. VIRAL HOOK  (0–30 s)
   Open with ONE of these proven formulas:
   • A shocking statistic: "X% of people don't know that..."
   • A counterintuitive claim: "Everything you've been told about [topic] is wrong."
   • A bold promise: "In the next {minutes} minutes, you'll know exactly how to [outcome]."
   The hook must make the viewer feel they will MISS OUT if they click away.
   [VISUAL: dramatic opening graphic or eye-catching text]

2. CURIOSITY LOOP INTRO  (30 s–1 min)
   Expand on the hook. Mention 3 specific things they will learn.
   End with: "And I'll also reveal [intriguing thing] at the end that most people miss."
   [VISUAL: animated list / preview of topics]

3. MAIN CONTENT  (split into 3–5 clearly signposted sections)
   Each section must have:
     a) A plain-English explanation with a real, specific example.
     b) A [VISUAL: …] cue every 2–3 sentences.
     c) End each section by teasing the next: "But here's where it gets interesting…"

   At the 40% mark, include a NATURAL mid-video CTA:
   "Quick note — if you find this useful, subscribing takes two seconds and it genuinely helps me keep making these. Now, back to [topic]..."

4. PATTERN INTERRUPT  (every 90–120 s)
   Insert a brief energy reset: a surprising fact, a relatable joke, a "did you know?", or a direct question to the viewer.

5. THE REVEAL  (near the end — this was teased in the intro loop)
   Deliver the "secret" or insight you hinted at in the intro. Make it genuinely valuable.

6. KEY TAKEAWAYS  (30 s)
   3–5 specific, actionable things they can do TODAY. Not vague advice.

7. STRONG CLOSE + CTA  (20 s)
   "If this video saved you time or taught you something new, subscribe — I post every week and the next video on [related topic] is coming [timeframe]."
   Ask a specific comment question to drive engagement.
   [VISUAL: subscribe animation / end screen]

─── HARD RULES ────────────────────────────────────────────────────────────────
• Narration ONLY — no section headers, no stage directions except [VISUAL:] cues.
• Every fact must be accurate. No placeholder stats.
• Do NOT include a title at the top of the script.
• Minimum {word_count} words. This is a 10-minute video — be thorough.
• Write the full script now:\
"""


def generate_script(keyword: str, output_path: Path) -> str:
    """
    Generate a script for *keyword* and persist it to *output_path*.
    Returns the script text (reads cache if the file already exists).
    """
    if output_path.exists():
        logger.info(f"Script cache hit → {output_path}")
        return output_path.read_text(encoding="utf-8")

    logger.info(f"Generating script for: '{keyword}'")

    prompt = _USER_TEMPLATE.format(
        keyword=keyword,
        niche=CHANNEL_NICHE,
        word_count=TARGET_WORD_COUNT,
        minutes=TARGET_WORD_COUNT // 150,
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    script_text: str = message.content[0].text
    word_count = len(script_text.split())
    output_path.write_text(script_text, encoding="utf-8")
    logger.info(f"Script saved ({word_count:,} words) → {output_path}")
    return script_text
