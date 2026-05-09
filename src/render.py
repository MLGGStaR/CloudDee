"""FFmpeg video assembly for long-form videos.

Each scene is composed of MULTIPLE short panels (~5–8s each), each panel a
different image/video clip with its own Ken Burns motion. This is what makes
the final video feel alive instead of slideshow-y.

Output: a single 1080p mp4, 30fps, AAC audio normalized to -16 LUFS (the
YouTube broadcast loudness target).
"""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import Channel, Settings
from .utils import ffprobe_duration, log, require_ffmpeg


W = 1920
H = 1080
FPS = 30

# Each panel inside a scene is targeted at this duration. We round to whatever
# integer-divides the scene audio cleanly so panels finish exactly with audio.
PANEL_TARGET_SEC = 6.5
PANEL_MIN_SEC = 4.0


@dataclass
class SceneAsset:
    audio_path: Path
    visuals: list[Path] = field(default_factory=list)   # one OR many — the renderer will tile them
    title_overlay: str = ""

    # Back-compat: allow constructing with a single visual_path keyword.
    def __post_init__(self):
        if not self.visuals:
            raise ValueError(f"SceneAsset needs at least one visual ({self.audio_path})")


def make_scene_asset(audio_path: Path, visuals: list[Path] | Path, title_overlay: str = "") -> SceneAsset:
    if isinstance(visuals, Path):
        visuals = [visuals]
    return SceneAsset(audio_path=audio_path, visuals=list(visuals), title_overlay=title_overlay)


def panels_needed_for(duration_sec: float) -> int:
    """How many ~6.5s panels we want to fill a scene of this length."""
    if duration_sec <= PANEL_MIN_SEC:
        return 1
    return max(1, math.ceil(duration_sec / PANEL_TARGET_SEC))


def assemble_video(
    settings: Settings,
    *,
    channel: Channel,
    scene_assets: list[SceneAsset],
    out_path: Path,
) -> Path:
    require_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    scene_videos: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="docket_render_") as td:
        td_path = Path(td)
        for i, sa in enumerate(scene_assets):
            sv = td_path / f"scene_{i:03d}.mp4"
            _render_one_scene(sa, out_path=sv)
            scene_videos.append(sv)

        concat_path = td_path / "concat.mp4"
        _concat_videos(scene_videos, out_path=concat_path)

        # Optional intro sting
        intro_path = _intro_for(channel)
        if intro_path and intro_path.exists():
            with_intro = td_path / "with_intro.mp4"
            _concat_videos([intro_path, concat_path], out_path=with_intro)
            voice_video = with_intro
        else:
            voice_video = concat_path

        # Music + loudness normalization in one ffmpeg pass.
        music_path = _music_for(channel)
        _finalize(voice_video, music_path, out_path=out_path)

    log().info("  rendered %s", out_path)
    return out_path


