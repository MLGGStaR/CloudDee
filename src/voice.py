"""Generate voiceover from a script using OpenAI TTS, plus per-scene SRT.

For each scene we render a WAV and (optionally) a transcribed SRT for
subtitle burn-in. The transcription happens on the SAME audio so the
captions match what the viewer actually hears (Whisper is more accurate
than naively splitting the source script into chunks).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .script import ScriptResult
from .transcribe import transcribe_to_srt, reflow_srt_max_words
from .utils import log


TTS_MODEL = "tts-1-hd"
TTS_FORMAT = "wav"


@dataclass
class VoicedScene:
    audio_path: Path
    srt_text: str       # SRT timestamps relative to the scene (start at 00:00)


def render_voiceover(
    *,
    api_key: str,
    script: ScriptResult,
    voice: str,
    speed: float,
    out_dir: Path,
    transcribe: bool = True,
    max_words_per_caption: int = 6,
) -> list[VoicedScene]:
    """Voice every non-empty scene. Returns one VoicedScene per scene."""
    out_dir.mkdir(parents=True, exist_ok=True)
    client = OpenAI(api_key=api_key)
    scenes: list[VoicedScene] = []

    for i, scene in enumerate(script.scenes):
        if not scene.narration.strip():
            continue
        path = out_dir / f"scene_{i:02d}_{scene.id}.{TTS_FORMAT}"
        _synthesize(client, scene.narration, voice=voice, speed=speed, out_path=path)
        path.with_suffix(".txt").write_text(scene.narration, encoding="utf-8")

        srt_text = ""
        if transcribe:
            try:
                raw_srt = transcribe_to_srt(api_key, path)
                srt_text = reflow_srt_max_words(raw_srt, max_words=max_words_per_caption)
                path.with_suffix(".srt").write_text(srt_text, encoding="utf-8")
            except Exception as e:
                log().warning("transcribe failed for %s (continuing without subs): %s",
                              scene.id, e)

        scenes.append(VoicedScene(audio_path=path, srt_text=srt_text))
        log().info("  voiced + %s scene %s → %s",
                   "transcribed" if srt_text else "no-srt", scene.id, path.name)
    return scenes


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=20), reraise=True)
def _synthesize(client: OpenAI, text: str, *, voice: str, speed: float, out_path: Path) -> None:
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
    cur, cur_len = [], 0
    for sentence in text.replace("\n", " ").split(". "):
        sentence = sentence.strip()
        if not sentence:
            continue
        sentence = sentence + ("." if not sentence.endswith(".") else "")
        if cur_len + len(sentence) > max_chars:
            out.append(" ".join(cur))
            cur, cur_len = [sentence], len(sentence)
        else:
            cur.append(sentence)
            cur_len += len(sentence) + 1
    if cur:
        out.append(" ".join(cur))
    return out
