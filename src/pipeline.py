"""End-to-end orchestrator.

Daily run, per enabled channel:
  - Pick top N records, render long-form + paired Short for each.
  - Pick the next M records, render standalone Shorts (no long-form parent).
"""

from __future__ import annotations

import json
import re
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
from .maps import location_from_record, map_for_location
from .render import SceneAsset, assemble_video, panels_needed_for
from .score import score_pending
from .script import write_script
from .shorts import make_short, make_standalone_short
from .thumbnail import make_thumbnail
from .transcribe import merge_srt
from .upload.youtube import (
    YouTubeQuotaExceeded,
    post_comment,
    set_thumbnail,
    upload_caption,
    upload_video,
)
from .utils import ffprobe_duration, log, setup_logging, slugify
from .voice import VoicedScene, render_voiceover


def run_daily() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)

    log().info("=== Docket daily run @ %s ===", datetime.now(timezone.utc).isoformat())
    if settings.dry_run:
        log().info("DRY-RUN: will produce video but skip YouTube upload.")

    with connect(settings.db_path) as conn:
        run_id = open_run(conn)
        summary = {"ingested": {}, "scored": 0, "produced": [], "shorts_only": [], "errors": []}

        try:
            summary["ingested"] = ingest.run_all_for_channels(settings, conn)
            summary["scored"] = score_pending(settings, conn)

            for channel in settings.channels:
                if not channel.enabled:
                    continue

                # ---- Long-form loop (each spawns a paired Short) ----
                produced = 0
                quota_exhausted = False
                for n in range(channel.videos_per_run):
                    try:
                        result = produce_one_for_channel(settings, conn, channel, slot=n)
                        if result is None:
                            log().info("[%s] no more eligible records (long slot %d)",
                                       channel.slug, n)
                            break
                        summary["produced"].append(result)
                        produced += 1
                    except YouTubeQuotaExceeded as e:
                        log().warning("[%s] YouTube quota exhausted at long slot %d — "
                                      "aborting further uploads this run", channel.slug, n)
                        summary["errors"].append(
                            {"channel": channel.slug, "slot": n, "error": "quota_exceeded"})
                        quota_exhausted = True
                        break
                    except Exception as e:
                        log().exception("[%s] long slot %d failed: %s", channel.slug, n, e)
                        summary["errors"].append({"channel": channel.slug, "slot": n, "error": str(e)})

                # ---- Extra standalone Shorts ----
                extra_shorts = max(0, channel.shorts_per_run - produced)
                if quota_exhausted:
                    log().warning("[%s] skipping %d standalone Shorts — quota exhausted",
                                  channel.slug, extra_shorts)
                    extra_shorts = 0
                elif channel.shorts_per_run and channel.make_shorts:
                    log().info("[%s] producing %d standalone Shorts (target=%d, longs=%d)",
                               channel.slug, extra_shorts, channel.shorts_per_run, produced)
                for n in range(extra_shorts):
                    try:
                        result = produce_standalone_short_for_channel(
                            settings, conn, channel, slot=n,
                        )
                        if result is None:
                            log().info("[%s] no more eligible records (short slot %d)",
                                       channel.slug, n)
                            break
                        summary["shorts_only"].append(result)
                    except YouTubeQuotaExceeded as e:
                        log().warning("[%s] YouTube quota exhausted at short slot %d — "
                                      "aborting", channel.slug, n)
                        summary["errors"].append(
                            {"channel": channel.slug, "short_slot": n, "error": "quota_exceeded"})
                        break
                    except Exception as e:
                        log().exception("[%s] short slot %d failed: %s", channel.slug, n, e)
                        summary["errors"].append(
                            {"channel": channel.slug, "short_slot": n, "error": str(e)})

            status = "ok" if not summary["errors"] else "partial"
            close_run(conn, run_id, status, json.dumps(summary, default=str))
        except Exception as e:
            log().exception("Run failed: %s", e)
            close_run(conn, run_id, "failed",
                      json.dumps({"fatal": str(e), "tb": traceback.format_exc()}))
            raise

    log().info("=== Run complete: %d longs + %d standalone shorts ===",
               len(summary["produced"]), len(summary["shorts_only"]))


# =============================================================================
# Long-form (with paired short)
# =============================================================================

