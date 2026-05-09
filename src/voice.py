"""Generate voiceover from a script using OpenAI TTS.

We render scene-by-scene so we can build a per-scene timing map for the editor.
Each scene's audio is written as a separate WAV; the renderer concatenates
them with small silences between.
"""

from __future__ import annotations

from pathlib import Path

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .script import ScriptResult
from .utils import log


# tts-1 is fast & cheap ($0.015 / 1K chars = ~$0.13 / 10-min script).
# tts-1-hd doubles the cost for noticeably better quality.
TTS_MODEL = "tts-1-hd"
TTS_FORMAT = "wav"


def render_voiceover(
    *,
    api_key: str,
    script: ScriptResult,
    voice: str,
    speed: float,
    out_dir: Path,
) -> list[Path]:
    """Returns a list of WAV paths, one per scene, in order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    client = OpenAI(api_key=api_key)
    paths: list[Path] = []
    for i, scene in enumerate(script.scenes):
        if not scene.narration.strip():
            continue
        path = out_dir / f"scene_{i:02d}_{scene.id}.{TTS_FORMAT}"
        _synthesize(client, scene.narration, voice=voice, speed=speed, out_path=path)
        # Sidecar narration text — used by the Shorts captioner.
        path.with_suffix(".txt").write_text(scene.narration, encoding="utf-8")
        paths.append(path)
        log().info("  voiced scene %s → %s", scene.id, path.name)
    return paths


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20), reraise=True)
def _synthesize(client: OpenAI, text: str, *, voice: str, speed: float, out_path: Path) -> None:
    # OpenAI TTS limit is 4096 characters per call. Long scenes get chunked.
    chunks = _chunk_for_tts(text, max_chars=4000)
    if len(chunks) == 1:
        with client.audio.speech.with_streaming_response.create(
            model=TTS_MODEL,
            voice=voice,
            input=chunks[0],
            response_format=TTS_FORMAT,
            speed=speed,
        ) as response:
            response.stream_to_file(out_path)
        return

    # Chunked: write each piece, then concatenate with ffmpeg.
    import subprocess
    import tempfile
    parts: list[Path] = []
    with tempfile.TemporaryDirectory() as td:
        for i, chunk in enumerate(chunks):
            p = Path(td) / f"part_{i:02d}.{TTS_FORMAT}"
            with client.audio.speech.with_streaming_response.create(
                model=TTS_MODEL,
                voice=voice,
                input=chunk,
                response_format=TTS_FORMAT,
                speed=speed,
            ) as response:
                response.stream_to_file(p)
            parts.append(p)

        listfile = Path(td) / "list.txt"
        listfile.write_text("\n".join(f"file '{p}'" for p in parts), encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
             "-c", "copy", str(out_path)],
            check=True, capture_output=True,
        )


def _chunk_for_tts(text: str, max_chars: int = 4000) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    out: list[str] = []
    cur = []
    cur_len = 0
    for sentence in text.replace("\n", " ").split(". "):
        sentence = sentence.strip()
        if not sentence:
            continue
        sentence = sentence + ("." if not sentence.endswith(".") else "")
        if cur_len + len(sentence) > max_chars:
            out.append(" ".join(cur))
            cur = [sentence]
            cur_len = len(sentence)
        else:
            cur.append(sentence)
            cur_len += len(sentence) + 1
    if cur:
        out.append(" ".join(cur))
    return out