def _render_one_scene(sa: SceneAsset, *, out_path: Path) -> None:
    """Render one scene. Splits the scene's audio across multiple visual
    panels so the picture changes every ~6.5 seconds and each panel has its
    own Ken Burns motion."""
    total_duration = ffprobe_duration(sa.audio_path)
    n_panels = panels_needed_for(total_duration)
    visuals = sa.visuals[:n_panels] if len(sa.visuals) >= n_panels else sa.visuals
    # If fewer visuals than panels, repeat the cycle.
    if len(visuals) < n_panels:
        visuals = (visuals * ((n_panels // len(visuals)) + 1))[:n_panels]

    panel_duration = total_duration / n_panels

    with tempfile.TemporaryDirectory(prefix="docket_scene_") as td:
        td_path = Path(td)
        # Render each panel as a silent video clip of length `panel_duration`,
        # then concat with the scene's audio overlaid on top.
        panel_paths: list[Path] = []
        for i, vis in enumerate(visuals):
            p = td_path / f"panel_{i:03d}.mp4"
            _render_panel(vis, duration=panel_duration, panel_index=i, out_path=p)
            panel_paths.append(p)

        # Concatenate the silent panels into a single video track of correct
        # length, then mux the scene audio.
        joined = td_path / "joined.mp4"
        _concat_videos(panel_paths, out_path=joined)

        cmd = [
            "ffmpeg", "-y",
            "-i", str(joined),
            "-i", str(sa.audio_path),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{total_duration:.3f}",
            "-shortest",
            str(out_path),
        ]
        _run(cmd)


def _render_panel(visual: Path, *, duration: float, panel_index: int, out_path: Path) -> None:
    """Render one ~6.5-second silent panel from a single image or short video.

    Alternates between four Ken Burns motions to keep the eye moving:
      0: zoom in slowly, anchored center
      1: zoom in slowly, anchored upper-left
      2: zoom in slowly, anchored lower-right
      3: pan-only (no zoom), drifting slowly right
    """
    is_video = visual.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}
    motion = panel_index % 4

    if is_video:
        # For video panels: scale, crop to 16:9, drop audio.
        v_input = ["-stream_loop", "-1", "-i", str(visual)]
        v_filter = (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},fps={FPS},format=yuv420p,setsar=1[v]"
        )
    else:
        frames = max(int(duration * FPS), FPS)
        # Pre-scale image larger than canvas so we have headroom for pan/zoom.
        if motion == 0:
            zoom = "min(zoom+0.0010,1.10)"
            x = "iw/2-(iw/zoom/2)"
            y = "ih/2-(ih/zoom/2)"
        elif motion == 1:
            zoom = "min(zoom+0.0011,1.12)"
            x = "0"
            y = "0"
        elif motion == 2:
            zoom = "min(zoom+0.0011,1.12)"
            x = "iw/zoom-iw/zoom"
            y = "ih/zoom-ih/zoom"
        else:
            # Pan-only, no zoom.
            zoom = "1.05"
            x = f"(iw-iw/zoom)*on/{frames}"
            y = "ih/2-(ih/zoom/2)"
        v_input = ["-loop", "1", "-i", str(visual)]
        v_filter = (
            f"[0:v]scale=2880:-1:flags=lanczos,"
            f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
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


def _concat_videos(parts: list[Path], *, out_path: Path) -> None:
    """Concat multiple videos with re-encode (always — codec mismatch safe)."""
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


def _finalize(video_in: Path, music: Path | None, *, out_path: Path) -> None:
    """Mix optional music bed at -22 dB under the voice and apply loudnorm to
    bring the master to -16 LUFS (YouTube target). Also lifts the voice a bit
    via a soft compressor to even out the TTS."""
    duration = ffprobe_duration(video_in)
    if music and music.exists():
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_in),
            "-stream_loop", "-1", "-i", str(music),
            "-filter_complex",
            (
                "[0:a]acompressor=threshold=-18dB:ratio=2.5:attack=10:release=200,"
                "volume=1.4[voice];"
                "[1:a]volume=0.07,aloop=loop=-1:size=2e9[bg];"
                "[voice][bg]amix=inputs=2:duration=first:dropout_transition=2,"
                "loudnorm=I=-16:TP=-1.5:LRA=11[a]"
            ),
            "-map", "0:v",
            "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{duration:.3f}",
            str(out_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_in),
            "-filter_complex",
            (
                "[0:a]acompressor=threshold=-18dB:ratio=2.5:attack=10:release=200,"
                "volume=1.4,loudnorm=I=-16:TP=-1.5:LRA=11[a]"
            ),
            "-map", "0:v",
            "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            str(out_path),
        ]
    _run(cmd)


def _intro_for(channel: Channel) -> Path | None:
    if not channel.intro_sting:
        return None
    p = Path(__file__).resolve().parent.parent / "assets" / "intros" / channel.intro_sting
    return p if p.exists() else None


def _music_for(channel: Channel) -> Path | None:
    if not channel.music_bed:
        return None
    p = Path(__file__).resolve().parent.parent / "assets" / "music" / channel.music_bed
    return p if p.exists() else None


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        log().error("ffmpeg failed: %s", " ".join(cmd[:8]) + " …")
        log().error(e.stderr.decode("utf-8", errors="ignore")[-2000:])
        raise
