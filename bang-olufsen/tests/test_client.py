"""
Mock-based tests for the Bang & Olufsen integration.

These do not require real hardware: the mozart-api client and the UC API are
mocked, so we test our own translation/routing logic.
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uc_intg_bang_olufsen import client as client_mod
from uc_intg_bang_olufsen.client import BeoClient, _metadata_to_attrs
from uc_intg_bang_olufsen.media_player import BeoMediaPlayer, RADIO_PREFIX
from uc_intg_bang_olufsen.remote import BeoRemote


def _make_client():
    """A BeoClient with its underlying mozart-api client mocked out."""
    c = BeoClient("192.168.1.50", "Beosound Emerge", "EMERGE123")
    c._client = MagicMock()
    return c


def test_metadata_translation():
    meta = SimpleNamespace(
        title="Test Track", artist_name="Test Artist", album_name="Test Album",
        total_duration_seconds=240,
        art=[SimpleNamespace(url="http://img/large.jpg")],
    )
    attrs = _metadata_to_attrs(meta)
    assert attrs == {
        "title": "Test Track", "artist": "Test Artist", "album": "Test Album",
        "duration": 240, "image_url": "http://img/large.jpg",
    }


def test_volume_scaling():
    c = _make_client()
    # Device reports a max of 90; UC works in 0-100.
    attrs = c._volume_to_attrs(SimpleNamespace(maximum=90, level=45, muted=False))
    assert attrs["volume"] == 50  # 45/90 -> 50%
    assert attrs["muted"] is False

    c._client.set_current_volume_level = AsyncMock()
    asyncio.run(c.set_volume(100))
    sent = c._client.set_current_volume_level.call_args.args[0]
    assert sent.level == 90  # scaled back to device maximum


def test_activate_preset_calls_library():
    c = _make_client()
    c._client.activate_preset = AsyncMock()
    assert asyncio.run(c.activate_preset(3)) is True
    c._client.activate_preset.assert_awaited_once_with(id=3)


def test_get_presets_maps_ids():
    c = _make_client()
    presets = {
        "1": SimpleNamespace(id=1, title="DR P3", name=None),
        "4": SimpleNamespace(id=4, title=None, name="BBC Radio 1"),
    }
    c._client.get_presets = AsyncMock(return_value=presets)
    result = asyncio.run(c.get_presets())
    assert result == [
        {"id": 1, "name": "DR P3"},
        {"id": 4, "name": "BBC Radio 1"},
    ]


def test_media_player_source_list_merges_sources_and_presets():
    api = MagicMock()
    c = _make_client()
    sources = [{"id": "spotify", "name": "Spotify"}, {"id": "tuneIn", "name": "TuneIn"}]
    presets = [{"id": 1, "name": "DR P3"}, {"id": 4, "name": "BBC Radio 1"}]
    mp = BeoMediaPlayer(api, c, sources, presets)
    source_list = mp.entity.attributes["source_list"]
    assert "Spotify" in source_list
    assert f"{RADIO_PREFIX}DR P3" in source_list
    assert len(source_list) == 4


def test_media_player_select_source_routes_preset_vs_source():
    api = MagicMock()
    c = _make_client()
    c.set_source = AsyncMock(return_value=True)
    c.activate_preset = AsyncMock(return_value=True)
    sources = [{"id": "spotify", "name": "Spotify"}]
    presets = [{"id": 7, "name": "Jazz FM"}]
    mp = BeoMediaPlayer(api, c, sources, presets)

    # Selecting a normal source -> set_active_source
    asyncio.run(mp._select_source({"source": "Spotify"}))
    c.set_source.assert_awaited_once_with("spotify")

    # Selecting a radio favourite -> activate_preset
    asyncio.run(mp._select_source({"source": f"{RADIO_PREFIX}Jazz FM"}))
    c.activate_preset.assert_awaited_once_with(7)


def test_remote_simple_commands_unique_and_bounded():
    api = MagicMock()
    c = _make_client()
    presets = [{"id": i, "name": f"Station {i}"} for i in range(3)]
    peers = [{"serial": "A9SERIAL", "name": "Beoplay A9"}]
    remote = BeoRemote(api, c, presets, peers, resolve_jid=AsyncMock(return_value="jid@peer"))
    cmds = remote.entity.options["simple_commands"]
    assert len(set(cmds)) == len(cmds)
    assert all(len(x) <= 20 for x in cmds)
    assert "BEOLINK_LEAVE" in cmds


def test_remote_expand_resolves_jid_and_expands():
    api = MagicMock()
    c = _make_client()
    c.beolink_expand = AsyncMock(return_value=True)
    resolver = AsyncMock(return_value="jid@a9")
    peers = [{"serial": "A9SERIAL", "name": "Beoplay A9"}]
    remote = BeoRemote(api, c, [], peers, resolve_jid=resolver)

    expand_cmd = next(cmd for cmd, action in remote._actions.items() if action[0] == "expand")
    rc = asyncio.run(remote._send({"command": expand_cmd}))
    resolver.assert_awaited_once_with("A9SERIAL")
    c.beolink_expand.assert_awaited_once_with("jid@a9")
    assert rc.name == "OK" if hasattr(rc, "name") else True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
