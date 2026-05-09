"""Score raw records with Claude Haiku.

We use Haiku for cost: scoring is volume-heavy and the task is structured. The
prompt template is in prompts/score.md.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from anthropic import Anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Settings, load_prompt
from .db import Score, Record, unscored_records, upsert_score
from .utils import log, render_template, truncate


SCORE_MODEL = "claude-haiku-4-5-20251001"


def score_pending(settings: Settings, conn: sqlite3.Connection, *, limit: int = 100) -> int:
    """Score up to `limit` unscored records. Returns number scored."""
    if not settings.anthropic_api_key:
        log().warning("ANTHROPIC_API_KEY not set; skipping scoring")
        return 0

    client = Anthropic(api_key=settings.anthropic_api_key)
    prompt_template = load_prompt("score")
    pending = unscored_records(conn, limit=limit)
    log().info("Scoring %s pending records", len(pending))

    scored = 0
    for r in pending:
        try:
            score = _score_one(client, prompt_template, r)
            upsert_score(conn, score)
            scored += 1
            if scored % 10 == 0:
                log().info("  … %s/%s", scored, len(pending))
        except Exception as e:
            log().warning("Scoring failed for record %s: %s", r.id, e)
            continue
    return scored


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
def _score_one(client: Anthropic, prompt_template: str, r: Record) -> Score:
    body = render_template(
        prompt_template,
        source=r.source,
        published_at=r.published_at,
        title=truncate(r.title, 200),
        url=r.url,
        text=truncate(r.raw_text, 8000),
    )
    msg = client.messages.create(
        model=SCORE_MODEL,
        max_tokens=1024,
        temperature=0.2,
        system=(
            "You return only valid JSON objects matching the requested schema. "
            "No prose. No markdown fences."
        ),
        messages=[{"role": "user", "content": body}],
    )
    text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lower().startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)
    return Score(
        record_id=r.id,
        drama=int(data.get("drama", 0)),
        novelty=int(data.get("novelty", 0)),
        visualization=int(data.get("visualization", 0)),
        niche_fit={k: int(v) for k, v in (data.get("niche_fit") or {}).items()},
        summary=(data.get("summary") or "")[:1000],
        flags=dict(data.get("flags") or {}),
        scored_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
