"""
Generates a 1 280 × 720 JPEG thumbnail:
  1. Extracts a representative frame from the assembled video via FFmpeg.
  2. Applies one of 4 rotating styles based on the hash of the title.
  3. Overlays the video title in large white bold text (wrapped, max 3 lines).
  4. Adds a channel branding label.

Styles:
  0 — NUMBER PUNCH   (yellow accent, giant number on left)
  1 — BOLD HEADLINE  (strong gradient, accent bar, centered title)
  2 — SPLIT SCREEN   (dark left panel 40% / video right 60%)
  3 — ALERT STYLE    (red/orange scheme, "MUST KNOW" badge)

Font fallback chain: Arial Bold → Arial → Segoe UI → PIL default.
"""
import logging
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import FFMPEG_EXE

logger = logging.getLogger(__name__)

THUMB_W = 1_280
THUMB_H = 720
MARGIN = 55
TITLE_SIZE = 72
BRAND_SIZE = 32
FRAME_SEEK = "00:00:05"   # extract frame at this timestamp
BRAND_LABEL = "CODE CREATIVITY BD"

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arialbd.ttf",   # Arial Bold
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",  # Segoe UI Bold
    r"C:\Windows\Fonts\segoeui.ttf",
]


def create_thumbnail(video_path: Path, title: str, output_path: Path) -> None:
    """
    Generate a thumbnail from *video_path* with *title* overlaid.
    Skips generation if *output_path* already exists.
    """
    if output_path.exists():
        logger.info(f"Thumbnail cache hit → {output_path}")
        return

    frame_path = output_path.parent / "_tmp_frame.jpg"
    try:
        _extract_frame(video_path, frame_path)
        img = _build_thumbnail(frame_path, title)
        img.save(str(output_path), "JPEG", quality=95, optimize=True)
        logger.info(f"Thumbnail saved → {output_path}")
    finally:
        frame_path.unlink(missing_ok=True)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _extract_frame(video_path: Path, dest: Path) -> None:
    subprocess.run(
        [
            FFMPEG_EXE, "-y",
            "-ss", FRAME_SEEK,
            "-i", str(video_path),
            "-vframes", "1",
            "-vf",
            (
                f"scale={THUMB_W}:{THUMB_H}:"
                "force_original_aspect_ratio=increase,"
                f"crop={THUMB_W}:{THUMB_H}"
            ),
            str(dest),
        ],
        capture_output=True,
        check=True,
    )


def _style_index(title: str) -> int:
    """Return a style index 0-3 based on a hash of the title."""
    return hash(title) % 4


def _build_thumbnail(frame_path: Path, title: str) -> Image.Image:
    style = _style_index(title)
    img = Image.open(frame_path).convert("RGB").resize((THUMB_W, THUMB_H), Image.LANCZOS)

    if style == 0:
        return _style_number_punch(img, title)
    elif style == 1:
        return _style_bold_headline(img, title)
    elif style == 2:
        return _style_split_screen(img, title)
    else:
        return _style_alert(img, title)


def _gradient_overlay(img: Image.Image) -> Image.Image:
    """Dark gradient rising from the bottom two-thirds of the image."""
    overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    start_y = THUMB_H // 3  # gradient begins at 1/3 from the top
    for y in range(THUMB_H):
        if y < start_y:
            alpha = 0
        else:
            progress = (y - start_y) / (THUMB_H - start_y)
            alpha = int(200 * (progress ** 0.8))
        draw.line([(0, y), (THUMB_W, y)], fill=(0, 0, 0, alpha))
    result = Image.alpha_composite(img.convert("RGBA"), overlay)
    return result.convert("RGB")


def _draw_text_outlined(
    draw: ImageDraw.ImageDraw,
    pos: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple,
    outline: tuple = (0, 0, 0),
    outline_width: int = 3,
) -> None:
    """Draw text with a solid outline for readability."""
    x, y = pos
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if dx != 0 or dy != 0:
                draw.text((x + dx, y + dy), text, font=font, fill=outline)
    draw.text((x, y), text, font=font, fill=fill)


# ── Style 0: NUMBER PUNCH ──────────────────────────────────────────────────────

