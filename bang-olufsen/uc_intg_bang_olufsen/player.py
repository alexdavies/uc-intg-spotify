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


class BeoPlayer:
    """One media player entity for a single speaker."""

    def __init__(self, api: ucapi.IntegrationAPI, speaker: Dict[str, Any],
                 radio_stations: Optional[List[Dict[str, str]]] = None,
                 spotify=None):
        """
        Args:
            speaker: {serial, name, client, sources}.
            radio_stations: custom cast radio list [{name, url, content_type}].
            spotify: optional shared SpotifyClient for playlist playback.
        """
        self._api = api
        self._client = speaker["client"]
        self._serial = speaker["serial"]
        self._name = speaker.get("name") or self._serial
        self._spotify = spotify

        # Native input sources (Spotify, Line-In, ...): name -> id.
        self._source_ids: Dict[str, str] = {
            src["name"]: src["id"] for src in speaker.get("sources", [])
        }
        # Custom radio list, played by casting a stream URL: "Radio: X" -> station.
        self._radio: Dict[str, Dict[str, str]] = {
            f"{RADIO_PREFIX}{s['name']}": s for s in (radio_stations or [])
        }
        self._cast = BeoCast(self._client.host, self._name)

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
        # Wake this speaker's Spotify source so it registers as a Connect device.
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
            self._update({
                Attributes.SOURCE: "Spotify Connect" if "Spotify Connect" in self._source_ids else "Spotify",
                Attributes.MEDIA_TITLE: name,
                Attributes.MEDIA_IMAGE_URL: "",
                Attributes.STATE: States.PLAYING,
            })
        return ok

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
        return list(self._source_ids) + list(self._radio)

    def close(self) -> None:
        """Release the Chromecast connection (called on shutdown)."""
        self._cast.disconnect()

    async def _on_update(self, attrs: Dict[str, Any]) -> None:
        await self._apply(attrs)

    async def _apply(self, attrs: Dict[str, Any]) -> None:
        """Translate a state dict from the client into entity attributes."""
        mapped: Dict[str, Any] = {}
        if "playing" in attrs:
            mapped[Attributes.STATE] = States.PLAYING if attrs["playing"] else States.PAUSED
        if attrs.get("on") is False:
            mapped[Attributes.STATE] = States.OFF
        if "volume" in attrs:
            mapped[Attributes.VOLUME] = attrs["volume"]
        if "muted" in attrs:
            mapped[Attributes.MUTED] = attrs["muted"]
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
        if attrs.get("source_name"):
            mapped[Attributes.SOURCE] = attrs["source_name"]
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
