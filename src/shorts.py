"""Generate a 9:16 vertical Short from a long-form video's hook scene.

Strategy:
  - Take the audio + visual from scene[0] (the HOOK scene), which is by design
    the strongest 15-30 seconds of the script.
  - If hook is shorter than 30s, append scene[1] to fill toward 60s max.
  - Cap at 58 seconds (Shorts max is 60; leave headroom).
  - Re-render at 1080x1920 (vertical) with the visual cropped to fill the
    upper 75% and a captions area in the lower 25%.
  - Append a 3-second outro card with the long-form video's title.
  - Captions are burned-in via ffmpeg drawtext, line-by-line word-timed
    approximately by splitting the narration into ~3-word chunks evenly
    distributed across the audio duration.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from .config import Channel
from .render import SceneAsset
from .utils import ffprobe_duration, log, require_ffmpeg


VERTICAL_W = 1080
VERTICAL_H = 1920
TOP_AREA_H = int(VERTICAL_H * 0.72)   # video occupies the top 72%
CAP_AREA_TOP = TOP_AREA_H + 40        # captions sit below the video
MAX_DURATION = 58.0                   # cap before YouTube stops calling it a Short


def make_short(
    *,
    scenes: list[SceneAsset],
    long_form_title: str,
    channel: Channel,
    out_path: Path,
    narration_full_text: str = "",
) -> Path:
    """Build a vertical Short from the first 1–2 scenes of the long-form video.
    `scenes` are the same SceneAsset list passed to assemble_video.
    """
    require_ffmpeg()
    if not scenes:
        raise RuntimeError("no scenes provided to make_short")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Pick scenes to fill ~30-58s
    selected: list[SceneAsset] = []
    total = 0.0
    for sa in scenes[:3]:
        d = ffprobe_duration(sa.audio_path)
        if total + d > MAX_DURATION - 5:  # leave room for outro
            break
        selected.append(sa)
        total += d
        if total >= 25.0:  # target floor
            break
    if not selected:
        selected = [scenes[0]]
        total = ffprobe_duration(scenes[0].audio_path)

    with tempfile.TemporaryDirectory(prefix="docket_short_") as td:
        td_path = Path(td)
        scene_paths: list[Path] = []
        for i, sa in enumerate(selected):
            sp = td_path / f"v_scene_{i}.mp4"
            _render_vertical_scene(sa, out_path=sp)
            scene_paths.append(sp)

        # Concat the vertical scenes
        body_path = td_path / "body.mp4"
        _concat(scene_paths, out_path=body_path)

        # Outro card (3s)
        outro_path = td_path / "outro.mp4"
        _render_outro(long_form_title, channel=channel, out_path=outro_path)

        # Final concat
        _concat([body_path, outro_path], out_path=out_path)

    log().info("  short → %s (%.1fs)", out_path, total + 3.0)
    return out_path


def _render_vertical_scene(sa: SceneAsset, *, out_path: Path) -> None:
    """Render one scene as 1080x1920 vertical with captions burned in."""
    duration = ffprobe_duration(sa.audio_path)
    is_video = sa.visual_path.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}

    if is_video:
        v_input = ["-stream_loop", "-1", "-i", str(sa.visual_path)]
        # Crop horizontal video into a square-ish 1080x1382 (the upper area).
        v_filter = (
            f"[0:v]scale=-2:{TOP_AREA_H}:flags=lanczos,"
            f"crop={VERTICAL_W}:{TOP_AREA_H},"
            f"pad={VERTICAL_W}:{VERTICAL_H}:0:0:black,"
            f"fps=30,format=yuv420p[bg]"
        )
    else:
        frames = max(int(duration * 30), 60)
        v_input = ["-loop", "1", "-i", str(sa.visual_path)]
        v_filter = (
            f"[0:v]scale=-2:{TOP_AREA_H}:flags=lanczos,"
            f"crop={VERTICAL_W}:{TOP_AREA_H},"
            f"pad={VERTICAL_W}:{VERTICAL_H}:0:0:black,"
            f"zoompan=z='min(zoom+0.0008,1.06)':d={frames}:s={VERTICAL_W}x{VERTICAL_H}:fps=30,"
            "format=yuv420p[bg]"
        )

    # Read narration text from the audio path's sibling .txt if present;
    # otherwise we'll skip captions for this scene.
    narration = _narration_for(sa)
    cap_filter = _captions_filter(narration, duration) if narration else "[bg]copy[v]"
    full_filter = v_filter + ";" + cap_filter

    cmd = [
        "ffmpeg", "-y",
        *v_input,
        "-i", str(sa.audio_path),
        "-filter_complex", full_filter,
        "-map", "[v]",
        "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration:.3f}",
        "-shortest",
        str(out_path),
    ]
    _run(cmd)


def _captions_filter(narration: str, duration: float) -> str:
    """Build an ffmpeg filter chain that burns animated 2-3 word captions
    over the bg track for `duration` seconds."""
    chunks = _chunk_narration(narration, words_per_chunk=3)
    if not chunks:
        return "[bg]copy[v]"

    per = duration / len(chunks)
    label_in = "bg"
    parts: list[str] = []
    for i, ch in enumerate(chunks):
        start = i * per
        end = (i + 1) * per
        text = _ffmpeg_escape(ch.upper())
        # Draw at lower-third area, centered, big bold.
        parts.append(
            f"[{label_in}]drawtext="
            f"text='{text}':"
            f"fontfile='':"
            f"fontsize=78:"
            f"fontcolor=white:"
            f"borderw=6:bordercolor=black:"
            f"x=(w-text_w)/2:"
            f"y={CAP_AREA_TOP}+60:"
            f"enable='between(t,{start:.2f},{end:.2f})'[c{i}]"
        )
        label_in = f"c{i}"
    parts[-1] = parts[-1][:-len(f"[c{len(chunks)-1}]")] + "[v]"
    return ";".join(parts)


def _chunk_narration(text: str, words_per_chunk: int = 3) -> list[str]:
    words = re.findall(r"[A-Za-z0-9'.,$%-]+", text)
    if not words:
        return []
    out: list[str] = []
    for i in range(0, len(words), words_per_chunk):
        out.append(" ".join(words[i:i + words_per_chunk]))
    return out


def _ffmpeg_escape(text: str) -> str:
    """Escape text for ffmpeg drawtext."""
    return (
        text.replace("\\", "\\\\")
            .replace("'", "")
            .replace(":", "\\:")
            .replace("%", "\\%")
            .replace(",", "\\,")
    )


def _narration_for(sa: SceneAsset) -> str:
    sidecar = sa.audio_path.with_suffix(".txt")
    if sidecar.exists():
        return sidecar.read_text(encoding="utf-8")
    return ""


def _render_outro(title: str, *, channel: Channel, out_path: Path) -> None:
    duration = 3.0
    safe_title = _ffmpeg_escape(title.upper()[:80])
    safe_channel = _ffmpeg_escape(f"FULL VIDEO ON {channel.name.upper()}")
    accent = channel.accent_color.lstrip("#") or "0c2d48"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=#{accent}:s={VERTICAL_W}x{VERTICAL_H}:d={duration}:r=30",
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex",
        (
            f"[0:v]drawtext=text='{safe_channel}':fontsize=64:fontcolor=white:"
            f"borderw=4:bordercolor=black:x=(w-text_w)/2:y=h*0.42[a];"
            f"[a]drawtext=text='{safe_title}':fontsize=58:fontcolor=#f5d067:"
            f"borderw=4:bordercolor=black:x=(w-text_w)/2:y=h*0.55:line_spacing=12[v]"
        ),
        "-map", "[v]",
        "-map", "1:a",
        "-shortest",
        "-t", f"{duration}",
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


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        log().error("ffmpeg failed: %s", " ".join(cmd[:8]) + " …")
        log().error(e.stderr.decode("utf-8", errors="ignore")[-2000:])
        raise
