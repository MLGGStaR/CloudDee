"""SEC EDGAR enforcement / 8-K material-event ingester.

We pull from EDGAR's full-text search API for litigation-release filings and
recent 8-K material-event filings. SEC requires a real User-Agent header.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from ..config import Source
from ..db import insert_record
from ..utils import log, normalize_text
from . import base


SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"


def ingest(conn: sqlite3.Connection, source: Source) -> int:
    cfg = source.config
    lookback_days = int(cfg.get("lookback_days", 7))
    max_per_run = int(cfg.get("max_per_run", 100))
    user_agent = cfg.get("user_agent", "Docket-Research research@example.com")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date()

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
    }

    inserted = 0
    with base.client(headers=headers) as c:
        for query in ("litigation release", "enforcement action", "8-K material definitive agreement"):
            params = {
                "q": f'"{query}"',
                "dateRange": "custom",
                "startdt": cutoff.isoformat(),
                "enddt": datetime.now(timezone.utc).date().isoformat(),
                "forms": "8-K,LR,LITIGATION",
            }
            try:
                data = base.get_json(c, SEARCH_URL, params=params)
            except Exception as e:
                log().warning("SEC EDGAR query %r failed: %s", query, e)
                continue

            hits = data.get("hits", {}).get("hits", [])
            for hit in hits[:max_per_run]:
                src = hit.get("_source", {})
                acc = (src.get("adsh") or "").replace("-", "")
                if not acc:
                    continue
                cik = (src.get("ciks") or [None])[0]
                form = src.get("form") or ""
                file_date = src.get("file_date") or ""
                title = normalize_text(
                    f"{form} — {' / '.join(src.get('display_names', []) or ['(unknown)'])}"
                )[:200]
                url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}"

                # Try to grab the primary doc text for narrative content.
                primary_doc = src.get("file_type", "") or ""
                desc = " ".join(filter(None, [
                    src.get("display_names") and "Filer: " + ", ".join(src["display_names"]),
                    f"Form: {form}",
                    f"File date: {file_date}",
                    src.get("items") and "Items: " + ", ".join(src["items"]),
                    src.get("file_description") and src["file_description"],
                ]))
                raw_text = normalize_text(desc)
                published_at = _to_iso(file_date)

                new_id = insert_record(
                    conn,
                    source=source.slug,
                    external_id=acc,
                    title=title or "(unknown SEC filing)",
                    url=url,
                    published_at=published_at,
                    raw_text=raw_text,
                    raw_json=src,
                )
                if new_id:
                    inserted += 1

    return inserted


def _to_iso(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return datetime.now(timezone.utc).date().isoformat()
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return s
