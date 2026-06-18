"""
Converts a script file to an MP3 narration using edge-tts.

Visual cue stripping:
  Anything inside square brackets (e.g. [VISUAL: show terminal]) is removed
  before synthesis so it never appears in the narration.
"""
import asyncio
import logging
import re
from pathlib import Path

import edge_tts

from config import TTS_VOICE

logger = logging.getLogger(__name__)

_BRACKET_RE = re.compile(r"\[.*?\]", re.DOTALL)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _clean(raw: str) -> str:
    """Strip visual cues and normalise whitespace."""
    text = _BRACKET_RE.sub("", raw)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    # Collapse any double-spaces left by the stripping
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


async def _synthesize(text: str, dest: str, voice: str) -> None:
    communicate = edge_tts.Communicate(text, voice=voice)
    await communicate.save(dest)


def generate_voice(
    script_path: Path,
    output_path: Path,
    voice: str = TTS_VOICE,
) -> None:
    """
    Read *script_path*, strip [VISUAL:] cues, synthesise speech → *output_path*.
    Skips synthesis if *output_path* already exists.
    """
    if output_path.exists():
        logger.info(f"Voice cache hit → {output_path}")
        return

    raw = script_path.read_text(encoding="utf-8")
    clean_text = _clean(raw)
    word_count = len(clean_text.split())
    logger.info(f"Synthesising {word_count:,} words with voice '{voice}'…")

    asyncio.run(_synthesize(clean_text, str(output_path), voice))
    logger.info(f"Voice saved → {output_path}")