def _style_number_punch(img: Image.Image, title: str) -> Image.Image:
    """Bright yellow accent with big number on left, title text on right."""
    img = _gradient_overlay(img)
    draw = ImageDraw.Draw(img)

    # Extract first number from title, fall back to "NEW"
    match = re.search(r'\b(\d+)\b', title)
    number_str = match.group(1) if match else "NEW"

    font_number = _font(200)
    font_title = _font(TITLE_SIZE)
    font_brand = _font(BRAND_SIZE)

    # Left side: giant yellow number
    num_w = draw.textbbox((0, 0), number_str, font=font_number)[2]
    num_x = MARGIN
    num_y = THUMB_H // 2 - 110
    _draw_text_outlined(draw, (num_x, num_y), number_str, font_number,
                        fill=(255, 220, 0), outline=(0, 0, 0), outline_width=6)

    # Vertical divider line
    divider_x = num_x + num_w + 30
    draw.line([(divider_x, MARGIN), (divider_x, THUMB_H - MARGIN)],
              fill=(255, 220, 0, 180), width=4)

    # Right side: title text
    right_margin = divider_x + 30
    max_w = THUMB_W - right_margin - MARGIN
    lines = _wrap(draw, title, font_title, max_w)[:3]
    line_h = TITLE_SIZE + 14
    total_h = line_h * len(lines)
    y = THUMB_H // 2 - total_h // 2
    for line in lines:
        _draw_text_outlined(draw, (right_margin, y), line, font_title,
                            fill=(255, 255, 255), outline=(0, 0, 0), outline_width=3)
        y += line_h

    # Brand label top-right
    brand_w = draw.textbbox((0, 0), BRAND_LABEL, font=font_brand)[2]
    _draw_text_outlined(draw, (THUMB_W - brand_w - MARGIN, MARGIN), BRAND_LABEL,
                        font_brand, fill=(255, 220, 0), outline=(0, 0, 0), outline_width=2)

    return img


# ── Style 1: BOLD HEADLINE ─────────────────────────────────────────────────────

