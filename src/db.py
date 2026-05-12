"""Thin SQLite layer. The DB is small enough to live in the repo.

Records are dedup'd by (source, external_id). Productions are dedup'd by
(record_id, channel_slug) so a single record can fan out across channels but
not be duplicated within one channel.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


@dataclass
class Record:
    id: int
    source: str
    external_id: str
    title: str
    url: str
    published_at: str
    fetched_at: str
    raw_text: str
    raw_json: str | None


@dataclass
class Score:
    record_id: int
    drama: int
    novelty: int
    visualization: int
    niche_fit: dict[str, int]
    summary: str
    flags: dict[str, Any]
    scored_at: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    new = not db_path.exists()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if new:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_record(
    conn: sqlite3.Connection,
    *,
    source: str,
    external_id: str,
    title: str,
    url: str,
    published_at: str,
    raw_text: str,
    raw_json: dict | None = None,
) -> int | None:
    """Insert a record. Returns the new id, or None if it was a duplicate."""
    try:
        cur = conn.execute(
            """INSERT INTO records (source, external_id, title, url, published_at,
                                    fetched_at, raw_text, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source,
                str(external_id),
                title or "",
                url or "",
                published_at,
                _utcnow(),
                raw_text or "",
                json.dumps(raw_json) if raw_json is not None else None,
            ),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def unscored_records(conn: sqlite3.Connection, limit: int = 100) -> list[Record]:
    rows = conn.execute(
        """SELECT r.* FROM records r
           LEFT JOIN scores s ON s.record_id = r.id
           WHERE s.record_id IS NULL
           ORDER BY r.published_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [_row_to_record(r) for r in rows]


def upsert_score(conn: sqlite3.Connection, score: Score) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO scores
           (record_id, drama, novelty, visualization, niche_fit_json, summary, flags_json, scored_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            score.record_id,
            score.drama,
            score.novelty,
            score.visualization,
            json.dumps(score.niche_fit),
            score.summary,
            json.dumps(score.flags),
            score.scored_at,
        ),
    )


def top_records_for_channel(
    conn: sqlite3.Connection,
    *,
    channel_slug: str,
    limit: int = 5,
    min_total: int = 18,
    sources: list[str] | None = None,
) -> list[tuple[Record, Score]]:
    """Pick the highest-scored records for a channel that haven't been produced
    for that channel yet.

    - `min_total` is a quality floor across drama+novelty+vis (out of 30).
    - `sources` (optional) restricts records to specific source slugs. Required
      for multi-topic channels; without it any unproduced record qualifies.

    The legacy `niche_fit[channel_slug] >= 6` hard filter has been retired
    because the channel can now be multi-topic; the scorer's per-topic
    niche_fit is still used as a ranking signal (max value across topics).
    """
    src_clause = ""
    src_params: list = []
    if sources:
        placeholders = ",".join("?" for _ in sources)
        src_clause = f" AND r.source IN ({placeholders})"
        src_params = list(sources)

    sql = f"""
        SELECT r.*, s.drama, s.novelty, s.visualization, s.niche_fit_json,
               s.summary, s.flags_json, s.scored_at
        FROM records r
        JOIN scores s ON s.record_id = r.id
        LEFT JOIN productions p
             ON p.record_id = r.id AND p.channel_slug = ?
        WHERE p.id IS NULL
          AND (s.drama + s.novelty + s.visualization) >= ?
          {src_clause}
        ORDER BY (s.drama + s.novelty + s.visualization) DESC,
                 r.published_at DESC
        LIMIT ?
    """
    rows = conn.execute(
        sql,
        (channel_slug, min_total, *src_params, limit * 4),
    ).fetchall()

    out: list[tuple[Record, Score]] = []
    for r in rows:
        niche_fit = json.loads(r["niche_fit_json"] or "{}")
        record = _row_to_record(r)
        score = Score(
            record_id=r["id"],
            drama=r["drama"],
            novelty=r["novelty"],
            visualization=r["visualization"],
            niche_fit=niche_fit,
            summary=r["summary"] or "",
            flags=json.loads(r["flags_json"] or "{}"),
            scored_at=r["scored_at"],
        )
        out.append((record, score))
        if len(out) >= limit:
            break
    return out


def latest_longform_for_channel(
    conn: sqlite3.Connection,
    *,
    channel_slug: str,
    source: str | None = None,
) -> dict | None:
    """Return the channel's most recent uploaded long-form video, used to
    link from a standalone Short's description ("watch the full breakdown").

    Long-forms vs standalone-shorts are distinguished by video_path: the
    standalone short pipeline writes work dirs prefixed `short_`, so any
    production whose video_path does NOT contain `/short_` is a long-form
    (paired shorts share the long-form's production row).

    If `source` is given, prefers a long-form from the same source; falls
    back to any long-form for the channel.
    """
    base_sql = """
        SELECT p.youtube_video_id, r.source, r.title
        FROM productions p
        JOIN records r ON r.id = p.record_id
        WHERE p.channel_slug = ?
          AND p.youtube_video_id IS NOT NULL
          AND (p.video_path IS NULL OR p.video_path NOT LIKE '%/short_%')
    """
    if source:
        row = conn.execute(
            base_sql + " AND r.source = ? ORDER BY p.completed_at DESC LIMIT 1",
            (channel_slug, source),
        ).fetchone()
        if row:
            return {"video_id": row["youtube_video_id"],
                    "source": row["source"], "title": row["title"]}
    row = conn.execute(
        base_sql + " ORDER BY p.completed_at DESC LIMIT 1",
        (channel_slug,),
    ).fetchone()
    if not row:
        return None
    return {"video_id": row["youtube_video_id"],
            "source": row["source"], "title": row["title"]}


def create_production(conn: sqlite3.Connection, *, record_id: int, channel_slug: str) -> int:
    cur = conn.execute(
        """INSERT INTO productions (record_id, channel_slug, status, started_at)
           VALUES (?, ?, 'pending', ?)""",
        (record_id, channel_slug, _utcnow()),
    )
    return cur.lastrowid


def update_production(conn: sqlite3.Connection, prod_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE productions SET {cols} WHERE id = ?",
                 (*fields.values(), prod_id))


def mark_production_complete(
    conn: sqlite3.Connection,
    prod_id: int,
    *,
    youtube_video_id: str,
    video_path: str,
    thumbnail_path: str,
) -> None:
    conn.execute(
        """UPDATE productions
           SET status = 'uploaded', youtube_video_id = ?, video_path = ?,
               thumbnail_path = ?, completed_at = ?
           WHERE id = ?""",
        (youtube_video_id, video_path, thumbnail_path, _utcnow(), prod_id),
    )


def mark_production_failed(conn: sqlite3.Connection, prod_id: int, error: str) -> None:
    conn.execute(
        "UPDATE productions SET status = 'failed', error = ?, completed_at = ? WHERE id = ?",
        (error[:1000], _utcnow(), prod_id),
    )


def open_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO run_log (started_at, status) VALUES (?, 'running')",
        (_utcnow(),),
    )
    return cur.lastrowid


def close_run(conn: sqlite3.Connection, run_id: int, status: str, summary: str) -> None:
    conn.execute(
        "UPDATE run_log SET finished_at = ?, status = ?, summary = ? WHERE id = ?",
        (_utcnow(), status, summary[:2000], run_id),
    )


def _row_to_record(r: sqlite3.Row) -> Record:
    return Record(
        id=r["id"],
        source=r["source"],
        external_id=r["external_id"],
        title=r["title"] or "",
        url=r["url"] or "",
        published_at=r["published_at"],
        fetched_at=r["fetched_at"],
        raw_text=r["raw_text"] or "",
        raw_json=r["raw_json"],
    )
