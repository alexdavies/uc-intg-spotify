"""
Spotify Web API client for Unfolded Circle integration.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import base64
import logging
import ssl
from typing import Any, Dict, List, Optional

import aiohttp
import certifi

_LOG = logging.getLogger(__name__)

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE_URL = "https://api.spotify.com/v1"


class SpotifyClient:
    """Spotify Web API client with OAuth2 authentication."""
    
    def __init__(self, config):
        """Initialize the Spotify client."""
        self._config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._token_refresh_lock = asyncio.Lock()
        self.redirect_uri = "https://example.com/callback"
        
        _LOG.info("Spotify client initialized")
        
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session with proper SSL context."""
        if self._session is None or self._session.closed:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={'User-Agent': 'UC-Spotify-Integration/0.1.0'}
            )
        return self._session
    
    def is_authenticated(self) -> bool:
        """Check if client is authenticated with valid tokens."""
        access_token = self._config.get_access_token()
        refresh_token = self._config.get_refresh_token()
        return access_token is not None and refresh_token is not None
    
    def get_authorization_url(self) -> str:
        """Generate Spotify authorization URL for OAuth2 flow."""
        import urllib.parse
        
        client_id = self._config.get_client_id()
        if not client_id:
            raise ValueError("Client ID not configured")
        
        scopes = [
            "user-read-currently-playing",
            "user-read-playback-state",
            "user-modify-playback-state",
            "user-read-private",
            "playlist-read-private",
            "playlist-read-collaborative"
        ]
        
        params = {
            "response_type": "code",
            "client_id": client_id,
            "scope": " ".join(scopes),
            "redirect_uri": self.redirect_uri,
            "show_dialog": "true"
        }
        
        return f"{SPOTIFY_AUTH_URL}?{urllib.parse.urlencode(params)}"
    
    async def exchange_code_for_token(self, authorization_code: str) -> bool:
        """Exchange authorization code for access and refresh tokens."""
        try:
            client_id = self._config.get_client_id()
            client_secret = self._config.get_client_secret()
            
            if not client_id or not client_secret:
                _LOG.error("Client ID or Client Secret not configured")
                return False
            
            session = await self._get_session()
            auth_header = base64.b64encode(
                f"{client_id}:{client_secret}".encode()
            ).decode()
            
            headers = {
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            data = {
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": self.redirect_uri
            }
            
            async with session.post(SPOTIFY_TOKEN_URL, headers=headers, data=data) as response:
                if response.status == 200:
                    token_data = await response.json()
                    self._config.set_tokens(
                        token_data["access_token"],
                        token_data["refresh_token"],
                        token_data.get("expires_in", 3600)
                    )
                    _LOG.info("Successfully obtained Spotify access tokens")
                    return True
                else:
                    error_data = await response.text()
                    _LOG.error("Token exchange failed: %s - %s", response.status, error_data)
                    return False
                    
        except Exception as e:
            _LOG.error("Error exchanging authorization code for token: %s", e)
            return False
    
    async def refresh_access_token(self) -> bool:
        """Refresh the access token using the refresh token."""
        async with self._token_refresh_lock:
            try:
                client_id = self._config.get_client_id()
                client_secret = self._config.get_client_secret()
                refresh_token = self._config.get_refresh_token()
                
                if not all([client_id, client_secret, refresh_token]):
                    _LOG.error("Missing credentials or refresh token")
                    return False
                
                session = await self._get_session()
                auth_header = base64.b64encode(
                    f"{client_id}:{client_secret}".encode()
                ).decode()
                
                headers = {
                    "Authorization": f"Basic {auth_header}",
                    "Content-Type": "application/x-www-form-urlencoded"
                }
                
                data = {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token
                }
                
                async with session.post(SPOTIFY_TOKEN_URL, headers=headers, data=data) as response:
                    if response.status == 200:
                        token_data = await response.json()
                        new_refresh_token = token_data.get("refresh_token", refresh_token)
                        self._config.set_tokens(
                            token_data["access_token"],
                            new_refresh_token,
                            token_data.get("expires_in", 3600)
                        )
                        _LOG.debug("Successfully refreshed Spotify access token")
                        return True
                    else:
                        error_data = await response.text()
                        _LOG.error("Token refresh failed: %s - %s", response.status, error_data)
                        return False
                        
            except Exception as e:
                _LOG.error("Error refreshing access token: %s", e)
                return False
    
    async def _make_authenticated_request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Make an authenticated request to the Spotify API."""
        if not self._config.get_access_token():
            _LOG.error("Not authenticated with Spotify")
            return None
        
        if self._config.is_token_expired():
            if not await self.refresh_access_token():
                _LOG.error("Failed to refresh access token")
                return None
        
        try:
            session = await self._get_session()
            access_token = self._config.get_access_token()
            
            headers = kwargs.get("headers", {})
            headers["Authorization"] = f"Bearer {access_token}"
            kwargs["headers"] = headers
            
            url = f"{SPOTIFY_API_BASE_URL}{endpoint}"
            
            async with session.request(method, url, **kwargs) as response:
                if 200 <= response.status < 300:
                    if response.status == 204:
                        return {}
                    
                    try:
                        return await response.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        return {}
                
                _LOG.error("API request failed: %s %s - Status: %s", method, url, response.status)
                return None
                
        except Exception as e:
            _LOG.error("Error making authenticated request: %s", e)
            return None
    
    async def get_currently_playing(self) -> Optional[Dict[str, Any]]:
        """Get the currently playing track and active device from Spotify."""
        data = await self._make_authenticated_request("GET", "/me/player")

        if not data or not data.get("item"):
            return None

        track = data["item"]
        device = data.get("device", {})
        context = data.get("context") or {}
        result = {
            "is_playing": data.get("is_playing", False),
            "title": track.get("name", "Unknown Title"),
            "artists": [artist["name"] for artist in track.get("artists", [])],
            "album": track.get("album", {}).get("name", "Unknown Album"),
            "duration_ms": track.get("duration_ms", 0),
            "progress_ms": data.get("progress_ms", 0),
            "image_url": None,
            "device_name": device.get("name"),
            "volume_percent": device.get("volume_percent"),
            "context_uri": context.get("uri"),
        }

        if track.get("album", {}).get("images"):
            result["image_url"] = track["album"]["images"][0]["url"]

        return result

    async def get_playback_state(self) -> Optional[Dict[str, Any]]:
        """Get current playback state including volume."""
        data = await self._make_authenticated_request("GET", "/me/player")
        if not data:
            return {}

        device = data.get("device", {})
        return {
            "is_playing": data.get("is_playing", False),
            "volume_percent": device.get("volume_percent", 50),
            "device_name": device.get("name", "Unknown"),
            "supports_volume": device.get("supports_volume", False),
        }

    async def get_playlists(self) -> List[Dict[str, Any]]:
        """Get the user's saved playlists (name + URI)."""
        data = await self._make_authenticated_request("GET", "/me/playlists?limit=50")
        if data is None:
            _LOG.warning(
                "Could not fetch playlists. If this integration was set up before playlist "
                "support was added, re-run the integration setup to grant the new permissions."
            )
            return []

        playlists = []
        for item in data.get("items", []):
            if item and item.get("uri"):
                playlists.append({
                    "name": item.get("name", "Unknown"),
                    "uri": item["uri"],
                    "id": item.get("id"),
                })
        return playlists

    async def get_devices(self) -> List[Dict[str, Any]]:
        """Get the available Spotify Connect devices."""
        data = await self._make_authenticated_request("GET", "/me/player/devices")
        if not data:
            return []

        devices = []
        for device in data.get("devices", []):
            devices.append({
                "id": device.get("id"),
                "name": device.get("name", "Unknown"),
                "type": device.get("type", ""),
                "is_active": device.get("is_active", False),
                "supports_volume": device.get("supports_volume", False),
                "volume_percent": device.get("volume_percent"),
            })

        # Remember device IDs so a speaker that drops out of the list (e.g. network
        # standby) can still be targeted by name later.
        self._config.cache_devices({d["name"]: d["id"] for d in devices if d["id"]})
        return devices

    async def resolve_target_device(self) -> Optional[str]:
        """
        Pick a device ID to target when starting playback.

        Returns None when a device is already active (no explicit target needed)
        or when no device could be found. Otherwise prefers the configured
        default device, falling back to the first available device.
        """
        devices = await self.get_devices()

        if any(d["is_active"] for d in devices):
            return None

        default_name = self._config.get_default_device()
        if default_name:
            for d in devices:
                if default_name.lower() in d["name"].lower():
                    return d["id"]
            # Device not currently visible - try its last known ID, which can
            # still reach devices in network standby.
            for name, device_id in self._config.get_cached_devices().items():
                if default_name.lower() in name.lower():
                    _LOG.info("Default device '%s' not in device list, trying cached ID", name)
                    return device_id

        return devices[0]["id"] if devices else None

    async def start_playback(self, context_uri: Optional[str] = None, device_id: Optional[str] = None) -> bool:
        """Start playback, optionally of a context (playlist/album) on a specific device."""
        endpoint = "/me/player/play"
        if device_id:
            endpoint += f"?device_id={device_id}"

        kwargs = {}
        if context_uri:
            kwargs["json"] = {"context_uri": context_uri}

        result = await self._make_authenticated_request("PUT", endpoint, **kwargs)
        return result is not None

    async def transfer_playback(self, device_id: str, play: bool = True) -> bool:
        """Transfer playback to another Spotify Connect device."""
        result = await self._make_authenticated_request(
            "PUT", "/me/player", json={"device_ids": [device_id], "play": play}
        )
        return result is not None

    async def play_pause(self) -> bool:
        """Toggle play/pause state."""
        current_data = await self._make_authenticated_request("GET", "/me/player")
        if not current_data:
            # No active device - try to start playback on the default/first available one
            return await self.play()

        is_playing = current_data.get("is_playing", False)
        endpoint = "/me/player/pause" if is_playing else "/me/player/play"
        result = await self._make_authenticated_request("PUT", endpoint)
        return result is not None

    async def play(self) -> bool:
        """Start playback, targeting a fallback device if none is active."""
        device_id = await self.resolve_target_device()
        return await self.start_playback(device_id=device_id)
    
    async def pause(self) -> bool:
        """Pause playback."""
        result = await self._make_authenticated_request("PUT", "/me/player/pause")
        return result is not None
    
    async def next_track(self) -> bool:
        """Skip to next track."""
        result = await self._make_authenticated_request("POST", "/me/player/next")
        return result is not None
    
    async def previous_track(self) -> bool:
        """Skip to previous track."""
        result = await self._make_authenticated_request("POST", "/me/player/previous")
        return result is not None

    async def set_volume(self, volume_percent: int) -> bool:
        """Set playback volume (0-100)."""
        # Check if device supports volume control
        state = await self.get_playback_state()
        if state and not state.get("supports_volume", False):
            _LOG.warning("Active device does not support volume control")
            return False

        volume_percent = max(0, min(100, volume_percent))
        result = await self._make_authenticated_request(
            "PUT",
            f"/me/player/volume?volume_percent={volume_percent}"
        )
        return result is not None

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()