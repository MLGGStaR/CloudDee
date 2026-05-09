"""Pexels stock photo & video fetcher.

Free API. Royalty-free, attribution appreciated. Rate limit: 200 req/hr / 20K
req/month for free tier — more than enough.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..utils import log


PHOTO_SEARCH = "https://api.pexels.com/v1/search"
VIDEO_SEARCH = "https://api.pexels.com/videos/search"


def fetch_photo(api_key: str, query: str, *, out_dir: Path, orientation: str = "landscape") -> Path | None:
    """Search Pexels for `query`, download the top photo, return its path.
    Returns None if no key or no result."""
    if not api_key:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": api_key}
    params = {"query": query, "per_page": 5, "orientation": orientation}
    try:
        r = _request("GET", PHOTO_SEARCH, headers=headers, params=params)
        photos = r.json().get("photos", [])
        if not photos:
            return None
        # Pick the largest "landscape" rendition
        photo = photos[0]
        src = photo["src"].get("large2x") or photo["src"].get("large") or photo["src"].get("original")
        if not src:
            return None
        ext = Path(src.split("?")[0]).suffix or ".jpg"
        slug = hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]
        dest = out_dir / f"pexels_{slug}{ext}"
        if not dest.exists():
            with httpx.stream("GET", src, timeout=30.0, follow_redirects=True) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
        return dest
    except Exception as e:
        log().debug("Pexels photo fetch failed for %r: %s", query, e)
        return None


def fetch_video(api_key: str, query: str, *, out_dir: Path) -> Path | None:
    """Search Pexels videos for `query`, download a short clip, return path."""
    if not api_key:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": api_key}
    params = {"query": query, "per_page": 5, "orientation": "landscape", "size": "medium"}
    try:
        r = _request("GET", VIDEO_SEARCH, headers=headers, params=params)
        videos = r.json().get("videos", [])
        if not videos:
            return None
        v = videos[0]
        # Pick a 1080p or smaller mp4 file
        files = sorted(v.get("video_files", []), key=lambda f: f.get("width", 0))
        chosen = next((f for f in files if f.get("width", 0) <= 1920 and f.get("file_type") == "video/mp4"), None)
        if chosen is None and files:
            chosen = files[-1]
        if chosen is None:
            return None
        slug = hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]
        dest = out_dir / f"pexels_{slug}.mp4"
        if not dest.exists():
            with httpx.stream("GET", chosen["link"], timeout=60.0, follow_redirects=True) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
        return dest
    except Exception as e:
        log().debug("Pexels video fetch failed for %r: %s", query, e)
        return None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
def _request(method: str, url: str, **kwargs) -> httpx.Response:
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        r = c.request(method, url, **kwargs)
        r.raise_for_status()
        return r
