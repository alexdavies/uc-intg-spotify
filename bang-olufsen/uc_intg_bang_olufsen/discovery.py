"""
mDNS/Zeroconf discovery for Bang & Olufsen Mozart devices.

Mozart-platform speakers (Beosound Emerge, Balance, Level, A5, A9 5th gen, ...)
advertise themselves on the LAN as ``_bangolufsen._tcp.local.``. This is the
discovery mechanism the UC built-in B&O integration lacks, which is why it sees
older (legacy-protocol) speakers but not Mozart ones like the Emerge.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import logging
from typing import Dict, List

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

_LOG = logging.getLogger(__name__)

MOZART_SERVICE_TYPE = "_bangolufsen._tcp.local."


async def discover_devices(timeout: float = 5.0) -> List[Dict[str, str]]:
    """
    Browse the LAN for Mozart devices.

    Returns a list of {host, name, serial, model} dicts. ``host`` is an IP
    address suitable for constructing a BeoClient.
    """
    found: Dict[str, Dict[str, str]] = {}
    azc = AsyncZeroconf()
    pending: List[asyncio.Task] = []

    def _on_change(zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added:
            return
        pending.append(asyncio.ensure_future(_resolve(azc, service_type, name, found)))

    browser = AsyncServiceBrowser(
        azc.zeroconf, MOZART_SERVICE_TYPE, handlers=[_on_change]
    )

    try:
        await asyncio.sleep(timeout)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await browser.async_cancel()
        await azc.async_close()

    devices = list(found.values())
    _LOG.info("Discovered %d Bang & Olufsen Mozart device(s)", len(devices))
    return devices


async def _resolve(azc: AsyncZeroconf, service_type: str, name: str, found: Dict[str, Dict[str, str]]) -> None:
    info = AsyncServiceInfo(service_type, name)
    if not await info.async_request(azc.zeroconf, 3000):
        return

    addresses = info.parsed_scoped_addresses() or info.parsed_addresses()
    if not addresses:
        return

    props = _decode_properties(info.properties)
    serial = props.get("serial") or name.split(".")[0]
    device = {
        "host": addresses[0],
        "name": props.get("friendlyName") or props.get("name") or name.split(".")[0],
        "serial": serial,
        "model": props.get("modelId") or props.get("model") or "",
    }
    found[serial] = device


def _decode_properties(properties: dict) -> Dict[str, str]:
    decoded: Dict[str, str] = {}
    for key, value in (properties or {}).items():
        try:
            k = key.decode() if isinstance(key, bytes) else str(key)
            v = value.decode() if isinstance(value, bytes) else value
            decoded[k] = v
        except Exception:
            continue
    return decoded
