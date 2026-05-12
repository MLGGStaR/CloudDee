"""End-to-end orchestrator.

Daily run, per enabled channel:
  - Pick top N records, render long-form + paired Short for each.
  - Pick the next M records, render standalone Shorts (no long-form parent).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import ingest
from .config import Channel, Settings, load_settings
from .db import (
    Record, Score, close_run, connect, create_production, latest_longform_for_channel,
    mark_production_complete, mark_production_failed, open_run,
    top_records_for_channel, update_production,
)
from .images import fetch_visual
from .maps import location_from_record, map_for_location
from .render import SceneAsset, assemble_video, panels_needed_for
from .score import score_pending
from .script import write_script
from .shorts import make_short, make_standalone_short
from .thumbnail import make_thumbnail
from .transcribe import merge_srt
from .upload.tiktok import TikTokError, upload_video as tiktok_upload_video
from .upload.youtube import (
    YouTubeQuotaExceeded,
    post_comment,
    set_thumbnail,
    upload_caption,
    upload_video,
)
from .utils import ffprobe_duration, log, setup_logging, slugify
from .voice import VoicedScene, render_voiceover


def _override_int(env_name: str) -> int | None:
    """Read an integer override from an env var. Treats empty/missing/0 as None."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if v >= 0 else None


def _pick_voice(channel: Channel, seed: str) -> str:
    """Deterministically pick a TTS voice from the channel's rotation pool.

    Same `seed` (e.g. record.id) → same voice every time, so re-renders of
    the same record use the same voice and a paired Short matches its
    parent long-form. Falls back to `channel.voice` if no pool is set.
    """
    pool = list(channel.voices or [])
    if not pool:
        return channel.voice
    h = int(hashlib.sha1(str(seed).encode("utf-8")).hexdigest(), 16)
    return pool[h % len(pool)]


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
                # Allow per-run overrides via env vars (workflow_dispatch inputs)
                # so we can do small test runs (e.g. 1 long + 1 short) without
                # editing the channel config.
                override_longs = _override_int("DOCKET_OVERRIDE_LONGS")
                override_shorts = _override_int("DOCKET_OVERRIDE_SHORTS")
                n_longs = override_longs if override_longs is not None else channel.videos_per_run
                n_shorts = override_shorts if override_shorts is not None else channel.shorts_per_run
                if override_longs is not None or override_shorts is not None:
                    log().info("[%s] override: longs=%d shorts=%d",
                               channel.slug, n_longs, n_shorts)

                produced = 0
                quota_exhausted = False
                for n in range(n_longs):
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
                extra_shorts = max(0, n_shorts - produced)
                if quota_exhausted:
                    log().warning("[%s] skipping %d standalone Shorts — quota exhausted",
                                  channel.slug, extra_shorts)
                    extra_shorts = 0
                elif n_shorts and channel.make_shorts:
                    log().info("[%s] producing %d standalone Shorts (target=%d, longs=%d)",
                               channel.slug, extra_shorts, n_shorts, produced)
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
        # Pick a voice from the rotation pool deterministically by record id
        # — so re-renders of the same record always use the same voice, and
        # so the paired Short shares its parent's voice.
        picked_voice = _pick_voice(channel, record.id)
        log().info("[%s] voice=%s (record %s)",
                   channel.slug, picked_voice, record.id)
        voiced = render_voiceover(
            api_key=settings.openai_api_key,
            script=script,
            voice=picked_voice,
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
                    voice_override=picked_voice,    # match long-form's rotation pick
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
                # Post a channel-owner comment with the long-form link. Pinning
                # via API isn't supported, but channel-owner comments are
                # auto-highlighted by YouTube at the top of the comments tab,
                # which is the most visible non-Studio placement available.
                post_comment(
                    refresh_token=refresh_token,
                    client_id=settings.google_client_id,
                    client_secret=settings.google_client_secret,
                    video_id=short_video_id,
                    text=_short_link_comment(long_video_id, script.title),
                )
                # Mirror to TikTok if configured.
                _try_tiktok_upload(
                    settings,
                    file_path=short_path,
                    caption=_tiktok_caption(script.title, record, channel),
                    label="paired-short",
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
    log().info("[%s] short slot %d — selecting candidates …", channel.slug, slot)
    # Pull a wider candidate pool so we can skip past flagged or write-failed
    # records without prematurely declaring "no more eligible records". The
    # previous code only tried candidates[0]; if that record was flagged or
    # the script writer returned non-JSON, the whole slot died.
    candidates = top_records_for_channel(
        conn, channel_slug=channel.slug, limit=10, min_total=10,
        sources=channel.sources or None,
    )
    if not candidates:
        return None

    for record, score in candidates:
        log().info(
            "[%s] standalone short — record %s (%s) — total=%s — %r",
            channel.slug, record.id, record.source,
            score.drama + score.novelty + score.visualization, record.title[:80],
        )

        if score.flags.get("sealed") or score.flags.get("minor_involved") or score.flags.get("tragedy_only"):
            log().info("[%s] record %s flagged: %s — skipping",
                       channel.slug, record.id, score.flags)
            prod_id = create_production(conn, record_id=record.id, channel_slug=channel.slug)
            mark_production_failed(conn, prod_id, f"flagged: {json.dumps(score.flags)}")
            continue

        try:
            result = _produce_standalone_short(settings, conn, channel, record, score)
            if result is not None:
                return result
            log().info("[%s] record %s: produced None (script refused or empty) — trying next",
                       channel.slug, record.id)
        except YouTubeQuotaExceeded:
            raise   # propagate; outer loop handles quota circuit-breaking
        except Exception as e:
            log().exception("[%s] record %s: standalone short failed (%s) — trying next",
                            channel.slug, record.id, e)

    log().info("[%s] all standalone-short candidates exhausted", channel.slug)
    return None


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
            voice_override=_pick_voice(channel, record.id),
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
        # Resolve a related long-form to link from the description (the
        # narration ends with "Full breakdown linked below" — this is the
        # link viewers see). Prefer same-source long-form; fall back to
        # any recent long-form from this channel.
        related = latest_longform_for_channel(
            conn, channel_slug=channel.slug, source=record.source,
        )
        short_video_id = upload_video(
            refresh_token=refresh_token,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            file_path=short_path,
            title=short_title,
            description=_short_only_description(record, channel, related=related),
            tags=_short_tags_for_record(record, channel),
            category_id=str(channel.youtube.get("category_id", "27")),
            privacy=channel.youtube.get("privacy", "public"),
        )

        # If we found a related long-form, post a channel-owner comment
        # linking to it. Worded as "another case file" (a recommendation)
        # rather than "full breakdown" because for standalone Shorts the
        # link is to a different topic from this Short, not its full
        # version. Paired Shorts use a different helper that does say
        # "full breakdown" since they ARE the same record.
        if related and related.get("video_id"):
            post_comment(
                refresh_token=refresh_token,
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
                video_id=short_video_id,
                text=_more_cases_comment(related["video_id"], related.get("title", "")),
            )

        # Mirror to TikTok if configured.
        _try_tiktok_upload(
            settings,
            file_path=short_path,
            caption=_tiktok_caption(short_title, record, channel),
            label="standalone-short",
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
    """Paired-short description. The narration ends with "Full breakdown
    linked below" — this is that link at the top of the description."""
    brand = channel.brand_name or channel.name
    return (
        f"▶ Watch the full breakdown: "
        f"https://youtube.com/watch?v={long_video_id}\n"
        f"   ({long_title[:80]})\n\n"
        f"Subscribe to {brand} for daily public-record breakdowns.\n\n"
        f"Source: {record.title}\n{record.url}\n"
        f"Published: {record.published_at}\n\n"
        f"{_AI_DISCLOSURE}\n\n"
        "#Shorts"
    )


def _short_only_description(
    record: Record,
    channel: Channel,
    *,
    related: dict | None = None,
) -> str:
    """Standalone-short description. If `related` is provided, the related
    long-form video link is placed at the top — this is what the narration's
    "Full breakdown linked below" callout points at."""
    brand = channel.brand_name or channel.name
    related_block = ""
    if related and related.get("video_id"):
        title = (related.get("title") or "Another case file").strip()
        # Phrased as "another case file" because for standalone Shorts
        # the linked long-form is a different topic, not this Short's
        # full version.
        related_block = (
            f"▶ Another case file: "
            f"https://youtube.com/watch?v={related['video_id']}\n"
            f"   ({title[:80]})\n\n"
        )
    return (
        f"{related_block}"
        f"Subscribe to {brand} for daily public-record breakdowns.\n\n"
        f"Source: {record.title}\n{record.url}\n"
        f"Published: {record.published_at}\n\n"
        f"{_AI_DISCLOSURE}\n\n"
        "#Shorts"
    )


def _tiktok_caption(title: str, record: Record, channel: Channel) -> str:
    """Build a TikTok caption from a short's title + per-source hashtags.

    TikTok caption max 2,200 chars but engagement is highest in the first
    ~100 chars — keep title up-front, hashtags after."""
    brand = (channel.brand_name or channel.name or "").replace(" ", "")
    src_tags = {
        "ntsb_aviation": ["aviation", "ntsb", "planecrash", "pilotlife", "aviationsafety"],
        "ntsb_marine":   ["maritime", "coastguard", "shipwreck"],
        "courtlistener": ["federalcourt", "truecrime", "law"],
        "doj":           ["truecrime", "doj", "federalcourt", "indictment"],
        "sec":           ["wallstreet", "fraud", "stocks", "finance", "secenforcement"],
    }.get(record.source, ["truecrime", "news"])
    hashtags = ["fyp", "publicrecords", brand.lower() or "documentary"] + src_tags
    hashtags = ["#" + t.strip("#").lower() for t in hashtags if t.strip()]
    return f"{title[:120]}\n\n{' '.join(hashtags[:12])}"


def _try_tiktok_upload(
    settings: Settings,
    *,
    file_path: Path,
    caption: str,
    label: str,
) -> str | None:
    """Optional secondary upload to TikTok. Never raises — TikTok failures
    must not break the YouTube run. Returns the publish_id or None."""
    if not settings.tiktok_enabled:
        return None
    try:
        publish_id = tiktok_upload_video(
            client_key=settings.tiktok_client_key,
            client_secret=settings.tiktok_client_secret,
            refresh_token=settings.tiktok_refresh_token,
            file_path=file_path,
            caption=caption,
            privacy_level=settings.tiktok_privacy,
        )
        log().info("  tiktok %s uploaded: publish_id=%s privacy=%s",
                   label, publish_id, settings.tiktok_privacy)
        return publish_id
    except TikTokError as e:
        log().warning("  tiktok %s upload failed (continuing): %s", label, e)
    except Exception as e:
        log().warning("  tiktok %s upload failed (non-API): %s", label, e)
    return None


def _short_link_comment(long_video_id: str, long_title: str) -> str:
    """Paired-short comment: the link IS the full breakdown of this
    Short's record. Channel-owner comments are auto-highlighted at top
    of the comments tab — most visible non-Studio placement available."""
    title = (long_title or "").strip()
    title_line = f"\n   ({title[:90]})" if title else ""
    return (
        f"▶ Full breakdown: https://youtube.com/watch?v={long_video_id}"
        f"{title_line}"
    )


def _more_cases_comment(long_video_id: str, long_title: str) -> str:
    """Standalone-short comment: the link is to a DIFFERENT case from
    the same channel, not the full version of this Short. Phrased as a
    related-content recommendation to avoid misleading viewers who'd
    expect "Full breakdown" to expand the exact same story."""
    title = (long_title or "").strip()
    title_line = f"\n   ({title[:90]})" if title else ""
    return (
        f"▶ Another case file: https://youtube.com/watch?v={long_video_id}"
        f"{title_line}"
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
        f"\n\n{_AI_DISCLOSURE}"
    )
    return (description or "")[:4500] + src_block


# YouTube's "altered or synthetic content" disclosure. Surfaced in every
# video description (separately from the upload-API flag) so reviewers and
# viewers can both see it. Honest framing — "narration is synthesized,
# facts are sourced" — is the same defense we use in YPP application
# language and quota-increase justifications.
_AI_DISCLOSURE = (
    "Disclosure: Narration and some visuals are AI-generated. "
    "All facts are sourced from the public records cited in this description."
)


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
