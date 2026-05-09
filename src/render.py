"""FFmpeg video assembly for long-form videos.

Each scene:
  - Has its own audio track (voice).
  - Has multiple short visual panels (~6.5s each) with their own Ken Burns.
  - Optionally has its own music bed mixed in at -22 dB.
  - Optionally has burned-in subtitles from a per-scene SRT.

Output: 1080p mp4, 30fps, AAC audio loudnorm'd to -16 LUFS.
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

PANEL_TARGET_SEC = 6.5
PANEL_MIN_SEC = 4.0


@dataclass
class SceneAsset:
    audio_path: Path
    visuals: list[Path] = field(default_factory=list)
    title_overlay: str = ""
    scene_id: str = ""
    srt_text: str = ""              # per-scene SRT (timestamps relative to scene start)

    def __post_init__(self):
        if not self.visuals:
            raise ValueError(f"SceneAsset needs at least one visual ({self.audio_path})")


def panels_needed_for(duration_sec: float) -> int:
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
            _render_one_scene(sa, channel=channel, out_path=sv)
            scene_videos.append(sv)

        concat_path = td_path / "concat.mp4"
        _concat_videos(scene_videos, out_path=concat_path)

        # Optional intro sting prepend
        intro_path = _intro_for(channel)
        if intro_path and intro_path.exists():
            with_intro = td_path / "with_intro.mp4"
            _concat_videos([intro_path, concat_path], out_path=with_intro)
            voice_video = with_intro
        else:
            voice_video = concat_path

        # Master loudnorm
        _finalize_loudness(voice_video, out_path=out_path)

    log().info("  rendered %s", out_path)
    return out_path


def _render_one_scene(sa: SceneAsset, *, channel: Channel, out_path: Path) -> None:
    """Render a single scene: panels + voice + optional per-scene music + burned subs."""
    total_duration = ffprobe_duration(sa.audio_path)
    n_panels = panels_needed_for(total_duration)
    visuals = sa.visuals[:n_panels] if len(sa.visuals) >= n_panels else sa.visuals
    if len(visuals) < n_panels:
        visuals = (visuals * ((n_panels // len(visuals)) + 1))[:n_panels]
    panel_duration = total_duration / n_panels

    music_path = _music_for_scene(channel, sa.scene_id)

    with tempfile.TemporaryDirectory(prefix="docket_scene_") as td:
        td_path = Path(td)
        # 1. Render silent panels.
        panel_paths: list[Path] = []
        for i, vis in enumerate(visuals):
            p = td_path / f"panel_{i:03d}.mp4"
            _render_panel(vis, duration=panel_duration, panel_index=i, out_path=p)
            panel_paths.append(p)

        joined = td_path / "joined.mp4"
        _concat_videos(panel_paths, out_path=joined)

        # 2. Mux with voice (+ optional music) and burn subtitles in one pass.
        srt_path: Path | None = None
        if sa.srt_text.strip():
            srt_path = td_path / "scene.srt"
            srt_path.write_text(sa.srt_text, encoding="utf-8")

        if music_path and music_path.exists():
            audio_filter = (
                "[1:a]acompressor=threshold=-18dB:ratio=2.5:attack=10:release=200,"
                "volume=1.4[voice];"
                "[2:a]volume=0.07,aloop=loop=-1:size=2e9[bg];"
                "[voice][bg]amix=inputs=2:duration=first:dropout_transition=2[a]"
            )
            inputs = [
                "-i", str(joined),
                "-i", str(sa.audio_path),
                "-stream_loop", "-1", "-i", str(music_path),
            ]
            audio_map = "[a]"
        else:
            audio_filter = (
                "[1:a]acompressor=threshold=-18dB:ratio=2.5:attack=10:release=200,"
                "volume=1.4[a]"
            )
            inputs = [
                "-i", str(joined),
                "-i", str(sa.audio_path),
            ]
            audio_map = "[a]"

        # Subtitle burn — long-form style: bottom-third, thick outline.
        if srt_path is not None:
            srt_filter_path = str(srt_path).replace("\\", "/").replace(":", r"\:")
            subs_filter = (
                f"subtitles='{srt_filter_path}':"
                "force_style='Fontsize=22,Outline=2,Shadow=1,BorderStyle=1,"
                "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                "BackColour=&H80000000,Alignment=2,MarginV=60,Bold=1'"
            )
            video_filter = f"[0:v]{subs_filter}[v]"
            video_map = "[v]"
        else:
            video_filter = "[0:v]copy[v]"
            video_map = "[v]"

        cmd = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", f"{video_filter};{audio_filter}",
            "-map", video_map, "-map", audio_map,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{total_duration:.3f}",
            "-shortest",
            str(out_path),
        ]
        _run(cmd)


def _render_panel(visual: Path, *, duration: float, panel_index: int, out_path: Path) -> None:
    is_video = visual.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}
    motion = panel_index % 4

    if is_video:
        v_input = ["-stream_loop", "-1", "-i", str(visual)]
        v_filter = (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},fps={FPS},format=yuv420p,setsar=1[v]"
        )
    else:
        frames = max(int(duration * FPS), FPS)
        if motion == 0:
            zoom, x, y = "min(zoom+0.0010,1.10)", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
        elif motion == 1:
            zoom, x, y = "min(zoom+0.0011,1.12)", "0", "0"
        elif motion == 2:
            zoom, x, y = "min(zoom+0.0011,1.12)", "iw/zoom-iw/zoom", "ih/zoom-ih/zoom"
        else:
            zoom, x, y = "1.05", f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"
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
    """Bring audio master to -16 LUFS so volume is consistent across all videos."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_in),
        "-filter_complex", "[0:a]loudnorm=I=-16:TP=-1.5:LRA=11[a]",
        "-map", "0:v", "-map", "[a]",
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


def _music_for_scene(channel: Channel, scene_id: str) -> Path | None:
    """Resolve a scene's music: per-scene mapping > channel default > none."""
    base = Path(__file__).resolve().parent.parent / "assets" / "music"
    name = (channel.music_by_scene_id or {}).get(scene_id)
    if not name:
        name = channel.music_bed
    if not name:
        return None
    p = base / name
    return p if p.exists() else None


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        log().error("ffmpeg failed: %s", " ".join(cmd[:8]) + " …")
        log().error(e.stderr.decode("utf-8", errors="ignore")[-2000:])
        raise
