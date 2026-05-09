"""End-to-end orchestrator.

Daily run:
  1. Ingest from every source needed by enabled channels.
  2. Score every unscored record with Haiku.
  3. For each enabled channel, produce up to `videos_per_run` videos. Each
     video uses a different top-scored record. After each long-form video
     uploads, optionally generate and upload a 9:16 Short.

Idempotent. Reruns won't double-publish a record on a channel.
"""

from __future__ import annotations

import json
import sqlite3
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import ingest
from .config import Channel, Settings, load_settings
from .db import (
    Record, Score, close_run, connect, create_production, mark_production_complete,
    mark_production_failed, open_run, top_records_for_channel, update_production,
)
from .images import fetch_visual
from .render import SceneAsset, assemble_video
from .score import score_pending
from .script import write_script
from .shorts import make_short
from .thumbnail import make_thumbnail
from .upload.youtube import set_thumbnail, upload_video
from .utils import log, setup_logging, slugify
from .voice import render_voiceover


def run_daily() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)

    log().info("=== Docket daily run @ %s ===", datetime.now(timezone.utc).isoformat())
    if settings.dry_run:
        log().info("DRY-RUN: will produce video but skip YouTube upload.")

    with connect(settings.db_path) as conn:
        run_id = open_run(conn)
        summary = {"ingested": {}, "scored": 0, "produced": [], "errors": []}

        try:
            summary["ingested"] = ingest.run_all_for_channels(settings, conn)
            summary["scored"] = score_pending(settings, conn)

            global_produced = 0
            for channel in settings.channels:
                if not channel.enabled:
                    continue
                target = channel.videos_per_run

                for n in range(target):
                    if global_produced >= settings.max_videos_per_run:
                        log().info("Hit DOCKET_MAX_VIDEOS_PER_RUN cap (%s)", settings.max_videos_per_run)
                        break
                    try:
                        result = produce_one_for_channel(settings, conn, channel, slot=n)
                        if result is None:
                            log().info("[%s] no more eligible records (slot %d/%d)",
                                       channel.slug, n, target)
                            break
                        summary["produced"].append(result)
                        global_produced += 1
                    except Exception as e:
                        log().exception("[%s] slot %d failed: %s", channel.slug, n, e)
                        summary["errors"].append({"channel": channel.slug, "slot": n, "error": str(e)})

            status = "ok" if not summary["errors"] else "partial"
            close_run(conn, run_id, status, json.dumps(summary, default=str))
        except Exception as e:
            log().exception("Run failed: %s", e)
            close_run(conn, run_id, "failed", json.dumps({"fatal": str(e), "tb": traceback.format_exc()}))
            raise

    log().info("=== Run complete: %d produced ===", len(summary["produced"]))


def produce_one_for_channel(
    settings: Settings,
    conn: sqlite3.Connection,
    channel: Channel,
    slot: int = 0,
) -> dict | None:
    """Pick the top-not-yet-produced record for `channel` and produce a long
    video (and optional short). Returns a summary dict or None if no record."""
    log().info("[%s] selecting top record (slot %d) …", channel.slug, slot)
    candidates = top_records_for_channel(conn, channel_slug=channel.slug, limit=5, min_total=18)
    if not candidates:
        return None

    record, score = candidates[0]
    log().info(
        "[%s] selected record %s — drama=%s novelty=%s vis=%s — %r",
        channel.slug, record.id, score.drama, score.novelty, score.visualization, record.title[:80],
    )

    if score.flags.get("sealed") or score.flags.get("minor_involved") or score.flags.get("tragedy_only"):
        log().info("[%s] record %s flagged: %s — skipping (will not retry)",
                   channel.slug, record.id, score.flags)
        # Insert a failed production so we don't pick this record again.
        prod_id = create_production(conn, record_id=record.id, channel_slug=channel.slug)
        mark_production_failed(conn, prod_id, f"flagged: {json.dumps(score.flags)}")
        return None

    return _produce_pipeline(settings, conn, channel, record, score)


