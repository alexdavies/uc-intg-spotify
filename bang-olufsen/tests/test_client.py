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


async def _select_and_cast(player, source):
    """Select a radio source and wait for the background cast task."""
    rc = await player._select_source({"source": source})
    if player._cast_task:
        await player._cast_task
    return rc


def test_picker_has_radio_and_playlists_no_raw_inputs():
    api, player, client = _player_for(
        "Beosound Emerge", "EMERGE",
        [{"id": "spotify", "name": "Spotify Connect"}, {"id": "bluetooth", "name": "Bluetooth"},
         {"id": "lineIn", "name": "Line-In"}],
        [{"name": "triple j", "url": "http://x/aac", "content_type": "audio/aac"}],
        [{"name": "Kitchen Disco", "uri": "spotify:playlist:1"}])
    src = player.entity.attributes["source_list"]
    assert f"{RADIO_PREFIX}triple j" in src and "Kitchen Disco" in src
    # Raw inputs are dropped (picker is radio + playlists only by default).
    assert "Spotify Connect" not in src and "Bluetooth" not in src and "Line-In" not in src
    assert "sound_mode_list" not in player.entity.attributes


def test_player_select_native_source_routes_to_client():
    api, player, client = _player_for("Beosound Emerge", "EMERGE", [])
    player._source_ids = {"Line-In": "lineIn"}  # inputs are filtered out by default; inject one
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

    async def run():
        from ucapi.media_player import States
        rc = await player._select_source({"source": f"{RADIO_PREFIX}triple j"})
        # Acknowledged immediately (the Remote must not wait on the cast) and
        # shown as buffering with the station card already filled in.
        assert rc == ucapi.StatusCodes.OK
        assert player.entity.attributes["state"] == States.BUFFERING
        await player._cast_task
        player._cast.play_sync.assert_called_once_with(
            "http://x/aac", "audio/aac", "triple j", "http://img/tj.png")
        assert player.entity.attributes["state"] == States.PLAYING
    asyncio.run(run())
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


def test_cast_source_label_survives_chromecast_push():
    api, player, client = _player_for(
        "Davies9", "A9", [], [{"name": "triple j", "url": "http://x", "content_type": "audio/aac"}])
    player.entity.attributes["source"] = f"{RADIO_PREFIX}triple j"
    asyncio.run(client.on_update({"source_name": "Chromecast built-in", "on": True}))
    assert player.entity.attributes["source"] == f"{RADIO_PREFIX}triple j"
    # A genuinely different source still comes through.
    asyncio.run(client.on_update({"source_name": "Spotify"}))
    assert player.entity.attributes["source"] == "Spotify"


def test_volume_up_reads_live_level_and_updates_cache():
    from ucapi.media_player import Commands as MpCommands
    api, player, client = _player_for("Davies9", "A9", [])
    # Entity cache is stale (0, as on a fresh start) but the speaker says 40%.
    client.get_volume = AsyncMock(return_value=40)
    client.set_volume = AsyncMock(return_value=True)
    rc = asyncio.run(player.cmd_handler(player.entity, MpCommands.VOLUME_UP, None))
    assert rc == ucapi.StatusCodes.OK
    client.set_volume.assert_awaited_once_with(42)  # default step is 2%
    assert player.entity.attributes["volume"] == 42
    asyncio.run(player.cmd_handler(player.entity, MpCommands.VOLUME_DOWN, None))
    client.set_volume.assert_awaited_with(38)  # live read (40) still wins


def test_volume_nudge_falls_back_to_cache_and_clamps():
    from ucapi.media_player import Commands as MpCommands
    api, player, client = _player_for("Davies9", "A9", [])
    client.get_volume = AsyncMock(return_value=None)
    client.set_volume = AsyncMock(return_value=True)
    player.entity.attributes["volume"] = 98
    asyncio.run(player.cmd_handler(player.entity, MpCommands.VOLUME_UP, None))
    client.set_volume.assert_awaited_once_with(100)


def test_volume_step_is_configurable():
    api, player, client = _player_for("Davies9", "A9", [])
    player._volume_step = 2
    client.get_volume = AsyncMock(return_value=10)
    client.set_volume = AsyncMock(return_value=True)
    asyncio.run(player._nudge_volume(-1))
    client.set_volume.assert_awaited_once_with(8)


def test_toggle_power_uses_entity_state():
    from ucapi.media_player import Commands as MpCommands, States
    api, player, client = _player_for("Davies9", "A9", [])
    client.power_on = AsyncMock(return_value=True)
    client.standby = AsyncMock(return_value=True)
    assert "toggle" in player.entity.features
    player.entity.attributes["state"] = States.OFF
    asyncio.run(player.cmd_handler(player.entity, MpCommands.TOGGLE, None))
    client.power_on.assert_awaited_once()
    player.entity.attributes["state"] = States.PLAYING
    asyncio.run(player.cmd_handler(player.entity, MpCommands.TOGGLE, None))
    client.standby.assert_awaited_once()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ----- radio now-playing ------------------------------------------------------

