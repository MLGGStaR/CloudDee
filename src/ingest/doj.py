"""DOJ press release RSS ingester.

The Department of Justice publishes a steady stream of press releases — most
of them describing fresh indictments, sentencings, and enforcement actions.
This is the single highest-signal RSS feed in the U.S. federal government for
narrative criminal cases, and it's free.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import feedparser

from ..config import Source
from ..db import insert_record
from ..utils import log, normalize_text


def ingest(conn: sqlite3.Connection, source: Source) -> int:
    cfg = source.config
    feed_url = cfg.get("feed_url")
    max_per_run = int(cfg.get("max_per_run", 100))
    if not feed_url:
        log().warning("DOJ ingester: no feed_url configured")
        return 0

    feed = feedparser.parse(feed_url, request_headers={
        "User-Agent": "Docket-Research (research@example.com)"
    })

    inserted = 0
    for entry in feed.entries[:max_per_run]:
        ext_id = entry.get("id") or entry.get("link") or ""
        if not ext_id:
            continue
        title = normalize_text(entry.get("title", ""))[:200]
        url = entry.get("link", "")
        published = entry.get("published", "") or entry.get("updated", "")
        # DOJ summaries are usually full enough to score on.
        raw_text = normalize_text(
            entry.get("summary", "")
            or " ".join(c.value for c in entry.get("content", []) if hasattr(c, "value"))
        )

        new_id = insert_record(
            conn,
            source=source.slug,
            external_id=str(ext_id),
            title=title,
            url=url,
            published_at=_iso(published),
            raw_text=raw_text,
            raw_json={
                "title": title,
                "link": url,
                "published": published,
                "summary": raw_text[:5000],
            },
        )
        if new_id:
            inserted += 1
    return inserted


def _iso(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return datetime.now(timezone.utc).date().isoformat()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            continue
    return s
