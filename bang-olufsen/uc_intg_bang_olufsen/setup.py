"""
Setup handler for the Bang & Olufsen integration.

Flow:
1. Driver setup request -> run mDNS discovery.
2. Present discovered speakers as a multi-select, plus an optional manual-IP
   field for speakers that don't show up (e.g. on a different subnet).
3. Persist the selection and create entities.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
from typing import Any, Callable, Coroutine, List

import ucapi

from uc_intg_bang_olufsen.config import BeoConfig
from uc_intg_bang_olufsen.discovery import discover_devices
from uc_intg_bang_olufsen.factory import PROTOCOL_MOZART, detect_protocol
from uc_intg_bang_olufsen.spotify import SpotifyClient

_LOG = logging.getLogger(__name__)


class BeoSetup:
    """Setup handler for the Bang & Olufsen integration."""

    def __init__(self, config: BeoConfig, setup_complete_callback: Callable[[], Coroutine[Any, Any, None]]):
        self._config = config
        self._setup_complete_callback = setup_complete_callback
        self._discovered: List[dict] = []

    async def setup_handler(self, msg: ucapi.SetupDriver) -> ucapi.SetupAction:
        if isinstance(msg, ucapi.DriverSetupRequest):
            return await self._handle_driver_setup_request(msg)
        if isinstance(msg, ucapi.UserDataResponse):
            return await self._handle_user_data_response(msg)
        if isinstance(msg, ucapi.AbortDriverSetup):
            _LOG.info("Setup aborted: %s", msg.error)
            return ucapi.SetupError(msg.error)
        return ucapi.SetupError(ucapi.IntegrationSetupError.OTHER)

    async def _handle_driver_setup_request(self, msg: ucapi.DriverSetupRequest) -> ucapi.SetupAction:
        setup_data = msg.setup_data or {}

        # Whole-config import (migration from an off-device driver): skips
        # discovery and Spotify authorization entirely.
        pasted = (setup_data.get("config_json") or "").strip()
        if pasted:
            if not self._config.import_json(pasted):
                return ucapi.SetupError(ucapi.IntegrationSetupError.OTHER)
            _LOG.info("Imported config.json (%d device(s))", len(self._config.get_devices()))
            await self._setup_complete_callback()
            return ucapi.SetupComplete()

        # Spotify Client ID/Secret may be supplied on the initial form (driver.json).
        client_id = (setup_data.get("spotify_client_id") or "").strip()
        client_secret = (setup_data.get("spotify_client_secret") or "").strip()
        if client_id and client_secret:
            self._config.set_app_credentials(client_id, client_secret)

        if self._config.is_configured() and not msg.reconfigure:
            # Already set up. If new Spotify creds were entered, go straight to
            # authorization; otherwise finish without re-running discovery.
            if client_id and client_secret:
                return self._spotify_auth_screen()
            await self._setup_complete_callback()
            return ucapi.SetupComplete()

        _LOG.info("Discovering Bang & Olufsen devices on the network...")
        self._discovered = await discover_devices()

        # One checkbox per discovered speaker, all ticked, so every speaker is
        # added by default (the common case is "add them all"); untick to skip.
        settings = [
            {
                "id": "info",
                "label": {"en": "Discovered Speakers"},
                "field": {
                    "label": {
                        "value": {
                            "en": (
                                f"Found {len(self._discovered)} speaker(s) on your network. "
                                "All are ticked — untick any you don't want to add. If a "
                                "speaker is missing (e.g. on another subnet), add it by IP below."
                            )
                        }
                    }
                },
            }
        ]
        for index, device in enumerate(self._discovered):
            settings.append({
                "id": f"add_{index}",
                "label": _device_label(device),
                "field": {"checkbox": {"value": True}},
            })
        settings.append({
            "id": "manual_host",
            "label": {"en": "Add speaker by IP (optional)"},
            "field": {"text": {"value": "", "placeholder": "e.g. 192.168.1.42"}},
        })
        # Spotify creds come from the initial (driver.json) screen, captured above.
        return ucapi.RequestUserInput({"en": "Add Bang & Olufsen Speakers"}, settings)

    async def _handle_user_data_response(self, msg: ucapi.UserDataResponse) -> ucapi.SetupAction:
        # Second step: Spotify authorization code submitted.
        if "auth_code" in msg.input_values:
            return await self._handle_spotify_auth(msg)

        devices: List[dict] = []

        # Collect every discovered speaker whose checkbox is ticked. UC returns
        # checkbox values as booleans, but accept the string forms defensively.
        for index, device in enumerate(self._discovered):
            value = msg.input_values.get(f"add_{index}")
            if value in (True, "true", "True", "on", 1):
                device.setdefault("protocol", PROTOCOL_MOZART)
                devices.append(device)

        manual_host = (msg.input_values.get("manual_host") or "").strip()
        if manual_host:
            protocol = await detect_protocol(manual_host)
            if not protocol:
                _LOG.error("Manual device %s is not reachable / not recognised", manual_host)
                return ucapi.SetupError(ucapi.IntegrationSetupError.CONNECTION_REFUSED)
            _LOG.info("Manual device %s detected as %s", manual_host, protocol)
            devices.append({"host": manual_host, "name": manual_host, "serial": manual_host,
                            "model": "", "protocol": protocol})

        if not devices:
            _LOG.error("No speakers selected or entered")
            return ucapi.SetupError(ucapi.IntegrationSetupError.OTHER)

        # Merge with any previously configured devices.
        merged = self._config.get_devices() + devices
        self._config.set_devices(merged)
        _LOG.info("Saved %d Bang & Olufsen device(s)", len(self._config.get_devices()))

        # If Spotify credentials were supplied (on the first screen) and we haven't
        # authorized yet, continue to authorization.
        if (self._config.get_client_id() and self._config.get_client_secret()
                and not self._config.spotify_is_configured()):
            return self._spotify_auth_screen()

        await self._setup_complete_callback()
        return ucapi.SetupComplete()

    def _spotify_auth_screen(self) -> ucapi.SetupAction:
        try:
            auth_url = SpotifyClient(self._config).get_authorization_url()
        except ValueError as e:
            _LOG.error("Spotify auth URL error: %s", e)
            return ucapi.SetupError(ucapi.IntegrationSetupError.OTHER)
        return ucapi.RequestUserInput(
            {"en": "Spotify Authorization"},
            [
                {"id": "spotify_steps", "label": {"en": "Steps"}, "field": {"label": {"value": {"en": (
                    "1. Open the URL below and log in / authorize.\n"
                    "2. Your browser will show a 'page not found' at example.com — that's fine.\n"
                    "3. Copy the 'code=...' value (or the whole URL) and paste it below."
                )}}}},
                {"id": "spotify_url", "label": {"en": "Authorization URL"},
                 "field": {"text": {"value": auth_url, "read_only": True}}},
                {"id": "auth_code", "label": {"en": "Paste code or full URL"},
                 "field": {"text": {"value": "", "placeholder": "code=... or the whole redirected URL"}}},
            ],
        )

    async def _handle_spotify_auth(self, msg: ucapi.UserDataResponse) -> ucapi.SetupAction:
        auth_input = (msg.input_values.get("auth_code") or "").strip()
        if not auth_input:
            return ucapi.SetupError(ucapi.IntegrationSetupError.OTHER)
        # Accept a raw code, "code=...", or a full redirect URL; always drop any
        # trailing query params (e.g. &ubi=...) that Spotify appends.
        code = auth_input
        if "code=" in code:
            code = code.split("code=", 1)[1]
        code = code.split("&")[0].split("#")[0].strip()
        client = SpotifyClient(self._config)
        try:
            ok = await client.exchange_code_for_token(code)
        finally:
            await client.close()
        if not ok:
            _LOG.error("Spotify token exchange failed")
            return ucapi.SetupError(ucapi.IntegrationSetupError.AUTHORIZATION_ERROR)
        _LOG.info("Spotify authorized for the B&O integration")
        await self._setup_complete_callback()
        return ucapi.SetupComplete()


def _device_label(device: dict) -> dict:
    model = device.get("model")
    name = device.get("name", device.get("host", "Speaker"))
    label = f"{name} ({model})" if model else name
    return {"en": label}
