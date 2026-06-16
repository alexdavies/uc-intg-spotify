"""
Per-speaker Bang & Olufsen media player entity.

One ``BeoPlayer`` fronts exactly one speaker (Mozart or legacy). Each speaker is
modelled as its own independent media player so its live state — now-playing,
volume, source — is unambiguous on the Remote. The driver creates one of these
per configured speaker; there is no shared/unified player or output selector.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

import ucapi
from ucapi.media_player import Attributes, Commands, DeviceClasses, Features, States

from uc_intg_bang_olufsen.cast import BeoCast

_LOG = logging.getLogger(__name__)

# Prefix marking radio stations in the source list.
RADIO_PREFIX = "Radio: "

# Physical inputs to keep in the source picker (match by name substring). Empty
# = picker shows only radio + playlists. Add e.g. "line", "optical" to include them.
_KEEP_INPUTS: tuple = ()


class BeoPlayer:
    """One media player entity for a single speaker."""

    def __init__(self, api: ucapi.IntegrationAPI, speaker: Dict[str, Any],
                 radio_stations: Optional[List[Dict[str, str]]] = None,
                 spotify=None, playlists: Optional[List[Dict[str, str]]] = None):
        """
        Args:
            speaker: {serial, name, client, sources}.
            radio_stations: custom cast radio list [{name, url, content_type}].
            spotify: optional shared SpotifyClient for playlist playback.
            playlists: curated Spotify playlists [{name, uri}].
        """
        self._api = api
        self._client = speaker["client"]
        self._serial = speaker["serial"]
        self._name = speaker.get("name") or self._serial
        self._spotify = spotify

        # The source picker should be the things you actually pick: radio,
        # playlists, and the physical inputs — not the long list of streaming/
        # system "sources" the speaker reports (Bluetooth, Tone Generator, ...).
        self._radio: Dict[str, Dict[str, str]] = {
            f"{RADIO_PREFIX}{s['name']}": s for s in (radio_stations or [])
        }
        self._playlists: Dict[str, str] = {p["name"]: p["uri"] for p in (playlists or [])}
        self._source_ids: Dict[str, str] = {
            src["name"]: src["id"] for src in speaker.get("sources", [])
            if any(k in (src["name"] or "").lower() for k in _KEEP_INPUTS)
        }
        self._cast = BeoCast(self._client.host, self._name)
        # Last known power state (Mozart reports it); used to ignore the
        # now-playing metadata B&O mirrors from other speakers while this one is off.
        self._powered: Optional[bool] = None
        # A Beolink-capable (Mozart) client used to join/leave multiroom. Set by
        # the driver when there's another speaker to group with.
        self._joiner = None
        self._other_name: str = ""

        # This speaker's push updates flow straight to this entity.
        self._client.on_update = self._on_update

        features = [
            Features.ON_OFF, Features.VOLUME, Features.VOLUME_UP_DOWN, Features.MUTE_TOGGLE,
            Features.PLAY_PAUSE, Features.STOP, Features.NEXT, Features.PREVIOUS,
            Features.MEDIA_DURATION, Features.MEDIA_POSITION, Features.MEDIA_TITLE,
            Features.MEDIA_ARTIST, Features.MEDIA_ALBUM, Features.MEDIA_IMAGE_URL,
            Features.SELECT_SOURCE,
        ]
        attributes = {
            Attributes.STATE: States.OFF,
            Attributes.VOLUME: 0,
            Attributes.MUTED: False,
            Attributes.MEDIA_TITLE: "", Attributes.MEDIA_ARTIST: "", Attributes.MEDIA_ALBUM: "",
            Attributes.MEDIA_DURATION: 0, Attributes.MEDIA_POSITION: 0, Attributes.MEDIA_IMAGE_URL: "",
            Attributes.SOURCE: "",
            Attributes.SOURCE_LIST: self._source_list(),
        }

        self.entity = ucapi.MediaPlayer(
            identifier=_entity_id(self._serial),
            name={"en": self._name},
            features=features,
            attributes=attributes,
            device_class=DeviceClasses.SPEAKER,
            cmd_handler=self.cmd_handler,
        )
        _LOG.info("Created B&O player '%s' (%s)", self._name, self.entity.id)

    # ----- lifecycle -------------------------------------------------------

    async def initialize(self) -> None:
        """Open the speaker's notification stream and load its current state."""
        await self._client.start_notifications()
        snapshot = await self._client.get_state()
        if snapshot:
            await self._apply(snapshot)

    # ----- command handling ------------------------------------------------

    async def cmd_handler(self, entity, cmd_id: str, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        _LOG.info("[%s] command %s %s", self._name, cmd_id, params)
        try:
            # Spotify Connect owns transport while it's the active source — B&O's
            # next/previous are no-ops there — so route skip/play to the Web API.
            if self._spotify_active():
                spotify_transport = {
                    Commands.NEXT: self._spotify.next_track,
                    Commands.PREVIOUS: self._spotify.previous_track,
                    Commands.PLAY_PAUSE: self._spotify.play_pause,
                    Commands.STOP: self._spotify.pause,
                }
                if cmd_id in spotify_transport:
                    return _status(await spotify_transport[cmd_id]())
            simple = {
                Commands.ON: self._client.power_on,
                Commands.OFF: self._client.standby,
                Commands.PLAY_PAUSE: self._client.play_pause,
                Commands.STOP: self._client.stop,
                Commands.NEXT: self._client.next_track,
                Commands.PREVIOUS: self._client.previous_track,
                Commands.MUTE_TOGGLE: self._mute_toggle,
                Commands.VOLUME_UP: lambda: self._nudge_volume(5),
                Commands.VOLUME_DOWN: lambda: self._nudge_volume(-5),
            }
            if cmd_id in simple:
                return _status(await simple[cmd_id]())
            if cmd_id == Commands.VOLUME:
                return await self._set_volume(params)
            if cmd_id == Commands.SELECT_SOURCE:
                return await self._select_source(params)
            return ucapi.StatusCodes.NOT_IMPLEMENTED
        except Exception as e:
            _LOG.error("[%s] error handling %s: %s", self._name, cmd_id, e)
            return ucapi.StatusCodes.SERVER_ERROR

    async def _select_source(self, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        if not params or "source" not in params:
            return ucapi.StatusCodes.BAD_REQUEST
        source = params["source"]
        if source in self._radio:
            station = self._radio[source]
            ok = await self._cast_station(station)
            if ok:
                # The cast carries no metadata back via the speaker's push, so
                # set the now-playing card (station name + logo) ourselves.
                self._update({
                    Attributes.SOURCE: source,
                    Attributes.MEDIA_TITLE: station["name"],
                    Attributes.MEDIA_ARTIST: "",
                    Attributes.MEDIA_ALBUM: "",
                    Attributes.MEDIA_IMAGE_URL: station.get("image", ""),
                    Attributes.STATE: States.PLAYING,
                })
            return _status(ok)
        if source in self._playlists:
            return _status(await self.play_spotify_playlist(self._playlists[source], source))
        if source in self._source_ids:
            ok = await self._client.set_source(self._source_ids[source])
            if ok:
                self._update({Attributes.SOURCE: source})
            return _status(ok)
        _LOG.warning("[%s] unknown source '%s'", self._name, source)
        return ucapi.StatusCodes.BAD_REQUEST

    async def play_spotify_playlist(self, uri: str, name: str = "") -> bool:
        """Start a Spotify playlist on this speaker via Spotify Connect."""
        if not self._spotify:
            return False
        # Fast path: target the known Connect device directly — Spotify wakes it,
        # so no B&O source wake / fixed delay is needed in the common case.
        device_id = self._spotify.cached_device_id(self._name) \
            or await self._spotify.resolve_device_id(self._name)
        if device_id and await self._spotify.start_playlist(uri, device_id):
            self._spotify_now_playing(name)
            return True

        # Fallback: the device wasn't reachable; wake this speaker's Spotify source
        # so it registers with Spotify, then retry (this is the slower path).
        spotify_src = self._source_ids.get("Spotify Connect") or self._source_ids.get("Spotify")
        if spotify_src:
            await self._client.set_source(spotify_src)
            await asyncio.sleep(1.5)
        device_id = await self._spotify.resolve_device_id(self._name)
        if not device_id:
            _LOG.error("[%s] no matching Spotify Connect device found", self._name)
            return False
        ok = await self._spotify.start_playlist(uri, device_id)
        if ok:
            self._spotify_now_playing(name)
        return ok

    def _spotify_active(self) -> bool:
        """True when this speaker's current source is Spotify (so transport should
        go through the Spotify Web API rather than the no-op B&O skip)."""
        source = (self.entity.attributes.get(Attributes.SOURCE) or "").lower()
        return self._spotify is not None and "spotify" in source

    def _spotify_now_playing(self, name: str) -> None:
        # Immediate feedback (don't clear art); the poll loop fills the real track.
        self._update({
            Attributes.SOURCE: "Spotify Connect" if "Spotify Connect" in self._source_ids else "Spotify",
            Attributes.MEDIA_TITLE: name,
            Attributes.STATE: States.PLAYING,
        })
        asyncio.ensure_future(self._first_now_playing())

    async def _first_now_playing(self) -> None:
        """Snappy first now-playing fetch (the driver's poll loop keeps it fresh)."""
        try:
            await asyncio.sleep(1.0)  # let the new track register with Spotify
            info = await self._spotify.get_now_playing()
            if info and (not info.get("device") or self.is_spotify_device(info["device"])):
                self.apply_now_playing(info)
        except Exception:  # noqa: BLE001
            pass

    def is_spotify_device(self, device_name: str) -> bool:
        """Whether a Spotify Connect device name refers to this speaker."""
        n = self._name.lower()
        d = (device_name or "").lower()
        return bool(d) and (d in n or n in d)

    def apply_now_playing(self, info: Dict[str, Any]) -> None:
        """Update the card from a Spotify now-playing snapshot (poll or push)."""
        mapped: Dict[str, Any] = {
            Attributes.SOURCE: "Spotify Connect" if "Spotify Connect" in self._source_ids else "Spotify",
            Attributes.STATE: States.PLAYING if info.get("is_playing", True) else States.PAUSED,
            Attributes.MEDIA_TITLE: info.get("title", ""),
            Attributes.MEDIA_ARTIST: info.get("artist", ""),
            Attributes.MEDIA_ALBUM: info.get("album", ""),
        }
        if "position" in info:
            mapped[Attributes.MEDIA_POSITION] = info["position"]
        if "duration" in info:
            mapped[Attributes.MEDIA_DURATION] = info["duration"]
        if info.get("image_url"):
            mapped[Attributes.MEDIA_IMAGE_URL] = info["image_url"]
        self._update(mapped)

    async def _cast_station(self, station: Dict[str, str]) -> bool:
        """Cast a radio stream URL to this speaker's Chromecast (off the loop)."""
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._cast.play_sync,
                station["url"], station.get("content_type", "audio/mpeg"),
                station["name"], station.get("image"),
            )
        except Exception as e:  # noqa: BLE001
            _LOG.error("[%s] cast of %r failed: %s", self._name, station.get("name"), e)
            return False

    async def _mute_toggle(self) -> bool:
        muted = bool(self.entity.attributes.get(Attributes.MUTED))
        return await self._client.set_mute(not muted)

    async def _nudge_volume(self, delta: int) -> bool:
        current = self.entity.attributes.get(Attributes.VOLUME, 0)
        return await self._client.set_volume(max(0, min(100, current + delta)))

    async def _set_volume(self, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        if not params or "volume" not in params:
            return ucapi.StatusCodes.BAD_REQUEST
        return _status(await self._client.set_volume(int(params["volume"])))

    # ----- helpers ---------------------------------------------------------

    def _source_list(self) -> List[str]:
        return list(self._radio) + list(self._playlists) + list(self._source_ids)

    def close(self) -> None:
        """Release the Chromecast connection (called on shutdown)."""
        self._cast.disconnect()

    # ----- multiroom (Beolink) --------------------------------------------

    def set_multiroom(self, joiner, other_name: str) -> None:
        """Enable a multiroom button. ``joiner`` is the Beolink-capable (Mozart)
        client that will join/leave the group; ``other_name`` labels the button."""
        self._joiner = joiner
        self._other_name = other_name

    @property
    def has_multiroom(self) -> bool:
        return self._joiner is not None

    @property
    def other_name(self) -> str:
        return self._other_name

    async def join_multiroom(self) -> bool:
        """Group the speakers: the Mozart speaker joins the current experience."""
        return bool(self._joiner) and await self._joiner.beolink_join_latest()

    async def leave_multiroom(self) -> bool:
        return bool(self._joiner) and await self._joiner.beolink_leave()

    async def _on_update(self, attrs: Dict[str, Any]) -> None:
        await self._apply(attrs)

    async def _apply(self, attrs: Dict[str, Any]) -> None:
        """Translate a state dict from the client into entity attributes."""
        mapped: Dict[str, Any] = {}
        if attrs.get("on") is not None:
            self._powered = attrs["on"]
        if "playing" in attrs and self._powered is not False:
            mapped[Attributes.STATE] = States.PLAYING if attrs["playing"] else States.PAUSED
        if attrs.get("on") is False:
            mapped[Attributes.STATE] = States.OFF
        if "volume" in attrs:
            mapped[Attributes.VOLUME] = attrs["volume"]
        if "muted" in attrs:
            mapped[Attributes.MUTED] = attrs["muted"]
        if attrs.get("source_name"):
            mapped[Attributes.SOURCE] = attrs["source_name"]

        if self._powered is False:
            # Speaker is off. B&O mirrors other speakers' now-playing over the
            # network, so ignore that metadata and keep this card empty.
            mapped.update({
                Attributes.MEDIA_TITLE: "", Attributes.MEDIA_ARTIST: "",
                Attributes.MEDIA_ALBUM: "", Attributes.MEDIA_IMAGE_URL: "",
                Attributes.MEDIA_POSITION: 0, Attributes.MEDIA_DURATION: 0,
            })
        else:
            if "title" in attrs:
                mapped[Attributes.MEDIA_TITLE] = attrs["title"]
            if "artist" in attrs:
                mapped[Attributes.MEDIA_ARTIST] = attrs["artist"]
            if "album" in attrs:
                mapped[Attributes.MEDIA_ALBUM] = attrs["album"]
            if "duration" in attrs:
                mapped[Attributes.MEDIA_DURATION] = attrs["duration"]
            if "position" in attrs:
                mapped[Attributes.MEDIA_POSITION] = attrs["position"]
            if "image_url" in attrs:
                mapped[Attributes.MEDIA_IMAGE_URL] = attrs["image_url"]
        self._update(mapped)

    def _update(self, attrs: Dict[str, Any]) -> None:
        changed = {k: v for k, v in attrs.items() if self.entity.attributes.get(k) != v}
        if changed:
            # Keep the entity's own cache authoritative so reads (volume nudge,
            # mute toggle) don't depend on the API echoing the update back.
            self.entity.attributes.update(changed)
            self._api.configured_entities.update_attributes(self.entity.id, changed)


def _entity_id(serial: str) -> str:
    """Stable, safe entity id derived from the speaker serial."""
    return "beo_player_" + re.sub(r"[^A-Za-z0-9_]+", "_", str(serial)).strip("_")


def _status(ok: bool) -> ucapi.StatusCodes:
    return ucapi.StatusCodes.OK if ok else ucapi.StatusCodes.SERVER_ERROR
