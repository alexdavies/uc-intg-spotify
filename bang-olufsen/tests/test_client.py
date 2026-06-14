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


def _two_speaker_player():
    """A unified player fronting an Emerge (Mozart) and an A9 (legacy)."""
    api = MagicMock()
    emerge = _make_client()
    emerge.name = "Beosound Emerge"
    a9 = _make_client()
    a9.name = "Davies9"
    speakers = [
        {"serial": "EMERGE", "name": "Beosound Emerge", "client": emerge,
         "sources": [{"id": "spotify", "name": "Spotify"}],
         "presets": [{"id": 1, "name": "DR P3"}]},
        {"serial": "A9", "name": "Davies9", "client": a9,
         "sources": [{"id": "radio:1", "name": "B&O Radio"}], "presets": []},
    ]
    return api, BeoPlayer(api, speakers), emerge, a9


def test_player_fronts_active_speaker_sources_and_outputs():
    api, player, emerge, a9 = _two_speaker_player()
    # Output list = both speakers; starts on the first.
    assert player.entity.attributes["sound_mode_list"] == ["Beosound Emerge", "Davies9"]
    assert player.entity.attributes["sound_mode"] == "Beosound Emerge"
    # Source list reflects the active (Emerge) speaker, incl. its radio preset.
    src = player.entity.attributes["source_list"]
    assert "Spotify" in src and f"{RADIO_PREFIX}DR P3" in src


def test_player_select_source_routes_to_active_speaker():
    api, player, emerge, a9 = _two_speaker_player()
    emerge.set_source = AsyncMock(return_value=True)
    emerge.activate_preset = AsyncMock(return_value=True)

    asyncio.run(player._select_source({"source": "Spotify"}))
    emerge.set_source.assert_awaited_once_with("spotify")
    asyncio.run(player._select_source({"source": f"{RADIO_PREFIX}DR P3"}))
    emerge.activate_preset.assert_awaited_once_with(1)


def test_player_switch_output_repoints_commands_and_sources():
    api, player, emerge, a9 = _two_speaker_player()
    a9.get_state = AsyncMock(return_value={})
    a9.set_source = AsyncMock(return_value=True)

    # Switch the active output to the A9.
    asyncio.run(player._select_output({"mode": "Davies9"}))
    assert player._active == "A9"
    # Source list now reflects the A9 (no presets, has B&O Radio).
    assert player.entity.attributes["source_list"] == ["B&O Radio"]
    # A source command now routes to the A9, not the Emerge.
    asyncio.run(player._select_source({"source": "B&O Radio"}))
    a9.set_source.assert_awaited_once_with("radio:1")
    emerge.set_source = AsyncMock()
    emerge.set_source.assert_not_awaited()


def test_player_only_active_speaker_pushes_state():
    api, player, emerge, a9 = _two_speaker_player()
    # A push from the inactive A9 must NOT update the entity.
    asyncio.run(a9.on_update({"title": "should be ignored"}))
    assert api.configured_entities.update_attributes.call_count == 0
    # A push from the active Emerge updates it.
    asyncio.run(emerge.on_update({"title": "Now Playing"}))
    assert api.configured_entities.update_attributes.call_count == 1


def test_controls_remote_buttons_route_into_player():
    from uc_intg_bang_olufsen.remote import BeoControlRemote
    api, player, emerge, a9 = _two_speaker_player()
    player.set_output = AsyncMock(return_value=True)
    player.play_preset_on = AsyncMock(return_value=True)
    speakers = [
        {"serial": "EMERGE", "name": "Beosound Emerge", "presets": [{"id": 1, "name": "DR P3"}]},
        {"serial": "A9", "name": "Davies9", "presets": []},
    ]
    remote = BeoControlRemote(api, player, speakers)

    cmds = remote.entity.options["simple_commands"]
    assert len(set(cmds)) == len(cmds)
    assert all(len(c) <= 20 for c in cmds)

    # An output button switches the active speaker.
    out_cmd = next(c for c, name in remote._output_cmds.items() if name == "Davies9")
    asyncio.run(remote._send({"command": out_cmd}))
    player.set_output.assert_awaited_once_with("Davies9")

    # A radio button plays that preset on its owning speaker.
    radio_cmd = next(iter(remote._radio_cmds))
    asyncio.run(remote._send({"command": radio_cmd}))
    player.play_preset_on.assert_awaited_once_with("EMERGE", 1)


def test_controls_remote_transport_routes_to_active_client():
    from uc_intg_bang_olufsen.remote import BeoControlRemote
    api, player, emerge, a9 = _two_speaker_player()
    emerge.play_pause = AsyncMock(return_value=True)
    speakers = [{"serial": "EMERGE", "name": "Beosound Emerge", "presets": []}]
    remote = BeoControlRemote(api, player, speakers)
    asyncio.run(remote._send({"command": "PLAY_PAUSE"}))
    emerge.play_pause.assert_awaited_once()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
