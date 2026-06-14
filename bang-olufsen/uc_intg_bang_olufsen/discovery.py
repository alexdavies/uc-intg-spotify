"""
mDNS/Zeroconf discovery for Bang & Olufsen speakers.

Modern Mozart-platform speakers (Beosound Emerge, Balance, Level, A5, A9 5th
gen, ...) advertise themselves on the LAN as ``_bangolufsen._tcp.local.``. Older
legacy ("ASE" / BeoNetRemote) speakers (Beoplay A9 4th gen, Beosound 35, ...)
advertise as ``_beoremote._tcp.local.`` instead. We browse both so either kind
shows up in setup already named, rather than forcing a manual-IP entry.

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
LEGACY_SERVICE_TYPE = "_beoremote._tcp.local."

# Service type -> protocol tag understood by factory.create_client().
_SERVICE_PROTOCOLS = {
    MOZART_SERVICE_TYPE: "mozart",
    LEGACY_SERVICE_TYPE: "legacy",
}


async def discover_devices(timeout: float = 5.0) -> List[Dict[str, str]]:
    """
    Browse the LAN for both Mozart and legacy Bang & Olufsen speakers.

    Returns a list of {host, name, serial, model, protocol} dicts. ``host`` is an
    IP address suitable for constructing a client.
    """
    found: Dict[str, Dict[str, str]] = {}
    azc = AsyncZeroconf()
    pending: List[asyncio.Task] = []

    def _on_change(zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added:
            return
        pending.append(asyncio.ensure_future(_resolve(azc, service_type, name, found)))

    browsers = [
        AsyncServiceBrowser(azc.zeroconf, service_type, handlers=[_on_change])
        for service_type in _SERVICE_PROTOCOLS
    ]

    try:
        await asyncio.sleep(timeout)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        for browser in browsers:
            await browser.async_cancel()
        await azc.async_close()

    devices = list(found.values())
    _LOG.info("Discovered %d Bang & Olufsen device(s)", len(devices))
    return devices


async def _resolve(azc: AsyncZeroconf, service_type: str, name: str, found: Dict[str, Dict[str, str]]) -> None:
    info = AsyncServiceInfo(service_type, name)
    if not await info.async_request(azc.zeroconf, 3000):
        return

    addresses = info.parsed_scoped_addresses() or info.parsed_addresses()
    if not addresses:
        return

    props = _decode_properties(info.properties)
    serial = props.get("serial") or _serial_from_jid(props.get("jid")) or name.split(".")[0]
    device = {
        "host": addresses[0],
        "name": props.get("friendlyName") or props.get("name") or name.split(".")[0],
        "serial": serial,
        "model": props.get("modelId") or props.get("model") or props.get("productType") or "",
        "protocol": _SERVICE_PROTOCOLS.get(service_type, "mozart"),
    }
    # If a speaker answers on both service types, prefer the Mozart entry.
    if serial not in found or device["protocol"] == "mozart":
        found[serial] = device


def _serial_from_jid(jid) -> str:
    """Extract the serial from a B&O JID like '3071.1200530.36069564@...'."""
    if not jid or "@" not in str(jid):
        return ""
    return str(jid).split("@", 1)[0].split(".")[-1]


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
