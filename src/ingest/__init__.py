"""Ingesters: pull fresh public records from various government sources."""

from __future__ import annotations

import sqlite3
from typing import Callable

from ..config import Settings, Source
from ..utils import log
from . import courtlistener, doj, ntsb, sec


# Map source `type` (from sources.yaml) → ingester callable
INGESTERS: dict[str, Callable] = {
    "ntsb": ntsb.ingest,
    "sec": sec.ingest,
    "courtlistener": courtlistener.ingest,
    "doj_rss": doj.ingest,
}


def run(settings: Settings, conn: sqlite3.Connection, source_slug: str) -> int:
    src: Source | None = settings.sources.get(source_slug)
    if src is None:
        log().warning("Unknown source: %s", source_slug)
        return 0
    fn = INGESTERS.get(src.type)
    if fn is None:
        log().warning("No ingester registered for type: %s", src.type)
        return 0
    log().info("Ingesting %s …", source_slug)
    n = fn(conn=conn, source=src)
    log().info("  → %s new records from %s", n, source_slug)
    return n


def run_all_for_channels(settings: Settings, conn: sqlite3.Connection) -> dict[str, int]:
    """Run every source referenced by an enabled channel.

    Each source is wrapped in its own try/except — a broken upstream
    (e.g. NTSB API schema change, DOJ RSS down) must not kill the whole
    pipeline. Failed sources return 0 ingested; other sources still run
    and the rest of the pipeline (scoring, scripting, render, upload)
    proceeds with whatever records did make it in.
    """
    needed: set[str] = set()
    for ch in settings.channels:
        if ch.enabled:
            needed.update(ch.sources)
    out: dict[str, int] = {}
    for s in sorted(needed):
        try:
            out[s] = run(settings, conn, s)
        except Exception as e:
            log().exception("ingest source %s failed (continuing): %s", s, e)
            out[s] = 0
    return out
