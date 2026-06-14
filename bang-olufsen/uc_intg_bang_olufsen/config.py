"""
Configuration management for the Bang & Olufsen integration.

Stores the set of configured speakers (host, name, serial) so the integration
can recreate entities on restart without re-running discovery.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import json
import logging
from typing import Any, Dict, List

_LOG = logging.getLogger(__name__)

# Custom radio list, played by *casting* a stream URL to each speaker's Chromecast
# (the B&O radio API can't be driven locally — see API_NOTES.md). Editable in
# config.json under "radio_stations"; these are the seeded defaults.
DEFAULT_RADIO_STATIONS: List[Dict[str, str]] = [
    {"name": "BBC Radio 2", "url": "https://lsn.lv/bbcradio.m3u8?station=bbc_radio_two&bitrate=320000", "content_type": "application/x-mpegurl", "image": "https://static.airable.io/29/89/434227.png"},
    {"name": "BBC Radio 4", "url": "https://lsn.lv/bbcradio.m3u8?station=bbc_radio_fourfm&bitrate=320000", "content_type": "application/x-mpegurl", "image": "https://static.airable.io/69/74/972558.png"},
    {"name": "BBC Radio 6 Music", "url": "https://lsn.lv/bbcradio.m3u8?station=bbc_6music&bitrate=320000", "content_type": "application/x-mpegurl", "image": "https://static.airable.io/49/42/159641.png"},
    {"name": "triple j", "url": "https://live-radio01.mediahubaustralia.com/2TJW/aac/", "content_type": "audio/aac", "image": "https://static.airable.io/15/36/215005.png"},
    {"name": "Energy Zürich", "url": "https://energyzuerich.ice.infomaniak.ch/energyzuerich-high.mp3", "content_type": "audio/mpeg", "image": "https://static.airable.io/74/24/436752.png"},
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

    def reset(self) -> bool:
        self._data = {"devices": []}
        return self._save()
