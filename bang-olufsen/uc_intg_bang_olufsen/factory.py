"""
Backend selection for Bang & Olufsen speakers.

Mozart-platform speakers use ``BeoClient``; older "ASE"/BeoNetRemote speakers
(e.g. Beoplay A9 4th gen) use ``LegacyBeoClient``. Both expose the same public
interface, so the entity layer is agnostic to which one it holds.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
from typing import Optional, Union

import aiohttp

from uc_intg_bang_olufsen.client import BeoClient
from uc_intg_bang_olufsen.legacy_client import LegacyBeoClient

_LOG = logging.getLogger(__name__)

PROTOCOL_MOZART = "mozart"
PROTOCOL_LEGACY = "legacy"

AnyBeoClient = Union[BeoClient, LegacyBeoClient]


def create_client(host: str, name: Optional[str] = None, serial: Optional[str] = None,
                  protocol: str = PROTOCOL_MOZART) -> AnyBeoClient:
    """Construct the right client for a known protocol."""
    if protocol == PROTOCOL_LEGACY:
        return LegacyBeoClient(host, name, serial)
    return BeoClient(host, name, serial)


async def detect_protocol(host: str) -> Optional[str]:
    """
    Probe a host and return its protocol, or None if unreachable.

    Tries Mozart first (the integration's primary target), then the legacy
    ASE descriptor at :8080/BeoDevice.
    """
    mozart = BeoClient(host)
    try:
        if await mozart.connect():
            return PROTOCOL_MOZART
    except Exception:
        pass
    finally:
        await mozart.close()

    try:
        async with aiohttp.ClientSession() as session:
            timeout = aiohttp.ClientTimeout(total=4, connect=3, sock_connect=3)
            async with session.get(f"http://{host}:8080/BeoDevice", timeout=timeout) as resp:
                if resp.status == 200:
                    return PROTOCOL_LEGACY
    except Exception:
        pass

    return None
