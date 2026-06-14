"""
Companion remote entity giving explicit, tappable button pages for the unified
Bang & Olufsen player.

The UC media player's source/sound-mode selectors are not very discoverable, so
this remote exposes the same actions as plain buttons:

- "Speakers": pick the active output (Emerge / A9 / ...).
- "Radio": one button per radio favourite (Mozart presets); pressing it switches
  to the owning speaker and plays the station.
- "Controls": transport + volume.

All actions route into the shared ``BeoPlayer`` so the media player and this
remote stay in sync. Buttons are also exposed as simple commands for use in
activities and macros.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
import re
from typing import Any, Dict, List

import ucapi
from ucapi.remote import Commands, Features, States
from ucapi.ui import Size, create_ui_icon, create_ui_text, UiPage

_LOG = logging.getLogger(__name__)


def _cmd(name: str, prefix: str, existing: set) -> str:
    base = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    base = f"{prefix}_{base}"[:20].rstrip("_") or prefix
    command, n = base, 2
    while command in existing:
        command = f"{base[:17]}_{n}"
        n += 1
    existing.add(command)
    return command


class BeoControlRemote:
    """Button-page remote wired to the shared BeoPlayer."""

    def __init__(self, api: ucapi.IntegrationAPI, player, speakers: List[Dict[str, Any]]):
        self._api = api
        self._player = player

        existing = set(["PLAY_PAUSE", "NEXT", "PREVIOUS", "STOP", "VOLUME_UP", "VOLUME_DOWN"])
        simple_commands = list(existing)

        # Output (speaker) buttons -> command : speaker name
        self._output_cmds: Dict[str, str] = {}
        self._output_buttons: List[tuple] = []
        for s in speakers:
            c = _cmd(s["name"], "OUT", existing)
            self._output_cmds[c] = s["name"]
            # Imperative label: a remote page can't show which speaker is active,
            # so make each button an unambiguous action.
            self._output_buttons.append((c, f"Switch to {s['name']}"))
            simple_commands.append(c)

        # Radio preset buttons -> command : (serial, preset_id)
        self._radio_cmds: Dict[str, tuple] = {}
        self._radio_buttons: List[tuple] = []
        for s in speakers:
            for preset in s.get("presets", []):
                c = _cmd(preset["name"], "RADIO", existing)
                self._radio_cmds[c] = (s["serial"], preset["id"])
                self._radio_buttons.append((c, preset["name"]))
                simple_commands.append(c)

        self.entity = ucapi.Remote(
            identifier="beo_controls",
            name={"en": "Bang & Olufsen Controls"},
            features=[Features.ON_OFF, Features.SEND_CMD],
            attributes={"state": States.ON},
            simple_commands=simple_commands,
            ui_pages=self._pages(),
            cmd_handler=self.cmd_handler,
        )
        _LOG.info("Created B&O controls remote (%d outputs, %d radio presets)",
                  len(self._output_buttons), len(self._radio_buttons))

    def _pages(self) -> List[UiPage]:
        pages = []

        controls = UiPage(page_id="controls", name="Controls", grid=Size(4, 6))
        controls.add(create_ui_icon("uc:play-pause", 1, 0, Size(2, 1), "PLAY_PAUSE"))
        controls.add(create_ui_icon("uc:backward", 0, 1, Size(1, 1), "PREVIOUS"))
        controls.add(create_ui_icon("uc:forward", 3, 1, Size(1, 1), "NEXT"))
        controls.add(create_ui_icon("uc:stop", 1, 2, Size(2, 1), "STOP"))
        controls.add(create_ui_icon("uc:volume-high", 1, 3, Size(1, 1), "VOLUME_UP"))
        controls.add(create_ui_icon("uc:volume-low", 2, 3, Size(1, 1), "VOLUME_DOWN"))
        pages.append(controls)

        # Transport first (used constantly), then Radio, then speaker-switching
        # last (one speaker is primary, so switching is occasional).
        pages.extend(_button_pages(self._radio_buttons, "radio", "Radio"))
        pages.extend(_button_pages(self._output_buttons, "speakers", "Switch speaker"))
        return pages

    async def cmd_handler(self, entity, cmd_id: str, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        try:
            if cmd_id == Commands.ON:
                self._api.configured_entities.update_attributes(self.entity.id, {"state": States.ON})
                return ucapi.StatusCodes.OK
            if cmd_id == Commands.OFF:
                self._api.configured_entities.update_attributes(self.entity.id, {"state": States.OFF})
                return ucapi.StatusCodes.OK
            if cmd_id == Commands.SEND_CMD:
                return await self._send(params)
            return ucapi.StatusCodes.NOT_IMPLEMENTED
        except Exception as e:
            _LOG.error("Remote command error: %s", e)
            return ucapi.StatusCodes.SERVER_ERROR

    async def _send(self, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        if not params or "command" not in params:
            return ucapi.StatusCodes.BAD_REQUEST
        command = params["command"]

        if command in self._output_cmds:
            return _status(await self._player.set_output(self._output_cmds[command]))
        if command in self._radio_cmds:
            serial, preset_id = self._radio_cmds[command]
            return _status(await self._player.play_preset_on(serial, preset_id))

        client = self._player.active_client
        transport = {
            "PLAY_PAUSE": client.play_pause,
            "NEXT": client.next_track,
            "PREVIOUS": client.previous_track,
            "STOP": client.stop,
            "VOLUME_UP": lambda: self._player.volume_step(5),
            "VOLUME_DOWN": lambda: self._player.volume_step(-5),
        }
        if command in transport:
            return _status(await transport[command]())
        _LOG.warning("Unknown remote command: %s", command)
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


def _status(ok: bool) -> ucapi.StatusCodes:
    return ucapi.StatusCodes.OK if ok else ucapi.StatusCodes.SERVER_ERROR
