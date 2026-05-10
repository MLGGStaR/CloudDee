"""Generate 9:16 vertical YouTube Shorts.

Two entry points:

  make_short(...)             — recap-style Short paired with a long-form video
                                (uses the long-form's narration + visual pool).

  make_standalone_short(...)  — Short generated directly from a public record,
                                with no long-form parent.

Both share `_build_short_video()` which handles audio voicing, Whisper
transcription, vertical body assembly, outro card, and master loudness.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from anthropic import Anthropic
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Channel, Settings, load_prompt
from .db import Record
from .images import fetch_visual
from .render import SceneAsset
from .transcribe import parse_srt_blocks, reflow_srt_max_words, transcribe_to_srt
from .utils import ffprobe_duration, log, render_template, require_ffmpeg, truncate


VERTICAL_W = 1080
VERTICAL_H = 1920
PANEL_TARGET_SEC = 8.0
RECAP_MODEL = "claude-sonnet-4-6"
TTS_MODEL = "tts-1-hd"
OUTRO_DURATION = 1.0          # one quick "Subscribe to <brand>" flash, no lingering
STANDALONE_PANEL_COUNT = 5    # ~11s per panel for a 55s short — still active
BODY_MAX_DURATION = 58.0      # cap recap body audio so we don't truncate mid-word
MAX_SHORT_DURATION = 59.5     # YouTube classifies <=60s vertical as a Short

# Where the fonts-dejavu Ubuntu package installs DejaVu Sans Bold. Pinned
# absolute path because drawtext requires fontfile=, not fontname=.
CAPTION_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


# =============================================================================
# Public entry points
# =============================================================================

def make_short(
    *,
    anthropic_key: str,
    openai_key: str,
    scenes: list[SceneAsset],
    long_form_title: str,
    long_form_narration: str,
    channel: Channel,
    out_path: Path,
    voice_override: str | None = None,
) -> Path:
    """Recap Short paired with a long-form video. Visuals come from the
    long-form's panel pool (cycling for variety). `voice_override` lets
    the caller force a specific TTS voice (e.g. to match the parent
    long-form's voice rotation pick)."""
    require_ffmpeg()
    if not scenes:
        raise RuntimeError("no scenes provided to make_short")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    brand = channel.brand_name or channel.name

    recap_text = _write_recap(
        anthropic_key,
        long_form_title=long_form_title,
        full_narration=long_form_narration,
        brand_name=brand,
    )
    log().info("  short recap: %d words", len(recap_text.split()))

    visual_pool: list[Path] = []
    for sa in scenes:
        visual_pool.extend(sa.visuals)
    if not visual_pool:
        raise RuntimeError("no visuals in long-form pool")

    return _build_short_video(
        narration=recap_text,
        visual_pool=visual_pool,
        channel=channel,
        openai_key=openai_key,
        voice_override=voice_override,
        out_path=out_path,
    )


def make_standalone_short(
    *,
    settings: Settings,
    record: Record,
    channel: Channel,
    record_context: str,
    work_dir: Path,
    out_path: Path,
    voice_override: str | None = None,
) -> Path:
    """Short generated directly from a record. Fetches its own visuals."""
    require_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    brand = channel.brand_name or channel.name

    # 1) Generate narration from the record itself (no long-form to recap)
    narration = _write_standalone_short_script(
        settings.anthropic_api_key,
        record=record,
        brand_name=brand,
    )
    if narration is None:
        raise RuntimeError("standalone-short writer refused this record")
    log().info("  standalone short: %d words / record %s",
               len(narration.split()), record.id)

    # 2) Fetch a vertical-friendly visual pool from Pexels (+ AI fallback)
    visuals_dir = work_dir / "visuals"
    visual_pool: list[Path] = []
    queries = _short_visual_queries(record, record_context)
    for q in queries[:STANDALONE_PANEL_COUNT * 2]:
        v = fetch_visual(
            settings,
            b_roll_prompt=q,
            out_dir=visuals_dir,
            prefer_video=False,
            allow_ai=True,
        )
        if v is None:
            continue
        if str(v) in {str(p) for p in visual_pool}:
            continue
        visual_pool.append(v)
        if len(visual_pool) >= STANDALONE_PANEL_COUNT:
            break
    if not visual_pool:
        raise RuntimeError(f"no visuals could be sourced for record {record.id}")

    return _build_short_video(
        narration=narration,
        visual_pool=visual_pool,
        channel=channel,
        openai_key=settings.openai_api_key,
        voice_override=voice_override,
        out_path=out_path,
    )


# =============================================================================
# Shared body — voice + transcribe + render
# =============================================================================

def _build_short_video(
    *,
    narration: str,
    visual_pool: list[Path],
    channel: Channel,
    openai_key: str,
    out_path: Path,
    voice_override: str | None = None,
) -> Path:
    """Voice the narration, transcribe for captions, render the vertical
    timeline + outro, master loudness."""
    brand = channel.brand_name or channel.name
    voice = voice_override or channel.voice
    # Last-resort word cap. The prompt asks for ≤125 words but if the
    # model overruns, hard-truncate at a sentence boundary so the audio
    # stays inside the budget and the outro flash card still plays.
    narration = _enforce_word_cap(narration, max_words=130)
    with tempfile.TemporaryDirectory(prefix="docket_short_") as td:
        td_path = Path(td)
        recap_audio = td_path / "recap.wav"
        _synthesize(
            openai_key,
            text=narration,
            voice=voice,
            speed=channel.voice_speed,
            out_path=recap_audio,
        )
        recap_duration = ffprobe_duration(recap_audio)
        log().info("  short narration: %d words → %.1fs audio (budget %.1fs)",
                   len(narration.split()), recap_duration, BODY_MAX_DURATION)

        srt_text = ""
        try:
            raw_srt = transcribe_to_srt(openai_key, recap_audio)
            log().info("  short whisper raw: %d chars, head=%r",
                       len(raw_srt or ""), (raw_srt or "")[:160])
            reflowed = reflow_srt_max_words(raw_srt, max_words=4)
            srt_text = reflowed if reflowed.strip() else raw_srt
            if not srt_text.strip():
                log().warning("  short: whisper returned empty SRT — captions will be missing")
        except Exception as e:
            log().warning("  short transcribe failed (no captions): %s", e)

        # Pick panels evenly spaced through the visual pool
        n_panels = max(4, int(round(recap_duration / PANEL_TARGET_SEC)))
        if len(visual_pool) < n_panels:
            visual_pool = (visual_pool * ((n_panels // len(visual_pool)) + 1))[:n_panels]
        else:
            step = len(visual_pool) / n_panels
            visual_pool = [visual_pool[int(i * step)] for i in range(n_panels)]

        body_path = td_path / "body.mp4"
        _render_vertical_body(
            visuals=visual_pool,
            audio=recap_audio,
            srt_text=srt_text,
            out_path=body_path,
        )
        log().info("  short body=%.1fs (audio=%.1fs)",
                   ffprobe_duration(body_path), recap_duration)

        outro_path = td_path / "outro.mp4"
        _render_outro(
            brand=brand,
            accent_color=channel.accent_color,
            out_path=outro_path,
        )
        log().info("  short outro=%.1fs (target=%.1fs)",
                   ffprobe_duration(outro_path), OUTRO_DURATION)

        concat_path = td_path / "concat.mp4"
        _concat([body_path, outro_path], out_path=concat_path)
        log().info("  short concat=%.1fs", ffprobe_duration(concat_path))
        _finalize_loudness(concat_path, out_path=out_path)

    log().info("  short → %s (%.1fs final)",
               out_path, ffprobe_duration(out_path))
    return out_path


# =============================================================================
# Script generation
# =============================================================================

@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=15), reraise=True)
def _write_recap(api_key: str, *, long_form_title: str, full_narration: str, brand_name: str) -> str:
    body = render_template(
        load_prompt("shorts_recap"),
        video_title=long_form_title[:200],
        full_narration=full_narration[:20_000],
        brand_name=brand_name,
    )
    client = Anthropic(api_key=api_key, max_retries=2, timeout=30.0)
    msg = client.messages.create(
        model=RECAP_MODEL,
        max_tokens=600,
        temperature=0.7,
        system="You return only valid JSON objects. No prose. No markdown fences.",
        messages=[{"role": "user", "content": body}],
    )
    text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lower().startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    narration = (data.get("narration") or "").strip()
    if not narration:
        raise RuntimeError("recap returned empty narration")
    return narration


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=15), reraise=True)
def _write_standalone_short_script(
    api_key: str,
    *,
    record: Record,
    brand_name: str,
) -> str | None:
    body = render_template(
        load_prompt("short_standalone"),
        source=record.source,
        title=truncate(record.title, 250),
        url=record.url,
        published_at=record.published_at,
        text=truncate(record.raw_text, 20_000),
        brand_name=brand_name,
    )
    client = Anthropic(api_key=api_key, max_retries=2, timeout=30.0)
    msg = client.messages.create(
        model=RECAP_MODEL,
        max_tokens=800,
        temperature=0.7,
        system="You return only valid JSON objects. No prose. No markdown fences.",
        messages=[{"role": "user", "content": body}],
    )
    text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lower().startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    if data.get("refuse"):
        log().info("standalone short writer refused: %s", data.get("reason"))
        return None
    narration = (data.get("narration") or "").strip()
    if not narration:
        raise RuntimeError("standalone short returned empty narration")
    return narration


