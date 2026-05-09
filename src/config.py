"""Loads YAML configs and environment into typed objects."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
ASSETS_DIR = ROOT / "assets"
PROMPTS_DIR = ROOT / "prompts"

load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Channel:
    slug: str
    name: str
    enabled: bool
    sources: list[str]
    niche_keywords: list[str]
    voice: str
    voice_speed: float
    script_prompt: str
    target_minutes: int
    music_bed: str | None
    intro_sting: str | None
    accent_color: str
    youtube: dict[str, Any]
    videos_per_run: int = 1
    make_shorts: bool = False
    brand_name: str = ""           # CTA-friendly brand (defaults to channel name if empty)


@dataclass(frozen=True)
class Source:
    slug: str
    type: str
    config: dict[str, Any]


@dataclass
class Settings:
    anthropic_api_key: str
    openai_api_key: str
    courtlistener_token: str
    pexels_api_key: str
    google_client_id: str
    google_client_secret: str
    yt_refresh_tokens: dict[str, str]
    db_path: Path
    output_dir: Path
    dry_run: bool
    max_videos_per_run: int
    log_level: str
    timezone: str
    channels: list[Channel] = field(default_factory=list)
    sources: dict[str, Source] = field(default_factory=dict)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_channels() -> list[Channel]:
    raw = _load_yaml(CONFIG_DIR / "channels.yaml")
    out = []
    for c in raw.get("channels", []):
        out.append(
            Channel(
                slug=c["slug"],
                name=c["name"],
                enabled=bool(c.get("enabled", False)),
                sources=list(c.get("sources", [])),
                niche_keywords=list(c.get("niche_keywords", [])),
                voice=c.get("voice", "onyx"),
                voice_speed=float(c.get("voice_speed", 1.0)),
                script_prompt=c.get("script_prompt", "script_court"),
                target_minutes=int(c.get("target_minutes", 8)),
                music_bed=c.get("music_bed"),
                intro_sting=c.get("intro_sting"),
                accent_color=c.get("accent_color", "#222222"),
                youtube=dict(c.get("youtube", {})),
                videos_per_run=int(c.get("videos_per_run", 1)),
                make_shorts=bool(c.get("make_shorts", False)),
                brand_name=str(c.get("brand_name") or c.get("name") or "").strip(),
            )
        )
    return out


def _load_sources() -> dict[str, Source]:
    raw = _load_yaml(CONFIG_DIR / "sources.yaml")
    out: dict[str, Source] = {}
    for slug, cfg in raw.get("sources", {}).items():
        cfg = dict(cfg)
        out[slug] = Source(slug=slug, type=cfg.pop("type"), config=cfg)
    return out


def _env(name: str, default: str = "") -> str:
    """Read an env var and strip surrounding whitespace.

    GitHub Actions secrets often arrive with a trailing newline depending on
    how they were pasted. That newline is fatal for any value used in an HTTP
    header (every API key we use), so we always strip.
    """
    return (os.environ.get(name, default) or "").strip()


def load_settings() -> Settings:
    s = Settings(
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        openai_api_key=_env("OPENAI_API_KEY"),
        courtlistener_token=_env("COURTLISTENER_API_TOKEN"),
        pexels_api_key=_env("PEXELS_API_KEY"),
        google_client_id=_env("GOOGLE_CLIENT_ID"),
        google_client_secret=_env("GOOGLE_CLIENT_SECRET"),
        yt_refresh_tokens=json.loads(_env("YT_REFRESH_TOKENS_JSON") or "{}"),
        db_path=Path(_env("DOCKET_DB_PATH", "docket.db")),
        output_dir=Path(_env("DOCKET_OUTPUT_DIR", "output")),
        dry_run=_env("DOCKET_DRY_RUN", "0") == "1",
        max_videos_per_run=int(_env("DOCKET_MAX_VIDEOS_PER_RUN", "3")),
        log_level=_env("DOCKET_LOG_LEVEL", "INFO"),
        timezone=_env("DOCKET_TIMEZONE", "America/New_York"),
        channels=_load_channels(),
        sources=_load_sources(),
    )
    # Also strip the YT refresh tokens themselves — same paste-with-newline risk.
    s.yt_refresh_tokens = {k: (v or "").strip() for k, v in s.yt_refresh_tokens.items()}
    s.output_dir.mkdir(parents=True, exist_ok=True)
    return s


def channel_by_slug(settings: Settings, slug: str) -> Channel | None:
    for c in settings.channels:
        if c.slug == slug:
            return c
    return None


def load_prompt(name: str) -> str:
    """Load a prompt template by name (without the .md extension)."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
