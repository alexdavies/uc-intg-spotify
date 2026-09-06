"""
Configuration management for the Bang & Olufsen integration.

Stores the set of configured speakers (host, name, serial) so the integration
can recreate entities on restart without re-running discovery.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

_LOG = logging.getLogger(__name__)

# Custom radio list, played by *casting* a stream URL to each speaker's Chromecast
# (the B&O radio API can't be driven locally — see API_NOTES.md). Editable in
# config.json under "radio_stations"; these are the seeded defaults.
DEFAULT_RADIO_STATIONS: List[Dict[str, str]] = [
    # ABC's mediahubaustralia AAC endpoint started answering 403 (2026-09); the
    # akamaized HLS master plays fine on the Chromecast default receiver.
    {"name": "triple j", "url": "https://mediaserviceslive.akamaized.net/hls/live/2038308/triplejnsw/masterhq.m3u8", "content_type": "application/vnd.apple.mpegurl",
     "image": "logo:triple_j.png",
     "nowplaying": {"type": "abc", "service": "triplej"}},
    {"name": "Energy Zürich", "url": "https://energyzuerich.ice.infomaniak.ch/energyzuerich-high.mp3", "content_type": "audio/mpeg",
     "image": "logo:energy_zuerich.png",
     "nowplaying": {"type": "icy"}},
    {"name": "BBC Radio 3", "url": "https://lsn.lv/bbcradio.m3u8?station=bbc_radio_three&bitrate=320000", "content_type": "application/x-mpegurl",
     "image": "logo:bbc_radio_3.png",
     "nowplaying": {"type": "bbc", "service": "bbc_radio_three"}},
]
# "image" is a URL, or "logo:<file>" for a PNG bundled in uc_intg_bang_olufsen/logos
# (sent to the Remote as a base64 data URL, so no external host is involved).
# "nowplaying" (optional) names a metadata provider in nowplaying.py so the card
# shows the current track while the station is cast: abc | icy | bbc.


class BeoConfig:
    """Persisted configuration for the Bang & Olufsen integration."""

    def __init__(self, config_file_path: str):
        self._config_file_path = config_file_path
        self._data: Dict[str, Any] = {"devices": []}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._config_file_path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except FileNotFoundError:
            _LOG.debug("No config file yet, starting empty")
        except (json.JSONDecodeError, OSError) as e:
            _LOG.error("Error loading config: %s", e)
        self._data.setdefault("devices", [])

    def _save(self) -> bool:
        try:
            with open(self._config_file_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            return True
        except OSError as e:
            _LOG.error("Error saving config: %s", e)
            return False

    def is_configured(self) -> bool:
        return bool(self._data.get("devices"))

    def get_devices(self) -> List[Dict[str, str]]:
        return list(self._data.get("devices", []))

    def set_devices(self, devices: List[Dict[str, str]]) -> bool:
        """Replace the device list, de-duplicating by serial (falling back to host)."""
        unique: Dict[str, Dict[str, str]] = {}
        for d in devices:
            key = d.get("serial") or d.get("host")
            if key:
                unique[key] = d
        self._data["devices"] = list(unique.values())
        return self._save()

    def get_radio_stations(self) -> List[Dict[str, str]]:
        """The custom cast radio list (config override, else seeded defaults)."""
        stations = self._data.get("radio_stations")
        return list(stations) if stations else list(DEFAULT_RADIO_STATIONS)

    def set_radio_stations(self, stations: List[Dict[str, str]]) -> bool:
        self._data["radio_stations"] = stations
        return self._save()

    # ----- Spotify (optional) ---------------------------------------------

    def spotify_is_configured(self) -> bool:
        return bool(self._data.get("spotify_access_token") and self._data.get("spotify_refresh_token"))

    def get_client_id(self) -> Optional[str]:
        return self._data.get("spotify_client_id")

    def get_client_secret(self) -> Optional[str]:
        return self._data.get("spotify_client_secret")

    def set_app_credentials(self, client_id: str, client_secret: str) -> bool:
        self._data["spotify_client_id"] = client_id
        self._data["spotify_client_secret"] = client_secret
        return self._save()

    def set_tokens(self, access_token: str, refresh_token: Optional[str], expires_in: int) -> bool:
        self._data["spotify_access_token"] = access_token
        if refresh_token:
            self._data["spotify_refresh_token"] = refresh_token
        self._data["spotify_token_expires_at"] = int(time.time()) + expires_in - 60
        return self._save()

    def get_access_token(self) -> Optional[str]:
        return self._data.get("spotify_access_token")

    def get_refresh_token(self) -> Optional[str]:
        return self._data.get("spotify_refresh_token")

    def is_token_expired(self) -> bool:
        return int(time.time()) >= self._data.get("spotify_token_expires_at", 0)

    def clear_tokens(self) -> bool:
        for k in ("spotify_access_token", "spotify_refresh_token", "spotify_token_expires_at"):
            self._data.pop(k, None)
        return self._save()

    def get_cached_devices(self) -> Dict[str, str]:
        return self._data.get("spotify_known_devices", {})

    def cache_devices(self, devices: Dict[str, str]) -> bool:
        known = self._data.get("spotify_known_devices", {})
        updated = {**known, **devices}
        if updated == known:
            return True
        self._data["spotify_known_devices"] = updated
        return self._save()

    def get_playlist_limit(self) -> int:
        return int(self._data.get("spotify_playlist_limit", 12))

    def get_last_source(self, serial: str) -> Optional[str]:
        """The station/playlist last selected on a speaker (power-on resumes it)."""
        return (self._data.get("last_sources") or {}).get(str(serial))

    def set_last_source(self, serial: str, source: str) -> bool:
        last = self._data.setdefault("last_sources", {})
        if last.get(str(serial)) == source:
            return True
        last[str(serial)] = source
        return self._save()

    def get_volume_step(self) -> int:
        """Percent change per volume up/down press (config "volume_step")."""
        return max(1, int(self._data.get("volume_step", 2)))

    def get_playlist_prefix(self) -> str:
        """Marker prefix for curated playlists; when any exist, only these are
        shown on the remote (and the marker is stripped from the button label)."""
        return self._data.get("spotify_playlist_prefix", "◆")

    def import_json(self, raw: str) -> bool:
        """Replace the whole configuration with a pasted config.json (used to
        migrate from an off-device driver). Must contain a device list."""
        try:
            data = json.loads(raw)
        except ValueError as e:
            _LOG.error("config import: invalid JSON: %s", e)
            return False
        if not isinstance(data, dict) or not isinstance(data.get("devices"), list) or not data["devices"]:
            _LOG.error("config import: no 'devices' list in pasted config")
            return False
        self._data = data
        return self._save()

    def reset(self) -> bool:
        self._data = {"devices": []}
        return self._save()