def _style_bold_headline(img: Image.Image, title: str) -> Image.Image:
    """Stronger dark gradient, accent bar at top, title centered in bottom 40%."""
    # Stronger gradient (80% opacity at bottom)
    overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    start_y = THUMB_H // 4
    for y in range(THUMB_H):
        if y < start_y:
            alpha = 0
        else:
            progress = (y - start_y) / (THUMB_H - start_y)
            alpha = int(204 * (progress ** 0.7))  # ~80% at bottom
        draw_ov.line([(0, y), (THUMB_W, y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    font_title = _font(TITLE_SIZE)
    font_brand = _font(BRAND_SIZE)

    # Accent bar at top (gradient yellow→orange)
    for x in range(THUMB_W):
        t = x / THUMB_W
        r = int(255 * (1 - t) + 255 * t)
        g = int(220 * (1 - t) + 140 * t)
        b = int(0 * (1 - t) + 0 * t)
        draw.line([(x, 0), (x, 4)], fill=(r, g, b))

    # Title centered in bottom 40%
    bottom_zone_y = int(THUMB_H * 0.60)
    lines = _wrap(draw, title, font_title, THUMB_W - 2 * MARGIN)[:3]
    line_h = TITLE_SIZE + 14
    total_h = line_h * len(lines)
    y = bottom_zone_y + (THUMB_H - bottom_zone_y - total_h) // 2
    for line in lines:
        text_w = draw.textbbox((0, 0), line, font=font_title)[2]
        x = (THUMB_W - text_w) // 2
        _draw_text_outlined(draw, (x, y), line, font_title,
                            fill=(255, 255, 255), outline=(0, 0, 0), outline_width=3)
        y += line_h

    # "AI & TECH" brand label with yellow background pill
    brand_text = BRAND_LABEL
    brand_w = draw.textbbox((0, 0), brand_text, font=font_brand)[2]
    brand_h = BRAND_SIZE + 10
    pill_x, pill_y = MARGIN, MARGIN
    draw.rounded_rectangle(
        [pill_x - 8, pill_y - 4, pill_x + brand_w + 8, pill_y + brand_h],
        radius=8, fill=(255, 220, 0)
    )
    draw.text((pill_x, pill_y), brand_text, font=font_brand, fill=(0, 0, 0))

    return img


# ── Style 2: SPLIT SCREEN ─────────────────────────────────────────────────────

def _style_split_screen(img: Image.Image, title: str) -> Image.Image:
    """Dark left panel 40%, video frame right 60%."""
    result = img.copy()
    draw = ImageDraw.Draw(result)

    font_title = _font(TITLE_SIZE)
    font_brand = _font(BRAND_SIZE)

    # Left panel: dark gradient (dark blue to black)
    panel_w = int(THUMB_W * 0.42)
    left_panel = Image.new("RGBA", (panel_w, THUMB_H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(left_panel)
    for x in range(panel_w):
        t = x / panel_w
        # dark blue at left → black at right
        r = int(8 * (1 - t))
        g = int(12 * (1 - t))
        b = int(35 * (1 - t))
        alpha = int(230 * (1 - t * 0.3))
        ld.line([(x, 0), (x, THUMB_H)], fill=(r, g, b, alpha))

    result = Image.alpha_composite(result.convert("RGBA"), left_panel.resize((panel_w, THUMB_H)))
    result = result.convert("RGB")
    draw = ImageDraw.Draw(result)

    # Title text in left panel
    max_w = panel_w - 2 * MARGIN
    lines = _wrap(draw, title, font_title, max_w)[:3]
    line_h = TITLE_SIZE + 14
    total_h = line_h * len(lines)
    y = THUMB_H // 2 - total_h // 2
    for line in lines:
        _draw_text_outlined(draw, (MARGIN, y), line, font_title,
                            fill=(255, 255, 255), outline=(0, 0, 0), outline_width=3)
        y += line_h

    # Brand label bottom-left
    brand_y = THUMB_H - MARGIN - BRAND_SIZE - 8
    _draw_text_outlined(draw, (MARGIN, brand_y), BRAND_LABEL, font_brand,
                        fill=(255, 220, 0), outline=(0, 0, 0), outline_width=2)

    # Vertical accent line between panels
    draw.line([(panel_w, 0), (panel_w, THUMB_H)], fill=(255, 220, 0), width=3)

    return result


# ── Style 3: ALERT STYLE ──────────────────────────────────────────────────────

def _style_alert(img: Image.Image, title: str) -> Image.Image:
    """Red/orange color scheme with alert badge."""
    # Red gradient overlay on bottom 50%
    overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    start_y = THUMB_H // 2
    for y in range(THUMB_H):
        if y < start_y:
            alpha = 0
        else:
            progress = (y - start_y) / (THUMB_H - start_y)
            alpha = int(200 * (progress ** 0.75))
        draw_ov.line([(0, y), (THUMB_W, y)], fill=(180, 20, 20, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    font_title = _font(TITLE_SIZE)
    font_brand = _font(BRAND_SIZE)
    font_badge = _font(28)

    # Title in white with red shadow
    lines = _wrap(draw, title, font_title, THUMB_W - 2 * MARGIN)[:3]
    line_h = TITLE_SIZE + 14
    total_h = line_h * len(lines)
    y = THUMB_H - MARGIN - total_h
    for line in lines:
        # Red shadow
        _draw_text_outlined(draw, (MARGIN + 4, y + 4), line, font_title,
                            fill=(200, 0, 0), outline=(0, 0, 0), outline_width=1)
        # White text
        _draw_text_outlined(draw, (MARGIN, y), line, font_title,
                            fill=(255, 255, 255), outline=(0, 0, 0), outline_width=3)
        y += line_h

    # "MUST KNOW" badge in top-right corner
    badge_text = "MUST KNOW"
    bw = draw.textbbox((0, 0), badge_text, font=font_badge)[2]
    bh = 28 + 14
    bx = THUMB_W - bw - MARGIN - 16
    by = MARGIN
    draw.rounded_rectangle([bx - 8, by - 4, bx + bw + 8, by + bh],
                            radius=6, fill=(220, 30, 30))
    draw.text((bx, by), badge_text, font=font_badge, fill=(255, 255, 255))

    # Brand label top-left
    _draw_text_outlined(draw, (MARGIN, MARGIN), BRAND_LABEL, font_brand,
                        fill=(255, 180, 50), outline=(0, 0, 0), outline_width=2)

    return img


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_w: int,
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        w = draw.textbbox((0, 0), candidate, font=font)[2]
        if w <= max_w:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
