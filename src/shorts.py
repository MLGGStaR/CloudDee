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
from .transcribe import reflow_srt_max_words, transcribe_to_srt
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
) -> Path:
    """Recap Short paired with a long-form video. Visuals come from the
    long-form's panel pool (cycling for variety)."""
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
) -> Path:
    """Voice the narration, transcribe for captions, render the vertical
    timeline + outro, master loudness."""
    brand = channel.brand_name or channel.name
    with tempfile.TemporaryDirectory(prefix="docket_short_") as td:
        td_path = Path(td)
        recap_audio = td_path / "recap.wav"
        _synthesize(
            openai_key,
            text=narration,
            voice=channel.voice,
            speed=channel.voice_speed,
            out_path=recap_audio,
        )
        recap_duration = ffprobe_duration(recap_audio)

        try:
            srt_text = transcribe_to_srt(openai_key, recap_audio)
            srt_text = reflow_srt_max_words(srt_text, max_words=4)
        except Exception as e:
            log().warning("Shorts transcribe failed (no captions): %s", e)
            srt_text = ""
        srt_path = td_path / "recap.srt"
        if srt_text:
            srt_path.write_text(srt_text, encoding="utf-8")

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
            srt_path=srt_path if srt_text else None,
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

def _render_vertical_body(
    *,
    visuals: list[Path],
    audio: Path,
    srt_path: Path | None,
    out_path: Path,
) -> None:
    duration = ffprobe_duration(audio)
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

        if srt_path is not None and srt_path.exists():
            srt_filter_path = str(srt_path).replace("\\", "/").replace(":", r"\:")
            log().info("  short subtitles: %d bytes → burning", srt_path.stat().st_size)
            # Mirror the (working) long-form filter exactly — only Fontsize and
            # MarginV differ to suit vertical 1080x1920 framing. The earlier
            # custom filter (Fontname=DejaVu Sans, no BackColour) wasn't
            # rendering for some reason; this matches the recipe that's known
            # to render in long-form scene videos.
            v_filter = (
                f"subtitles='{srt_filter_path}':"
                "force_style='Fontsize=48,Outline=2,Shadow=1,BorderStyle=1,"
                "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                "BackColour=&H80000000,Alignment=2,MarginV=600,Bold=1'"
            )
        else:
            log().warning("  short: no SRT → captions will be missing")
            v_filter = "null"

        cmd = [
            "ffmpeg", "-y",
            "-i", str(joined),
            "-i", str(audio),
            "-filter_complex", f"[0:v]{v_filter}[v]",
            "-map", "[v]", "-map", "1:a",
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
    accent = (accent_color or "#0c2d48").lstrip("#") or "0c2d48"
    line1 = _ffmpeg_escape("SUBSCRIBE TO")
    line2 = _ffmpeg_escape(brand.upper())
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"color=c=#{accent}:s={VERTICAL_W}x{VERTICAL_H}:d={OUTRO_DURATION}:r=30",
        "-f", "lavfi", "-i",
        f"anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex",
        (
            f"[0:v]drawtext=text='{line1}':fontsize=80:fontcolor=white:"
            f"borderw=4:bordercolor=black:x=(w-text_w)/2:y=h*0.40[a];"
            f"[a]drawtext=text='{line2}':fontsize=130:fontcolor=#f5d067:"
            f"borderw=6:bordercolor=black:x=(w-text_w)/2:y=h*0.50[v]"
        ),
        "-map", "[v]",
        "-map", "1:a",
        "-shortest",
        "-t", f"{OUTRO_DURATION}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        str(out_path),
    ]
    _run(cmd)


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
