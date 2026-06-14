"""
Chromecast control for a single Bang & Olufsen speaker.

Both Mozart and legacy B&O speakers expose "Chromecast built-in", and casting a
stream URL is the only way to play an *arbitrary* radio station on the legacy A9
(its B&O Radio source has no local tune API — see API_NOTES.md). pychromecast is
synchronous and thread-based, so the async player calls these via an executor.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import logging
import threading
import time
from typing import Optional

import pychromecast

_LOG = logging.getLogger(__name__)


class BeoCast:
    """Casts stream URLs to one speaker's Chromecast receiver.

    All methods are blocking (pychromecast is sync); call them from an executor.
    The connection is established lazily and reused; it reconnects if dropped.
    """

    def __init__(self, host: str, name: str):
        self._host = host
        self._name = name
        self._cast = None
        self._browser = None
        self._lock = threading.Lock()

    def _connect(self):
        cast = self._cast
        if cast is not None:
            try:
                if cast.socket_client and cast.socket_client.is_connected:
                    return cast
            except Exception:  # noqa: BLE001 - any error -> reconnect below
                pass
            self._teardown()  # stale connection; rediscover

        # The browser's zeroconf instance must stay alive for cast.wait() (and
        # later reconnects) to resolve the device, so we keep it until disconnect.
        casts, self._browser = pychromecast.get_listed_chromecasts(
            friendly_names=[self._name], discovery_timeout=10
        )
        # Prefer an exact host match (names can collide / be renamed).
        cast = next((c for c in casts if c.cast_info.host == self._host), None)
        if cast is None and casts:
            cast = casts[0]
        if cast is None:
            self._teardown()
            raise RuntimeError(f"Chromecast '{self._name}' ({self._host}) not found")
        cast.wait(timeout=10)
        self._cast = cast
        return cast

    def _teardown(self) -> None:
        if self._cast is not None:
            try:
                self._cast.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._cast = None
        if self._browser is not None:
            try:
                pychromecast.discovery.stop_discovery(self._browser)
            except Exception:  # noqa: BLE001
                pass
            self._browser = None

    def play_sync(self, url: str, content_type: str, title: str) -> bool:
        """Cast a stream URL and return whether it reached an active/playing state."""
        with self._lock:
            cast = self._connect()
            mc = cast.media_controller
            mc.play_media(url, content_type, title=title, stream_type="LIVE")
            try:
                mc.block_until_active(timeout=10)
            except Exception:  # noqa: BLE001 - fall through to the status poll
                pass
            # Confirm it actually started rather than erroring out immediately.
            for _ in range(6):
                try:
                    mc.update_status()
                except Exception:  # noqa: BLE001 - transient reconnect; retry
                    time.sleep(1)
                    continue
                state = mc.status.player_state
                if state in ("PLAYING", "BUFFERING"):
                    return True
                if state == "IDLE" and mc.status.idle_reason == "ERROR":
                    _LOG.error("Cast of %r to %s failed (receiver error)", title, self._name)
                    return False
                time.sleep(1)
            # Sent and not errored within the window; treat as success (live
            # streams can take a moment to settle).
            return True

    def stop_sync(self) -> None:
        with self._lock:
            if self._cast is not None:
                try:
                    self._cast.media_controller.stop()
                except Exception:  # noqa: BLE001
                    pass

    def disconnect(self) -> None:
        with self._lock:
            self._teardown()
