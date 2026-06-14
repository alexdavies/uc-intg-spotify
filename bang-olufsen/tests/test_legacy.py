"""
Mock-based tests for the legacy (ASE/BeoZone) client and parsing helpers.

Grounded in the real shapes observed on a Beoplay A9 4th gen (sw 6.5.x):
volume range 0-90, /BeoZone/Zone/Sources, and BeoNotify notification types
SOURCE / VOLUME / PROGRESS_INFORMATION / NOW_PLAYING_NET_RADIO.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uc_intg_bang_olufsen.legacy_client import (
    LegacyBeoClient,
    _active_source,
    _iter_sources,
    _notification_to_attrs,
    _to_percent,
)
from uc_intg_bang_olufsen import factory


def test_volume_scaling_0_to_90():
    assert _to_percent(0) == 0
    assert _to_percent(90) == 100
    assert _to_percent(45) == 50
    assert _to_percent(6) == 7  # the value seen on the real device


def test_set_volume_scales_back_to_device_range():
    c = LegacyBeoClient("10.0.0.9")
    c._command = AsyncMock(return_value=True)
    asyncio.run(c.set_volume(100))
    method, path, body = c._command.call_args.args
    assert path.endswith("/Speaker/Level")
    assert body == {"level": 90}


class _FakeResp:
    def __init__(self, status, json_value=None, raise_json=False):
        self.status = status
        self._jv = json_value
        self._raise = raise_json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        if self._raise:
            raise ValueError("empty body")
        return self._jv


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []
        self.closed = False

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json))
        return self._resp

    async def close(self):
        self.closed = True


def _client_with_response(resp):
    c = LegacyBeoClient("10.0.0.9")
    sess = _FakeSession(resp)
    c._get_session = AsyncMock(return_value=sess)
    return c, sess


def test_command_sends_json_body_and_succeeds_on_empty_200():
    # The ASE API 400s a bodyless POST; a bodyless command must still send a
    # JSON body so aiohttp sets Content-Type, and an empty 200 body (json()
    # raises) must read as success.
    c, sess = _client_with_response(_FakeResp(200, raise_json=True))
    assert asyncio.run(c.play()) is True
    method, url, body = sess.calls[-1]
    assert method == "POST" and url.endswith("/Stream/Play")
    assert body == {}


def test_command_succeeds_when_empty_body_parses_to_none():
    # Some aiohttp versions return None (instead of raising) for an empty body.
    c, _ = _client_with_response(_FakeResp(200, json_value=None))
    assert asyncio.run(c.pause()) is True


def test_command_fails_on_non_2xx():
    c, _ = _client_with_response(_FakeResp(400, raise_json=True))
    assert asyncio.run(c.next_track()) is False


def test_parse_sources_ase_pair_format():
    data = {"sources": [
        ["spotify:123@products", {"friendlyName": "Spotify", "sourceType": {"type": "SPOTIFY"}}],
        ["radio:456", {"friendlyName": "B&O Radio", "sourceType": {"type": "TUNEIN"}}],
    ]}
    pairs = list(_iter_sources(data))
    assert pairs[0][0] == "spotify:123@products"
    assert pairs[1][1]["friendlyName"] == "B&O Radio"


def test_active_source_extraction():
    data = {"primaryExperience": {"source": {"id": "radio:456", "friendlyName": "B&O Radio"}}}
    assert _active_source(data) == ("radio:456", "B&O Radio")
    assert _active_source({}) is None


def test_notification_volume():
    attrs = _notification_to_attrs("VOLUME", {"speaker": {"level": 45, "muted": False}})
    assert attrs == {"volume": 50, "muted": False}


def test_notification_now_playing_net_radio():
    attrs = _notification_to_attrs("NOW_PLAYING_NET_RADIO", {"name": "triple j", "stationName": "ABC"})
    assert attrs["title"] == "triple j"
    assert attrs["artist"] == "ABC"


def test_notification_progress():
    attrs = _notification_to_attrs("PROGRESS_INFORMATION", {"state": "play", "position": 35, "totalDuration": 0})
    assert attrs["playing"] is True
    assert attrs["position"] == 35


def test_notification_source():
    data = {"primaryExperience": {"source": {"id": "radio:456", "friendlyName": "B&O Radio"}}}
    attrs = _notification_to_attrs("SOURCE", data)
    assert attrs == {"source_id": "radio:456", "source_name": "B&O Radio"}


def test_factory_creates_correct_backend():
    from uc_intg_bang_olufsen.client import BeoClient
    assert isinstance(factory.create_client("1.2.3.4", protocol="legacy"), LegacyBeoClient)
    assert isinstance(factory.create_client("1.2.3.4", protocol="mozart"), BeoClient)


def test_legacy_client_satisfies_entity_interface():
    """The legacy client must expose the same methods the entities call."""
    c = LegacyBeoClient("1.2.3.4", "A9", "S9")
    for method in ("connect", "start_notifications", "close", "get_sources", "get_presets",
                   "get_state", "play", "pause", "stop", "next_track", "previous_track",
                   "set_volume", "set_mute", "set_source", "activate_preset", "power_on",
                   "standby", "beolink_expand", "beolink_join", "beolink_leave", "get_beolink_jid"):
        assert callable(getattr(c, method)), f"missing {method}"
    assert hasattr(c, "on_update")