def test_nowplaying_parsers():
    from uc_intg_bang_olufsen import nowplaying as np
    abc = np.parse_abc({"now": {"recording": {"title": "What A Life", "artists": [{"name": "FISHER"}],
                       "releases": [{"artwork": [{"url": "http://a/orig.jpg", "sizes": [
                           {"aspect_ratio": "1x1", "width": 100, "url": "http://a/100.jpg"},
                           {"aspect_ratio": "1x1", "width": 580, "url": "http://a/580.jpg"},
                           {"aspect_ratio": "4x3", "width": 600, "url": "http://a/wide.jpg"}]}]}]}}})
    assert abc == {"title": "What A Life", "artist": "FISHER", "image_url": "http://a/580.jpg"}
    assert np.parse_abc({"now": {}}) is None

    icy = np.parse_icy("StreamTitle='Selena Gomez ˗ Single Soon';StreamUrl='https://x/cover.jpg?ref=1';")
    assert icy == {"title": "Single Soon", "artist": "Selena Gomez", "image_url": "https://x/cover.jpg?ref=1"}
    assert np.parse_icy("StreamTitle='';") is None
    assert np.parse_icy("StreamTitle='Just a jingle';")["artist"] == ""

    seg = np.parse_bbc_segment({"data": [{"titles": {"primary": "Britten", "secondary": "Young Person's Guide"},
                                          "image_url": "https://i/{recipe}/p.jpg", "offset": {"now_playing": True}}]})
    assert seg == {"title": "Young Person's Guide", "artist": "Britten", "image_url": "https://i/640x640/p.jpg"}
    assert np.parse_bbc_segment({"data": [{"titles": {"primary": "x"}, "offset": {"now_playing": False}}]}) is None
    bc = np.parse_bbc_broadcast({"data": [{"programme": {"titles": {"primary": "BBC Proms", "secondary": "2026",
                                                                    "tertiary": "New World"}, "image_url": None}}]})
    assert bc == {"title": "New World", "artist": "BBC Proms · 2026", "image_url": ""}


def test_radio_now_playing_owns_card_while_active():
    from ucapi.media_player import States
    station = {"name": "Energy Zürich", "url": "http://x", "content_type": "audio/mpeg",
               "image": "http://logo.png", "nowplaying": {"type": "icy"}}
    api, player, client = _player_for("Davies9", "A9", [], [station])
    player._cast.play_sync = MagicMock(return_value=True)

    async def run():
        rc = await _select_and_cast(player, f"{RADIO_PREFIX}Energy Zürich")
        assert rc == ucapi.StatusCodes.OK
        assert player._radio_metadata_active  # poller started
        player.apply_radio_now_playing(station, {"title": "Single Soon", "artist": "Selena Gomez", "image_url": "http://cover.jpg"})
        a = player.entity.attributes
        assert (a["media_title"], a["media_artist"], a["media_album"], a["media_image_url"]) == \
            ("Single Soon", "Selena Gomez", "Energy Zürich", "http://cover.jpg")
        # The A9 echoing the cast's station name must not clobber the track.
        await client.on_update({"title": "Energy Zürich", "image_url": "http://logo.png"})
        assert player.entity.attributes["media_title"] == "Single Soon"
        # Switching to another source stops the poller.
        await client.on_update({"source_name": "Spotify"})
        assert not player._radio_metadata_active
        player._stop_now_playing()
    asyncio.run(run())


def test_logo_reference_becomes_data_url(tmp_path):
    from uc_intg_bang_olufsen import player as player_mod
    (tmp_path / "x.png").write_bytes(b"\x89PNG fake")
    player_mod._LOGO_DIR = str(tmp_path)
    assert player_mod.resolve_image("logo:x.png").startswith("data:image/png;base64,")
    assert player_mod.resolve_image("logo:missing.png") == ""
    assert player_mod.resolve_image("https://x/y.png") == "https://x/y.png"
    api, player, client = _player_for("Davies9", "A9", [], [{"name": "S", "url": "u", "image": "logo:x.png"}])
    assert player._radio[f"{RADIO_PREFIX}S"]["image"].startswith("data:")


def test_running_cast_is_adopted_from_push():
    station = {"name": "Energy Zürich", "url": "http://x", "content_type": "audio/mpeg",
               "image": "http://logo.png", "nowplaying": {"type": "icy"}}
    api, player, client = _player_for("Davies9", "A9", [], [station])
    player.entity.attributes["source"] = "Chromecast built-in"

    async def run():
        # The A9 echoes the cast's title with the station name.
        await client.on_update({"title": "Energy Zürich", "image_url": "http://a9-echo.png"})
        a = player.entity.attributes
        assert a["source"] == f"{RADIO_PREFIX}Energy Zürich"
        assert a["media_image_url"] == "http://logo.png"
        assert player._radio_metadata_active
        player._stop_now_playing()
        # An unknown title on Chromecast is left alone.
        await client.on_update({"title": "Some podcast"})
        assert not player._radio_metadata_active
    asyncio.run(run())


def test_cast_transport_uses_chromecast_session():
    from ucapi.media_player import Commands as MpCommands, States
    station = {"name": "triple j", "url": "http://x", "content_type": "audio/aac", "image": ""}
    api, player, client = _player_for("Davies9", "A9", [], [station])
    client.stop = AsyncMock(return_value=True)
    client.play_pause = AsyncMock(return_value=True)
    player._cast.play_sync = MagicMock(return_value=True)
    player._cast.stop_sync = MagicMock(return_value=True)
    player._cast.pause_sync = MagicMock(return_value=True)

    async def run():
        await _select_and_cast(player, f"{RADIO_PREFIX}triple j")
        assert player.entity.attributes["state"] == States.PLAYING
        # STOP -> cast session, not the A9's (ignored) stream command.
        assert await player.cmd_handler(player.entity, MpCommands.STOP, None) == ucapi.StatusCodes.OK
        player._cast.stop_sync.assert_called_once(); client.stop.assert_not_awaited()
        assert player.entity.attributes["state"] == States.ON  # idle, station kept
        assert player.entity.attributes["source"] == f"{RADIO_PREFIX}triple j"
        # PLAY while stopped re-casts the station.
        await player.cmd_handler(player.entity, MpCommands.PLAY_PAUSE, None)
        await player._cast_task
        assert player._cast.play_sync.call_count == 2
        assert player.entity.attributes["state"] == States.PLAYING
        # PAUSE while playing pauses the cast.
        await player.cmd_handler(player.entity, MpCommands.PLAY_PAUSE, None)
        player._cast.pause_sync.assert_called_once(); client.play_pause.assert_not_awaited()
        assert player.entity.attributes["state"] == States.PAUSED
        # Not casting (e.g. Spotify source): the normal path is used.
        player.entity.attributes["source"] = "Spotify"
        player._spotify = None
        await player.cmd_handler(player.entity, MpCommands.STOP, None)
        client.stop.assert_awaited_once()
    asyncio.run(run())


def test_power_on_resumes_last_source(tmp_path):
    from ucapi.media_player import Commands as MpCommands, States
    from uc_intg_bang_olufsen.config import BeoConfig
    from uc_intg_bang_olufsen.player import BeoPlayer
    cfg = BeoConfig(str(tmp_path / "config.json"))
    station = {"name": "triple j", "url": "http://x", "content_type": "audio/aac", "image": ""}
    client = MagicMock(); client.host = "10.0.0.9"; client.power_on = AsyncMock(return_value=True)
    speaker = {"serial": "A9", "name": "Davies9", "client": client, "sources": []}
    player = BeoPlayer(MagicMock(), speaker, [station], None, [], config=cfg)
    player._cast.play_sync = MagicMock(return_value=True)

    async def run():
        # Nothing remembered yet: plain power on.
        player.entity.attributes["state"] = States.OFF
        await player.cmd_handler(player.entity, MpCommands.ON, None)
        client.power_on.assert_awaited_once(); player._cast.play_sync.assert_not_called()
        # Play a station, switch off, power on -> the station is re-cast.
        await _select_and_cast(player, f"{RADIO_PREFIX}triple j")
        assert cfg.get_last_source("A9") == f"{RADIO_PREFIX}triple j"
        player.entity.attributes["state"] = States.OFF
        rc = await player.cmd_handler(player.entity, MpCommands.ON, None)
        await player._cast_task
        assert rc == ucapi.StatusCodes.OK and player._cast.play_sync.call_count == 2
        assert player.entity.attributes["state"] == States.PLAYING
        player._stop_now_playing()
    asyncio.run(run())

    # A fresh player (driver restart) still knows the last source.
    again = BeoPlayer(MagicMock(), speaker, [station], None, [], config=BeoConfig(str(tmp_path / "config.json")))
    assert again._last_source == f"{RADIO_PREFIX}triple j"


