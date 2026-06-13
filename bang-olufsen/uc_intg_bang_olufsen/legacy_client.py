"""
Bang & Olufsen legacy ("ASE" / BeoNetRemote) API client.

For older Beoplay speakers (e.g. Beoplay A9 4th gen) that predate the Mozart
platform. They expose a REST API on port 8080 under /BeoDevice and /BeoZone,
plus a chunked /BeoNotify/Notifications stream for real-time state.

This class mirrors the public interface of ``BeoClient`` (the Mozart wrapper) so
the media-player and remote entities can use either backend unchanged. Methods
return plain booleans / dicts and swallow transport errors.

Endpoints and field shapes here are confirmed against a Beoplay A9 4th gen
(software 6.5.x); parsing is deliberately defensive about field names.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp

_LOG = logging.getLogger(__name__)

# Volume on legacy speakers is 0-90; the UC entities work in 0-100.
LEGACY_VOLUME_MAX = 90

# Notification types emitted by /BeoNotify that we care about.
_NOW_PLAYING_TYPES = {
    "NOW_PLAYING_NET_RADIO",
    "NOW_PLAYING_STORED_MUSIC",
    "NOW_PLAYING_LEGACY",
    "NOW_PLAYING_MUSIC",
}


class LegacyBeoClient:
    """Async client for a single legacy ASE/BeoZone speaker."""

    def __init__(self, host: str, name: Optional[str] = None, serial: Optional[str] = None):
        self.host = host
        self.name = name or host
        self.serial = serial
        self._base = f"http://{host}:8080"
        self._session: Optional[aiohttp.ClientSession] = None
        self._notify_task: Optional[asyncio.Task] = None
        self.on_update: Optional[Callable[[Dict[str, Any]], Awaitable[None] | None]] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ----- lifecycle -------------------------------------------------------

    async def connect(self) -> bool:
        data = await self._request("GET", "/BeoDevice")
        return data is not None

    async def start_notifications(self) -> None:
        if self._notify_task and not self._notify_task.done():
            return
        self._notify_task = asyncio.ensure_future(self._notification_loop())
        _LOG.info("Notification stream task started for %s", self.name)

    async def close(self) -> None:
        if self._notify_task:
            self._notify_task.cancel()
            try:
                await self._notify_task
            except (asyncio.CancelledError, Exception):
                pass
            self._notify_task = None
        if self._session and not self._session.closed:
            await self._session.close()

    # ----- HTTP helpers ----------------------------------------------------

    async def _request(self, method: str, path: str, json: Any = None) -> Optional[Any]:
        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(total=6, connect=4, sock_connect=4)
            async with session.request(method, self._base + path, json=json, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    try:
                        return await resp.json(content_type=None)
                    except Exception:
                        return {}
                _LOG.debug("%s %s -> HTTP %s", method, path, resp.status)
                return None
        except Exception as e:
            _LOG.debug("%s %s failed: %s", method, path, e)
            return None

    # ----- read state ------------------------------------------------------

    async def get_sources(self) -> List[Dict[str, str]]:
        data = await self._request("GET", "/BeoZone/Zone/Sources")
        result: List[Dict[str, str]] = []
        for source_id, meta in _iter_sources(data):
            if not source_id:
                continue
            name = (meta or {}).get("friendlyName") or (meta or {}).get("sourceType", {}).get("type") or source_id
            # Skip sources that are not currently usable when the flag is present.
            if (meta or {}).get("inUse") is False and (meta or {}).get("category") == "DELETED":
                continue
            result.append({"id": source_id, "name": name})
        return result

    async def get_presets(self) -> List[Dict[str, Any]]:
        # The A9 4th gen exposes no Favorites endpoint over the API; radio is
        # reached via the "B&O Radio" source instead. Kept for interface parity.
        return []

    async def get_state(self) -> Dict[str, Any]:
        state: Dict[str, Any] = {}

        level = await self._request("GET", "/BeoZone/Zone/Sound/Volume/Speaker/Level")
        if isinstance(level, dict) and "level" in level:
            state["volume"] = _to_percent(level["level"])

        muted = await self._request("GET", "/BeoZone/Zone/Sound/Volume/Speaker/Muted")
        if isinstance(muted, dict) and "muted" in muted:
            state["muted"] = bool(muted["muted"])

        active = await self._request("GET", "/BeoZone/Zone/ActiveSources")
        source = _active_source(active)
        if source:
            state["source_id"], state["source_name"] = source

        return state

    async def get_beolink_jid(self) -> Optional[str]:
        # Legacy multiroom uses a different join model; not exposed as a JID.
        return None

    # ----- commands --------------------------------------------------------

    async def play(self) -> bool:
        return await self._command("POST", "/BeoZone/Zone/Stream/Play")

    async def pause(self) -> bool:
        return await self._command("POST", "/BeoZone/Zone/Stream/Pause")

    async def stop(self) -> bool:
        return await self._command("POST", "/BeoZone/Zone/Stream/Stop")

    async def next_track(self) -> bool:
        return await self._command("POST", "/BeoZone/Zone/Stream/Forward")

    async def previous_track(self) -> bool:
        return await self._command("POST", "/BeoZone/Zone/Stream/Backward")

    async def set_volume(self, percent: int) -> bool:
        percent = max(0, min(100, percent))
        level = round(percent * LEGACY_VOLUME_MAX / 100)
        return await self._command("PUT", "/BeoZone/Zone/Sound/Volume/Speaker/Level", {"level": level})

    async def set_mute(self, muted: bool) -> bool:
        return await self._command("PUT", "/BeoZone/Zone/Sound/Volume/Speaker/Muted", {"muted": muted})

    async def set_source(self, source_id: str) -> bool:
        body = {"primaryExperience": {"source": {"id": source_id}}}
        return await self._command("POST", "/BeoZone/Zone/ActiveSources", body)

    async def activate_preset(self, preset_id: int) -> bool:
        # No preset API on this model.
        _LOG.info("Preset activation is not supported on legacy device %s", self.name)
        return False

    async def power_on(self) -> bool:
        return await self._command("PUT", "/BeoDevice/powerManagement/standby",
                                   {"standby": {"powerState": "on"}})

    async def standby(self) -> bool:
        return await self._command("PUT", "/BeoDevice/powerManagement/standby",
                                   {"standby": {"powerState": "standby"}})

    # Multiroom on legacy is limited and model-specific; expose no-op stubs so
    # the remote entity's interface stays uniform.
    async def beolink_expand(self, jid: str) -> bool:
        _LOG.info("Beolink expand not supported on legacy device %s", self.name)
        return False

    async def beolink_join(self, jid: str) -> bool:
        return False

    async def beolink_leave(self) -> bool:
        return False

    async def _command(self, method: str, path: str, body: Any = None) -> bool:
        result = await self._request(method, path, json=body)
        return result is not None

    # ----- notifications ---------------------------------------------------

    async def _notification_loop(self) -> None:
        """Read the chunked /BeoNotify stream and translate events to updates."""
        url = self._base + "/BeoNotify/Notifications"
        while True:
            try:
                session = await self._get_session()
                timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_connect=5)
                async with session.get(url, timeout=timeout) as resp:
                    async for raw in resp.content:
                        line = raw.decode(errors="replace").strip().rstrip(",")
                        if not line:
                            continue
                        await self._handle_notification(line)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _LOG.debug("Notification stream for %s dropped: %s; retrying", self.name, e)
                await asyncio.sleep(5)

    async def _handle_notification(self, line: str) -> None:
        import json
        try:
            payload = json.loads(line)
        except ValueError:
            return
        note = payload.get("notification", payload)
        ntype = note.get("type")
        data = note.get("data") or {}
        attrs = _notification_to_attrs(ntype, data)
        if attrs and self.on_update:
            result = self.on_update(attrs)
            if hasattr(result, "__await__"):
                await result


# ---------------------------------------------------------------------------
# Parsing helpers (kept module-level and pure, so they are unit-testable)
# ---------------------------------------------------------------------------

def _iter_sources(data: Any):
    """Yield (source_id, meta) pairs from the ASE /Sources response."""
    if not isinstance(data, dict):
        return
    sources = data.get("sources")
    if isinstance(sources, list):
        for entry in sources:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                yield entry[0], entry[1]
            elif isinstance(entry, dict):
                yield entry.get("id"), entry


def _active_source(data: Any):
    """Return (id, friendlyName) of the active source, or None."""
    if not isinstance(data, dict):
        return None
    src = (data.get("primaryExperience") or {}).get("source") or {}
    if src.get("id"):
        return src["id"], src.get("friendlyName") or src["id"]
    return None


def _notification_to_attrs(ntype: Optional[str], data: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a single BeoNotify notification into entity-update attributes."""
    attrs: Dict[str, Any] = {}
    if not ntype:
        return attrs

    if ntype == "VOLUME":
        speaker = data.get("speaker") or data
        if "level" in speaker:
            attrs["volume"] = _to_percent(speaker["level"])
        if "muted" in speaker:
            attrs["muted"] = bool(speaker["muted"])

    elif ntype == "PROGRESS_INFORMATION":
        if data.get("state") is not None:
            attrs["playing"] = str(data["state"]).lower() in {"play", "playing"}
        if data.get("position") is not None:
            attrs["position"] = data["position"]
        if data.get("totalDuration") is not None:
            attrs["duration"] = data["totalDuration"]

    elif ntype in _NOW_PLAYING_TYPES:
        title = data.get("name") or data.get("title") or data.get("liveDescription") or data.get("trackName")
        if title:
            attrs["title"] = title
        artist = data.get("artist") or data.get("stationName") or data.get("genre")
        if artist:
            attrs["artist"] = artist
        album = data.get("album")
        if album:
            attrs["album"] = album

    elif ntype == "SOURCE":
        src = (data.get("primaryExperience") or {}).get("source") or data.get("source") or {}
        if src.get("id"):
            attrs["source_id"] = src["id"]
            attrs["source_name"] = src.get("friendlyName") or src["id"]

    return attrs


def _to_percent(level: Any) -> int:
    try:
        return round(int(level) * 100 / LEGACY_VOLUME_MAX)
    except (TypeError, ValueError):
        return 0
