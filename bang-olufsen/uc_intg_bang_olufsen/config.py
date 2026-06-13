"""
Configuration management for the Bang & Olufsen integration.

Stores the set of configured speakers (host, name, serial) so the integration
can recreate entities on restart without re-running discovery.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import json
import logging
from typing import Any, Dict, List, Optional

_LOG = logging.getLogger(__name__)


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

    def get_active_speaker(self) -> Optional[str]:
        """Serial of the last-selected output speaker, if any."""
        return self._data.get("active_speaker")

    def set_active_speaker(self, serial: str) -> bool:
        self._data["active_speaker"] = serial
        return self._save()

    def reset(self) -> bool:
        self._data = {"devices": []}
        return self._save()
