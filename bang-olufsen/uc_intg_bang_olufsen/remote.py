"""
Per-speaker companion remote entity — the on-screen "control" surface.

Each speaker gets, alongside its media-player ("now playing" view), a remote
entity with tappable button pages:

- "Controls": transport + volume.
- "Radio": one button per custom radio station (casts it).

Every button delegates to the speaker's ``BeoPlayer`` (via its media-player
command handler), so the media-player view and this remote stay in sync. Buttons
are also exposed as simple commands for activities/macros.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
import re
from typing import Any, Dict, List, Optional

import ucapi
from ucapi.media_player import Commands as MpCommands
from ucapi.remote import Commands, Features, States
from ucapi.ui import Size, create_ui_icon, create_ui_text, UiPage

from uc_intg_bang_olufsen.player import RADIO_PREFIX, _entity_id

_LOG = logging.getLogger(__name__)

_TRANSPORT = {
    "PLAY_PAUSE": MpCommands.PLAY_PAUSE,
    "NEXT": MpCommands.NEXT,
    "PREVIOUS": MpCommands.PREVIOUS,
    "STOP": MpCommands.STOP,
    "VOLUME_UP": MpCommands.VOLUME_UP,
    "VOLUME_DOWN": MpCommands.VOLUME_DOWN,
}


def _cmd(name: str, prefix: str, existing: set) -> str:
    base = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    base = f"{prefix}_{base}"[:20].rstrip("_") or prefix
    command, n = base, 2
    while command in existing:
        command = f"{base[:17]}_{n}"
        n += 1
    existing.add(command)
    return command


class BeoRemote:
    """Button-page remote for a single speaker, delegating to its BeoPlayer."""

    def __init__(self, api: ucapi.IntegrationAPI, player, name: str, serial: str,
                 radio_stations: Optional[List[Dict[str, str]]] = None,
                 playlists: Optional[List[Dict[str, str]]] = None):
        self._api = api
        self._player = player
        self._name = name

        existing = set(_TRANSPORT)
        simple_commands = list(_TRANSPORT)

        # Radio buttons -> command : source name ("Radio: <station>").
        self._radio_cmds: Dict[str, str] = {}
        self._radio_buttons: List[tuple] = []
        for station in (radio_stations or []):
            c = _cmd(station["name"], "RADIO", existing)
            self._radio_cmds[c] = f"{RADIO_PREFIX}{station['name']}"
            self._radio_buttons.append((c, station["name"]))
            simple_commands.append(c)

        # Spotify playlist buttons -> command : (uri, name).
        self._playlist_cmds: Dict[str, tuple] = {}
        self._playlist_buttons: List[tuple] = []
        for pl in (playlists or []):
            c = _cmd(pl["name"], "SPOT", existing)
            self._playlist_cmds[c] = (pl["uri"], pl["name"])
            self._playlist_buttons.append((c, pl["name"]))
            simple_commands.append(c)

        self.entity = ucapi.Remote(
            identifier="beo_remote_" + _entity_id(serial)[len("beo_player_"):],
            name={"en": f"{name} Controls"},
            features=[Features.ON_OFF, Features.SEND_CMD],
            attributes={"state": States.ON},
            simple_commands=simple_commands,
            ui_pages=self._pages(),
            cmd_handler=self.cmd_handler,
        )
        _LOG.info("Created B&O remote '%s Controls' (%d radio, %d playlist buttons)",
                  name, len(self._radio_buttons), len(self._playlist_buttons))

    def _pages(self) -> List[UiPage]:
        controls = UiPage(page_id="controls", name="Controls", grid=Size(4, 6))
        controls.add(create_ui_icon("uc:play-pause", 1, 0, Size(2, 1), "PLAY_PAUSE"))
        controls.add(create_ui_icon("uc:backward", 0, 1, Size(1, 1), "PREVIOUS"))
        controls.add(create_ui_icon("uc:forward", 3, 1, Size(1, 1), "NEXT"))
        controls.add(create_ui_icon("uc:stop", 1, 2, Size(2, 1), "STOP"))
        controls.add(create_ui_icon("uc:volume-high", 1, 3, Size(1, 1), "VOLUME_UP"))
        controls.add(create_ui_icon("uc:volume-low", 2, 3, Size(1, 1), "VOLUME_DOWN"))
        pages = [controls]
        pages.extend(_button_pages(self._radio_buttons, "radio", "Radio"))
        pages.extend(_button_pages(self._playlist_buttons, "spotify", "Spotify"))
        return pages

    async def cmd_handler(self, entity, cmd_id: str, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        try:
            if cmd_id == Commands.ON:
                await self._player.cmd_handler(self._player.entity, MpCommands.ON, None)
                self._api.configured_entities.update_attributes(self.entity.id, {"state": States.ON})
                return ucapi.StatusCodes.OK
            if cmd_id == Commands.OFF:
                await self._player.cmd_handler(self._player.entity, MpCommands.OFF, None)
                self._api.configured_entities.update_attributes(self.entity.id, {"state": States.OFF})
                return ucapi.StatusCodes.OK
            if cmd_id == Commands.SEND_CMD:
                return await self._send(params)
            return ucapi.StatusCodes.NOT_IMPLEMENTED
        except Exception as e:  # noqa: BLE001
            _LOG.error("[%s] remote command error: %s", self._name, e)
            return ucapi.StatusCodes.SERVER_ERROR

    async def _send(self, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        if not params or "command" not in params:
            return ucapi.StatusCodes.BAD_REQUEST
        command = params["command"]
        # All actions go through the player's media-player handler so the player
        # view and this remote stay in sync.
        if command in self._radio_cmds:
            return await self._player.cmd_handler(
                self._player.entity, MpCommands.SELECT_SOURCE,
                {"source": self._radio_cmds[command]},
            )
        if command in self._playlist_cmds:
            uri, plname = self._playlist_cmds[command]
            ok = await self._player.play_spotify_playlist(uri, plname)
            return ucapi.StatusCodes.OK if ok else ucapi.StatusCodes.SERVER_ERROR
        if command in _TRANSPORT:
            return await self._player.cmd_handler(self._player.entity, _TRANSPORT[command], None)
        _LOG.warning("[%s] unknown remote command: %s", self._name, command)
        return ucapi.StatusCodes.NOT_IMPLEMENTED


def _button_pages(buttons: List[tuple], page_id: str, page_name: str) -> List[UiPage]:
    pages = []
    per_page = 6
    for i in range(0, len(buttons), per_page):
        chunk = buttons[i:i + per_page]
        number = i // per_page + 1
        name = page_name if number == 1 else f"{page_name} {number}"
        page = UiPage(page_id=f"{page_id}_{number}", name=name, grid=Size(4, 6))
        for row, (command, label) in enumerate(chunk):
            page.add(create_ui_text(label, 0, row, Size(4, 1), command))
        pages.append(page)
    return pages
