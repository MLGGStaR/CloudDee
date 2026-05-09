"""Generate a static map image for an incident location.

We do this in two steps with no auth required:
  1. Geocode the location string with Nominatim (OpenStreetMap's free service).
  2. Fetch a static map image from staticmap.openstreetmap.de.

Both have polite-use rate limits but for our 3-videos-a-day cadence we are
many orders of magnitude under.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx

from .utils import log


# Photon is Komoot's free OpenStreetMap-backed geocoder. No auth, generous
# rate limits, much more reliable for our use than Nominatim's public proxy
# (which 403s many cloud and many home IPs).
PHOTON_URL = "https://photon.komoot.io/api/"
STATICMAP_URL = "https://staticmap.openstreetmap.de/staticmap.php"
USER_AGENT = "Docket-Research (research@example.com)"

_LAST_GEOCODE_AT = 0.0


def map_for_location(location: str, *, out_dir: Path, zoom: int = 9) -> Path | None:
    """Return a path to a 1280x720 static map JPEG of `location`, or None
    if geocoding fails. Cached on disk by location hash."""
    location = (location or "").strip()
    if not location:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = hashlib.sha1(location.encode("utf-8")).hexdigest()[:12]
    dest = out_dir / f"map_{slug}.png"
    if dest.exists():
        return dest

    try:
        lat, lon = _geocode(location)
    except Exception as e:
        log().warning("geocode failed for %r: %s", location, e)
        return None
    if lat is None:
        return None

    try:
        _fetch_static_map(lat, lon, zoom=zoom, out_path=dest)
    except Exception as e:
        log().warning("static map failed for %r: %s", location, e)
        return None

    log().info("  map → %s (%s lat=%.3f lon=%.3f)", dest.name, location, lat, lon)
    return dest


def _geocode(location: str) -> tuple[float, float]:
    global _LAST_GEOCODE_AT
    elapsed = time.time() - _LAST_GEOCODE_AT
    if elapsed < 0.6:
        time.sleep(0.6 - elapsed)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    params = {"q": location, "limit": 1}
    with httpx.Client(timeout=15.0, follow_redirects=True) as c:
        r = c.get(PHOTON_URL, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
    _LAST_GEOCODE_AT = time.time()
    features = data.get("features") or []
    if not features:
        return None, None
    coords = features[0].get("geometry", {}).get("coordinates") or []
    if len(coords) < 2:
        return None, None
    # GeoJSON ordering is [lon, lat] — flip.
    return float(coords[1]), float(coords[0])


def _fetch_static_map(lat: float, lon: float, *, zoom: int, out_path: Path) -> None:
    """Render a static map locally by fetching OSM tiles directly.

    Uses the `staticmap` package: stitches the necessary tiles, draws a red
    pin at (lat, lon), saves as PNG. No third-party static-map service.
    """
    # Imported lazily so users without staticmap installed (e.g. running ingest
    # only) don't pay the import cost.
    from staticmap import CircleMarker, StaticMap

    # Wikimedia Maps tile server — free, no auth, lenient for third-party
    # embedded use (Wikipedia itself uses these tiles publicly).
    # If this fails for any reason, callers fall back to skipping the map.
    last_error: Exception | None = None
    for url_template in (
        "https://maps.wikimedia.org/osm-intl/{z}/{x}/{y}.png",
        "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
    ):
        try:
            m = StaticMap(
                1280, 720,
                url_template=url_template,
                headers={"User-Agent": USER_AGENT},
            )
            m.add_marker(CircleMarker((lon, lat), "white", 22))
            m.add_marker(CircleMarker((lon, lat), "#dc1e1e", 16))
            image = m.render(zoom=zoom)
            image.save(out_path)
            return
        except Exception as e:
            last_error = e
            log().debug("tile source %s failed: %s", url_template, e)
            continue
    raise last_error or RuntimeError("all tile sources failed")


def location_from_record(raw_text: str, raw_json: dict | None = None) -> str:
    """Heuristic: extract a 'City, State, Country' string from an NTSB-style
    flattened record. Returns "" if nothing usable."""
    js = raw_json or {}
    parts = [
        (js.get("City") or "").strip(),
        (js.get("State") or "").strip(),
        (js.get("Country") or "").strip(),
    ]
    parts = [p for p in parts if p]
    if parts:
        return ", ".join(parts)

    # Fallback: scan raw_text for "City: …", "State: …" lines.
    city = state = country = ""
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if line.lower().startswith("city:"):
            city = line.split(":", 1)[1].strip()
        elif line.lower().startswith("state:"):
            state = line.split(":", 1)[1].strip()
        elif line.lower().startswith("country:"):
            country = line.split(":", 1)[1].strip()
    parts = [p for p in (city, state, country) if p]
    return ", ".join(parts)
