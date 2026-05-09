"""NTSB CAROL public data ingester.

CAROL exposes a JSON POST endpoint that returns recent investigations.
The response shape is a list of records, each containing a `Fields` list of
{FieldName, Values} pairs, which we flatten into a dict.

We sort by EventDate descending and take the top N, relying on the database's
unique constraint to skip records we've already ingested.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from ..config import Source
from ..db import insert_record
from ..utils import log, normalize_text
from . import base


def ingest(conn: sqlite3.Connection, source: Source) -> int:
    cfg = source.config
    mode = cfg.get("mode", "aviation").capitalize()      # 'Aviation' or 'Marine'
    base_url = cfg["base_url"]
    max_per_run = int(cfg.get("max_per_run", 200))

    payload = {
        "ResultSetSize": max_per_run,
        "ResultSetOffset": 0,
        "QueryGroups": [{
            "QueryRules": [{
                "RuleType": "Simple",
                "Values": [mode],
                "Columns": ["Event.Mode"],
                "Operator": "is",
                "selectedOption": {"FieldName": "is", "DisplayText": "is"},
            }],
            "AndOr": "and",
            "inLastSearch": True,
            "editedSinceLastSearch": False,
        }],
        "AndOr": "and",
        "SortColumn": "Event.EventDate",
        "SortDescending": True,
        "TargetCollection": "cases",
        "SessionId": 0,
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Docket-Research)",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://data.ntsb.gov",
        "Referer": "https://data.ntsb.gov/carol-main-public/basic-search",
    }

    inserted = 0
    with base.client(headers=headers) as c:
        try:
            data = base.post_json(c, base_url, json=payload)
        except Exception as e:
            log().warning("NTSB request failed: %s", e)
            return 0

        for case in data.get("Results", [])[:max_per_run]:
            flat = _flatten_fields(case)
            ev_id = flat.get("Mkey") or flat.get("NtsbNo") or ""
            if not ev_id:
                continue

            event_date = flat.get("EventDate", "") or ""
            published_at = _normalize_date(event_date)

            title_parts = [
                flat.get("EventType") or "",
                flat.get("VehicleMake") or "",
                flat.get("VehicleModel") or "",
                f"near {flat.get('City')}, {flat.get('State')}" if flat.get("City") else "",
            ]
            title = normalize_text(" ".join(p for p in title_parts if p))[:200] or f"NTSB {flat.get('NtsbNo','case')}"

            # CAROL summary endpoint isn't always available; build a richer
            # raw_text from the structured fields we already have.
            kv_lines = [f"{k}: {v}" for k, v in flat.items() if v not in (None, "", [])]
            raw_text = normalize_text("\n".join(kv_lines))

            url = f"https://data.ntsb.gov/carol-main-public/sr-details/{ev_id}"

            new_id = insert_record(
                conn,
                source=source.slug,
                external_id=str(ev_id),
                title=title,
                url=url,
                published_at=published_at,
                raw_text=raw_text,
                raw_json=flat,
            )
            if new_id:
                inserted += 1

    return inserted


def _flatten_fields(case: dict) -> dict[str, str]:
    """Convert {Fields: [{FieldName, Values}, ...]} → {FieldName: 'val1 / val2'}.

    Values are always coerced to strings (joining multi-value lists with ' / ')
    so downstream callers can safely treat every dict value as a string.
    """
    out: dict[str, str] = {}
    for f in case.get("Fields") or []:
        name = f.get("FieldName") or ""
        values = f.get("Values") or []
        if not name:
            continue
        out[name] = " / ".join(str(v) for v in values if v not in (None, ""))
    return out


def _normalize_date(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return datetime.now(timezone.utc).date().isoformat()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            continue
    return s
