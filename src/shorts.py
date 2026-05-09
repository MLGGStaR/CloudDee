"""Generate a 9:16 vertical YouTube Short.

This is NOT just a clip of the long-form. It is a separately-written 55-second
recap of the whole story, voiced fresh, with visuals borrowed from the long
form's panel pool and distributed evenly across the recap audio. A 3-second
"Subscribe to {brand}" outro card is appended at the end.

Pipeline:
  1. Claude condenses the full long-form narration into a ~140-word recap.
  2. OpenAI TTS voices it with the same channel voice.
  3. ffmpeg builds a 1080x1920 vertical timeline using the long-form's
     visuals as a panel pool, ~6 panels across the recap duration, each
     with its own Ken Burns motion. Captions are burned in.
  4. A separate 3-second outro is appended.
  5. Audio is loudnorm'd to -16 LUFS, matching the long form.
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

from .config import Channel, load_prompt
from .render import SceneAsset
from .utils import ffprobe_duration, log, render_template, require_ffmpeg


VERTICAL_W = 1080
VERTICAL_H = 1920
PANEL_TARGET_SEC = 8.0          # vertical panels can be a bit longer than long-form
RECAP_MODEL = "claude-sonnet-4-6"
TTS_MODEL = "tts-1-hd"
OUTRO_DURATION = 3.0


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
    """Build a Short. Returns the path to the rendered .mp4."""
    require_ffmpeg()
    if not scenes:
        raise RuntimeError("no scenes provided to make_short")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    brand = channel.brand_name or channel.name

    # 1) Recap script (~55s)
    recap_text = _write_recap(
        anthropic_key,
        long_form_title=long_form_title,
        full_narration=long_form_narration,
        brand_name=brand,
    )
    log().info("  recap: %d words", len(recap_text.split()))

    # 2) Voice the recap
    with tempfile.TemporaryDirectory(prefix="docket_short_") as td:
        td_path = Path(td)
        recap_audio = td_path / "recap.wav"
        _synthesize(
            openai_key,
            text=recap_text,
            voice=channel.voice,
            speed=channel.voice_speed,
            out_path=recap_audio,
        )
        recap_duration = ffprobe_duration(recap_audio)

        # 3) Build vertical body using the long-form's visuals as a panel pool.
        visual_pool: list[Path] = []
        for sa in scenes:
            visual_pool.extend(sa.visuals)
        if not visual_pool:
            raise RuntimeError("no visuals available for short")

        n_panels = max(4, int(round(recap_duration / PANEL_TARGET_SEC)))
        # Cycle through the pool if we need more panels than visuals.
        if len(visual_pool) < n_panels:
            visual_pool = (visual_pool * ((n_panels // len(visual_pool)) + 1))[:n_panels]
        else:
            # Spread — pick visuals at evenly-spaced indices for variety.
            step = len(visual_pool) / n_panels
            visual_pool = [visual_pool[int(i * step)] for i in range(n_panels)]

        body_path = td_path / "body.mp4"
        _render_vertical_body(
            visuals=visual_pool,
            audio=recap_audio,
            narration=recap_text,
            out_path=body_path,
        )

        # 4) Outro card
        outro_path = td_path / "outro.mp4"
        _render_outro(
            brand=brand,
            accent_color=channel.accent_color,
            out_path=outro_path,
        )

        # 5) Concat + master loudness
        concat_path = td_path / "concat.mp4"
        _concat([body_path, outro_path], out_path=concat_path)
        _finalize_loudness(concat_path, out_path=out_path)

    log().info("  short → %s (%.1fs)", out_path, recap_duration + OUTRO_DURATION)
    return out_path


# -----------------------------------------------------------------------------
# Recap script
# -----------------------------------------------------------------------------

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


# -----------------------------------------------------------------------------
# Voice
# -----------------------------------------------------------------------------

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


# -----------------------------------------------------------------------------
# Vertical body assembly
# -----------------------------------------------------------------------------

def _render_vertical_body(*, visuals: list[Path], audio: Path, narration: str, out_path: Path) -> None:
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

        # Mux audio + caption overlay in one pass.
        cap_filter = _captions_filter(narration, duration)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(joined),
            "-i", str(audio),
            "-filter_complex", f"[0:v]{cap_filter}[v]",
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


def _captions_filter(narration: str, duration: float) -> str:
    """Burned-in 2-3 word captions, evenly time-spaced. Sits in the lower
    third area, big bold white with thick black border."""
    chunks = _chunk_narration(narration, words_per_chunk=3)
    if not chunks:
        return "null"

    per = duration / len(chunks)
    parts: list[str] = []
    for i, ch in enumerate(chunks):
        start = i * per
        end = (i + 1) * per
        text = _ffmpeg_escape(ch.upper())
        parts.append(
            f"drawtext=text='{text}':"
            f"fontsize=88:fontcolor=white:borderw=8:bordercolor=black:"
            f"x=(w-text_w)/2:y=h*0.78:"
            f"enable='between(t,{start:.2f},{end:.2f})'"
        )
    return ",".join(parts)


def _chunk_narration(text: str, words_per_chunk: int = 3) -> list[str]:
    words = re.findall(r"[A-Za-z0-9'.,$%-]+", text)
    if not words:
        return []
    return [" ".join(words[i:i + words_per_chunk])
            for i in range(0, len(words), words_per_chunk)]


def _ffmpeg_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
            .replace("'", "")
            .replace(":", "\\:")
            .replace("%", "\\%")
            .replace(",", "\\,")
    )


# -----------------------------------------------------------------------------
# Outro
# -----------------------------------------------------------------------------

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


# -----------------------------------------------------------------------------
# Concat / loudness
# -----------------------------------------------------------------------------

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
    """Bring the whole audio track to -16 LUFS to match the long form."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_in),
        "-filter_complex",
        "[0:a]acompressor=threshold=-18dB:ratio=2.5:attack=10:release=200,"
        "loudnorm=I=-16:TP=-1.5:LRA=11[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy",
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
