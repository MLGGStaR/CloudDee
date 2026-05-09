"""FFmpeg video assembly.

Inputs:
  - Per-scene audio WAV files (in order)
  - Per-scene visual paths (image or short video)
  - Optional channel music bed (looped, ducked under voice)
  - Optional channel intro sting (3–5 seconds, prepended)

Output: a single 1080p mp4, 30fps, AAC audio.

Visual treatment:
  - Stills: scaled and cropped to 1920x1080, with a slow Ken Burns pan/zoom
    matching the scene's audio duration.
  - Videos: scaled to 1920x1080, looped or sped to fit the scene's audio
    duration, audio track stripped (we only use the visual track).
  - Lower-third title bar appears for the first 4 seconds of each scene.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Channel, Settings
from .utils import ffprobe_duration, log, require_ffmpeg


@dataclass
class SceneAsset:
    audio_path: Path
    visual_path: Path
    title_overlay: str = ""        # e.g., scene id or label


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

        # Concat all scenes
        concat_path = td_path / "concat.mp4"
        _concat_videos(scene_videos, out_path=concat_path)

        # Optional: prepend channel intro sting
        intro_path = _intro_for(channel)
        if intro_path and intro_path.exists():
            with_intro = td_path / "with_intro.mp4"
            _concat_videos([intro_path, concat_path], out_path=with_intro)
            voice_video = with_intro
        else:
            voice_video = concat_path

        # Optional: mix in music bed (ducked under narration)
        music_path = _music_for(channel)
        if music_path and music_path.exists():
            _mix_music(voice_video, music_path, out_path=out_path)
        else:
            shutil.copy2(voice_video, out_path)

    log().info("  rendered %s", out_path)
    return out_path


def _render_one_scene(sa: SceneAsset, *, out_path: Path) -> None:
    duration = ffprobe_duration(sa.audio_path)
    is_video = sa.visual_path.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}

    if is_video:
        # Loop video, scale, and pad to 1920x1080.
        v_input = ["-stream_loop", "-1", "-i", str(sa.visual_path)]
        v_filter = (
            "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080,fps=30,format=yuv420p[v]"
        )
    else:
        # Still image with slow Ken Burns zoom.
        v_input = ["-loop", "1", "-i", str(sa.visual_path)]
        # Subtle 6% zoom over the scene duration. zoompan needs explicit fps.
        frames = int(duration * 30)
        v_filter = (
            "[0:v]scale=2400:-1:flags=lanczos,"
            f"zoompan=z='min(zoom+0.0008,1.06)':d={frames}:s=1920x1080:fps=30,"
            "format=yuv420p[v]"
        )

    cmd = [
        "ffmpeg", "-y",
        *v_input,
        "-i", str(sa.audio_path),
        "-filter_complex", v_filter,
        "-map", "[v]",
        "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration:.3f}",
        "-shortest",
        str(out_path),
    ]
    _run(cmd)


def _concat_videos(parts: list[Path], *, out_path: Path) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in parts:
            f.write(f"file '{p.as_posix()}'\n")
        list_path = f.name
    try:
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            str(out_path),
        ]
        try:
            _run(cmd)
        except subprocess.CalledProcessError:
            # Codec mismatch fallback — re-encode.
            cmd = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", list_path,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k",
                str(out_path),
            ]
            _run(cmd)
    finally:
        Path(list_path).unlink(missing_ok=True)


def _mix_music(video_in: Path, music: Path, *, out_path: Path) -> None:
    """Mix music bed at -22 dB under the voice. Music loops to match length."""
    duration = ffprobe_duration(video_in)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_in),
        "-stream_loop", "-1", "-i", str(music),
        "-filter_complex",
        # Bring music to -22 dB; sidechaincompress would be better but adds
        # complexity. -22 dB sits cleanly under TTS.
        "[1:a]volume=0.08,aloop=loop=-1:size=2e9[bg];"
        "[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]",
        "-map", "0:v",
        "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration:.3f}",
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
