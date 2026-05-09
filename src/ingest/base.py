"""Shared HTTP client and helpers for ingesters."""

from __future__ import annotations

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def client(headers: dict | None = None) -> httpx.Client:
    """An httpx Client with sane defaults and follow_redirects on."""
    return httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        headers=headers or {},
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
def get_json(c: httpx.Client, url: str, **kwargs) -> dict:
    r = c.get(url, **kwargs)
    r.raise_for_status()
    return r.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
def get_text(c: httpx.Client, url: str, **kwargs) -> str:
    r = c.get(url, **kwargs)
    r.raise_for_status()
    return r.text


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
def post_json(c: httpx.Client, url: str, json: dict, **kwargs) -> dict:
    r = c.post(url, json=json, **kwargs)
    r.raise_for_status()
    return r.json()
