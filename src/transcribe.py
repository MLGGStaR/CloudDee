"""Whisper-based transcription for accurately timed subtitles.

We use OpenAI's Whisper-1 endpoint, which returns SRT directly. We then
post-process the SRT to:
  - Shift timestamps by an offset (so a scene-local SRT becomes a master
    SRT once we know its position in the final timeline).
  - Re-flow segments to a max of `max_words_per_caption` so captions don't
    feel like reading walls.

Whisper costs ~$0.006 per minute of audio. An 8-minute long-form ≈ $0.05.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .utils import log


WHISPER_MODEL = "whisper-1"
WHISPER_FILE_LIMIT_MB = 24    # API hard cap is 25 MB; we keep headroom


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=10), reraise=True)
def transcribe_to_srt(
    api_key: str,
    audio_path: Path,
    *,
    language: str = "en",
) -> str:
    """Transcribe `audio_path` with Whisper-1, return SRT string."""
    audio_path = Path(audio_path)
    upload_path = _ensure_uploadable(audio_path)
    client = OpenAI(api_key=api_key)
    with open(upload_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=f,
            response_format="srt",
            language=language,
        )
    if upload_path != audio_path:
        upload_path.unlink(missing_ok=True)
    # Whisper SDK returns a string when response_format is srt.
    return str(result)


def _ensure_uploadable(audio_path: Path) -> Path:
    """If the audio file is too big for Whisper, transcode to a small mp3."""
    size_mb = audio_path.stat().st_size / (1024 * 1024)
    if size_mb < WHISPER_FILE_LIMIT_MB:
        return audio_path
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            f"audio is {size_mb:.1f}MB which exceeds the Whisper limit, "
            "and ffmpeg is not available to compress it."
        )
    out = audio_path.with_suffix(".whisper.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(audio_path), "-b:a", "64k", "-ac", "1", str(out)],
        check=True, capture_output=True,
    )
    log().info("compressed %.1fMB → %.1fMB for Whisper", size_mb,
               out.stat().st_size / (1024 * 1024))
    return out


# -----------------------------------------------------------------------------
# SRT helpers
# -----------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)")


def shift_srt(srt_text: str, offset_seconds: float) -> str:
    """Add `offset_seconds` to every timestamp in an SRT string."""
    if offset_seconds == 0:
        return srt_text

    def repl(m):
        start = _ts_to_seconds(*m.group(1, 2, 3, 4)) + offset_seconds
        end = _ts_to_seconds(*m.group(5, 6, 7, 8)) + offset_seconds
        return f"{_seconds_to_ts(start)} --> {_seconds_to_ts(end)}"

    return _TIMESTAMP_RE.sub(repl, srt_text)


def merge_srt(srts_with_offsets: list[tuple[str, float]]) -> str:
    """Combine multiple SRT segments into one master SRT, renumbering."""
    blocks = []
    for srt, offset in srts_with_offsets:
        if not srt or not srt.strip():
            continue
        shifted = shift_srt(srt, offset)
        blocks.extend(_parse_blocks(shifted))
    blocks.sort(key=lambda b: b["start"])
    return _format_blocks(blocks)


def reflow_srt_max_words(srt_text: str, max_words: int = 7) -> str:
    """Break long captions into shorter segments at word-boundary, holding
    timestamps proportional to text length. Keeps the rhythm crisp for video."""
    blocks = _parse_blocks(srt_text)
    out: list[dict] = []
    for b in blocks:
        words = b["text"].split()
        if len(words) <= max_words:
            out.append(b)
            continue
        n_pieces = (len(words) + max_words - 1) // max_words
        per_dur = (b["end"] - b["start"]) / n_pieces
        for i in range(n_pieces):
            chunk_words = words[i * max_words:(i + 1) * max_words]
            if not chunk_words:
                continue
            out.append({
                "start": b["start"] + i * per_dur,
                "end": b["start"] + (i + 1) * per_dur,
                "text": " ".join(chunk_words),
            })
    return _format_blocks(out)


# Internal SRT parse/format helpers

def _ts_to_seconds(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _seconds_to_ts(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_blocks(srt_text: str) -> list[dict]:
    out: list[dict] = []
    for chunk in re.split(r"\n\s*\n", srt_text.strip()):
        lines = chunk.splitlines()
        if len(lines) < 2:
            continue
        # Optional sequence number on first line
        idx = 1 if lines[0].strip().isdigit() else 0
        if idx >= len(lines):
            continue
        m = _TIMESTAMP_RE.search(lines[idx])
        if not m:
            continue
        start = _ts_to_seconds(*m.group(1, 2, 3, 4))
        end = _ts_to_seconds(*m.group(5, 6, 7, 8))
        text = "\n".join(lines[idx + 1:]).strip()
        if text:
            out.append({"start": start, "end": end, "text": text})
    return out


def _format_blocks(blocks: list[dict]) -> str:
    out_parts: list[str] = []
    for i, b in enumerate(blocks, 1):
        out_parts.append(
            f"{i}\n{_seconds_to_ts(b['start'])} --> {_seconds_to_ts(b['end'])}\n{b['text']}\n"
        )
    return "\n".join(out_parts)
