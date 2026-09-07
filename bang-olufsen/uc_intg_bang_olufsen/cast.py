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
import uuid
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
            self._teardown()  # stale connection; reconnect

        # Connect straight to the speaker's IP (about a second). mDNS discovery
        # is only a fallback: its 10 s window alone exceeds the Remote's command
        # timeout, which showed up as "not responding" on the first command
        # after the Remote had been asleep.
        try:
            cast = pychromecast.get_chromecast_from_host(
                (self._host, 8009, uuid.uuid4(), None, self._name), tries=1, timeout=5
            )
            cast.wait(timeout=6)
            self._cast = cast
            return cast
        except Exception as e:  # noqa: BLE001
            _LOG.warning("Direct cast connection to %s (%s) failed: %s; falling back to discovery",
                         self._name, self._host, e)
            try:
                cast.disconnect()
            except Exception:  # noqa: BLE001
                pass

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

    def play_sync(self, url: str, content_type: str, title: str,
                  image: Optional[str] = None) -> bool:
        """Cast a stream URL and return whether the receiver actually took it.

        Always connects fresh (about half a second by IP): a cached socket can
        look connected but be dead after the Remote or its WiFi has slept, in
        which case the LOAD silently goes nowhere. Success means a media status
        for *this* stream was seen in a playing/buffering state; anything else
        is a failure (one retry with a new connection).
        """
        with self._lock:
            for attempt in (1, 2):
                self._teardown()
                try:
                    cast = self._connect()
                    mc = cast.media_controller
                    mc.play_media(url, content_type, title=title, stream_type="LIVE",
                                  thumb=image)
                    try:
                        mc.block_until_active(timeout=10)
                    except Exception:  # noqa: BLE001 - fall through to the status poll
                        pass
                    deadline = time.time() + 12
                    while time.time() < deadline:
                        try:
                            mc.update_status()
                        except Exception:  # noqa: BLE001 - transient; retry
                            time.sleep(0.5)
                            continue
                        st = mc.status
                        ours = (st.content_id == url) or (st.title == title)
                        if ours and st.player_state in ("PLAYING", "BUFFERING"):
                            _LOG.info("Cast of %r to %s confirmed (%s, session %s)",
                                      title, self._name, st.player_state, st.media_session_id)
                            return True
                        if ours and st.player_state == "IDLE" and st.idle_reason == "ERROR":
                            _LOG.error("Cast of %r to %s failed (receiver error)", title, self._name)
                            return False
                        time.sleep(0.5)
                    _LOG.warning("Cast of %r to %s not confirmed (attempt %d; last status %s/%r)",
                                 title, self._name, attempt, mc.status.player_state, mc.status.title)
                except Exception as e:  # noqa: BLE001
                    _LOG.error("Cast of %r to %s failed on attempt %d: %s", title, self._name, attempt, e)
            return False

    def _live_cast(self):
        """A connection that demonstrably answers. A cached socket may be dead
        after a sleep while still claiming to be connected, so ask the receiver
        for its status and reconnect if nothing comes back."""
        cast = self._connect()
        if self._responds(cast):
            return cast
        _LOG.info("Cast connection to %s is stale; reconnecting", self._name)
        self._teardown()
        return self._connect()

    @staticmethod
    def _responds(cast, timeout: float = 2.0) -> bool:
        got = threading.Event()
        try:
            cast.socket_client.receiver_controller.update_status(
                callback_function=lambda *_a, **_k: got.set())
        except Exception:  # noqa: BLE001
            return False
        return got.wait(timeout)

    def stop_sync(self) -> bool:
        """End the cast: quit the receiver app. A media-level STOP only pauses
        the live stream and leaves "Casting: <station>" on the speaker (its
        source stays Chromecast); quitting the app returns it to idle. Verified
        on the A9."""
        with self._lock:
            try:
                self._live_cast().quit_app()
                return True
            except Exception as e:  # noqa: BLE001
                _LOG.error("Cast stop on %s failed: %s", self._name, e)
                return False

    def pause_sync(self) -> bool:
        """Pause the cast. Needs a media session id, which a freshly opened
        connection doesn't have yet; ask for status first and, failing that,
        end the cast instead (a live stream can't resume anyway)."""
        with self._lock:
            try:
                cast = self._live_cast()
                mc = cast.media_controller
                if not mc.status.media_session_id:
                    mc.update_status()
                    for _ in range(10):
                        if mc.status.media_session_id:
                            break
                        time.sleep(0.2)
                if mc.status.media_session_id:
                    mc.pause()
                else:
                    cast.quit_app()
                return True
            except Exception as e:  # noqa: BLE001
                _LOG.error("Cast pause on %s failed: %s", self._name, e)
                return False

    def resume_sync(self) -> bool:
        with self._lock:
            try:
                self._live_cast().media_controller.play()
                return True
            except Exception as e:  # noqa: BLE001
                _LOG.error("Cast resume on %s failed: %s", self._name, e)
                return False

    def disconnect(self) -> None:
        with self._lock:
            self._teardown()
