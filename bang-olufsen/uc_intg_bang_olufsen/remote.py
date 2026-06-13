"""
Remote entity for a single Bang & Olufsen Mozart speaker.

Exposes transport controls, one button per radio favourite (preset), and
Beolink multiroom actions ("play on <peer>", "leave") as both UI buttons and
simple commands usable in activities and macros.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

import ucapi
from ucapi.remote import Commands, Features, States
from ucapi.ui import Size, create_ui_icon, create_ui_text, UiPage

from uc_intg_bang_olufsen.client import BeoClient

_LOG = logging.getLogger(__name__)

MAX_PRESET_BUTTONS = 12

# Resolver returns the Beolink JID for a peer serial, or None.
JidResolver = Callable[[str], Awaitable[Optional[str]]]


def _simple_command(name: str, prefix: str, existing: set) -> str:
    base = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    base = f"{prefix}_{base}"[:20].rstrip("_") or prefix
    command = base
    suffix = 2
    while command in existing:
        command = f"{base[:17]}_{suffix}"
        suffix += 1
    existing.add(command)
    return command


class BeoRemote:
    """A remote entity backed by one Mozart speaker."""

    def __init__(self, api: ucapi.IntegrationAPI, client: BeoClient,
                 presets: Optional[List[Dict[str, Any]]] = None,
                 peers: Optional[List[Dict[str, str]]] = None,
                 resolve_jid: Optional[JidResolver] = None):
        self._api = api
        self._client = client
        self._resolve_jid = resolve_jid
        # command -> ("preset", int) | ("expand", peer_serial) | ("leave", None)
        self._actions: Dict[str, tuple] = {}

        existing = set(["PLAY_PAUSE", "NEXT", "PREVIOUS", "STOP", "VOLUME_UP", "VOLUME_DOWN"])
        simple_commands = list(existing)

        self._preset_buttons: List[tuple] = []
        for preset in (presets or [])[:MAX_PRESET_BUTTONS]:
            cmd = _simple_command(preset["name"], "RADIO", existing)
            self._actions[cmd] = ("preset", preset["id"])
            self._preset_buttons.append((cmd, preset["name"]))
            simple_commands.append(cmd)

        self._peer_buttons: List[tuple] = []
        for peer in (peers or []):
            cmd = _simple_command(peer["name"], "PLAYON", existing)
            self._actions[cmd] = ("expand", peer["serial"])
            self._peer_buttons.append((cmd, f"Play on {peer['name']}"))
            simple_commands.append(cmd)
        if self._peer_buttons:
            self._actions["BEOLINK_LEAVE"] = ("leave", None)
            simple_commands.append("BEOLINK_LEAVE")

        identifier = f"beo_remote_{_slug(client.serial or client.host)}"
        self.entity = ucapi.Remote(
            identifier=identifier,
            name={"en": f"{client.name} Remote"},
            features=[Features.ON_OFF, Features.SEND_CMD],
            attributes={"state": States.ON},
            simple_commands=simple_commands,
            ui_pages=self._create_ui_pages(),
            cmd_handler=self.cmd_handler,
        )
        _LOG.info("Created remote entity for %s", client.name)

    def _create_ui_pages(self) -> List[UiPage]:
        pages = []
        main = UiPage(page_id="main", name="Controls", grid=Size(4, 6))
        main.add(create_ui_icon("uc:play-pause", 1, 0, Size(2, 1), "PLAY_PAUSE"))
        main.add(create_ui_icon("uc:backward", 0, 1, Size(1, 1), "PREVIOUS"))
        main.add(create_ui_icon("uc:forward", 3, 1, Size(1, 1), "NEXT"))
        main.add(create_ui_icon("uc:stop", 1, 2, Size(2, 1), "STOP"))
        main.add(create_ui_icon("uc:volume-high", 1, 3, Size(1, 1), "VOLUME_UP"))
        main.add(create_ui_icon("uc:volume-low", 2, 3, Size(1, 1), "VOLUME_DOWN"))
        pages.append(main)

        pages.extend(_button_pages(self._preset_buttons, "radio", "Radio"))
        pages.extend(_button_pages(self._peer_buttons + ([("BEOLINK_LEAVE", "Leave multiroom")] if self._peer_buttons else []), "multiroom", "Multiroom"))
        return pages

    async def cmd_handler(self, entity: ucapi.Entity, cmd_id: str, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        _LOG.info("[%s remote] %s %s", self._client.name, cmd_id, params)
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
            _LOG.error("Error in remote handler for %s: %s", self._client.name, e)
            return ucapi.StatusCodes.SERVER_ERROR

    async def _send(self, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        if not params or "command" not in params:
            return ucapi.StatusCodes.BAD_REQUEST
        command = params["command"]

        if command in self._actions:
            return await self._run_action(*self._actions[command])

        fixed = {
            "PLAY_PAUSE": self._play_pause,
            "NEXT": self._client.next_track,
            "PREVIOUS": self._client.previous_track,
            "STOP": self._client.stop,
            "VOLUME_UP": lambda: self._nudge(5),
            "VOLUME_DOWN": lambda: self._nudge(-5),
        }
        if command in fixed:
            return _status(await fixed[command]())
        _LOG.warning("Unknown remote command: %s", command)
        return ucapi.StatusCodes.NOT_IMPLEMENTED

    async def _run_action(self, kind: str, target) -> ucapi.StatusCodes:
        if kind == "preset":
            return _status(await self._client.activate_preset(target))
        if kind == "leave":
            return _status(await self._client.beolink_leave())
        if kind == "expand":
            if not self._resolve_jid:
                return ucapi.StatusCodes.NOT_IMPLEMENTED
            jid = await self._resolve_jid(target)
            if not jid:
                _LOG.warning("Could not resolve Beolink JID for peer %s", target)
                return ucapi.StatusCodes.SERVER_ERROR
            return _status(await self._client.beolink_expand(jid))
        return ucapi.StatusCodes.NOT_IMPLEMENTED

    async def _play_pause(self) -> bool:
        return await self._client.play_pause()

    async def _nudge(self, delta: int) -> bool:
        snapshot = await self._client.get_state()
        current = snapshot.get("volume", 0)
        return await self._client.set_volume(max(0, min(100, current + delta)))


def _button_pages(buttons: List[tuple], page_id: str, page_name: str) -> List[UiPage]:
    pages = []
    per_page = 6
    for index in range(0, len(buttons), per_page):
        chunk = buttons[index:index + per_page]
        number = index // per_page + 1
        name = page_name if number == 1 else f"{page_name} {number}"
        page = UiPage(page_id=f"{page_id}_{number}", name=name, grid=Size(4, 6))
        for row, (command, label) in enumerate(chunk):
            page.add(create_ui_text(label, 0, row, Size(4, 1), command))
        pages.append(page)
    return pages


def _status(ok: bool) -> ucapi.StatusCodes:
    return ucapi.StatusCodes.OK if ok else ucapi.StatusCodes.SERVER_ERROR


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in value).strip("_").lower()
