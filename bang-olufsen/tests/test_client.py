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
import ucapi

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


def _player_for(name, serial, sources, radio_stations=None, playlists=None, spotify=None):
    """A single-speaker BeoPlayer with a mocked client and UC API."""
    api = MagicMock()
    client = _make_client()
    client.name = name
    speaker = {"serial": serial, "name": name, "client": client, "sources": sources}
    return api, BeoPlayer(api, speaker, radio_stations or [], spotify, playlists or []), client


def test_picker_has_radio_playlists_and_keeps_only_physical_inputs():
    api, player, client = _player_for(
        "Beosound Emerge", "EMERGE",
        [{"id": "spotify", "name": "Spotify Connect"}, {"id": "bluetooth", "name": "Bluetooth"},
         {"id": "lineIn", "name": "Line-In"}, {"id": "spdif", "name": "Optical"}],
        [{"name": "triple j", "url": "http://x/aac", "content_type": "audio/aac"}],
        [{"name": "Kitchen Disco", "uri": "spotify:playlist:1"}])
    src = player.entity.attributes["source_list"]
    assert f"{RADIO_PREFIX}triple j" in src      # radio
    assert "Kitchen Disco" in src                # playlist
    assert "Line-In" in src and "Optical" in src  # physical inputs kept
    assert "Spotify Connect" not in src and "Bluetooth" not in src  # noise dropped
    assert "sound_mode_list" not in player.entity.attributes


def test_player_select_native_source_routes_to_client():
    api, player, client = _player_for(
        "Beosound Emerge", "EMERGE", [{"id": "lineIn", "name": "Line-In"}])
    client.set_source = AsyncMock(return_value=True)
    asyncio.run(player._select_source({"source": "Line-In"}))
    client.set_source.assert_awaited_once_with("lineIn")


def test_player_select_playlist_from_picker_plays_spotify():
    spot = MagicMock()
    spot.cached_device_id = MagicMock(return_value="dev1")
    spot.start_playlist = AsyncMock(return_value=True)
    api, player, client = _player_for(
        "Davies9", "A9", [], None,
        [{"name": "Kitchen Disco", "uri": "spotify:playlist:1"}], spot)
    rc = asyncio.run(player._select_source({"source": "Kitchen Disco"}))
    spot.start_playlist.assert_awaited_once_with("spotify:playlist:1", "dev1")
    assert rc == ucapi.StatusCodes.OK


def test_player_select_radio_casts_stream_url():
    api, player, client = _player_for(
        "Davies9", "A9", [],
        [{"name": "triple j", "url": "http://x/aac", "content_type": "audio/aac",
          "image": "http://img/tj.png"}])
    # Casting is delegated to BeoCast.play_sync (run in an executor).
    player._cast.play_sync = MagicMock(return_value=True)
    rc = asyncio.run(player._select_source({"source": f"{RADIO_PREFIX}triple j"}))
    player._cast.play_sync.assert_called_once_with(
        "http://x/aac", "audio/aac", "triple j", "http://img/tj.png")
    assert rc == ucapi.StatusCodes.OK
    # The now-playing card is set from the station (push carries no cast metadata).
    attrs = player.entity.attributes
    assert attrs["source"] == f"{RADIO_PREFIX}triple j"
    assert attrs["media_title"] == "triple j"
    assert attrs["media_image_url"] == "http://img/tj.png"


def test_player_push_updates_entity():
    api, player, client = _player_for(
        "Davies9", "A9", [{"id": "radio:1", "name": "B&O Radio"}])
    # The client's push handler is wired to this player; a push updates the entity.
    asyncio.run(client.on_update({"title": "Now Playing"}))
    api.configured_entities.update_attributes.assert_called_once()


def test_player_ignores_mirrored_metadata_when_off():
    from ucapi.media_player import States
    api, player, client = _player_for(
        "Alex's Emerge", "E", [{"id": "spotify", "name": "Spotify Connect"}])
    # Off + a mirrored now-playing (B&O shares it across speakers) -> stays empty.
    asyncio.run(player._apply({"on": False}))
    asyncio.run(player._apply({"title": "Mirror", "artist": "X", "image_url": "http://x"}))
    assert player.entity.attributes["media_title"] == ""
    assert player.entity.attributes["media_image_url"] == ""
    assert player.entity.attributes["state"] == States.OFF
    # Powers on and plays its own track -> shows it.
    asyncio.run(player._apply({"on": True}))
    asyncio.run(player._apply({"playing": True, "title": "Real", "image_url": "http://y"}))
    assert player.entity.attributes["media_title"] == "Real"
    assert player.entity.attributes["state"] == States.PLAYING


def test_player_entity_id_is_sanitised():
    api, player, client = _player_for("X", "3071.1200530@products", [])
    assert player.entity.id == "beo_player_3071_1200530_products"


def test_player_play_spotify_playlist():
    spot = MagicMock()
    spot.cached_device_id = MagicMock(return_value=None)  # force live resolve
    spot.resolve_device_id = AsyncMock(return_value="dev123")
    spot.start_playlist = AsyncMock(return_value=True)
    api = MagicMock()
    client = _make_client()
    client.name = "Davies9"
    speaker = {"serial": "A9", "name": "Davies9", "client": client, "sources": []}
    player = BeoPlayer(api, speaker, [], spot)

    ok = asyncio.run(player.play_spotify_playlist("spotify:playlist:1", "Chill"))
    assert ok is True
    spot.resolve_device_id.assert_awaited_once_with("Davies9")
    spot.start_playlist.assert_awaited_once_with("spotify:playlist:1", "dev123")
    assert player.entity.attributes["media_title"] == "Chill"


