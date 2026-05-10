"""YouTube Data API v3 uploader.

Each channel uses its own OAuth refresh token, kept in YT_REFRESH_TOKENS_JSON
keyed by channel slug. The Google Cloud project shares a single client_id /
client_secret across channels.

This module exposes:
  upload_video(...) → returns the YouTube video id
  set_thumbnail(...) → uploads a thumbnail to an existing video
"""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from ..utils import log


SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def _client_for_channel(
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
):
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


class YouTubeQuotaExceeded(RuntimeError):
    """Raised when the YouTube Data API rejects a call with quotaExceeded.
    Callers should treat this as 'stop attempting more uploads today'."""


def _is_quota_exceeded(err: HttpError) -> bool:
    try:
        content = err.content.decode("utf-8", errors="ignore") if err.content else ""
    except Exception:
        content = ""
    return ("quotaExceeded" in content
            or "uploadLimitExceeded" in content
            or err.resp.status == 403 and "quota" in content.lower())


def upload_video(
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    file_path: Path,
    title: str,
    description: str,
    tags: list[str],
    category_id: str = "27",
    privacy: str = "public",
    made_for_kids: bool = False,
    publish_at: str | None = None,
) -> str:
    """Upload a video. Returns the new YouTube video id."""
    yt = _client_for_channel(
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
    )
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:30],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": "private" if publish_at else privacy,
            "selfDeclaredMadeForKids": made_for_kids,
            "madeForKids": made_for_kids,
            "embeddable": True,
            # YouTube "altered or synthetic content" disclosure flag, added
            # to the API in 2024. We disclose because (a) narration is AI
            # TTS and (b) some panels are gpt-image-1 outputs. Hiding this
            # from YouTube's own detection is the worst case for YPP
            # eligibility; declaring it upfront is the right move.
            "containsSyntheticMedia": True,
        },
    }
    if publish_at:
        body["status"]["publishAt"] = publish_at  # ISO 8601 UTC

    media = MediaFileUpload(str(file_path), chunksize=-1, resumable=True, mimetype="video/mp4")

    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log().info("  upload progress %d%%", int(status.progress() * 100))
        except HttpError as e:
            if _is_quota_exceeded(e):
                log().error("  YouTube daily quota exceeded — aborting further uploads this run")
                raise YouTubeQuotaExceeded(str(e)) from e
            log().error("  upload error: %s", e)
            raise
    video_id = response["id"]
    log().info("  uploaded → https://youtube.com/watch?v=%s", video_id)
    return video_id


def set_thumbnail(
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    video_id: str,
    thumbnail_path: Path,
) -> None:
    yt = _client_for_channel(
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
    )
    yt.thumbnails().set(
        videoId=video_id,
        media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/png"),
    ).execute()
    log().info("  thumbnail set on %s", video_id)


def post_comment(
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    video_id: str,
    text: str,
) -> str | None:
    """Post a top-level comment from the channel on its own video.

    Returns the comment thread id, or None on failure (we never want a
    failed comment to fail the run).

    NOTE: Pinning a comment requires a manual action in YouTube Studio —
    the YouTube Data API does not currently expose pinning. The comment
    will appear from the channel itself, which gets it visual prominence
    in the comments tab even before a manual pin.
    """
    try:
        yt = _client_for_channel(
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
        )
        body = {
            "snippet": {
                "videoId": video_id,
                "topLevelComment": {
                    "snippet": {"textOriginal": text[:9500]},
                },
            }
        }
        resp = yt.commentThreads().insert(part="snippet", body=body).execute()
        cid = resp.get("id")
        log().info("  posted comment %s on %s", cid, video_id)
        return cid
    except HttpError as e:
        log().warning("comment post failed for %s: %s", video_id, e)
        return None
    except Exception as e:
        log().warning("comment post failed for %s: %s", video_id, e)
        return None


def upload_caption(
    *,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    video_id: str,
    srt_path: Path,
    language: str = "en",
    name: str = "English",
) -> str | None:
    """Upload an SRT caption track for a video. Returns caption id or None."""
    try:
        yt = _client_for_channel(
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
        )
        body = {
            "snippet": {
                "videoId": video_id,
                "language": language,
                "name": name,
                "isDraft": False,
            }
        }
        media = MediaFileUpload(str(srt_path), mimetype="application/octet-stream")
        resp = yt.captions().insert(part="snippet", body=body, media_body=media).execute()
        cid = resp.get("id")
        log().info("  uploaded caption track %s on %s", cid, video_id)
        return cid
    except HttpError as e:
        log().warning("caption upload failed for %s: %s", video_id, e)
        return None
    except Exception as e:
        log().warning("caption upload failed for %s: %s", video_id, e)
        return None
