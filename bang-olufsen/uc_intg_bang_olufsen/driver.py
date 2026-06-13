#!/usr/bin/env python3
"""
Bang & Olufsen (Mozart) integration driver for Unfolded Circle Remote 2/3.

Creates one media-player and one remote entity per configured speaker. State is
pushed from each speaker's Mozart notification WebSocket, so there is no polling.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os
import signal
from typing import Dict, List, Optional

import ucapi

from uc_intg_bang_olufsen.client import BeoClient
from uc_intg_bang_olufsen.config import BeoConfig
from uc_intg_bang_olufsen.media_player import BeoMediaPlayer
from uc_intg_bang_olufsen.remote import BeoRemote
from uc_intg_bang_olufsen.setup import BeoSetup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)8s | %(name)s | %(message)s",
)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
_LOG = logging.getLogger(__name__)

loop = asyncio.get_event_loop()
api: Optional[ucapi.IntegrationAPI] = None
config: Optional[BeoConfig] = None

# serial -> client / entities
clients: Dict[str, BeoClient] = {}
media_players: Dict[str, BeoMediaPlayer] = {}


async def on_setup_complete():
    """Build clients and entities for every configured device."""
    global clients, media_players
    _LOG.info("Setup complete. Creating Bang & Olufsen entities...")

    devices = config.get_devices()
    if not devices:
        await api.set_device_state(ucapi.DeviceStates.ERROR)
        return

    for device in devices:
        serial = device.get("serial") or device.get("host")
        if serial in clients:
            continue
        client = BeoClient(device["host"], device.get("name"), serial)
        clients[serial] = client

        sources = await client.get_sources()
        presets = await client.get_presets()
        peers = [
            {"serial": d.get("serial") or d.get("host"), "name": d.get("name")}
            for d in devices
            if (d.get("serial") or d.get("host")) != serial
        ]

        media = BeoMediaPlayer(api, client, sources, presets)
        media_players[serial] = media
        api.available_entities.add(media.entity)

        remote = BeoRemote(api, client, presets, peers, _resolve_peer_jid)
        api.available_entities.add(remote.entity)

    await api.set_device_state(ucapi.DeviceStates.CONNECTED)


async def _resolve_peer_jid(peer_serial: str) -> Optional[str]:
    """Resolve a configured peer's Beolink JID for multiroom expansion."""
    client = clients.get(peer_serial)
    if client:
        return await client.get_beolink_jid()
    return None


async def on_connect():
    if api and config and config.is_configured():
        await api.set_device_state(ucapi.DeviceStates.CONNECTED)


async def on_subscribe_entities(entity_ids: List[str]):
    _LOG.info("Subscribed: %s", entity_ids)
    for media in media_players.values():
        if media.entity.id in entity_ids:
            await media.initialize()


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
        for client in clients.values():
            await client.close()
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
