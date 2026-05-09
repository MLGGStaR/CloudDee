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
