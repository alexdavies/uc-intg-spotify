"""
Unified Bang & Olufsen media player entity.

A single media player that fronts all configured speakers. The active output is
chosen via the sound-mode control (e.g. "Beosound Emerge" / "Davies9"); the
entity mirrors that speaker's now-playing, volume and source list, and routes
all commands to it. The speakers are used as either/or - never playing different
content - so one interface is the natural model. ("Both"/multiroom is a planned
follow-up.)

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
from typing import Any, Dict, List, Optional

import ucapi
from ucapi.media_player import Attributes, Commands, Features, States

_LOG = logging.getLogger(__name__)

# Prefix marking radio favourites (presets) in the source list.
RADIO_PREFIX = "Radio: "

# Now-playing attributes cleared when switching output.
_MEDIA_ATTRS = [
    Attributes.MEDIA_TITLE, Attributes.MEDIA_ARTIST, Attributes.MEDIA_ALBUM,
    Attributes.MEDIA_DURATION, Attributes.MEDIA_POSITION, Attributes.MEDIA_IMAGE_URL,
]


class BeoPlayer:
    """One media player entity fronting all configured speakers."""

    def __init__(self, api: ucapi.IntegrationAPI, speakers: List[Dict[str, Any]],
                 active_serial: Optional[str] = None, config=None):
        """
        Args:
            speakers: list of {serial, name, client, sources, presets}.
            active_serial: serial of the speaker to start active on.
            config: optional BeoConfig, used to persist the active output.
        """
        self._api = api
        self._config = config
        self._speakers = {s["serial"]: s for s in speakers}
        self._order = [s["serial"] for s in speakers]
        self._name_to_serial = {s["name"]: s["serial"] for s in speakers}

        # Per-speaker source/preset lookups.
        self._source_ids: Dict[str, Dict[str, str]] = {}
        self._preset_ids: Dict[str, Dict[str, int]] = {}
        for s in speakers:
            self._source_ids[s["serial"]] = {src["name"]: src["id"] for src in s.get("sources", [])}
            self._preset_ids[s["serial"]] = {
                f"{RADIO_PREFIX}{p['name']}": p["id"] for p in s.get("presets", [])
            }

        self._active = active_serial if active_serial in self._speakers else self._order[0]

        # Route every speaker's push updates through a per-serial guard so only
        # the active speaker's state reaches the entity.
        for s in speakers:
            s["client"].on_update = self._make_handler(s["serial"])

        features = [
            Features.ON_OFF, Features.VOLUME, Features.VOLUME_UP_DOWN, Features.MUTE_TOGGLE,
            Features.PLAY_PAUSE, Features.STOP, Features.NEXT, Features.PREVIOUS,
            Features.MEDIA_DURATION, Features.MEDIA_POSITION, Features.MEDIA_TITLE,
            Features.MEDIA_ARTIST, Features.MEDIA_ALBUM, Features.MEDIA_IMAGE_URL,
            Features.SELECT_SOURCE, Features.SELECT_SOUND_MODE,
        ]
        attributes = {
            Attributes.STATE: States.OFF,
            Attributes.VOLUME: 0,
            Attributes.MUTED: False,
            Attributes.MEDIA_TITLE: "", Attributes.MEDIA_ARTIST: "", Attributes.MEDIA_ALBUM: "",
            Attributes.MEDIA_DURATION: 0, Attributes.MEDIA_POSITION: 0, Attributes.MEDIA_IMAGE_URL: "",
            Attributes.SOURCE: "",
            Attributes.SOURCE_LIST: self._source_list(self._active),
            Attributes.SOUND_MODE: self._speakers[self._active]["name"],
            Attributes.SOUND_MODE_LIST: [self._speakers[s]["name"] for s in self._order],
        }

        self.entity = ucapi.MediaPlayer(
            identifier="beo_player",
            name={"en": "Bang & Olufsen"},
            features=features,
            attributes=attributes,
            cmd_handler=self.cmd_handler,
        )
        _LOG.info("Created unified B&O player; active output: %s", self._active_name)

    # ----- helpers ---------------------------------------------------------

    @property
    def _client(self):
        return self._speakers[self._active]["client"]

    @property
    def active_client(self):
        """The client for the currently selected output (for the remote entity)."""
        return self._client

    async def set_output(self, name: str) -> bool:
        """Public: switch the active output by speaker name."""
        rc = await self._select_output({"mode": name})
        return rc == ucapi.StatusCodes.OK

    async def play_preset_on(self, serial: str, preset_id: int) -> bool:
        """Public: switch output to a speaker (if needed) and play one of its presets."""
        if serial in self._speakers and serial != self._active:
            await self._select_output({"mode": self._speakers[serial]["name"]})
        return await self._client.activate_preset(preset_id)

    async def select_source_by_name(self, name: str) -> bool:
        rc = await self._select_source({"source": name})
        return rc == ucapi.StatusCodes.OK

    async def volume_step(self, delta: int) -> bool:
        """Public: nudge the active output's volume."""
        return await self._nudge_volume(delta)

    @property
    def _active_name(self) -> str:
        return self._speakers[self._active]["name"]

    def _source_list(self, serial: str) -> List[str]:
        return list(self._source_ids[serial]) + list(self._preset_ids[serial])

    def _make_handler(self, serial: str):
        async def handler(attrs: Dict[str, Any]):
            if serial == self._active:
                await self._apply(attrs)
        return handler

    async def initialize(self) -> None:
        """Connect every speaker's stream and load the active speaker's state."""
        for s in self._speakers.values():
            await s["client"].start_notifications()
        snapshot = await self._client.get_state()
        if snapshot:
            await self._apply(snapshot)

    # ----- command handling ------------------------------------------------

    async def cmd_handler(self, entity, cmd_id: str, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        _LOG.info("[%s] command %s %s", self._active_name, cmd_id, params)
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
            if cmd_id == Commands.SELECT_SOUND_MODE:
                return await self._select_output(params)
            return ucapi.StatusCodes.NOT_IMPLEMENTED
        except Exception as e:
            _LOG.error("Error handling %s: %s", cmd_id, e)
            return ucapi.StatusCodes.SERVER_ERROR

    async def _select_output(self, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        """Switch the active speaker (sound-mode)."""
        name = (params or {}).get("mode") or (params or {}).get("sound_mode")
        serial = self._name_to_serial.get(name)
        if not serial:
            return ucapi.StatusCodes.BAD_REQUEST

        self._active = serial
        if self._config:
            try:
                self._config.set_active_speaker(serial)
            except Exception:
                pass

        # Re-point the interface: new output name, its source list, and reset the
        # now-playing card before loading the new speaker's live state.
        reset = {a: ("" if a != Attributes.MEDIA_DURATION and a != Attributes.MEDIA_POSITION else 0)
                 for a in _MEDIA_ATTRS}
        reset[Attributes.SOUND_MODE] = name
        reset[Attributes.SOURCE] = ""
        reset[Attributes.SOURCE_LIST] = self._source_list(serial)
        self._update(reset)

        snapshot = await self._client.get_state()
        if snapshot:
            await self._apply(snapshot)
        return ucapi.StatusCodes.OK

    async def _select_source(self, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        if not params or "source" not in params:
            return ucapi.StatusCodes.BAD_REQUEST
        source = params["source"]
        presets = self._preset_ids[self._active]
        sources = self._source_ids[self._active]
        if source in presets:
            ok = await self._client.activate_preset(presets[source])
        elif source in sources:
            ok = await self._client.set_source(sources[source])
        else:
            _LOG.warning("Unknown source '%s' on %s", source, self._active_name)
            return ucapi.StatusCodes.BAD_REQUEST
        if ok:
            self._update({Attributes.SOURCE: source})
        return _status(ok)

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

    # ----- state application ----------------------------------------------

    async def _apply(self, attrs: Dict[str, Any]) -> None:
        """Translate an active-speaker state dict into entity attributes."""
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
            # mute toggle, current source list) don't depend on the API echoing
            # the update back.
            self.entity.attributes.update(changed)
            self._api.configured_entities.update_attributes(self.entity.id, changed)


def _status(ok: bool) -> ucapi.StatusCodes:
    return ucapi.StatusCodes.OK if ok else ucapi.StatusCodes.SERVER_ERROR
