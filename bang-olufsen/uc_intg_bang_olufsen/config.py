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
    {"name": "triple j", "url": "https://live-radio01.mediahubaustralia.com/2TJW/aac/", "content_type": "audio/aac", "image": "https://static.airable.io/15/36/215005.png"},
    {"name": "Energy Zürich", "url": "https://energyzuerich.ice.infomaniak.ch/energyzuerich-high.mp3", "content_type": "audio/mpeg", "image": "https://static.airable.io/74/24/436752.png"},
    {"name": "BBC Radio 3", "url": "https://lsn.lv/bbcradio.m3u8?station=bbc_radio_three&bitrate=320000", "content_type": "application/x-mpegurl", "image": ""},
]


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

    def get_playlist_prefix(self) -> str:
        """Marker prefix for curated playlists; when any exist, only these are
        shown on the remote (and the marker is stripped from the button label)."""
        return self._data.get("spotify_playlist_prefix", "◆")

    def reset(self) -> bool:
        self._data = {"devices": []}
        return self._save()
