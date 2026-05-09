"""Visual sourcing for video scenes.

Resolution order:
1. Pexels stock photo / video matching the b_roll keywords (cheap, royalty-free).
2. OpenAI gpt-image-1 generated image (fallback when stock has nothing relevant).

We bias hard toward stock to keep cost down. AI generation runs only when the
b_roll prompt requests something stock cannot deliver (e.g. "a recreated
courtroom scene") or stock returned nothing.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Settings
from .stock import pexels
from .utils import log


IMAGE_MODEL = "gpt-image-1"


def fetch_visual(
    settings: Settings,
    *,
    b_roll_prompt: str,
    out_dir: Path,
    prefer_video: bool = False,
    allow_ai: bool = True,
) -> Path | None:
    """Resolve a scene's b_roll into a media file. Returns a Path or None."""
    out_dir.mkdir(parents=True, exist_ok=True)
    keywords = _keywords(b_roll_prompt)

    if prefer_video and settings.pexels_api_key:
        for q in keywords:
            v = pexels.fetch_video(settings.pexels_api_key, q, out_dir=out_dir)
            if v:
                return v

    if settings.pexels_api_key:
        for q in keywords:
            p = pexels.fetch_photo(settings.pexels_api_key, q, out_dir=out_dir)
            if p:
                return p

    if allow_ai and settings.openai_api_key:
        return _generate_ai_image(settings.openai_api_key, b_roll_prompt, out_dir=out_dir)

    return None


def _keywords(b_roll: str, max_queries: int = 3) -> list[str]:
    """Turn a b_roll direction into a list of stock-search queries, simplest first."""
    text = b_roll.strip()
    if not text:
        return []
    # First query: the whole direction (Pexels handles natural language fine).
    out = [text[:80]]
    # Then progressively simpler: drop everything after a comma; keep nouns.
    if "," in text:
        out.append(text.split(",", 1)[0][:60])
    # Last resort: top-3 words by length excluding stopwords.
    words = re.findall(r"\b[a-zA-Z]{4,}\b", text)
    stop = {"with", "from", "above", "across", "after", "before", "around", "their", "there"}
    chunks = [w for w in words if w.lower() not in stop]
    if chunks:
        out.append(" ".join(chunks[:3])[:60])
    # De-dupe, preserve order
    seen, uniq = set(), []
    for q in out:
        if q.lower() not in seen:
            uniq.append(q)
            seen.add(q.lower())
    return uniq[:max_queries]


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=15), reraise=True)
def _generate_ai_image(api_key: str, prompt: str, *, out_dir: Path) -> Path:
    client = OpenAI(api_key=api_key)
    safe_prompt = (
        f"Editorial documentary still, photographic style. {prompt}. "
        "No text, no logos, no watermarks. Natural color grading."
    )
    log().info("  AI image: %s", prompt[:60])
    res = client.images.generate(
        model=IMAGE_MODEL,
        prompt=safe_prompt,
        size="1536x1024",
        n=1,
    )
    b64 = res.data[0].b64_json
    raw = base64.b64decode(b64)
    slug = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:10]
    dest = out_dir / f"ai_{slug}.png"
    dest.write_bytes(raw)
    return dest