def _short_visual_queries(record: Record, record_context: str) -> list[str]:
    """Generate a sequence of search queries for the short's visuals.
    Mixes specific (aircraft model + crash) and generic (location + investigation).
    """
    title = (record.title or "").strip()
    queries: list[str] = []
    if record_context:
        queries.append(f"{record_context} aircraft")
        queries.append(f"{record_context} cockpit")
    if title:
        queries.append(title[:80])
    # Generic per-source fallbacks
    if record.source.startswith("ntsb_aviation"):
        queries.extend(["plane crash investigation", "small aircraft runway",
                        "aviation cockpit", "FAA inspector wreckage", "airfield night"])
    elif record.source.startswith("ntsb_marine"):
        queries.extend(["maritime accident", "coast guard rescue",
                        "ship wreck", "harbor at night"])
    elif record.source == "sec":
        queries.extend(["wall street stock chart", "office building skyline",
                        "court documents desk", "executive courtroom"])
    elif record.source in ("courtlistener", "doj"):
        queries.extend(["federal courthouse exterior", "judge gavel courtroom",
                        "lawyer briefcase", "FBI badge agent", "evidence box files"])
    else:
        queries.extend(["government documents", "investigator file", "federal seal"])
    return queries


# =============================================================================
# Voice
# =============================================================================

