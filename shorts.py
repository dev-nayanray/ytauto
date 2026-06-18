"""
Generates a 60-second vertical (1080×1920) YouTube Short from the same keyword.

Pipeline:
  1. Generate a short hook-style script (~130 words, ~52 sec at 150wpm)
  2. Synthesise voice (reuses voice.py)
  3. Download vertical portrait clips from Pexels
  4. Assemble vertical video with FFmpeg (1080×1920)
  5. Add text overlay with the hook text
  6. Upload as a YouTube Short
"""
import logging
import re
import subprocess
from pathlib import Path

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    FFMPEG_EXE,
    FFPROBE_EXE,
    OUTPUT_DIR,
    PEXELS_API_KEY,
)

logger = logging.getLogger(__name__)

SHORT_W, SHORT_H = 1080, 1920
SHORT_MAX_SECONDS = 58  # stay under 60s


def generate_short(keyword: str, output_dir: Path) -> Path:
    """
    Create a YouTube Short for *keyword* in *output_dir/short/*.
    Returns the path to the assembled short video.mp4.
    Skips if short/video.mp4 already exists.
    """
    short_dir = output_dir / "short"
    short_dir.mkdir(exist_ok=True)
    out_path = short_dir / "video.mp4"

    if out_path.exists():
        logger.info(f"Short cache hit → {out_path}")
        return out_path

    # 1. Script
    script_path = short_dir / "script.txt"
    script = _generate_short_script(keyword, script_path)

    # 2. Voice
    voice_path = short_dir / "voice.mp3"
    _generate_voice(script, voice_path)

    # 3. Clips
    clips_dir = short_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    _download_portrait_clips(keyword, clips_dir)

    # 4. Assemble
    _assemble_short(voice_path, clips_dir, out_path)

    logger.info(f"Short assembled → {out_path}")
    return out_path


def _generate_short_script(keyword: str, output_path: Path) -> str:
    if output_path.exists():
        return output_path.read_text(encoding="utf-8")

    prompt = f"""Write a YouTube Shorts script for: "{keyword}"

Rules:
- EXACTLY 100-120 words (58 seconds at 150wpm)
- Hook in first 3 words (make it impossible to scroll past)
- 3 rapid-fire tips or facts, each 1 sentence
- End with a punchy call to action: "Follow for more AI tips"
- No intros, no fluff, no "hey guys"
- Sound like a fast-talking expert giving insider knowledge
- Write narration ONLY — no labels, no headers

Write the script now:"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    output_path.write_text(text, encoding="utf-8")
    return text


def _generate_voice(script: str, output_path: Path) -> None:
    if output_path.exists():
        return
    import tempfile
    from voice import generate_voice
    tmp = Path(tempfile.mktemp(suffix=".txt"))
    tmp.write_text(script, encoding="utf-8")
    try:
        generate_voice(tmp, output_path)
    finally:
        tmp.unlink(missing_ok=True)


def _download_portrait_clips(keyword: str, clips_dir: Path) -> None:
    """Download 4 portrait-oriented clips from Pexels."""
    import requests
    headers = {"Authorization": PEXELS_API_KEY}
    params  = {"query": keyword, "per_page": 4, "orientation": "portrait", "size": "medium"}
    try:
        resp = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params, timeout=15)
        videos = resp.json().get("videos", [])
        for i, vid in enumerate(videos[:4]):
            files = sorted(vid.get("video_files", []), key=lambda f: f.get("width", 0))
            portrait = next((f for f in files if f.get("width", 0) <= 720), files[0] if files else None)
            if not portrait:
                continue
            clip_path = clips_dir / f"clip_{i:02d}.mp4"
            if clip_path.exists():
                continue
            r = requests.get(portrait["link"], stream=True, timeout=60)
            with open(clip_path, "wb") as fh:
                for chunk in r.iter_content(65536):
                    fh.write(chunk)
            logger.info(f"Short clip {i}: {clip_path.name}")
    except Exception as exc:
        logger.warning(f"Portrait clip download failed: {exc}")


def _assemble_short(voice_path: Path, clips_dir: Path, out_path: Path) -> None:
    """Assemble portrait clips + voice into a 1080×1920 Short."""
    clips = sorted(clips_dir.glob("clip_*.mp4"))
    if not clips:
        raise RuntimeError("No portrait clips found for Short assembly")

    # Get voice duration
    probe = subprocess.run(
        [FFPROBE_EXE, "-v", "error",
         "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
         str(voice_path)],
        capture_output=True, text=True,
    )
    duration = min(float(probe.stdout.strip() or "58"), SHORT_MAX_SECONDS)

    # Build concat filter — loop clips to fill duration
    filter_parts = []
    concat_inputs = []
    clip_dur = duration / len(clips)
    for i, clip in enumerate(clips):
        filter_parts.append(
            f"[{i}:v]scale={SHORT_W}:{SHORT_H}:force_original_aspect_ratio=increase,"
            f"crop={SHORT_W}:{SHORT_H},setpts=PTS-STARTPTS[v{i}];"
        )
        concat_inputs.append(f"[v{i}]")

    filtergraph = "".join(filter_parts) + "".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=0[vout]"

    input_args = []
    for clip in clips:
        input_args += ["-stream_loop", "-1", "-t", str(clip_dur), "-i", str(clip)]

    subprocess.run(
        [FFMPEG_EXE, "-y"]
        + input_args
        + ["-i", str(voice_path),
           "-filter_complex", filtergraph,
           "-map", "[vout]", "-map", f"{len(clips)}:a",
           "-c:v", "libx264", "-preset", "fast", "-crf", "22",
           "-c:a", "aac", "-b:a", "192k",
           "-t", str(duration),
           "-movflags", "+faststart",
           str(out_path)],
        check=True,
        capture_output=True,
    )


def upload_short(short_video_path: Path, keyword: str, output_dir: Path) -> str:
    """Upload the Short to YouTube. Returns video ID."""
    from upload import get_youtube_service
    from googleapiclient.http import MediaFileUpload

    sentinel = output_dir / "short" / "uploaded.txt"
    if sentinel.exists():
        return sentinel.read_text(encoding="utf-8").strip()

    yt = get_youtube_service()
    title = f"{keyword[:60]} #Shorts"
    body = {
        "snippet": {
            "title":      title,
            "description": f"Quick tip about {keyword}\n\n#Shorts #AI #Tech",
            "tags":        ["shorts", "ai", "tech", keyword[:30]],
            "categoryId":  "28",
        },
        "status": {
            "privacyStatus":            "public",
            "selfDeclaredMadeForKids":  False,
        },
    }
    media   = MediaFileUpload(str(short_video_path), mimetype="video/mp4", resumable=True, chunksize=256*1024)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()
    video_id = response["id"]

    sentinel.write_text(video_id, encoding="utf-8")
    logger.info(f"Short uploaded → https://youtube.com/shorts/{video_id}")
    return video_id
