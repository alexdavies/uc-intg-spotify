"""
Bang & Olufsen Mozart API client wrapper.

Wraps the official ``mozart-api`` library (REST + WebSocket) behind a small,
async surface tailored to what the Unfolded Circle entities need: discovery of
sources and presets (radio favourites), transport/volume control, real-time
state via the notification WebSocket, and Beolink multiroom.

The wrapper deliberately swallows library exceptions and returns simple
success booleans / plain dictionaries so the entity layer stays free of
mozart-api types.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from mozart_api.mozart_client import MozartClient
from mozart_api.models.volume_level import VolumeLevel
from mozart_api.models.volume_mute import VolumeMute

_LOG = logging.getLogger(__name__)

# Named transport commands accepted by POST /BeoZone/Zone/Stream/.../playbackCommand.
PLAYBACK_PLAY = "play"
PLAYBACK_PAUSE = "pause"
PLAYBACK_STOP = "stop"
PLAYBACK_NEXT = "next"
PLAYBACK_PREVIOUS = "previous"

# RenderingState.value strings that mean "actively playing".
PLAYING_STATES = {"started", "playing", "buffering"}


class BeoClient:
    """Async wrapper around a single Bang & Olufsen Mozart device."""

    def __init__(self, host: str, name: Optional[str] = None, serial: Optional[str] = None):
        """
        Initialize the client.

        Args:
            host: IP address or hostname of the speaker.
            name: Friendly name (falls back to host).
            serial: Device serial number / unique id, if known.
        """
        self.host = host
        self.name = name or host
        self.serial = serial
        self._client = MozartClient(host)
        self._jid: Optional[str] = None
        self._volume_maximum: int = 100
        # id -> friendly name, populated by get_sources(); used to label the
        # active source in get_state() (Mozart has no active-source getter).
        self._source_names: Dict[str, str] = {}
        # Callback invoked with a dict of changed attributes on any push update.
        self.on_update: Optional[Callable[[Dict[str, Any]], Awaitable[None] | None]] = None
        self._notifications_started = False

    # ----- lifecycle -------------------------------------------------------

    async def connect(self) -> bool:
        """Verify the device is reachable. Returns True on success."""
        try:
            return await self._client.check_device_connection(raise_error=False)
        except Exception as e:
            _LOG.warning("Could not reach Bang & Olufsen device %s: %s", self.host, e)
            return False

    async def start_notifications(self) -> None:
        """Register push callbacks and open the notification WebSocket."""
        if self._notifications_started:
            return

        self._client.get_playback_state_notifications(self._on_state)
        self._client.get_playback_metadata_notifications(self._on_metadata)
        self._client.get_playback_progress_notifications(self._on_progress)
        self._client.get_volume_notifications(self._on_volume)
        self._client.get_source_change_notifications(self._on_source)

        try:
            await self._client.connect_notifications(reconnect=True)
            self._notifications_started = True
            _LOG.info("Notification stream connected for %s", self.name)
        except Exception as e:
            _LOG.error("Failed to open notification stream for %s: %s", self.name, e)

    async def close(self) -> None:
        """Tear down the WebSocket and HTTP client."""
        try:
            self._client.disconnect_notifications()
        except Exception:
            pass
        try:
            await self._client.close_api_client()
        except Exception:
            pass
        self._notifications_started = False

    # ----- read state ------------------------------------------------------

    async def get_beolink_jid(self) -> Optional[str]:
        """Return (and cache) this device's Beolink JID, used for multiroom."""
        if self._jid:
            return self._jid
        try:
            me = await self._client.get_beolink_self()
            self._jid = me.jid
        except Exception as e:
            _LOG.debug("Could not read Beolink JID for %s: %s", self.name, e)
        return self._jid

    async def get_sources(self) -> List[Dict[str, str]]:
        """Return selectable playback sources as {id, name} dicts."""
        try:
            sources = await self._client.get_available_sources(target_remote=False)
        except Exception as e:
            _LOG.warning("Could not fetch sources for %s: %s", self.name, e)
            return []

        result = []
        for source in (sources.items or []):
            # is_enabled / is_playable are Optional and often None or False when
            # idle, so only exclude sources that are *explicitly* disabled.
            if not source.id or source.is_enabled is False:
                continue
            result.append({"id": source.id, "name": source.name or source.id})
        self._source_names = {s["id"]: s["name"] for s in result}
        return result

    async def get_presets(self) -> List[Dict[str, Any]]:
        """
        Return presets (the physical favourite buttons - typically radio
        stations) as {id, name} dicts. ``id`` is the integer preset number.
        """
        try:
            presets = await self._client.get_presets()
        except Exception as e:
            _LOG.warning("Could not fetch presets for %s: %s", self.name, e)
            return []

        result = []
        for key, preset in (presets or {}).items():
            # The dict key ('1'..'4') is the integer preset number that
            # activate_preset() expects. ``preset.id`` is an unrelated UUID and
            # must NOT be used for activation.
            preset_id = _safe_int(key)
            if preset_id is None:
                continue
            label = preset.title or preset.name or f"Preset {preset_id}"
            result.append({"id": preset_id, "name": label})
        result.sort(key=lambda p: p["id"])
        return result

    async def get_state(self) -> Dict[str, Any]:
        """Fetch a full state snapshot (used on startup, before push updates)."""
        state: Dict[str, Any] = {}
        try:
            playback = await self._client.get_playback_state()
            if playback.state and playback.state.value:
                state["playing"] = playback.state.value in PLAYING_STATES
            if playback.metadata:
                state.update(_metadata_to_attrs(playback.metadata))
                # Mozart has no active-source getter; the metadata carries the
                # current source id, which the push path (_on_source) also sends.
                source_id = getattr(playback.metadata, "source", None)
                if source_id:
                    state["source_id"] = source_id
                    name = self._source_names.get(source_id)
                    if not name:
                        await self.get_sources()  # refresh id -> name map
                        name = self._source_names.get(source_id)
                    if name:
                        state["source_name"] = name
            if playback.progress and playback.progress.progress is not None:
                state["position"] = playback.progress.progress
        except Exception as e:
            _LOG.debug("Could not fetch playback state for %s: %s", self.name, e)

        try:
            volume = await self._client.get_current_volume()
            state.update(self._volume_to_attrs(volume))
        except Exception as e:
            _LOG.debug("Could not fetch volume for %s: %s", self.name, e)

        try:
            power = await self._client.get_power_state()
            if power.value is not None:
                state["on"] = power.value == "on"
        except Exception as e:
            _LOG.debug("Could not fetch power state for %s: %s", self.name, e)

        return state

    # ----- commands --------------------------------------------------------

    async def play(self) -> bool:
        return await self._playback(PLAYBACK_PLAY)

    async def pause(self) -> bool:
        return await self._playback(PLAYBACK_PAUSE)

    async def play_pause(self) -> bool:
        """Toggle play/pause based on the device's *live* state."""
        playing = False
        try:
            pb = await self._client.get_playback_state()
            if pb and pb.state and pb.state.value:
                playing = pb.state.value in PLAYING_STATES
        except Exception as e:
            _LOG.debug("Could not read playback state on %s: %s", self.name, e)
        return await (self.pause() if playing else self.play())

    async def stop(self) -> bool:
        return await self._playback(PLAYBACK_STOP)

    async def next_track(self) -> bool:
        return await self._playback(PLAYBACK_NEXT)

    async def previous_track(self) -> bool:
        return await self._playback(PLAYBACK_PREVIOUS)

    async def _playback(self, command: str) -> bool:
        try:
            await self._client.post_playback_command(command=command)
            return True
        except Exception as e:
            _LOG.error("Playback command '%s' failed on %s: %s", command, self.name, e)
            return False

    async def set_volume(self, percent: int) -> bool:
        """Set absolute volume from a 0-100 percentage."""
        percent = max(0, min(100, percent))
        level = round(percent * self._volume_maximum / 100)
        try:
            await self._client.set_current_volume_level(VolumeLevel(level=level))
            return True
        except Exception as e:
            _LOG.error("Set volume failed on %s: %s", self.name, e)
            return False

    async def set_mute(self, muted: bool) -> bool:
        try:
            await self._client.set_volume_mute(VolumeMute(muted=muted))
            return True
        except Exception as e:
            _LOG.error("Set mute failed on %s: %s", self.name, e)
            return False

    async def activate_preset(self, preset_id: int) -> bool:
        """Trigger a preset (radio favourite). Wakes the device from standby."""
        try:
            await self._client.activate_preset(id=preset_id)
            return True
        except Exception as e:
            _LOG.error("Activate preset %s failed on %s: %s", preset_id, self.name, e)
            return False

    async def set_source(self, source_id: str) -> bool:
        try:
            await self._client.set_active_source(source_id=source_id)
            return True
        except Exception as e:
            _LOG.error("Set source '%s' failed on %s: %s", source_id, self.name, e)
            return False

    async def power_on(self) -> bool:
        """Wake the device. Mozart has no explicit 'on'; resume playback."""
        return await self.play()

    async def standby(self) -> bool:
        try:
            await self._client.post_standby()
            return True
        except Exception as e:
            _LOG.error("Standby failed on %s: %s", self.name, e)
            return False

    # ----- multiroom (Beolink) ---------------------------------------------

    async def beolink_expand(self, jid: str) -> bool:
        """Expand this device's current experience to a peer (play on both)."""
        try:
            await self._client.post_beolink_expand(jid=jid)
            return True
        except Exception as e:
            _LOG.error("Beolink expand to %s failed on %s: %s", jid, self.name, e)
            return False

    async def beolink_join(self, jid: str) -> bool:
        """Join the experience currently playing on a peer."""
        try:
            await self._client.join_beolink_peer(jid=jid)
            return True
        except Exception as e:
            _LOG.error("Beolink join %s failed on %s: %s", jid, self.name, e)
            return False

    async def beolink_leave(self) -> bool:
        try:
            await self._client.post_beolink_leave()
            return True
        except Exception as e:
            _LOG.error("Beolink leave failed on %s: %s", self.name, e)
            return False

    # ----- notification callbacks ------------------------------------------

    async def _emit(self, attrs: Dict[str, Any]) -> None:
        if attrs and self.on_update:
            result = self.on_update(attrs)
            if hasattr(result, "__await__"):
                await result

    async def _on_state(self, notification) -> None:
        value = getattr(notification, "value", None)
        if value is not None:
            await self._emit({"playing": value in PLAYING_STATES})

    async def _on_metadata(self, metadata) -> None:
        await self._emit(_metadata_to_attrs(metadata))

    async def _on_progress(self, progress) -> None:
        if getattr(progress, "progress", None) is not None:
            await self._emit({"position": progress.progress})

    async def _on_volume(self, volume) -> None:
        await self._emit(self._volume_to_attrs(volume))

    async def _on_source(self, source) -> None:
        if getattr(source, "id", None):
            await self._emit({"source_id": source.id, "source_name": source.name})

    def _volume_to_attrs(self, volume) -> Dict[str, Any]:
        # Mozart wraps each value in a sub-object: VolumeState.level is a
        # VolumeLevel with an int ``.level`` field, .maximum a VolumeMaximum
        # (also ``.level``), and .muted a Muted with a bool ``.muted`` field.
        attrs: Dict[str, Any] = {}
        maximum = _vol_int(getattr(volume, "maximum", None))
        if maximum:
            self._volume_maximum = maximum
        level = _vol_int(getattr(volume, "level", None))
        if level is not None:
            attrs["volume"] = round(level * 100 / self._volume_maximum)
        muted_obj = getattr(volume, "muted", None)
        muted = getattr(muted_obj, "muted", muted_obj)
        if isinstance(muted, bool):
            attrs["muted"] = muted
        return attrs


def _metadata_to_attrs(metadata) -> Dict[str, Any]:
    """Translate PlaybackContentMetadata into UC media-player attributes."""
    attrs: Dict[str, Any] = {}
    if getattr(metadata, "title", None):
        attrs["title"] = metadata.title
    if getattr(metadata, "artist_name", None):
        attrs["artist"] = metadata.artist_name
    if getattr(metadata, "album_name", None):
        attrs["album"] = metadata.album_name
    duration = getattr(metadata, "total_duration_seconds", None)
    if duration is not None:
        attrs["duration"] = duration
    art = getattr(metadata, "art", None)
    if art:
        # Prefer the largest available image.
        url = next((a.url for a in art if getattr(a, "url", None)), None)
        if url:
            attrs["image_url"] = url
    return attrs


def _safe_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _vol_int(obj) -> Optional[int]:
    """Extract an int from a Mozart volume wrapper (VolumeLevel/VolumeMaximum
    expose the number as ``.level``) or from a plain number."""
    if obj is None:
        return None
    inner = getattr(obj, "level", obj)
    return inner if isinstance(inner, (int, float)) else None