def test_player_spotify_fast_path_uses_cached_device():
    spot = MagicMock()
    spot.cached_device_id = MagicMock(return_value="cachedDev")
    spot.resolve_device_id = AsyncMock(return_value=None)
    spot.start_playlist = AsyncMock(return_value=True)
    api = MagicMock()
    client = _make_client()
    client.name = "Davies9"
    client.set_source = AsyncMock()
    speaker = {"serial": "A9", "name": "Davies9", "client": client, "sources": []}
    player = BeoPlayer(api, speaker, [], spot)

    ok = asyncio.run(player.play_spotify_playlist("spotify:playlist:1", "X"))
    assert ok is True
    spot.start_playlist.assert_awaited_once_with("spotify:playlist:1", "cachedDev")
    spot.resolve_device_id.assert_not_awaited()  # no live lookup needed
    client.set_source.assert_not_called()        # no wake / fixed delay


def test_player_transport_routes_to_spotify_when_active():
    from ucapi.media_player import Commands as MpCommands
    spot = MagicMock()
    spot.next_track = AsyncMock(return_value=True)
    api = MagicMock()
    client = _make_client()
    client.next_track = AsyncMock(return_value=True)
    speaker = {"serial": "E", "name": "Emerge", "client": client,
               "sources": [{"id": "spotify", "name": "Spotify Connect"}]}
    player = BeoPlayer(api, speaker, [], spot)

    # Spotify active -> NEXT goes to the Spotify Web API, not the (no-op) B&O skip.
    player.entity.attributes["source"] = "Spotify Connect"
    asyncio.run(player.cmd_handler(player.entity, MpCommands.NEXT, None))
    spot.next_track.assert_awaited_once()
    client.next_track.assert_not_awaited()

    # Non-Spotify source -> NEXT goes to the B&O client.
    player.entity.attributes["source"] = "Radio: triple j"
    asyncio.run(player.cmd_handler(player.entity, MpCommands.NEXT, None))
    client.next_track.assert_awaited_once()


def test_remote_playlist_button_plays_spotify():
    from uc_intg_bang_olufsen.remote import BeoRemote
    api, player, client = _player_for("Davies9", "A9", [])
    player.play_spotify_playlist = AsyncMock(return_value=True)
    remote = BeoRemote(api, player, "Davies9", "A9", [],
                       [{"name": "Chill", "uri": "spotify:playlist:1"}])
    cmd = next(iter(remote._playlist_cmds))
    rc = asyncio.run(remote._send({"command": cmd}))
    player.play_spotify_playlist.assert_awaited_once_with("spotify:playlist:1", "Chill")
    assert rc == ucapi.StatusCodes.OK


def test_remote_multiroom_join_leave():
    from uc_intg_bang_olufsen.remote import BeoRemote
    api, player, client = _player_for("Davies9", "A9", [])
    joiner = MagicMock()
    joiner.beolink_join_latest = AsyncMock(return_value=True)
    joiner.beolink_leave = AsyncMock(return_value=True)
    player.set_multiroom(joiner, "Alex's Emerge")
    remote = BeoRemote(api, player, "Davies9", "A9", [], [])

    assert "JOIN_GROUP" in remote.entity.options["simple_commands"]
    asyncio.run(remote._send({"command": "JOIN_GROUP"}))
    joiner.beolink_join_latest.assert_awaited_once()
    asyncio.run(remote._send({"command": "LEAVE_GROUP"}))
    joiner.beolink_leave.assert_awaited_once()


def test_remote_no_multiroom_page_when_solo():
    from uc_intg_bang_olufsen.remote import BeoRemote
    api, player, client = _player_for("Davies9", "A9", [])
    remote = BeoRemote(api, player, "Davies9", "A9", [], [])
    assert "JOIN_GROUP" not in remote.entity.options["simple_commands"]


def test_remote_buttons_delegate_to_player():
    from ucapi.media_player import Commands as MpCommands
    from uc_intg_bang_olufsen.remote import BeoRemote
    stations = [{"name": "triple j", "url": "http://x", "content_type": "audio/aac"}]
    api, player, client = _player_for("Davies9", "A9", [], stations)
    player.cmd_handler = AsyncMock(return_value=ucapi.StatusCodes.OK)
    remote = BeoRemote(api, player, "Davies9", "A9", stations)

    assert remote.entity.id == "beo_remote_A9"
    cmds = remote.entity.options["simple_commands"]
    assert "PLAY_PAUSE" in cmds and len(set(cmds)) == len(cmds)

    # Transport button -> player's media-player handler.
    asyncio.run(remote._send({"command": "PLAY_PAUSE"}))
    assert player.cmd_handler.await_args.args[1] == MpCommands.PLAY_PAUSE

    # Radio button -> select_source with the "Radio: <name>" source (casts).
    radio_cmd = next(iter(remote._radio_cmds))
    asyncio.run(remote._send({"command": radio_cmd}))
    args = player.cmd_handler.await_args.args
    assert args[1] == MpCommands.SELECT_SOURCE and args[2] == {"source": "Radio: triple j"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