def produce_one_for_channel(
    settings: Settings,
    conn: sqlite3.Connection,
    channel: Channel,
    slot: int = 0,
) -> dict | None:
    log().info("[%s] long slot %d — selecting top record …", channel.slug, slot)
    candidates = top_records_for_channel(
        conn, channel_slug=channel.slug, limit=5, min_total=18,
        sources=channel.sources or None,
    )
    if not candidates:
        return None

    record, score = candidates[0]
    log().info(
        "[%s] selected record %s (%s) — drama=%s novelty=%s vis=%s — %r",
        channel.slug, record.id, record.source,
        score.drama, score.novelty, score.visualization, record.title[:80],
    )

    if score.flags.get("sealed") or score.flags.get("minor_involved") or score.flags.get("tragedy_only"):
        log().info("[%s] record %s flagged: %s — skipping", channel.slug, record.id, score.flags)
        prod_id = create_production(conn, record_id=record.id, channel_slug=channel.slug)
        mark_production_failed(conn, prod_id, f"flagged: {json.dumps(score.flags)}")
        return None

    return _produce_long_form(settings, conn, channel, record, score)


def _produce_long_form(
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
        # SCRIPT
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

        # VOICE + per-scene SRT
        voiced = render_voiceover(
            api_key=settings.openai_api_key,
            script=script,
            voice=channel.voice,
            speed=channel.voice_speed,
            out_dir=work_dir / "audio",
        )
        if not voiced:
            raise RuntimeError("voiceover produced no audio")
        narration_scenes = [s for s in script.scenes if s.narration.strip()]

        # Aircraft / vehicle context
        record_context = _record_context(record)
        log().info("  record context: %s", record_context or "(none)")

        # Location map (inserted in setup scene)
        location_str = location_from_record(record.raw_text, _safe_json(record.raw_json))
        location_map: Path | None = None
        if location_str:
            location_map = map_for_location(location_str, out_dir=work_dir / "visuals" / "map")

        # VISUALS
        visuals_dir = work_dir / "visuals"
        scene_assets: list[SceneAsset] = []
        for i, scene in enumerate(narration_scenes):
            vs = voiced[i] if i < len(voiced) else voiced[-1]
            scene_seconds = ffprobe_duration(vs.audio_path)
            n_panels = panels_needed_for(scene_seconds)

            visuals = _fetch_panel_visuals(
                settings,
                scene_b_roll=scene.b_roll or scene.narration[:200],
                scene_label=scene.label or scene.id,
                n_panels=n_panels,
                out_dir=visuals_dir / f"scene_{i:02d}",
                scene_id=scene.id,
                record_context=record_context,
            )
            if scene.id == "setup" and location_map and location_map.exists():
                visuals = [location_map] + visuals[:max(1, n_panels - 1)]
            if not visuals:
                raise RuntimeError(f"no visuals for scene {scene.id}")

            scene_assets.append(SceneAsset(
                audio_path=vs.audio_path,
                visuals=visuals,
                title_overlay=scene.label or scene.id,
                scene_id=scene.id,
                srt_text=vs.srt_text,
            ))

        # RENDER LONG-FORM
        video_path = work_dir / "video.mp4"
        assemble_video(settings, channel=channel, scene_assets=scene_assets, out_path=video_path)
        update_production(conn, prod_id, status="rendered", video_path=str(video_path))

        master_srt = _master_srt(scene_assets, channel)
        master_srt_path = work_dir / "captions.srt"
        if master_srt:
            master_srt_path.write_text(master_srt, encoding="utf-8")

        actual_description = _description_with_timestamps(
            base_description=script.description,
            scenes=narration_scenes,
            voiced=voiced,
            channel=channel,
        )

        # THUMBNAIL
        thumb_path = work_dir / "thumbnail.png"
        make_thumbnail(
            anthropic_key=settings.anthropic_api_key,
            openai_key=settings.openai_api_key,
            channel=channel,
            video_title=script.title,
            summary=score.summary,
            out_path=thumb_path,
        )

        # PAIRED SHORT
        short_path: Path | None = None
        if channel.make_shorts:
            try:
                short_path = work_dir / "short.mp4"
                make_short(
                    anthropic_key=settings.anthropic_api_key,
                    openai_key=settings.openai_api_key,
                    scenes=scene_assets,
                    long_form_title=script.title,
                    long_form_narration=script.full_narration,
                    channel=channel,
                    out_path=short_path,
                )
            except Exception as e:
                log().warning("[%s] short render failed (continuing): %s", channel.slug, e)
                short_path = None

        # UPLOAD
        if settings.dry_run:
            log().info("[%s] DRY-RUN: skipping upload. Output: %s", channel.slug, video_path)
            update_production(conn, prod_id, status="rendered", thumbnail_path=str(thumb_path))
            return {
                "channel": channel.slug, "title": script.title,
                "video_path": str(video_path),
                "short_path": str(short_path) if short_path else None,
                "uploaded": False, "kind": "long",
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
            description=_with_source_block(actual_description, record),
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
            log().warning("thumbnail upload failed: %s", e)

        if master_srt and master_srt_path.exists():
            upload_caption(
                refresh_token=refresh_token,
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
                video_id=long_video_id,
                srt_path=master_srt_path,
            )

        post_comment(
            refresh_token=refresh_token,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            video_id=long_video_id,
            text=_discussion_comment(record, channel),
        )

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
                    description=_short_description(script.title, long_video_id, record, channel),
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
            "kind": "long",
        }

    except Exception as e:
        mark_production_failed(conn, prod_id, str(e))
        raise


# =============================================================================
# Standalone Shorts (no long-form parent)
# =============================================================================

def produce_standalone_short_for_channel(
    settings: Settings,
    conn: sqlite3.Connection,
    channel: Channel,
    slot: int = 0,
) -> dict | None:
    log().info("[%s] short slot %d — selecting top record …", channel.slug, slot)
    candidates = top_records_for_channel(
        conn, channel_slug=channel.slug, limit=5, min_total=15,
        sources=channel.sources or None,
    )
    if not candidates:
        return None

    record, score = candidates[0]
    log().info(
        "[%s] standalone short — record %s (%s) — total=%s — %r",
        channel.slug, record.id, record.source,
        score.drama + score.novelty + score.visualization, record.title[:80],
    )

    if score.flags.get("sealed") or score.flags.get("minor_involved") or score.flags.get("tragedy_only"):
        log().info("[%s] record %s flagged: %s — skipping", channel.slug, record.id, score.flags)
        prod_id = create_production(conn, record_id=record.id, channel_slug=channel.slug)
        mark_production_failed(conn, prod_id, f"flagged: {json.dumps(score.flags)}")
        return None

    return _produce_standalone_short(settings, conn, channel, record, score)


def _produce_standalone_short(
    settings: Settings,
    conn: sqlite3.Connection,
    channel: Channel,
    record: Record,
    score: Score,
) -> dict | None:
    prod_id = create_production(conn, record_id=record.id, channel_slug=channel.slug)
    work_dir = settings.output_dir / channel.slug / f"short_{prod_id:06d}_{slugify(record.title, max_len=50)}"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        record_context = _record_context(record)
        short_path = work_dir / "short.mp4"
        make_standalone_short(
            settings=settings,
            record=record,
            channel=channel,
            record_context=record_context,
            work_dir=work_dir,
            out_path=short_path,
        )
        update_production(conn, prod_id, status="rendered", video_path=str(short_path))

        if settings.dry_run:
            log().info("[%s] DRY-RUN: skipping short upload. Output: %s", channel.slug, short_path)
            return {
                "channel": channel.slug, "title": _short_title_from_record(record),
                "short_path": str(short_path), "uploaded": False, "kind": "short",
            }

        refresh_token = settings.yt_refresh_tokens.get(channel.slug)
        if not refresh_token:
            raise RuntimeError(f"no YT refresh token for channel {channel.slug}")

        short_title = _shorts_title(_short_title_from_record(record))
        short_video_id = upload_video(
            refresh_token=refresh_token,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            file_path=short_path,
            title=short_title,
            description=_short_only_description(record, channel),
            tags=_short_tags_for_record(record, channel),
            category_id=str(channel.youtube.get("category_id", "27")),
            privacy=channel.youtube.get("privacy", "public"),
        )

        mark_production_complete(
            conn, prod_id,
            youtube_video_id=short_video_id,
            video_path=str(short_path),
            thumbnail_path="",
        )
        return {
            "channel": channel.slug,
            "title": short_title,
            "short_id": short_video_id,
            "short_url": f"https://youtube.com/shorts/{short_video_id}",
            "kind": "short",
        }

    except Exception as e:
        mark_production_failed(conn, prod_id, str(e))
        raise


# =============================================================================
# Helpers
# =============================================================================

_AIRCRAFT_PREFER_SCENES = {"hook", "setup", "incident", "investigation"}


def _record_context(record: Record) -> str:
    raw = _safe_json(record.raw_json)
    if isinstance(raw, dict):
        make = (raw.get("VehicleMake") or "").strip()
        model = (raw.get("VehicleModel") or "").strip()
        if make and model:
            return f"{make} {model}".strip()
        if model:
            return model
        if make:
            return make
    return ""


def _fetch_panel_visuals(
    settings: Settings,
    *,
    scene_b_roll: str,
    scene_label: str,
    n_panels: int,
    out_dir: Path,
    scene_id: str,
    record_context: str = "",
) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()

    base = scene_b_roll.strip()
    candidates: list[str] = []
    for part in re.split(r"[;,]| and ", base, flags=re.I):
        part = part.strip()
        if part and part not in candidates:
            candidates.append(part)
    if not candidates:
        candidates = [base or scene_label]

    use_context = record_context and scene_id in _AIRCRAFT_PREFER_SCENES
    if use_context:
        candidates = [f"{record_context} {q}" for q in candidates] + candidates

    while len(candidates) < n_panels:
        candidates.append(f"{candidates[len(candidates) % len(candidates)]} {scene_label}")

    prefer_video_for = {"hook", "incident", "scheme", "allegations", "investigation", "tell"}

    for i in range(n_panels):
        query = candidates[i % len(candidates)]
        v = fetch_visual(
            settings,
            b_roll_prompt=query,
            out_dir=out_dir,
            prefer_video=(scene_id in prefer_video_for and i % 3 == 0),
            allow_ai=True,
        )
        if v is None:
            continue
        if str(v) in seen:
            continue
        seen.add(str(v))
        out.append(v)

    return out


def _master_srt(scene_assets: list[SceneAsset], channel: Channel) -> str:
    intro_offset = 0.0
    intro = (Path(__file__).resolve().parent.parent
             / "assets" / "intros" / (channel.intro_sting or ""))
    if channel.intro_sting and intro.exists():
        try:
            intro_offset = ffprobe_duration(intro)
        except Exception:
            intro_offset = 0.0

    pieces: list[tuple[str, float]] = []
    cursor = intro_offset
    for sa in scene_assets:
        if sa.srt_text.strip():
            pieces.append((sa.srt_text, cursor))
        try:
            cursor += ffprobe_duration(sa.audio_path)
        except Exception:
            cursor += 30.0
    return merge_srt(pieces)


def _description_with_timestamps(
    *,
    base_description: str,
    scenes: list,
    voiced: list[VoicedScene],
    channel: Channel,
) -> str:
    intro_offset = 0.0
    intro = (Path(__file__).resolve().parent.parent
             / "assets" / "intros" / (channel.intro_sting or ""))
    if channel.intro_sting and intro.exists():
        try:
            intro_offset = ffprobe_duration(intro)
        except Exception:
            intro_offset = 0.0

    lines = ["", "— Chapters —"]
    cursor = intro_offset
    for i, scene in enumerate(scenes):
        label = getattr(scene, "label", "") or scene.id
        lines.append(f"{_format_timestamp(cursor)} {label}")
        try:
            vs = voiced[i] if i < len(voiced) else voiced[-1]
            cursor += ffprobe_duration(vs.audio_path)
        except Exception:
            cursor += 30.0
    return (base_description or "").rstrip() + "\n".join(lines)


def _format_timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _shorts_title(long_title: str) -> str:
    base = long_title.strip()
    if len(base) > 90:
        base = base[:87].rstrip() + "…"
    if "#shorts" in base.lower():
        return base[:100]
    return f"{base} #Shorts"[:100]


def _short_description(long_title: str, long_video_id: str, record: Record, channel: Channel) -> str:
    brand = channel.brand_name or channel.name
    return (
        f"Full video: https://youtube.com/watch?v={long_video_id}\n\n"
        f"Subscribe to {brand} for daily public-record breakdowns.\n\n"
        f"Source: {record.title}\n{record.url}\n"
        f"Published: {record.published_at}\n\n"
        "#Shorts"
    )


def _short_only_description(record: Record, channel: Channel) -> str:
    brand = channel.brand_name or channel.name
    return (
        f"Subscribe to {brand} for daily public-record breakdowns.\n\n"
        f"Source: {record.title}\n{record.url}\n"
        f"Published: {record.published_at}\n\n"
        "#Shorts"
    )


def _short_title_from_record(record: Record) -> str:
    """Build a 60-90 char title from the raw record. Standalone shorts don't
    get a Claude-generated title (saves a Sonnet call)."""
    base = (record.title or "Public record breakdown").strip()
    return base[:88].rstrip()


def _short_tags_for_record(record: Record, channel: Channel) -> list[str]:
    base_tags = list(channel.youtube.get("tags") or [])
    src = record.source
    if src.startswith("ntsb"):
        base_tags.extend(["aviation", "NTSB", "plane crash"])
    elif src == "sec":
        base_tags.extend(["securities fraud", "SEC enforcement", "finance"])
    elif src in ("courtlistener", "doj"):
        base_tags.extend(["federal court", "indictment", "true crime"])
    base_tags.append("shorts")
    # Dedupe preserving order
    seen, out = set(), []
    for t in base_tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:30]


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


def _discussion_comment(record: Record, channel: Channel) -> str:
    brand = channel.brand_name or channel.name
    return (
        f"What's the bigger lesson here — procedure failure, regulatory blind "
        f"spot, or accountability gap? Drop your take.\n\n"
        f"📄 Source: {record.url}\n"
        f"🔔 Subscribe to {brand} — new case files daily."
    )


def _safe_json(raw_json: str | None) -> dict:
    if not raw_json:
        return {}
    try:
        return json.loads(raw_json)
    except Exception:
        return {}
