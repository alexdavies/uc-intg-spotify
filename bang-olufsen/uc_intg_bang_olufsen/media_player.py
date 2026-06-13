"""
Media player entity for a single Bang & Olufsen Mozart speaker.

State is driven by the Mozart notification WebSocket (push), so there is no
polling loop. Sources and presets (radio favourites) are merged into the
``source_list``; selecting a radio favourite routes to ``activate_preset`` while
a normal source routes to ``set_active_source``.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
from typing import Any, Dict, List, Optional

import ucapi
from ucapi.media_player import Attributes, Commands, Features, States

from uc_intg_bang_olufsen.client import BeoClient

_LOG = logging.getLogger(__name__)

# Prefix used to mark radio favourites in the source list.
RADIO_PREFIX = "Radio: "


class BeoMediaPlayer:
    """A media player entity backed by one Mozart speaker."""

    def __init__(self, api: ucapi.IntegrationAPI, client: BeoClient,
                 sources: Optional[List[Dict[str, str]]] = None,
                 presets: Optional[List[Dict[str, Any]]] = None):
        self._api = api
        self._client = client
        # name -> source id
        self._source_ids: Dict[str, str] = {s["name"]: s["id"] for s in (sources or [])}
        # display name -> preset int id
        self._preset_ids: Dict[str, int] = {
            f"{RADIO_PREFIX}{p['name']}": p["id"] for p in (presets or [])
        }

        client.on_update = self._on_update

        features = [
            Features.ON_OFF,
            Features.VOLUME,
            Features.VOLUME_UP_DOWN,
            Features.MUTE_TOGGLE,
            Features.PLAY_PAUSE,
            Features.STOP,
            Features.NEXT,
            Features.PREVIOUS,
            Features.MEDIA_DURATION,
            Features.MEDIA_POSITION,
            Features.MEDIA_TITLE,
            Features.MEDIA_ARTIST,
            Features.MEDIA_ALBUM,
            Features.MEDIA_IMAGE_URL,
            Features.SELECT_SOURCE,
        ]

        attributes = {
            Attributes.STATE: States.OFF,
            Attributes.VOLUME: 0,
            Attributes.MUTED: False,
            Attributes.MEDIA_TITLE: "",
            Attributes.MEDIA_ARTIST: "",
            Attributes.MEDIA_ALBUM: "",
            Attributes.MEDIA_DURATION: 0,
            Attributes.MEDIA_POSITION: 0,
            Attributes.MEDIA_IMAGE_URL: "",
            Attributes.SOURCE: "",
            Attributes.SOURCE_LIST: self._source_list(),
        }

        identifier = f"beo_media_player_{_slug(client.serial or client.host)}"
        self.entity = ucapi.MediaPlayer(
            identifier=identifier,
            name={"en": client.name},
            features=features,
            attributes=attributes,
            cmd_handler=self.cmd_handler,
        )
        _LOG.info("Created media player entity for %s", client.name)

    def _source_list(self) -> List[str]:
        return list(self._source_ids.keys()) + list(self._preset_ids.keys())

    async def initialize(self) -> None:
        """Pull an initial snapshot and start the push stream."""
        await self._client.start_notifications()
        snapshot = await self._client.get_state()
        if snapshot:
            await self._on_update(snapshot)

    async def cmd_handler(self, entity: ucapi.Entity, cmd_id: str, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        _LOG.info("[%s] command %s %s", self._client.name, cmd_id, params)
        try:
            handlers = {
                Commands.ON: self._client.power_on,
                Commands.OFF: self._client.standby,
                Commands.PLAY_PAUSE: self._play_pause,
                Commands.STOP: self._client.stop,
                Commands.NEXT: self._client.next_track,
                Commands.PREVIOUS: self._client.previous_track,
                Commands.MUTE_TOGGLE: self._mute_toggle,
                Commands.VOLUME_UP: lambda: self._nudge_volume(5),
                Commands.VOLUME_DOWN: lambda: self._nudge_volume(-5),
            }
            if cmd_id in handlers:
                return _status(await handlers[cmd_id]())
            if cmd_id == Commands.VOLUME:
                return await self._set_volume(params)
            if cmd_id == Commands.SELECT_SOURCE:
                return await self._select_source(params)
            _LOG.info("Unhandled command %s", cmd_id)
            return ucapi.StatusCodes.NOT_IMPLEMENTED
        except Exception as e:
            _LOG.error("Error handling %s on %s: %s", cmd_id, self._client.name, e)
            return ucapi.StatusCodes.SERVER_ERROR

    async def _play_pause(self) -> bool:
        # The client decides direction from the device's live/last-known state,
        # so a stale cached attribute can't make this re-issue play.
        return await self._client.play_pause()

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

    async def _select_source(self, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        if not params or "source" not in params:
            return ucapi.StatusCodes.BAD_REQUEST
        source = params["source"]
        if source in self._preset_ids:
            ok = await self._client.activate_preset(self._preset_ids[source])
        elif source in self._source_ids:
            ok = await self._client.set_source(self._source_ids[source])
        else:
            _LOG.warning("Unknown source '%s' on %s", source, self._client.name)
            return ucapi.StatusCodes.BAD_REQUEST
        if ok:
            self._update({Attributes.SOURCE: source})
        return _status(ok)

    async def _on_update(self, attrs: Dict[str, Any]) -> None:
        """Translate a client state dict into entity attribute updates."""
        mapped: Dict[str, Any] = {}

        if "playing" in attrs:
            mapped[Attributes.STATE] = States.PLAYING if attrs["playing"] else States.PAUSED
        if "on" in attrs and not attrs.get("on"):
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
            self._api.configured_entities.update_attributes(self.entity.id, changed)


def _status(ok: bool) -> ucapi.StatusCodes:
    return ucapi.StatusCodes.OK if ok else ucapi.StatusCodes.SERVER_ERROR


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value).strip("_").lower()
