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
    """Score up to `limit` unscored records. Returns number scored.

    Raises RuntimeError if every attempted record fails — that almost always
    means an upstream outage (Anthropic flap) or a bad key, and we'd rather
    the run go red than silently produce zero videos.
    """
    if not settings.anthropic_api_key:
        log().warning("ANTHROPIC_API_KEY not set; skipping scoring")
        return 0

    # Disable the SDK's internal retry stack — we wrap with our own tighter
    # retries below. Stacked retries waste budget on transient outages.
    client = Anthropic(api_key=settings.anthropic_api_key, max_retries=2, timeout=30.0)
    prompt_template = load_prompt("score")
    pending = unscored_records(conn, limit=limit)
    if not pending:
        return 0
    log().info("Scoring %s pending records", len(pending))

    scored = 0
    failed = 0
    last_error: str | None = None
    for r in pending:
        try:
            score = _score_one(client, prompt_template, r)
            upsert_score(conn, score)
            scored += 1
            if scored % 10 == 0:
                log().info("  … %s/%s", scored, len(pending))
        except Exception as e:
            failed += 1
            last_error = f"{type(e).__name__}: {e}"
            log().warning("Scoring failed for record %s: %s", r.id, last_error)
            # If the first 5 in a row all fail, the API is down. Bail loudly
            # rather than burn through 100 records' worth of API attempts.
            if scored == 0 and failed >= 5:
                raise RuntimeError(
                    f"5 consecutive scoring failures, no successes. "
                    f"Last error: {last_error}. Likely upstream outage or bad API key."
                )

    if scored == 0 and failed > 0:
        raise RuntimeError(f"Scored 0 of {failed} records. Last error: {last_error}")
    if failed > scored:
        log().warning("Scoring degraded: %d ok, %d failed (%.0f%% fail rate)",
                      scored, failed, 100 * failed / (scored + failed))
    return scored


# SDK already retries 2x; our outer wrapper adds 1 extra slow retry to ride
# through brief flaps without exploding total attempts per record.
@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=15), reraise=True)
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
