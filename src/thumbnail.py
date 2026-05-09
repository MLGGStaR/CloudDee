"""Generate a 1280x720 thumbnail for a video.

Two-step:
  1. Ask Claude for a thumbnail concept (image prompt + overlay text).
  2. Generate the base image with OpenAI gpt-image-1, composite the text
     overlay with PIL.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from anthropic import Anthropic
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Channel, load_prompt
from .utils import log, render_template


CONCEPT_MODEL = "claude-haiku-4-5-20251001"
IMAGE_MODEL = "gpt-image-1"

THUMB_W = 1280
THUMB_H = 720


def make_thumbnail(
    *,
    anthropic_key: str,
    openai_key: str,
    channel: Channel,
    video_title: str,
    summary: str,
    out_path: Path,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    concept = _request_concept(
        anthropic_key,
        channel=channel,
        video_title=video_title,
        summary=summary,
    )

    base_image_path = out_path.with_suffix(".base.png")
    _generate_image(openai_key, concept["image_prompt"], out_path=base_image_path)

    _composite(
        base_image_path,
        out_path=out_path,
        title_text=concept.get("title_text", ""),
        subtitle_text=concept.get("subtitle_text", ""),
        badge_text=concept.get("badge_text", ""),
        accent_color=channel.accent_color,
        draw_circle=bool(concept.get("circle_subject")),
    )
    base_image_path.unlink(missing_ok=True)
    return out_path


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
def _request_concept(api_key: str, *, channel, video_title, summary) -> dict:
    client = Anthropic(api_key=api_key)
    body = render_template(
        load_prompt("thumbnail"),
        video_title=video_title[:200],
        summary=summary[:500],
        channel_name=channel.name,
        accent_color=channel.accent_color,
    )
    msg = client.messages.create(
        model=CONCEPT_MODEL,
        max_tokens=512,
        temperature=0.5,
        system="Return only valid JSON. No prose.",
        messages=[{"role": "user", "content": body}],
    )
    text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=15), reraise=True)
def _generate_image(api_key: str, prompt: str, *, out_path: Path) -> None:
    client = OpenAI(api_key=api_key)
    safe_prompt = (
        f"YouTube thumbnail base, 16:9 cinematic still, photographic, "
        f"strong focal subject, dramatic lighting, no text, no logos. {prompt}"
    )
    res = client.images.generate(
        model=IMAGE_MODEL,
        prompt=safe_prompt,
        size="1536x1024",
        n=1,
    )
    out_path.write_bytes(base64.b64decode(res.data[0].b64_json))


def _composite(
    base_path: Path,
    *,
    out_path: Path,
    title_text: str,
    subtitle_text: str,
    badge_text: str,
    accent_color: str,
    draw_circle: bool = True,
) -> None:
    img = Image.open(base_path).convert("RGB").resize((THUMB_W, THUMB_H), Image.LANCZOS)

    # Optional red highlight circle (rule-of-thirds, upper-right area where
    # the focal subject usually lives in our image_prompt outputs).
    if draw_circle:
        circle_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        cd = ImageDraw.Draw(circle_layer)
        cx, cy = int(THUMB_W * 0.62), int(THUMB_H * 0.42)
        r = 130
        # Thick red ring (no fill).
        cd.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(220, 30, 30, 255), width=10)
        img = Image.alpha_composite(img.convert("RGBA"), circle_layer).convert("RGB")

    # Vignette over the bottom 60% to make text readable.
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    for y in range(int(THUMB_H * 0.4), THUMB_H):
        alpha = int(((y - THUMB_H * 0.4) / (THUMB_H * 0.6)) * 200)
        draw_ov.line([(0, y), (THUMB_W, y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Bigger / bolder than before — 150 pt main, 56 pt subtitle.
    title_font = _load_font(150)
    subtitle_font = _load_font(56)
    badge_font = _load_font(40)

    # ---- Title (bottom-left, max ~2 lines, MASSIVE) ----
    title_top_y = THUMB_H  # default if no title rendered
    if title_text:
        title = title_text.upper()
        wrapped = _wrap_text(title, title_font, max_width=int(THUMB_W * 0.85))[:2]
        text = "\n".join(wrapped)
        x = 50
        title_top_y = THUMB_H - 60 - 160 * len(wrapped)
        # Black drop shadow first
        for dx in range(-6, 7, 2):
            for dy in range(-6, 7, 2):
                draw.multiline_text((x + dx, title_top_y + dy), text,
                                    font=title_font, fill="black", spacing=12)
        # Yellow fill — high-CTR signal color, more attention-grabbing than white
        draw.multiline_text((x, title_top_y), text,
                            font=title_font, fill="#FFEB3B", spacing=12)

    # ---- Subtitle (above title) ----
    if subtitle_text:
        sx, sy = 52, max(20, title_top_y - 70)
        for dx in (-2, 2):
            for dy in (-2, 2):
                draw.text((sx + dx, sy + dy), subtitle_text,
                          font=subtitle_font, fill="black")
        draw.text((sx, sy), subtitle_text, font=subtitle_font, fill="#f5d067")

    # ---- Badge (top-right corner) ----
    if badge_text:
        pad = 18
        bbox = draw.textbbox((0, 0), badge_text.upper(), font=badge_font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        bw, bh = tw + pad * 2, th + pad * 2
        bx, by = THUMB_W - bw - 30, 30
        draw.rectangle([bx, by, bx + bw, by + bh], fill=accent_color)
        draw.text((bx + pad, by + pad - 5), badge_text.upper(), font=badge_font, fill="white")

    img.save(out_path, "PNG", optimize=True)
    log().info("  thumbnail → %s", out_path.name)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Try a couple of common font paths; fall back to PIL default."""
    candidates = [
        Path(__file__).resolve().parent.parent / "assets" / "fonts" / "BebasNeue-Regular.ttf",
        Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Inter-Bold.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("/System/Library/Fonts/HelveticaNeueDeskInterface.ttc"),
    ]
    for c in candidates:
        if c.exists():
            try:
                return ImageFont.truetype(str(c), size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap_text(text: str, font, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines, cur = [], words[0]
    for w in words[1:]:
        trial = cur + " " + w
        bbox = font.getbbox(trial)
        if bbox[2] - bbox[0] <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines
