#!/usr/bin/env python3
"""
Bang & Olufsen (Mozart) integration driver for Unfolded Circle Remote 2/3.

Creates one independent media-player entity per configured speaker (Mozart or
legacy). State is pushed from each speaker's notification stream, so there is no
polling.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os
import signal
from typing import Dict, List, Optional

import ucapi

from uc_intg_bang_olufsen.config import BeoConfig
from uc_intg_bang_olufsen.factory import AnyBeoClient, create_client
from uc_intg_bang_olufsen.player import BeoPlayer
from uc_intg_bang_olufsen.remote import BeoRemote
from uc_intg_bang_olufsen.setup import BeoSetup
from uc_intg_bang_olufsen.spotify import SpotifyClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)8s | %(name)s | %(message)s",
)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
_LOG = logging.getLogger(__name__)

loop = asyncio.get_event_loop()
api: Optional[ucapi.IntegrationAPI] = None
config: Optional[BeoConfig] = None

clients: Dict[str, AnyBeoClient] = {}
players: Dict[str, BeoPlayer] = {}  # entity id -> player
spotify: Optional[SpotifyClient] = None
spotify_poll_task: Optional[asyncio.Task] = None

# How often to refresh now-playing from Spotify while it's the active source.
SPOTIFY_POLL_SEC = 5


async def spotify_poll_loop():
    """Keep the active Spotify speaker's now-playing card in sync. The speakers'
    own push doesn't track Spotify Connect track changes (and the legacy A9 sends
    no artwork), so poll Spotify and route the update to the matching speaker."""
    while True:
        await asyncio.sleep(SPOTIFY_POLL_SEC)
        if not spotify:
            continue
        try:
            info = await spotify.get_now_playing()
        except Exception:  # noqa: BLE001
            continue
        if not info or not info.get("is_playing") or not info.get("device"):
            continue
        for player in players.values():
            if player.is_spotify_device(info["device"]):
                player.apply_now_playing(info)
                break


async def on_setup_complete():
    """Build the clients and one media-player entity per configured speaker."""
    global clients, players, spotify, spotify_poll_task
    _LOG.info("Setup complete. Creating one Bang & Olufsen player per speaker...")

    devices = config.get_devices()
    if not devices:
        await api.set_device_state(ucapi.DeviceStates.ERROR)
        return

    clients = {}
    players = {}
    # Rebuild from scratch so a reconfigure (e.g. adding Spotify) replaces stale
    # entity definitions rather than keeping the old ones (add() won't overwrite).
    api.available_entities.clear()
    radio_stations = config.get_radio_stations()

    # Optional Spotify playlists (cast to speakers via Spotify Connect).
    spotify = None
    playlists: List[dict] = []
    if config.spotify_is_configured():
        spotify = SpotifyClient(config)
        # When curated (prefixed) playlists exist, show only those, with the
        # marker stripped from the label; otherwise fall back to all.
        all_playlists = await spotify.get_playlists(50)
        prefix = config.get_playlist_prefix()
        curated = [p for p in all_playlists if p["name"].lstrip().startswith(prefix)]
        chosen = curated or all_playlists
        playlists = [
            {"name": (p["name"].lstrip()[len(prefix):].strip() or p["name"]) if curated else p["name"],
             "uri": p["uri"]}
            for p in chosen
        ][:config.get_playlist_limit()]
        # Pre-warm the Connect device cache so the first playlist tap can target
        # the speaker directly without a live device lookup.
        await spotify.get_devices()
        _LOG.info("Spotify enabled: %d playlist(s) shown (%d curated)", len(playlists), len(curated))

    # Phase 1: build clients + players.
    built = []  # (player, serial, name)
    for device in devices:
        serial = device.get("serial") or device.get("host")
        client = create_client(
            device["host"], device.get("name"), serial,
            protocol=device.get("protocol", "mozart"),
        )
        clients[serial] = client
        speaker = {
            "serial": serial,
            "name": device.get("name") or serial,
            "client": client,
            "sources": await client.get_sources(),
        }
        player = BeoPlayer(api, speaker, radio_stations, spotify, playlists)
        players[player.entity.id] = player
        api.available_entities.add(player.entity)
        built.append((player, serial, speaker["name"]))

    # Enable multiroom when there's a 2nd speaker and a Beolink-capable (Mozart)
    # client to do the joining. "Join" always drives that joiner to join the
    # current experience; from either remote it groups the two speakers.
    joiner = next((c for c in clients.values() if hasattr(c, "beolink_join_latest")), None)
    if joiner and len(built) > 1:
        for player, _serial, name in built:
            other = next((n for _p, _s, n in built if n != name), name)
            player.set_multiroom(joiner, other)

    # Phase 2: build each speaker's companion "control" remote.
    for player, serial, name in built:
        remote = BeoRemote(api, player, name, serial, radio_stations, playlists)
        api.available_entities.add(remote.entity)

    # (Re)start the Spotify now-playing poller.
    if spotify_poll_task and not spotify_poll_task.done():
        spotify_poll_task.cancel()
    spotify_poll_task = None
    if spotify:
        spotify_poll_task = asyncio.ensure_future(spotify_poll_loop())

    await api.set_device_state(ucapi.DeviceStates.CONNECTED)


async def on_connect():
    if api and config and config.is_configured():
        await api.set_device_state(ucapi.DeviceStates.CONNECTED)


async def on_subscribe_entities(entity_ids: List[str]):
    _LOG.info("Subscribed: %s", entity_ids)
    for entity_id in entity_ids:
        player = players.get(entity_id)
        if player:
            await player.initialize()


async def init_integration():
    global api, config
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    driver_json_path = os.path.join(project_root, "driver.json")

    api = ucapi.IntegrationAPI(loop)
    config = BeoConfig(os.path.join(api.config_dir_path, "config.json"))

    setup = BeoSetup(config, on_setup_complete)
    await api.init(driver_json_path, setup.setup_handler)

    api.add_listener(ucapi.Events.CONNECT, on_connect)
    api.add_listener(ucapi.Events.SUBSCRIBE_ENTITIES, on_subscribe_entities)


async def main():
    _LOG.info("Starting Bang & Olufsen Integration Driver")
    await init_integration()
    if config and config.is_configured():
        await on_setup_complete()
    else:
        await api.set_device_state(ucapi.DeviceStates.ERROR)
    _LOG.info("Integration is running.")


def shutdown_handler(signum, frame):
    _LOG.warning("Received signal %s. Shutting down...", signum)

    async def cleanup():
        for player in players.values():
            player.close()
        for client in clients.values():
            await client.close()
        if spotify:
            await spotify.close()
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        loop.stop()

    loop.create_task(cleanup())


if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    try:
        loop.run_until_complete(main())
        loop.run_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        _LOG.info("Driver stopped.")
    finally:
        if loop and not loop.is_closed():
            loop.close()
