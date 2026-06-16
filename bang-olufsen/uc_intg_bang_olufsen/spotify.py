"""
Minimal Spotify Web API client (vendored for the Bang & Olufsen integration).

Used only to list the user's playlists and start one playing on a B&O speaker via
Spotify Connect — the speaker's local API can't originate Spotify playback, so we
drive it through Spotify's cloud, targeting the speaker as a Connect device.

Adapted from the parent ``uc_intg_spotify`` client so this integration is
self-contained (separate package, separate deployment). Reuses the same OAuth app
(redirect URI ``https://example.com/callback``) and the same token storage shape.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import base64
import logging
import ssl
import urllib.parse
from typing import Any, Dict, List, Optional

import aiohttp
import certifi

_LOG = logging.getLogger(__name__)

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
REDIRECT_URI = "https://example.com/callback"
SCOPES = [
    "user-read-playback-state",
    "user-modify-playback-state",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-top-read",
    "playlist-modify-private",
    "playlist-modify-public",
]


class SpotifyClient:
    """OAuth2 Spotify Web API client backed by the integration config."""

    def __init__(self, config):
        self._config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._refresh_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=ssl_context),
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
            )
        return self._session

    def is_authenticated(self) -> bool:
        return bool(self._config.get_access_token() and self._config.get_refresh_token())

    # ----- OAuth ------------------------------------------------------------

    def get_authorization_url(self) -> str:
        client_id = self._config.get_client_id()
        if not client_id:
            raise ValueError("Spotify client ID not configured")
        params = {
            "response_type": "code",
            "client_id": client_id,
            "scope": " ".join(SCOPES),
            "redirect_uri": REDIRECT_URI,
            "show_dialog": "true",
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code_for_token(self, code: str) -> bool:
        return await self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        })

    async def refresh_access_token(self) -> bool:
        async with self._refresh_lock:
            refresh_token = self._config.get_refresh_token()
            if not refresh_token:
                return False
            return await self._token_request({
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }, fallback_refresh=refresh_token)

    async def _token_request(self, data: Dict[str, str], fallback_refresh: Optional[str] = None) -> bool:
        client_id = self._config.get_client_id()
        client_secret = self._config.get_client_secret()
        if not client_id or not client_secret:
            _LOG.error("Spotify client id/secret not configured")
            return False
        try:
            session = await self._get_session()
            auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            headers = {"Authorization": f"Basic {auth}",
                       "Content-Type": "application/x-www-form-urlencoded"}
            async with session.post(TOKEN_URL, headers=headers, data=data) as resp:
                if resp.status == 200:
                    td = await resp.json()
                    self._config.set_tokens(
                        td["access_token"],
                        td.get("refresh_token", fallback_refresh),
                        td.get("expires_in", 3600),
                    )
                    return True
                _LOG.error("Spotify token request failed: %s %s", resp.status, await resp.text())
                return False
        except Exception as e:  # noqa: BLE001
            _LOG.error("Spotify token request error: %s", e)
            return False

    async def _request(self, method: str, endpoint: str, **kwargs) -> Optional[Any]:
        if not self._config.get_access_token():
            return None
        if self._config.is_token_expired() and not await self.refresh_access_token():
            return None
        try:
            session = await self._get_session()
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {self._config.get_access_token()}"
            async with session.request(method, f"{API_BASE}{endpoint}", headers=headers, **kwargs) as resp:
                if 200 <= resp.status < 300:
                    if resp.status == 204:
                        return {}
                    try:
                        return await resp.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        return {}
                _LOG.error("Spotify API %s %s -> %s", method, endpoint, resp.status)
                return None
        except Exception as e:  # noqa: BLE001
            _LOG.error("Spotify API error %s %s: %s", method, endpoint, e)
            return None

    # ----- API --------------------------------------------------------------

    async def get_playlists(self, limit: int = 12) -> List[Dict[str, str]]:
        """The user's playlists (name + uri), capped to ``limit``."""
        data = await self._request("GET", f"/me/playlists?limit={max(1, min(50, limit))}")
        if data is None:
            return []
        out = []
        for item in data.get("items", []):
            if item and item.get("uri"):
                out.append({"name": item.get("name", "Unknown"), "uri": item["uri"]})
        return out

    async def get_now_playing(self) -> Optional[Dict[str, str]]:
        """Current track's title/artist/album/art from Spotify (authoritative)."""
        d = await self._request("GET", "/me/player")
        item = (d or {}).get("item")
        if not item:
            return None
        images = (item.get("album") or {}).get("images") or []
        return {
            "title": item.get("name", ""),
            "artist": ", ".join(a["name"] for a in item.get("artists", [])),
            "album": (item.get("album") or {}).get("name", ""),
            "image_url": images[0]["url"] if images else "",
        }

    async def get_devices(self) -> List[Dict[str, Any]]:
        data = await self._request("GET", "/me/player/devices")
        devices = (data or {}).get("devices", [])
        if devices:
            self._config.cache_devices({d["name"]: d["id"] for d in devices if d.get("id")})
        return devices

    def cached_device_id(self, name: str) -> Optional[str]:
        """A previously seen Connect device id for this speaker (no network call)."""
        target = (name or "").lower()
        for dn, did in self._config.get_cached_devices().items():
            if dn and (dn.lower() in target or target in dn.lower()):
                return did
        return None

    async def resolve_device_id(self, name: str) -> Optional[str]:
        """Find a Spotify Connect device id matching a speaker name (live, then cached)."""
        target = (name or "").lower()
        for d in await self.get_devices():
            dn = (d.get("name") or "").lower()
            if dn and (dn in target or target in dn) and d.get("id"):
                return d["id"]
        return self.cached_device_id(name)

    async def start_playlist(self, context_uri: str, device_id: Optional[str]) -> bool:
        endpoint = "/me/player/play"
        if device_id:
            endpoint += f"?device_id={device_id}"
        return await self._request("PUT", endpoint, json={"context_uri": context_uri}) is not None

    # ----- transport (for Spotify Connect content; B&O skip is a no-op there) --

    async def next_track(self) -> bool:
        return await self._request("POST", "/me/player/next") is not None

    async def previous_track(self) -> bool:
        return await self._request("POST", "/me/player/previous") is not None

    async def pause(self) -> bool:
        return await self._request("PUT", "/me/player/pause") is not None

    async def resume(self) -> bool:
        return await self._request("PUT", "/me/player/play") is not None

    async def play_pause(self) -> bool:
        data = await self._request("GET", "/me/player")
        if data and data.get("is_playing"):
            return await self.pause()
        return await self.resume()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
