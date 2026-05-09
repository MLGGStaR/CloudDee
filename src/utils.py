"""Shared helpers."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

from rich.logging import RichHandler


def render_template(template: str, **vars: Any) -> str:
    """Substitute {name} placeholders without choking on JSON-style braces in
    the rest of the template. Only the names you pass in are touched.

    Example:
        render_template("{title} :: {price}", title="x", price=10)
        # works even if `template` also contains literal '{' / '}' (e.g. JSON).
    """
    if not vars:
        return template
    pattern = re.compile(r"\{(" + "|".join(re.escape(k) for k in vars) + r")\}")
    return pattern.sub(lambda m: str(vars[m.group(1)]), template)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
        force=True,
    )


def log() -> logging.Logger:
    return logging.getLogger("docket")


def slugify(text: str, *, max_len: int = 80) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:max_len] or "untitled"


def truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "…"


def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def chunk(items: Iterable, size: int):
    buf = []
    for it in items:
        buf.append(it)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found in PATH. Install with `apt-get install ffmpeg` "
            "(Ubuntu/Debian) or `brew install ffmpeg` (macOS)."
        )


def ffprobe_duration(path: Path) -> float:
    """Return the duration of an audio/video file in seconds."""
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        text=True,
    )
    return float(out.strip())
