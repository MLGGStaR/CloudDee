"""CourtListener / RECAP Free PACER Archive ingester.

CourtListener is the Free Law Project's free index of federal court records.
A free token bumps your rate limit. Without a token, the public RSS feed still
works for a smaller volume.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import feedparser

from ..config import Source
from ..db import insert_record
from ..utils import log, normalize_text
from . import base


def ingest(conn: sqlite3.Connection, source: Source) -> int:
    cfg = source.config
    base_url = cfg["base_url"].rstrip("/")
    lookback_days = int(cfg.get("lookback_days", 7))
    max_per_run = int(cfg.get("max_per_run", 100))
    courts = cfg.get("courts", [])
    token = os.environ.get("COURTLISTENER_API_TOKEN", "")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()

    headers = {
        "User-Agent": "Docket-Research (research@example.com)",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Token {token}"

    inserted = 0
    if token:
        inserted += _ingest_via_api(conn, source, headers, base_url, cutoff, max_per_run, courts)
    else:
        log().info("No COURTLISTENER_API_TOKEN; falling back to RSS")
        inserted += _ingest_via_rss(conn, source, cfg.get("feed_url"), max_per_run)
    return inserted


def _ingest_via_api(conn, source, headers, base_url, cutoff, max_per_run, courts) -> int:
    inserted = 0
    with base.client(headers=headers) as c:
        params = {
            "date_filed__gte": cutoff,
            "page_size": min(max_per_run, 100),
            "order_by": "-date_filed",
        }
        if courts:
            params["court__in"] = ",".join(courts)
        try:
            data = base.get_json(c, f"{base_url}/opinions/", params=params)
        except Exception as e:
            log().warning("CourtListener API failed: %s", e)
            return 0
        for op in (data.get("results") or [])[:max_per_run]:
            ext_id = str(op.get("id") or op.get("resource_uri") or "").strip()
            if not ext_id:
                continue
            title = normalize_text(op.get("case_name") or op.get("caseName") or "Federal opinion")[:200]
            published = (op.get("date_filed") or "")
            url = op.get("absolute_url") or op.get("download_url") or ""
            if url and url.startswith("/"):
                url = f"https://www.courtlistener.com{url}"
            raw_text = normalize_text(
                op.get("plain_text")
                or op.get("html")
                or op.get("html_lawbox")
                or op.get("html_columbia")
                or ""
            )[:120_000]
            new_id = insert_record(
                conn,
                source=source.slug,
                external_id=ext_id,
                title=title,
                url=url,
                published_at=_iso(published),
                raw_text=raw_text,
                raw_json={k: v for k, v in op.items() if isinstance(v, (str, int, float, bool))},
            )
            if new_id:
                inserted += 1
    return inserted


def _ingest_via_rss(conn, source, feed_url, max_per_run) -> int:
    if not feed_url:
        return 0
    inserted = 0
    feed = feedparser.parse(feed_url)
    for entry in feed.entries[:max_per_run]:
        ext_id = entry.get("id") or entry.get("link") or ""
        if not ext_id:
            continue
        title = normalize_text(entry.get("title", ""))[:200]
        url = entry.get("link", "")
        published = entry.get("published", "") or entry.get("updated", "")
        raw_text = normalize_text(entry.get("summary", ""))
        new_id = insert_record(
            conn,
            source=source.slug,
            external_id=str(ext_id),
            title=title,
            url=url,
            published_at=_iso(published),
            raw_text=raw_text,
            raw_json=dict(entry),
        )
        if new_id:
            inserted += 1
    return inserted


def _iso(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return datetime.now(timezone.utc).date().isoformat()
    for fmt in ("%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            continue
    return s