def test_setup_imports_pasted_config(tmp_path):
    from uc_intg_bang_olufsen.config import BeoConfig
    from uc_intg_bang_olufsen.setup import BeoSetup
    cfg = BeoConfig(str(tmp_path / "config.json"))
    done = AsyncMock()
    setup = BeoSetup(cfg, done)
    pasted = '{"devices": [{"host": "10.0.0.9", "name": "Davies9", "serial": "1", "protocol": "legacy"}], "spotify_access_token": "t", "spotify_refresh_token": "r"}'
    msg = ucapi.DriverSetupRequest(False, {"config_json": pasted, "spotify_client_id": "", "spotify_client_secret": ""})
    result = asyncio.run(setup.setup_handler(msg))
    assert isinstance(result, ucapi.SetupComplete)
    done.assert_awaited_once()
    assert cfg.get_devices()[0]["name"] == "Davies9" and cfg.spotify_is_configured()
    # Garbage is rejected rather than wiping the config.
    bad = ucapi.DriverSetupRequest(False, {"config_json": "{not json"})
    assert isinstance(asyncio.run(setup.setup_handler(bad)), ucapi.SetupError)
    assert cfg.get_devices()[0]["name"] == "Davies9"


def test_cast_stop_quits_receiver_app_and_pause_falls_back():
    from uc_intg_bang_olufsen.cast import BeoCast
    c = BeoCast("10.0.0.9", "Davies9")
    fake = MagicMock(); fake.media_controller.status.media_session_id = None
    c._connect = MagicMock(return_value=fake)
    c._responds = MagicMock(return_value=True)  # cached connection answers
    assert c.stop_sync() is True
    fake.quit_app.assert_called_once(); fake.media_controller.stop.assert_not_called()
    # pause with no session id -> asks for status, then ends the cast.
    fake.quit_app.reset_mock()
    assert c.pause_sync() is True
    fake.media_controller.update_status.assert_called(); fake.quit_app.assert_called_once()
    fake.media_controller.pause.assert_not_called()
    # pause with a session id -> real pause.
    fake.media_controller.status.media_session_id = 7; fake.quit_app.reset_mock()
    assert c.pause_sync() is True
    fake.media_controller.pause.assert_called_once(); fake.quit_app.assert_not_called()


def test_failed_cast_is_reported_on_card():
    from ucapi.media_player import States
    station = {"name": "triple j", "url": "http://x", "content_type": "audio/aac", "image": ""}
    api, player, client = _player_for("Davies9", "A9", [], [station])
    player._cast.play_sync = MagicMock(return_value=False)

    async def run():
        rc = await player._select_source({"source": f"{RADIO_PREFIX}triple j"})
        assert rc == ucapi.StatusCodes.OK  # acknowledged; outcome arrives on the card
        await player._cast_task
        assert player.entity.attributes["state"] == States.ON
        assert "failed" in player.entity.attributes["media_title"]
        assert not player._radio_metadata_active
    asyncio.run(run())


def test_cast_play_confirms_own_stream_and_reconnects_fresh():
    from uc_intg_bang_olufsen.cast import BeoCast
    c = BeoCast("10.0.0.9", "Davies9")
    fake = MagicMock(); st = fake.media_controller.status
    st.content_id = "http://x"; st.title = "triple j"; st.player_state = "BUFFERING"; st.media_session_id = 5
    c._connect = MagicMock(return_value=fake); c._teardown = MagicMock()
    assert c.play_sync("http://x", "audio/aac", "triple j", None) is True
    c._teardown.assert_called()  # always a fresh connection per cast
    fake.media_controller.play_media.assert_called_once()
    # A status that never reflects our stream is a failure, not a silent success.
    st.title = "old station"; st.content_id = "http://old"; st.player_state = "PAUSED"
    import uc_intg_bang_olufsen.cast as cast_mod
    real_sleep = cast_mod.time.sleep; cast_mod.time.sleep = lambda *_: None
    real_time = cast_mod.time.time; ticks = iter(range(0, 10000))
    cast_mod.time.time = lambda: next(ticks) * 2.0  # fast-forward the deadline
    try:
        assert c.play_sync("http://x", "audio/aac", "triple j", None) is False
    finally:
        cast_mod.time.sleep = real_sleep; cast_mod.time.time = real_time


def test_now_playing_session_uses_certifi_ca_bundle():
    """The Remote has no system CA store; the poller must use certifi like spotify.py."""
    import inspect
    from uc_intg_bang_olufsen import player as player_mod
    src = inspect.getsource(player_mod.BeoPlayer._now_playing_loop)
    assert "certifi.where()" in src and "TCPConnector(ssl=" in src


def test_cast_never_sends_data_url_as_thumbnail():
    station = {"name": "Energy Zürich", "url": "http://x", "content_type": "audio/mpeg",
               "image": "data:image/png;base64,AAAA"}
    api, player, client = _player_for("Davies9", "A9", [], [station])
    player._cast.play_sync = MagicMock(return_value=True)
    asyncio.run(_select_and_cast(player, f"{RADIO_PREFIX}Energy Zürich"))
    player._cast.play_sync.assert_called_once_with("http://x", "audio/mpeg", "Energy Zürich", None)
    # The Remote's card still gets the data URL.
    assert player.entity.attributes["media_image_url"].startswith("data:")