@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=10), reraise=True)
def _synthesize(api_key: str, *, text: str, voice: str, speed: float, out_path: Path) -> None:
    client = OpenAI(api_key=api_key)
    with client.audio.speech.with_streaming_response.create(
        model=TTS_MODEL,
        voice=voice,
        input=text[:3900],
        response_format="wav",
        speed=speed,
    ) as response:
        response.stream_to_file(out_path)


# =============================================================================
# Vertical body / outro / concat / loudness
# =============================================================================

def _enforce_word_cap(text: str, *, max_words: int) -> str:
    """If `text` exceeds `max_words`, cut at the last sentence boundary
    inside the cap. Ensures the audio fits the shorts budget even when
    the LLM ignores the prompt's word limit."""
    words = text.split()
    if len(words) <= max_words:
        return text
    truncated = " ".join(words[:max_words])
    # Prefer ending on a sentence terminator.
    for stop in (".", "!", "?"):
        idx = truncated.rfind(stop)
        if idx >= len(truncated) * 0.6:    # only honor if reasonably close to the end
            return truncated[: idx + 1]
    return truncated + "."


def _build_caption_drawtext_chain(srt_text: str, td_path: Path) -> str | None:
    """Convert an SRT into a chain of drawtext filters — one per caption
    block, timed via `enable='between(t,start,end)'`. Each block's text is
    written to its own .txt file so we sidestep all of drawtext's escape
    rules (single quotes, colons, commas, percent signs).

    Returns the filter chain string ending with [v], or None if there is
    nothing to draw. Replaces the old `subtitles=` (libass) approach which
    was silently rendering nothing for shorts.
    """
    if not srt_text or not srt_text.strip():
        return None
    blocks = parse_srt_blocks(srt_text)
    if not blocks:
        return None
    if not Path(CAPTION_FONT_PATH).exists():
        log().warning("  short captions: font %s not found, skipping",
                      CAPTION_FONT_PATH)
        return None

    parts: list[str] = []
    for i, b in enumerate(blocks):
        in_label = "0:v" if i == 0 else f"vc{i - 1}"
        out_label = "v" if i == len(blocks) - 1 else f"vc{i}"
        txt_path = td_path / f"cap_{i:03d}.txt"
        txt_path.write_text(b["text"], encoding="utf-8")
        # Bottom-third placement, large bold caps with a thick black outline.
        # x centered, y at 65% of frame height — clear of the YouTube Shorts
        # bottom UI overlay (title, channel, like/comment, progress bar).
        drawtext = (
            f"drawtext="
            f"fontfile={CAPTION_FONT_PATH}:"
            f"textfile={txt_path}:"
            f"fontsize=64:fontcolor=white:"
            f"borderw=5:bordercolor=black:"
            f"x=(w-text_w)/2:y=h*0.65:"
            f"enable='between(t\\,{b['start']:.3f}\\,{b['end']:.3f})'"
        )
        parts.append(f"[{in_label}]{drawtext}[{out_label}]")
    chain = ";".join(parts)
    log().info("  short captions: %d drawtext segments", len(blocks))
    return chain


