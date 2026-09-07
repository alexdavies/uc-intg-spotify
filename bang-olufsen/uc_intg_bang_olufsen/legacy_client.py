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
        self._is_playing: Optional[bool] = None
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

    async def restart_notifications(self) -> None:
        """Drop and reopen the /BeoNotify stream (after a suspend the old TCP
        connection may be dead without ever raising)."""
        if self._notify_task and not self._notify_task.done():
            self._notify_task.cancel()
            try:
                await self._notify_task
            except (asyncio.CancelledError, Exception):
                pass
        self._notify_task = None
        await self.start_notifications()

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
                    # Commands return an empty 2xx body, which parses to None;
                    # never return None on success or callers read it as failure.
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = None
                    return {} if data is None else data
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

        on = await self.is_on()
        if on is not None:
            state["on"] = on

        return state

    async def get_volume(self) -> Optional[int]:
        """Current volume as a 0-100 percentage, or None if unreadable."""
        level = await self._request("GET", "/BeoZone/Zone/Sound/Volume/Speaker/Level")
        if isinstance(level, dict) and "level" in level:
            return _to_percent(level["level"])
        return None

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

    async def play_pause(self) -> bool:
        """Toggle play/pause using the last state seen on the notification stream."""
        if self._is_playing:
            return await self.pause()
        return await self.play()

    async def next_track(self) -> bool:
        return await self._command("POST", "/BeoZone/Zone/Stream/Forward")

    async def previous_track(self) -> bool:
        return await self._command("POST", "/BeoZone/Zone/Stream/Backward")

    async def set_volume(self, percent: int) -> bool:
        """Set the volume. The ASE firmware answers 200 but silently ignores
        volume writes while the speaker is in standby, and applies accepted
        writes asynchronously - so poll the level back briefly and only report
        failure when it never changes *and* the speaker is in standby."""
        percent = max(0, min(100, percent))
        level = round(percent * LEGACY_VOLUME_MAX / 100)
        if not await self._command("PUT", "/BeoZone/Zone/Sound/Volume/Speaker/Level", {"level": level}):
            return False
        for _ in range(5):
            readback = await self._request("GET", "/BeoZone/Zone/Sound/Volume/Speaker/Level")
            applied = _safe_int((readback or {}).get("level")) if isinstance(readback, dict) else None
            if applied is None or applied == level:
                return True
            await asyncio.sleep(0.1)
        if await self.is_on() is False:
            _LOG.info("%s ignored volume %s - speaker is in standby", self.name, level)
            return False
        return True  # accepted; the device is just slow to reflect it

    async def is_on(self) -> Optional[bool]:
        """Power state: True (on), False (standby) or None if unreadable."""
        power = await self._request("GET", "/BeoDevice/powerManagement/standby")
        power_state = ((power or {}).get("standby") or {}).get("powerState") if isinstance(power, dict) else None
        return None if not power_state else power_state == "on"

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
        # The ASE API rejects any POST/PUT that lacks a JSON content-type with
        # HTTP 400 ('Content-Type must be "application/json"'). Bodyless commands
        # (play/pause/stop/forward/backward) therefore need an explicit empty
        # JSON body so aiohttp sets the header.
        result = await self._request(method, path, json=body if body is not None else {})
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
        if "playing" in attrs:
            self._is_playing = attrs["playing"]
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
            # "preparing" is emitted repeatedly while a cast/stream buffers
            # (verified on the A9); treat it as playing so the card doesn't flap.
            attrs["playing"] = str(data["state"]).lower() in {"play", "playing", "preparing"}
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
        images = data.get("trackImage") or data.get("image") or []
        if isinstance(images, list):
            # Prefer the large rendition; fall back to whatever has a URL.
            large = next((i.get("url") for i in images if isinstance(i, dict) and i.get("size") == "large" and i.get("url")), None)
            url = large or next((i.get("url") for i in images if isinstance(i, dict) and i.get("url")), None)
            if url:
                attrs["image_url"] = url

    elif ntype == "SHUTDOWN":
        # Verified on the A9 4th gen: entering standby emits SHUTDOWN
        # {"reason": "standby"}. There is no matching "on" event; the player
        # infers power-on from the next playing/source update.
        if str(data.get("reason", "")).lower() in {"standby", "allstandby", "off"}:
            attrs["on"] = False

    elif ntype == "SOURCE":
        src = (data.get("primaryExperience") or {}).get("source") or data.get("source") or {}
        if src.get("id"):
            attrs["source_id"] = src["id"]
            attrs["source_name"] = src.get("friendlyName") or src["id"]
            # A real source becoming active means the speaker is on (there is
            # no explicit power-on notification on this firmware).
            attrs["on"] = True

    return attrs


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_percent(level: Any) -> int:
    try:
        return round(int(level) * 100 / LEGACY_VOLUME_MAX)
    except (TypeError, ValueError):
        return 0
