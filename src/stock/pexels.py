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


MIN_PHOTO_WIDTH = 1920    # don't accept anything smaller than HD video width
MIN_PHOTO_HEIGHT = 1080   # ditto for height; rules out tall-but-narrow
MIN_PHOTO_PIXELS = 1920 * 1080  # gates aggressively cropped landscape shots


def fetch_photo(api_key: str, query: str, *, out_dir: Path, orientation: str = "landscape") -> Path | None:
    """Search Pexels for `query`, download the highest-quality eligible photo.

    Eligibility = at least 1920x1080 native resolution AND the `original`
    URL must be usable. We were previously grabbing the `large2x` rendition
    (~940px wide) and the top result regardless of size — that's why some
    long-form panels looked low-quality. Now we skip results that don't
    meet the bar and download the `original` for full resolution.
    """
    if not api_key:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": api_key}
    # size=large filter rules out tiny stock photos at the API level.
    params = {"query": query, "per_page": 15, "orientation": orientation, "size": "large"}
    try:
        r = _request("GET", PHOTO_SEARCH, headers=headers, params=params)
        photos = r.json().get("photos", [])
        if not photos:
            return None

        # Pick the first photo whose native resolution clears the bar.
        photo = None
        for p in photos:
            w = int(p.get("width") or 0)
            h = int(p.get("height") or 0)
            if w >= MIN_PHOTO_WIDTH and h >= MIN_PHOTO_HEIGHT and w * h >= MIN_PHOTO_PIXELS:
                photo = p
                break
        if photo is None:
            log().debug("Pexels: no photo >=%dx%d for %r (checked %d)",
                        MIN_PHOTO_WIDTH, MIN_PHOTO_HEIGHT, query, len(photos))
            return None

        # Prefer the native original (full resolution). Fall back to large2x
        # only if original is missing for some reason.
        src = photo["src"].get("original") or photo["src"].get("large2x") or photo["src"].get("large")
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
        log().info("  pexels photo %dx%d for %r", photo.get("width"), photo.get("height"), query[:40])
        return dest
    except Exception as e:
        log().debug("Pexels photo fetch failed for %r: %s", query, e)
        return None


MIN_VIDEO_WIDTH = 1920
MIN_VIDEO_HEIGHT = 1080


def fetch_video(api_key: str, query: str, *, out_dir: Path) -> Path | None:
    """Search Pexels videos for `query`, download a high-quality clip.

    Previously: picked any clip <=1920px wide with no minimum bar — so a
    640x360 SD result would pass and look terrible upscaled to 1080p.
    Now: requires the video AND the chosen file to be at least 1920x1080,
    and picks the closest-to-1080p file (preferring 1920x1080 over 4K to
    keep download time + storage reasonable).
    """
    if not api_key:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": api_key}
    # size=medium = "at least Full HD" per Pexels' docs — that's our floor.
    params = {"query": query, "per_page": 15, "orientation": "landscape", "size": "medium"}
    try:
        r = _request("GET", VIDEO_SEARCH, headers=headers, params=params)
        videos = r.json().get("videos", [])
        if not videos:
            return None

        # Pick the first video whose NATIVE resolution clears the bar.
        v = None
        for cand in videos:
            if int(cand.get("width") or 0) >= MIN_VIDEO_WIDTH and \
               int(cand.get("height") or 0) >= MIN_VIDEO_HEIGHT:
                v = cand
                break
        if v is None:
            log().debug("Pexels: no video >=%dx%d for %r (checked %d)",
                        MIN_VIDEO_WIDTH, MIN_VIDEO_HEIGHT, query, len(videos))
            return None

        # Among that video's renditions, pick mp4 closest to 1920x1080 from
        # at or above HD. Sorted by closeness-to-1920 width.
        mp4s = [f for f in v.get("video_files", [])
                if f.get("file_type") == "video/mp4"
                and int(f.get("width") or 0) >= MIN_VIDEO_WIDTH
                and int(f.get("height") or 0) >= MIN_VIDEO_HEIGHT]
        if not mp4s:
            log().debug("Pexels: video %s has no HD+ mp4 rendition", v.get("id"))
            return None
        chosen = min(mp4s, key=lambda f: abs(int(f.get("width") or 0) - 1920))

        slug = hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]
        dest = out_dir / f"pexels_{slug}.mp4"
        if not dest.exists():
            with httpx.stream("GET", chosen["link"], timeout=60.0, follow_redirects=True) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
        log().info("  pexels video %dx%d for %r",
                   chosen.get("width"), chosen.get("height"), query[:40])
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