def _render_vertical_body(
    *,
    visuals: list[Path],
    audio: Path,
    srt_text: str,
    out_path: Path,
) -> None:
    audio_duration = ffprobe_duration(audio)
    # Cap body to BODY_MAX_DURATION. This is the last line of defense
    # against TTS overrun: even if the narration runs 65s, the body is
    # clipped at 58s and the 1s outro is guaranteed to play before the
    # MAX_SHORT_DURATION ceiling. The prompt + _enforce_word_cap upstream
    # should keep this from firing in practice.
    duration = min(audio_duration, BODY_MAX_DURATION)
    if audio_duration > BODY_MAX_DURATION:
        log().warning("  short body: audio %.1fs > %.1fs cap — clipping",
                      audio_duration, BODY_MAX_DURATION)
    n = len(visuals)
    panel_duration = duration / n

    with tempfile.TemporaryDirectory(prefix="docket_short_panels_") as td:
        td_path = Path(td)
        panels: list[Path] = []
        for i, vis in enumerate(visuals):
            p = td_path / f"v_{i:03d}.mp4"
            _render_vertical_panel(vis, duration=panel_duration, panel_index=i, out_path=p)
            panels.append(p)

        joined = td_path / "joined.mp4"
        _concat(panels, out_path=joined)

        caption_chain = _build_caption_drawtext_chain(srt_text, td_path)
        if caption_chain:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(joined),
                "-i", str(audio),
                "-filter_complex", caption_chain,
                "-map", "[v]", "-map", "1:a",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{duration:.3f}",
                str(out_path),
            ]
        else:
            log().warning("  short: no captions → plain body")
            cmd = [
                "ffmpeg", "-y",
                "-i", str(joined),
                "-i", str(audio),
                "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{duration:.3f}",
                str(out_path),
            ]
        _run(cmd)


def _render_vertical_panel(visual: Path, *, duration: float, panel_index: int, out_path: Path) -> None:
    is_video = visual.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}
    motion = panel_index % 3

    if is_video:
        v_input = ["-stream_loop", "-1", "-i", str(visual)]
        v_filter = (
            f"[0:v]scale=-2:{VERTICAL_H}:flags=lanczos,"
            f"crop={VERTICAL_W}:{VERTICAL_H},fps=30,format=yuv420p,setsar=1[v]"
        )
    else:
        frames = max(int(duration * 30), 60)
        if motion == 0:
            zoom, x, y = "min(zoom+0.0014,1.15)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
        elif motion == 1:
            zoom, x, y = "min(zoom+0.0014,1.15)", "0", "0"
        else:
            zoom, x, y = "min(zoom+0.0014,1.15)", "iw/zoom-iw/zoom", "ih/zoom-ih/zoom"
        v_input = ["-loop", "1", "-i", str(visual)]
        v_filter = (
            f"[0:v]scale=1500:-1:flags=lanczos,"
            f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:s={VERTICAL_W}x{VERTICAL_H}:fps=30,"
            f"format=yuv420p,setsar=1[v]"
        )

    cmd = [
        "ffmpeg", "-y",
        *v_input,
        "-filter_complex", v_filter,
        "-map", "[v]",
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-t", f"{duration:.3f}",
        str(out_path),
    ]
    _run(cmd)


