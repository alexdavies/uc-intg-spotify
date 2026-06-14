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
from uc_intg_bang_olufsen.player import BeoPlayer, RADIO_PREFIX


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
    # Mozart nests each value in a wrapper: VolumeState.level is a VolumeLevel
    # (int under ``.level``), .maximum a VolumeMaximum (also ``.level``), and
    # .muted a Muted (bool under ``.muted``). Device reports a max of 90.
    volume_state = SimpleNamespace(
        maximum=SimpleNamespace(level=90),
        level=SimpleNamespace(level=45),
        muted=SimpleNamespace(muted=False),
    )
    attrs = c._volume_to_attrs(volume_state)
    assert attrs["volume"] == 50  # 45/90 -> 50%
    assert attrs["muted"] is False
    # The wrapper must be unwrapped to an int, not stored as the object, or the
    # next set_volume() arithmetic breaks.
    assert c._volume_maximum == 90

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
    # The dict key is the integer preset number activate_preset() expects;
    # ``id`` is an unrelated UUID that must NOT be used as the id.
    presets = {
        "1": SimpleNamespace(id="1b174cc6-uuid", title="DR P3", name="Preset1"),
        "4": SimpleNamespace(id="4816d6d3-uuid", title=None, name="BBC Radio 1"),
    }
    c._client.get_presets = AsyncMock(return_value=presets)
    result = asyncio.run(c.get_presets())
    assert result == [
        {"id": 1, "name": "DR P3"},
        {"id": 4, "name": "BBC Radio 1"},
    ]


def _player_for(name, serial, sources, presets):
    """A single-speaker BeoPlayer with a mocked client and UC API."""
    api = MagicMock()
    client = _make_client()
    client.name = name
    speaker = {"serial": serial, "name": name, "client": client,
               "sources": sources, "presets": presets}
    return api, BeoPlayer(api, speaker), client


def test_player_exposes_own_sources_and_radio_presets():
    api, player, client = _player_for(
        "Beosound Emerge", "EMERGE",
        [{"id": "spotify", "name": "Spotify"}], [{"id": 1, "name": "DR P3"}])
    assert player.entity.id == "beo_player_EMERGE"
    src = player.entity.attributes["source_list"]
    assert "Spotify" in src and f"{RADIO_PREFIX}DR P3" in src
    # Per-speaker player has no output/sound-mode selector.
    assert "sound_mode_list" not in player.entity.attributes


def test_player_select_source_routes_to_its_client():
    api, player, client = _player_for(
        "Beosound Emerge", "EMERGE",
        [{"id": "spotify", "name": "Spotify"}], [{"id": 1, "name": "DR P3"}])
    client.set_source = AsyncMock(return_value=True)
    client.activate_preset = AsyncMock(return_value=True)

    asyncio.run(player._select_source({"source": "Spotify"}))
    client.set_source.assert_awaited_once_with("spotify")
    asyncio.run(player._select_source({"source": f"{RADIO_PREFIX}DR P3"}))
    client.activate_preset.assert_awaited_once_with(1)


def test_player_push_updates_entity():
    api, player, client = _player_for(
        "Davies9", "A9", [{"id": "radio:1", "name": "B&O Radio"}], [])
    # The client's push handler is wired to this player; a push updates the entity.
    asyncio.run(client.on_update({"title": "Now Playing"}))
    api.configured_entities.update_attributes.assert_called_once()


def test_player_entity_id_is_sanitised():
    api, player, client = _player_for("X", "3071.1200530@products", [], [])
    assert player.entity.id == "beo_player_3071_1200530_products"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
