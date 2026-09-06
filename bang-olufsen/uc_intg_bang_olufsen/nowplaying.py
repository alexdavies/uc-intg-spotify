"""
"Now playing" lookups for cast radio stations.

A cast stream carries no track metadata back through the speaker (the A9 just
echoes the station name we cast), so per-station providers fetch it directly:

- ``abc``: ABC Radio's public plays API (triple j etc.) - title, artist, artwork.
- ``icy``: Icecast/Shoutcast in-band metadata (``Icy-MetaData: 1``) - the
  ``StreamTitle`` ("Artist - Title") and, when the station sends one, a cover
  image in ``StreamUrl``.
- ``bbc``: BBC Sounds' public "rms" API - the current music segment (composer /
  work) or, between tracks, the on-air programme.

Each provider is a coroutine returning ``{"title", "artist", "image_url"}``
(any key may be missing/empty) or ``None`` when nothing is known. Providers
never raise; network errors just yield ``None``.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
import re
from typing import Any, Dict, Optional

import aiohttp

_LOG = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=5, sock_connect=5)
_UA = {"User-Agent": "uc-intg-bang-olufsen/0.2 (+https://github.com/alexdavies/uc-intg-spotify)"}

NowPlaying = Dict[str, str]


async def fetch(station: Dict[str, Any], session: aiohttp.ClientSession) -> Optional[NowPlaying]:
    """Look up what a station is playing, per its ``nowplaying`` config."""
    cfg = station.get("nowplaying") or {}
    kind = cfg.get("type")
    try:
        if kind == "abc":
            return await _abc(cfg.get("service", "triplej"), session)
        if kind == "icy":
            return await _icy(station["url"], session)
        if kind == "bbc":
            return await _bbc(cfg.get("service", "bbc_radio_three"), session)
    except Exception as e:  # noqa: BLE001 - metadata is best-effort
        _LOG.debug("now-playing lookup (%s) failed for %s: %s", kind, station.get("name"), e)
    return None


# ----- ABC (triple j, Double J, ...) ---------------------------------------

async def _abc(service: str, session: aiohttp.ClientSession) -> Optional[NowPlaying]:
    url = f"https://music.abcradio.net.au/api/v1/plays/{service}/now.json"
    async with session.get(url, timeout=_TIMEOUT, headers=_UA) as resp:
        if resp.status != 200:
            return None
        data = await resp.json(content_type=None)
    return parse_abc(data)


def parse_abc(data: Any) -> Optional[NowPlaying]:
    rec = ((data or {}).get("now") or {}).get("recording") or {}
    if not rec.get("title"):
        return None
    out: NowPlaying = {
        "title": rec.get("title", ""),
        "artist": ", ".join(a.get("name", "") for a in rec.get("artists", []) if a.get("name")),
    }
    releases = rec.get("releases") or []
    art = (releases[0].get("artwork") or []) if releases else []
    if art:
        # Prefer a square rendition around 600px; fall back to the original.
        sizes = [s for s in (art[0].get("sizes") or []) if s.get("aspect_ratio") == "1x1" and s.get("url")]
        sizes.sort(key=lambda s: abs(int(s.get("width") or 0) - 600))
        out["image_url"] = (sizes[0]["url"] if sizes else art[0].get("url")) or ""
    return out


# ----- Icecast / Shoutcast in-band metadata ---------------------------------

_META_RE = re.compile(r"StreamTitle='(.*?)';", re.S)
_URL_RE = re.compile(r"StreamUrl='(.*?)';", re.S)
_SEPARATORS = (" ˗ ", " - ", " – ", " — ")


async def _icy(url: str, session: aiohttp.ClientSession) -> Optional[NowPlaying]:
    headers = {**_UA, "Icy-MetaData": "1"}
    async with session.get(url, timeout=_TIMEOUT, headers=headers) as resp:
        if resp.status != 200:
            return None
        metaint = int(resp.headers.get("icy-metaint", "0") or 0)
        if not metaint:
            return None
        # Skip one audio block, then read the metadata block (length byte * 16).
        await resp.content.readexactly(metaint)
        length = (await resp.content.readexactly(1))[0] * 16
        raw = await resp.content.readexactly(length) if length else b""
    return parse_icy(raw.decode("utf-8", errors="replace"))


def parse_icy(meta: str) -> Optional[NowPlaying]:
    m = _META_RE.search(meta or "")
    if not m or not m.group(1).strip():
        return None
    text = m.group(1).strip()
    artist, title = "", text
    for sep in _SEPARATORS:
        if sep in text:
            artist, title = (p.strip() for p in text.split(sep, 1))
            break
    out: NowPlaying = {"title": title, "artist": artist}
    u = _URL_RE.search(meta or "")
    if u and re.search(r"\.(jpe?g|png|webp)(\?|$)", u.group(1), re.I):
        out["image_url"] = u.group(1)
    return out


# ----- BBC (Sounds "rms" API) ----------------------------------------------

async def _bbc(service: str, session: aiohttp.ClientSession) -> Optional[NowPlaying]:
    seg_url = f"https://rms.api.bbc.co.uk/v2/services/{service}/segments/latest?experience=domestic&offset=0&limit=1"
    async with session.get(seg_url, timeout=_TIMEOUT, headers=_UA) as resp:
        segments = await resp.json(content_type=None) if resp.status == 200 else None
    result = parse_bbc_segment(segments)
    if result:
        return result
    bc_url = f"https://rms.api.bbc.co.uk/v2/broadcasts/latest?service={service}&on_air=now"
    async with session.get(bc_url, timeout=_TIMEOUT, headers=_UA) as resp:
        broadcasts = await resp.json(content_type=None) if resp.status == 200 else None
    return parse_bbc_broadcast(broadcasts)


def _bbc_image(url: Optional[str], size: str = "640x640") -> str:
    return (url or "").replace("{recipe}", size)


def parse_bbc_segment(data: Any) -> Optional[NowPlaying]:
    items = (data or {}).get("data") or []
    if not items:
        return None
    seg = items[0]
    if not ((seg.get("offset") or {}).get("now_playing")):
        return None
    titles = seg.get("titles") or {}
    # For music: primary = composer/artist, secondary = work/track.
    return {
        "title": titles.get("secondary") or titles.get("primary") or "",
        "artist": titles.get("primary") if titles.get("secondary") else "",
        "image_url": _bbc_image(seg.get("image_url")),
    }


def parse_bbc_broadcast(data: Any) -> Optional[NowPlaying]:
    items = (data or {}).get("data") or []
    if not items:
        return None
    prog = items[0].get("programme") or {}
    titles = prog.get("titles") or {}
    if not titles.get("primary"):
        return None
    # e.g. primary "BBC Proms", secondary "2026", tertiary "Dvořák's 'New World'
    # Symphony": show the most specific part as the title, the rest as context.
    parts = [titles.get("primary"), titles.get("secondary"), titles.get("tertiary")]
    parts = [p for p in parts if p]
    return {
        "title": parts[-1],
        "artist": " · ".join(parts[:-1]),
        "image_url": _bbc_image(prog.get("image_url")),
    }
