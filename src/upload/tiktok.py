"""TikTok Content Posting API upload (Direct Post mode).

Three-step flow:
  1. POST /v2/post/publish/video/init/    → upload_url + publish_id
  2. PUT  <upload_url>                    → MP4 bytes (single chunk)
  3. POST /v2/post/publish/status/fetch/  → poll until PUBLISH_COMPLETE

Auth: OAuth 2.0 with a long-lived refresh token (≈365 days). The refresh
token is exchanged for a short-lived access token on every upload.

Sandbox / unaudited apps are forced to `privacy_level=SELF_ONLY` regardless
of what we request — public posting unlocks only after TikTok app audit.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..utils import log


TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

DEFAULT_PRIVACY = "SELF_ONLY"   # pre-audit lock; switch to PUBLIC_TO_EVERYONE post-audit
POLL_INTERVAL_SEC = 5
POLL_MAX_ATTEMPTS = 60          # 5 min max


class TikTokError(RuntimeError):
    """Raised when TikTok's API returns a non-recoverable error.
    Callers should catch + log + continue (do not fail the whole run)."""


# -----------------------------------------------------------------------------
# OAuth helpers
# -----------------------------------------------------------------------------

def exchange_code_for_tokens(
    *, client_key: str, client_secret: str, code: str, redirect_uri: str,
) -> dict:
    """One-time: exchange an authorization code (from the consent redirect)
    for an access_token + refresh_token. Used by cli.tiktok_oauth_init."""
    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise TikTokError(f"code exchange failed: {data}")
    return data


def refresh_access_token(
    *, client_key: str, client_secret: str, refresh_token: str,
) -> dict:
    """Trade the long-lived refresh_token for a fresh access_token."""
    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise TikTokError(f"refresh_token exchange failed: {data}")
    return data


# -----------------------------------------------------------------------------
# Upload (Direct Post)
# -----------------------------------------------------------------------------

@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=15), reraise=True)
def upload_video(
    *,
    client_key: str,
    client_secret: str,
    refresh_token: str,
    file_path: Path,
    caption: str,
    privacy_level: str = DEFAULT_PRIVACY,
    disable_duet: bool = False,
    disable_comment: bool = False,
    disable_stitch: bool = False,
) -> str:
    """Direct-post a vertical MP4 to the authenticated user's TikTok profile.

    Returns the TikTok publish_id once status reaches PUBLISH_COMPLETE.
    Raises TikTokError on any non-recoverable failure.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise TikTokError(f"file not found: {file_path}")
    file_size = file_path.stat().st_size
    if file_size <= 0:
        raise TikTokError(f"file is empty: {file_path}")

    # Fresh access token (refresh_token reused indefinitely until rotated)
    tokens = refresh_access_token(
        client_key=client_key,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )
    access_token = tokens["access_token"]

    # ---- 1. init upload ----
    init_body = {
        "post_info": {
            "title": (caption or "")[:2200],
            "privacy_level": privacy_level,
            "disable_duet": disable_duet,
            "disable_comment": disable_comment,
            "disable_stitch": disable_stitch,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": file_size,    # single-chunk upload (≤64 MB shorts)
            "total_chunk_count": 1,
        },
    }
    init_resp = httpx.post(
        INIT_URL,
        json=init_body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        timeout=60.0,
    )
    if init_resp.status_code != 200:
        raise TikTokError(f"init failed [{init_resp.status_code}]: {init_resp.text}")
    init_data = init_resp.json().get("data", {}) or {}
    upload_url = init_data.get("upload_url")
    publish_id = init_data.get("publish_id")
    if not upload_url or not publish_id:
        raise TikTokError(f"init missing upload_url/publish_id: {init_resp.text}")
    log().info("  tiktok init OK publish_id=%s size=%dMB",
               publish_id, max(1, file_size // 1_000_000))

    # ---- 2. PUT bytes ----
    with file_path.open("rb") as f:
        bytes_data = f.read()
    put_resp = httpx.put(
        upload_url,
        content=bytes_data,
        headers={
            "Content-Type": "video/mp4",
            "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
        },
        timeout=300.0,
    )
    if put_resp.status_code not in (200, 201):
        raise TikTokError(f"upload bytes failed [{put_resp.status_code}]: {put_resp.text[:500]}")
    log().info("  tiktok upload bytes OK")

    # ---- 3. poll for PUBLISH_COMPLETE ----
    for attempt in range(POLL_MAX_ATTEMPTS):
        time.sleep(POLL_INTERVAL_SEC)
        s_resp = httpx.post(
            STATUS_URL,
            json={"publish_id": publish_id},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            timeout=30.0,
        )
        if s_resp.status_code != 200:
            log().warning("  tiktok status check transient fail [%d]: %s",
                          s_resp.status_code, s_resp.text[:200])
            continue
        s_data = (s_resp.json().get("data") or {})
        status = s_data.get("status")
        if attempt == 0 or attempt % 4 == 0:
            log().info("  tiktok status: %s", status)
        if status == "PUBLISH_COMPLETE":
            log().info("  tiktok publish complete: %s", publish_id)
            return publish_id
        if status in ("FAILED", "PUBLISH_FAILED"):
            raise TikTokError(f"publish failed: {s_data}")

    raise TikTokError(f"publish timed out after {POLL_MAX_ATTEMPTS * POLL_INTERVAL_SEC}s")