def _ffmpeg_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
            .replace("'", "")
            .replace(":", "\\:")
            .replace("%", "\\%")
            .replace(",", "\\,")
    )


def _render_outro(*, brand: str, accent_color: str, out_path: Path) -> None:
    """Render a polished 1-second outro: PIL paints a single frame with a
    gradient background, kerned brand wordmark, and a red YouTube-style
    rounded SUBSCRIBE pill — then ffmpeg displays it with a subtle
    scale-in animation. Previously this was raw ffmpeg drawtext on a
    flat color block, which looked low-effort and AI-default."""
    with tempfile.TemporaryDirectory(prefix="docket_outro_") as td:
        td_path = Path(td)
        frame_path = td_path / "outro_frame.png"
        _paint_outro_frame(brand=brand, accent_color=accent_color, out_path=frame_path)

        # Animate: subtle scale-up from 1.0 to 1.06 over the duration —
        # gives the still frame a touch of life without screaming "animated".
        frames = max(int(OUTRO_DURATION * 30), 12)
        zoom_expr = f"1+0.06*on/{frames}"
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(frame_path),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex",
            (
                f"[0:v]scale={int(VERTICAL_W*1.1)}:-1:flags=lanczos,"
                f"zoompan=z='{zoom_expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d={frames}:s={VERTICAL_W}x{VERTICAL_H}:fps=30,"
                f"format=yuv420p,setsar=1[v]"
            ),
            "-map", "[v]", "-map", "1:a",
            "-shortest",
            "-t", f"{OUTRO_DURATION}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "128k",
            str(out_path),
        ]
        _run(cmd)