def _produce_pipeline(
    settings: Settings,
    conn: sqlite3.Connection,
    channel: Channel,
    record: Record,
    score: Score,
) -> dict | None:
    prod_id = create_production(conn, record_id=record.id, channel_slug=channel.slug)
    work_dir = settings.output_dir / channel.slug / f"prod_{prod_id:06d}_{slugify(record.title, max_len=60)}"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ---- 1. SCRIPT ----
        script = write_script(settings.anthropic_api_key, channel, record)
        if script.refused:
            log().info("[%s] writer refused: %s", channel.slug, script.refused_reason)
            mark_production_failed(conn, prod_id, f"refused: {script.refused_reason}")
            return None
        script_path = work_dir / "script.json"
        script_path.write_text(
            json.dumps({
                "title": script.title,
                "description": script.description,
                "tags": script.tags,
                "scenes": [s.__dict__ for s in script.scenes],
            }, indent=2),
            encoding="utf-8",
        )
        update_production(conn, prod_id, status="scripted", script_path=str(script_path))

        # ---- 2. VOICE ----
        audio_paths = render_voiceover(
            api_key=settings.openai_api_key,
            script=script,
            voice=channel.voice,
            speed=channel.voice_speed,
            out_dir=work_dir / "audio",
        )
        if not audio_paths:
            raise RuntimeError("voiceover produced no audio")
        update_production(conn, prod_id, status="voiced", audio_path=str(audio_paths[0].parent))

        # ---- 3. VISUALS ----
        visuals_dir = work_dir / "visuals"
        scene_assets: list[SceneAsset] = []
        for i, scene in enumerate(script.scenes):
            audio_p = audio_paths[i] if i < len(audio_paths) else audio_paths[-1]
            visual_p = fetch_visual(
                settings,
                b_roll_prompt=scene.b_roll or scene.narration[:200],
                out_dir=visuals_dir,
                prefer_video=(scene.id in {"hook", "the-incident", "the-allegations", "the-scheme"}),
                allow_ai=True,
            )
            if visual_p is None:
                raise RuntimeError(f"no visual found for scene {scene.id}")
            scene_assets.append(SceneAsset(audio_path=audio_p, visual_path=visual_p,
                                           title_overlay=scene.id))

        # ---- 4. RENDER LONG-FORM ----
        video_path = work_dir / "video.mp4"
        assemble_video(settings, channel=channel, scene_assets=scene_assets, out_path=video_path)
        update_production(conn, prod_id, status="rendered", video_path=str(video_path))

        # ---- 5. THUMBNAIL ----
        thumb_path = work_dir / "thumbnail.png"
        make_thumbnail(
            anthropic_key=settings.anthropic_api_key,
            openai_key=settings.openai_api_key,
            channel=channel,
            video_title=script.title,
            summary=score.summary,
            out_path=thumb_path,
        )

        # ---- 6. SHORT (optional) ----
        short_path: Path | None = None
        if channel.make_shorts:
            try:
                short_path = work_dir / "short.mp4"
                make_short(
                    scenes=scene_assets,
                    long_form_title=script.title,
                    channel=channel,
                    out_path=short_path,
                )
            except Exception as e:
                log().warning("[%s] short render failed (continuing): %s", channel.slug, e)
                short_path = None

        # ---- 7. UPLOAD ----
        if settings.dry_run:
            log().info("[%s] DRY-RUN: skipping upload. Output: %s", channel.slug, video_path)
            update_production(conn, prod_id, status="rendered", thumbnail_path=str(thumb_path))
            return {
                "channel": channel.slug, "title": script.title,
                "video_path": str(video_path),
                "short_path": str(short_path) if short_path else None,
                "uploaded": False,
            }

        refresh_token = settings.yt_refresh_tokens.get(channel.slug)
        if not refresh_token:
            raise RuntimeError(f"no YT refresh token for channel {channel.slug}")

        long_video_id = upload_video(
            refresh_token=refresh_token,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            file_path=video_path,
            title=script.title,
            description=_with_source_block(script.description, record),
            tags=script.tags or channel.youtube.get("tags", []),
            category_id=str(channel.youtube.get("category_id", "27")),
            privacy=channel.youtube.get("privacy", "public"),
        )
        try:
            set_thumbnail(
                refresh_token=refresh_token,
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
                video_id=long_video_id,
                thumbnail_path=thumb_path,
            )
        except Exception as e:
            log().warning("thumbnail upload failed (channel needs verification?): %s", e)

        # Upload the Short as its own video. YouTube auto-classifies vertical
        # videos under 60s as Shorts, which routes them through the Shorts feed.
        short_video_id: str | None = None
        if short_path and short_path.exists():
            try:
                short_title = _shorts_title(script.title)
                short_video_id = upload_video(
                    refresh_token=refresh_token,
                    client_id=settings.google_client_id,
                    client_secret=settings.google_client_secret,
                    file_path=short_path,
                    title=short_title,
                    description=_short_description(script.title, long_video_id, record),
                    tags=(script.tags or [])[:10] + ["shorts"],
                    category_id=str(channel.youtube.get("category_id", "27")),
                    privacy=channel.youtube.get("privacy", "public"),
                )
            except Exception as e:
                log().warning("[%s] short upload failed: %s", channel.slug, e)

        mark_production_complete(
            conn, prod_id,
            youtube_video_id=long_video_id,
            video_path=str(video_path),
            thumbnail_path=str(thumb_path),
        )
        return {
            "channel": channel.slug,
            "title": script.title,
            "video_id": long_video_id,
            "url": f"https://youtube.com/watch?v={long_video_id}",
            "short_id": short_video_id,
            "short_url": f"https://youtube.com/shorts/{short_video_id}" if short_video_id else None,
        }

    except Exception as e:
        mark_production_failed(conn, prod_id, str(e))
        raise


def _shorts_title(long_title: str) -> str:
    base = long_title.strip()
    if len(base) > 90:
        base = base[:87].rstrip() + "…"
    if "#shorts" in base.lower():
        return base[:100]
    return f"{base} #Shorts"[:100]


def _short_description(long_title: str, long_video_id: str, record: Record) -> str:
    return (
        f"Full video: https://youtube.com/watch?v={long_video_id}\n\n"
        f"Source: {record.title}\n{record.url}\n"
        f"Published: {record.published_at}\n\n"
        "#Shorts #Aviation #NTSB"
    )


def _with_source_block(description: str, record: Record) -> str:
    src_block = (
        "\n\n— Source —\n"
        f"{record.title}\n"
        f"{record.url}\n"
        f"Published: {record.published_at}\n\n"
        "All facts in this video are drawn from the public record cited above. "
        "Assertions about uncharged or unconvicted parties are characterized as "
        "allegations, consistent with the underlying source. Corrections welcome "
        "in the comments."
    )
    return (description or "")[:4500] + src_block
