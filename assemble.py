"""
Assembles the final 1 920 × 1 080 video:
  1. Scale & pad every clip to 1 920 × 1 080 @ 30 fps (letterbox/pillar-box).
  2. Loop the clip sequence until it covers the full audio duration.
  3. Transcribe voice.mp3 with Whisper (tiny model) → SRT captions.
  4. Render: concat video + audio + burned-in captions, trimmed to audio length.

Windows note: FFmpeg's subtitles filter requires a specially escaped path.
The helper _srt_filter_path() handles the C:/… drive-letter escaping.
"""
import json
import logging
import subprocess
from pathlib import Path

from config import FFMPEG_EXE, FFPROBE_EXE, VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH

logger = logging.getLogger(__name__)

_CAPTION_STYLE = (
    "FontSize=22,"
    "PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,"
    "BackColour=&H80000000,"
    "Bold=1,Outline=2,Shadow=1,"
    "Alignment=2"   # centred at bottom
)


# ── Public entry point ─────────────────────────────────────────────────────────

def assemble_video(
    clips: list[Path],
    audio_path: Path,
    output_path: Path,
    work_dir: Path,
) -> None:
    """
    Full assembly pipeline.  Skips if *output_path* already exists.
    *work_dir* stores intermediate files (scaled clips, concat list, SRT).
    """
    if output_path.exists():
        logger.info(f"Video cache hit → {output_path}")
        return

    if not clips:
        raise ValueError("No clips available for assembly.")

    work_dir.mkdir(parents=True, exist_ok=True)

    audio_dur = _probe_duration(audio_path)
    logger.info(f"Audio duration: {audio_dur:.1f} s")

    # Step 1 – scale clips
    scaled_dir = work_dir / "scaled"
    scaled_dir.mkdir(exist_ok=True)
    scaled = _scale_clips(clips, scaled_dir)

    # Step 2 – build looping concat list
    concat_list = work_dir / "concat.txt"
    _write_concat_list(scaled, concat_list, audio_dur)

    # Step 3 – whisper captions
    srt_path = work_dir / "captions.srt"
    if not srt_path.exists():
        _generate_captions(audio_path, srt_path)

    # Step 4 – final render
    _render(concat_list, audio_path, srt_path, output_path, audio_dur)
    logger.info(f"Final video → {output_path}")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _probe_duration(path: Path) -> float:
    """Return duration of a media file in seconds via ffprobe."""
    r = subprocess.run(
        [
            FFPROBE_EXE, "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(r.stdout)["format"]["duration"])


def _scale_clips(clips: list[Path], out_dir: Path) -> list[Path]:
    """Scale and pad each clip to VIDEO_WIDTH × VIDEO_HEIGHT @ VIDEO_FPS."""
    scaled: list[Path] = []
    for i, clip in enumerate(clips):
        dest = out_dir / f"s{i:02d}.mp4"
        if not dest.exists():
            logger.info(f"  Scaling {clip.name}…")
            _ffmpeg(
                "-i", str(clip),
                "-vf",
                (
                    f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:"
                    "force_original_aspect_ratio=decrease,"
                    f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
                    f"setsar=1,fps={VIDEO_FPS}"
                ),
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-an",           # strip original audio from clips
                str(dest),
            )
        scaled.append(dest)
    return scaled


def _write_concat_list(clips: list[Path], list_file: Path, target_dur: float) -> None:
    """
    Write an FFmpeg concat demuxer file that loops *clips* until the total
    duration exceeds *target_dur*.
    """
    clip_durs: list[float] = []
    for c in clips:
        try:
            clip_durs.append(_probe_duration(c))
        except Exception:
            clip_durs.append(30.0)  # safe fallback if a clip is unreadable

    total = sum(clip_durs) or 1.0
    repeats = max(1, int(target_dur / total) + 2)

    with open(list_file, "w", encoding="utf-8") as f:
        for _ in range(repeats):
            for clip in clips:
                # FFmpeg concat file requires absolute paths with forward slashes
                abs_fwd = str(clip.resolve()).replace("\\", "/")
                f.write(f"file '{abs_fwd}'\n")


def _generate_captions(audio_path: Path, srt_path: Path) -> None:
    """Transcribe *audio_path* with Whisper (tiny model) and write SRT."""
    logger.info("Transcribing with Whisper tiny model (CPU) — this may take a few minutes…")
    import whisper  # lazy import so the package is optional at import time

    model = whisper.load_model("tiny")
    result = model.transcribe(str(audio_path), fp16=False, language="en")
    srt_text = _segments_to_srt(result["segments"])
    srt_path.write_text(srt_text, encoding="utf-8")
    logger.info(f"Captions saved → {srt_path}  ({len(result['segments'])} segments)")


def _segments_to_srt(segments: list[dict]) -> str:
    parts: list[str] = []
    for i, seg in enumerate(segments, 1):
        start = _ts(seg["start"])
        end = _ts(seg["end"])
        text = seg["text"].strip()
        parts.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(parts)


def _ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _srt_filter_path(srt_path: Path) -> str:
    """
    Convert an absolute Windows path to the format FFmpeg's subtitles filter
    expects.  Backslashes become forward slashes; the drive-letter colon is
    escaped as \\:.

    Example:  C:\\Users\\foo\\caps.srt  →  C\\:/Users/foo/caps.srt
    """
    p = str(srt_path.resolve()).replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        p = p[0] + "\\:" + p[2:]
    return p


def _render(
    concat_list: Path,
    audio_path: Path,
    srt_path: Path,
    output_path: Path,
    duration: float,
) -> None:
    logger.info("Rendering final video (this takes several minutes)…")
    escaped = _srt_filter_path(srt_path)
    _ffmpeg(
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-i", str(audio_path),
        "-vf", f"subtitles='{escaped}':force_style='{_CAPTION_STYLE}'",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "256k",
        "-t", f"{duration:.3f}",
        "-movflags", "+faststart",
        str(output_path),
    )


def _ffmpeg(*args: str) -> None:
    """Run FFmpeg with the given arguments; raises RuntimeError on failure."""
    cmd = [FFMPEG_EXE, "-y"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = result.stderr[-3_000:]
        raise RuntimeError(f"FFmpeg failed (exit {result.returncode}):\n{tail}")
