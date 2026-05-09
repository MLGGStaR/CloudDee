"""Generate full video scripts with Claude Sonnet.

Returns a structured dict containing the title, description, tags, and a list
of scenes. Each scene has narration text and a b_roll direction string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from anthropic import Anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Channel, load_prompt
from .db import Record
from .utils import log, render_template, truncate


SCRIPT_MODEL = "claude-sonnet-4-6"


@dataclass
class Scene:
    id: str
    narration: str
    b_roll: str
    label: str = ""        # human-readable label for description timestamps


@dataclass
class ScriptResult:
    title: str
    description: str
    tags: list[str]
    scenes: list[Scene]
    refused: bool = False
    refused_reason: str = ""

    @property
    def full_narration(self) -> str:
        return "\n\n".join(s.narration for s in self.scenes if s.narration)

    @property
    def word_count(self) -> int:
        return len(self.full_narration.split())


def write_script(api_key: str, channel: Channel, record: Record) -> ScriptResult:
    client = Anthropic(api_key=api_key)
    prompt = load_prompt(channel.script_prompt)
    target_words = int(channel.target_minutes * 150)

    body = render_template(
        prompt,
        title=truncate(record.title, 250),
        url=record.url,
        published_at=record.published_at,
        text=truncate(record.raw_text, 30_000),
        target_minutes=channel.target_minutes,
        target_words=target_words,
        brand_name=channel.brand_name or channel.name,
    )

    return _call(client, body)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20), reraise=True)
def _call(client: Anthropic, body: str) -> ScriptResult:
    msg = client.messages.create(
        model=SCRIPT_MODEL,
        max_tokens=8000,
        temperature=0.7,
        system=(
            "You return only valid JSON objects matching the requested schema. "
            "No prose. No markdown fences. No leading explanations."
        ),
        messages=[{"role": "user", "content": body}],
    )
    text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lower().startswith("json"):
            text = text[4:].strip()
    data = json.loads(text)

    if data.get("refuse"):
        return ScriptResult(
            title="", description="", tags=[], scenes=[],
            refused=True, refused_reason=data.get("reason", "writer refused"),
        )

    scenes = [
        Scene(
            id=str(s.get("id") or f"scene_{i}"),
            narration=str(s.get("narration") or "").strip(),
            b_roll=str(s.get("b_roll") or "").strip(),
            label=str(s.get("label") or s.get("id") or f"Section {i+1}").strip(),
        )
        for i, s in enumerate(data.get("scenes") or [])
    ]
    result = ScriptResult(
        title=str(data.get("title") or "Untitled")[:100],
        description=str(data.get("description") or "")[:5000],
        tags=[t for t in (data.get("tags") or []) if isinstance(t, str)][:30],
        scenes=scenes,
    )
    log().info("Script: %d scenes / %d words / %r", len(scenes), result.word_count, result.title)
    return result