def _paint_outro_frame(*, brand: str, accent_color: str, out_path: Path) -> None:
    """PIL-paint a 1080x1920 outro frame with a documentary "case file"
    aesthetic. Layers:
      1. Vertical accent→dark gradient background
      2. Gold frame bars top + bottom
      3. Decorative line/diamond ornament flanking "SUBSCRIBE TO"
      4. Brand wordmark in big bold gold + drop shadow
      5. Gold underline + tagline below the brand
      6. Bell icon (gold) + red rounded SUBSCRIBE pill with drop shadow
      7. Subtle corner accent marks
    """
    from PIL import Image, ImageDraw
    from .thumbnail import _load_font

    GOLD = (245, 208, 103)
    WHITE = (255, 255, 255)
    SHADOW = (0, 0, 0)
    RED = (204, 0, 0)

    accent_hex = (accent_color or "#0c2d48").lstrip("#") or "0c2d48"
    accent_rgb = tuple(int(accent_hex[i:i + 2], 16) for i in (0, 2, 4))

    img = Image.new("RGB", (VERTICAL_W, VERTICAL_H), (10, 10, 18))
    draw = ImageDraw.Draw(img)

    # 1. Vertical gradient: accent in middle, deep-dark at top/bottom
    mid_y = VERTICAL_H / 2
    edge_rgb = (10, 10, 20)
    for y in range(VERTICAL_H):
        t = (abs(y - mid_y) / mid_y) ** 1.4
        r = int(accent_rgb[0] * (1 - t) + edge_rgb[0] * t)
        g = int(accent_rgb[1] * (1 - t) + edge_rgb[1] * t)
        b = int(accent_rgb[2] * (1 - t) + edge_rgb[2] * t)
        draw.line([(0, y), (VERTICAL_W, y)], fill=(r, g, b))

    # 2. Gold frame bars top and bottom — "case file" header/footer
    bar_h = 10
    draw.rectangle([0, 0, VERTICAL_W, bar_h], fill=GOLD)
    draw.rectangle([0, VERTICAL_H - bar_h, VERTICAL_W, VERTICAL_H], fill=GOLD)
    # Thin secondary stripes parallel to the bars for typography depth
    draw.rectangle([0, bar_h + 8, VERTICAL_W, bar_h + 10], fill=GOLD)
    draw.rectangle([0, VERTICAL_H - bar_h - 10, VERTICAL_W, VERTICAL_H - bar_h - 8], fill=GOLD)

    # 3. Corner marks (asymmetric crops in each corner, evoke a stamped doc)
    _draw_corner_marks(draw, color=GOLD, length=80, width=4, inset=44)

    # 4. "SUBSCRIBE TO" line + flanking ornaments
    f_small = _load_font(88)
    line1 = "SUBSCRIBE TO"
    bbox = draw.textbbox((0, 0), line1, font=f_small)
    w1 = bbox[2] - bbox[0]
    x1 = (VERTICAL_W - w1) // 2
    y1 = int(VERTICAL_H * 0.26)
    draw.text((x1 + 3, y1 + 3), line1, font=f_small, fill=SHADOW)
    draw.text((x1, y1), line1, font=f_small, fill=WHITE)

    # Decorative line + diamond on each side of "SUBSCRIBE TO"
    line_y = y1 + (bbox[3] - bbox[1]) // 2 + 8
    diamond = 16
    margin = 40
    # Left side
    draw.line([(80, line_y), (x1 - margin - diamond, line_y)], fill=GOLD, width=3)
    _draw_diamond(draw, cx=x1 - margin - diamond // 2, cy=line_y, size=diamond, color=GOLD)
    # Right side
    draw.line([(x1 + w1 + margin + diamond, line_y), (VERTICAL_W - 80, line_y)], fill=GOLD, width=3)
    _draw_diamond(draw, cx=x1 + w1 + margin + diamond // 2, cy=line_y, size=diamond, color=GOLD)

    # 5. Brand wordmark — big bold gold caps, scaled to 84% width
    line2 = brand.upper()
    f_brand = _fit_font_to_width(draw, line2, max_width=int(VERTICAL_W * 0.84), start_size=320)
    bbox = draw.textbbox((0, 0), line2, font=f_brand)
    w2 = bbox[2] - bbox[0]
    h2 = bbox[3] - bbox[1]
    x2 = (VERTICAL_W - w2) // 2
    y2 = int(VERTICAL_H * 0.33)
    # Strong drop shadow for depth
    for off in (8, 5):
        draw.text((x2 + off, y2 + off), line2, font=f_brand, fill=SHADOW)
    draw.text((x2, y2), line2, font=f_brand, fill=GOLD)

    # 6. Underline + tagline below brand
    underline_y = y2 + h2 + 55
    underline_w = int(w2 * 0.48)
    ux1 = (VERTICAL_W - underline_w) // 2
    draw.rectangle([ux1, underline_y, ux1 + underline_w, underline_y + 5], fill=GOLD)

    f_tag = _load_font(48)
    tagline = "NEW CASE FILES DAILY"
    bbox = draw.textbbox((0, 0), tagline, font=f_tag)
    wt = bbox[2] - bbox[0]
    draw.text(((VERTICAL_W - wt) // 2, underline_y + 30), tagline, font=f_tag, fill=WHITE)

    # 7. Subscribe pill + bell icon to its left
    pill_text = "SUBSCRIBE"
    f_pill = _load_font(108)
    bbox = draw.textbbox((0, 0), pill_text, font=f_pill)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    pad_x, pad_y = 78, 48
    pill_w = text_w + pad_x * 2
    pill_h = text_h + pad_y * 2

    bell_size = 96
    bell_gap = 32
    total_w = bell_size + bell_gap + pill_w
    group_x = (VERTICAL_W - total_w) // 2
    pill_x = group_x + bell_size + bell_gap
    pill_y = int(VERTICAL_H * 0.68)
    bell_y = pill_y + (pill_h - bell_size) // 2
    radius = pill_h // 2

    # Pill shadow
    shadow_img = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_img)
    sd.rounded_rectangle(
        [pill_x + 6, pill_y + 12, pill_x + pill_w + 6, pill_y + pill_h + 12],
        radius=radius, fill=(0, 0, 0, 170),
    )
    img.paste(shadow_img, (0, 0), shadow_img)
    # Pill
    draw.rounded_rectangle(
        [pill_x, pill_y, pill_x + pill_w, pill_y + pill_h],
        radius=radius, fill=RED,
    )
    # White pill text
    tx = pill_x + (pill_w - text_w) // 2
    ty = pill_y + (pill_h - text_h) // 2 - 8
    draw.text((tx, ty), pill_text, font=f_pill, fill=WHITE)

    # Bell icon
    _draw_bell(draw, x=group_x, y=bell_y, size=bell_size, color=GOLD)

    img.save(out_path, "PNG")


def _draw_diamond(draw, *, cx: int, cy: int, size: int, color) -> None:
    half = size // 2
    draw.polygon(
        [(cx, cy - half), (cx + half, cy), (cx, cy + half), (cx - half, cy)],
        fill=color,
    )


def _draw_corner_marks(draw, *, color, length: int, width: int, inset: int) -> None:
    """L-shaped marks in each corner — like file-folder registration ticks."""
    W, H = VERTICAL_W, VERTICAL_H
    # Top-left
    draw.rectangle([inset, inset, inset + length, inset + width], fill=color)
    draw.rectangle([inset, inset, inset + width, inset + length], fill=color)
    # Top-right
    draw.rectangle([W - inset - length, inset, W - inset, inset + width], fill=color)
    draw.rectangle([W - inset - width, inset, W - inset, inset + length], fill=color)
    # Bottom-left
    draw.rectangle([inset, H - inset - width, inset + length, H - inset], fill=color)
    draw.rectangle([inset, H - inset - length, inset + width, H - inset], fill=color)
    # Bottom-right
    draw.rectangle([W - inset - length, H - inset - width, W - inset, H - inset], fill=color)
    draw.rectangle([W - inset - width, H - inset - length, W - inset, H - inset], fill=color)


def _draw_bell(draw, *, x: int, y: int, size: int, color) -> None:
    """Stylized notification bell. Built from primitives so we don't need
    an emoji font on the runner."""
    cx = x + size // 2
    # Small top knob (the bell's handle)
    knob_r = size // 14
    draw.ellipse(
        [cx - knob_r, y, cx + knob_r, y + knob_r * 2],
        fill=color,
    )
    # Dome (top half of an ellipse via pieslice)
    dome_top = y + knob_r * 2
    dome_bot = y + int(size * 0.75)
    draw.pieslice(
        [x + size // 8, dome_top - size // 6, x + size - size // 8, dome_bot + size // 8],
        180, 360, fill=color,
    )
    # Bottom flange — a thin gold strip across the bell base
    flange_y = dome_bot
    draw.rectangle(
        [x + 2, flange_y, x + size - 2, flange_y + size // 14],
        fill=color,
    )
    # Clapper — small circle hanging below
    clap_r = size // 12
    draw.ellipse(
        [cx - clap_r, flange_y + size // 14 + 4,
         cx + clap_r, flange_y + size // 14 + 4 + clap_r * 2],
        fill=color,
    )


def _fit_font_to_width(draw, text: str, *, max_width: int, start_size: int):
    """Binary-search down font size until text fits within max_width."""
    from .thumbnail import _load_font
    size = start_size
    while size > 20:
        font = _load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size = int(size * 0.92)
    return _load_font(20)


def _concat(parts: list[Path], *, out_path: Path) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in parts:
            f.write(f"file '{p.as_posix()}'\n")
        list_path = f.name
    try:
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            str(out_path),
        ]
        _run(cmd)
    finally:
        Path(list_path).unlink(missing_ok=True)


def _finalize_loudness(video_in: Path, *, out_path: Path) -> None:
    """Master loudness AND hard-cap to MAX_SHORT_DURATION.

    The cap is deliberate: YouTube classifies <=60s vertical as a Short.
    Earlier runs were producing 90+s files because something downstream
    (concat / loudnorm / lavfi outro) was extending duration. The -t cap
    is the failsafe — even if a panel/outro duration drifts, the final
    file is bounded.
    """
    in_dur = ffprobe_duration(video_in)
    target = min(in_dur, MAX_SHORT_DURATION)
    log().info("  short finalize: input=%.1fs → output=%.1fs (cap=%.1f)",
               in_dur, target, MAX_SHORT_DURATION)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_in),
        "-filter_complex",
        "[0:a]acompressor=threshold=-18dB:ratio=2.5:attack=10:release=200,"
        "loudnorm=I=-16:TP=-1.5:LRA=11[a]",
        "-map", "0:v", "-map", "[a]",
        "-t", f"{target:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        str(out_path),
    ]
    _run(cmd)


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        log().error("ffmpeg failed: %s", " ".join(cmd[:8]) + " …")
        log().error(e.stderr.decode("utf-8", errors="ignore")[-2000:])
        raise
