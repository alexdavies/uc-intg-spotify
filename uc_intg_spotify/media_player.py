"""
Spotify media player entity for Unfolded Circle integration.

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""
import asyncio
import logging
from typing import Any, Dict, Optional

import ucapi
from ucapi.media_player import Attributes, Commands, Features, States

from uc_intg_spotify.client import SpotifyClient
from uc_intg_spotify.config import SpotifyConfig

_LOG = logging.getLogger(__name__)


class SpotifyMediaPlayer:
    """Spotify media player entity."""
    
    def __init__(self, api: ucapi.IntegrationAPI, client: SpotifyClient,
                 playlists: Optional[list] = None, devices: Optional[list] = None):
        """Initialize Spotify media player."""
        self._api = api
        self._client = client
        self._config: SpotifyConfig = client._config if client else None
        self._polling_task: Optional[asyncio.Task] = None
        # Playlists are exposed as sources, Spotify Connect devices as sound modes
        self._playlist_uris: Dict[str, str] = {p["name"]: p["uri"] for p in (playlists or [])}
        self._device_ids: Dict[str, str] = {d["name"]: d["id"] for d in (devices or []) if d.get("id")}

        features = [
            Features.ON_OFF,
            Features.MEDIA_DURATION,
            Features.MEDIA_POSITION,
            Features.MEDIA_TITLE,
            Features.MEDIA_ARTIST,
            Features.MEDIA_ALBUM,
            Features.MEDIA_IMAGE_URL,
            Features.MEDIA_TYPE,
            Features.PLAY_PAUSE,
            Features.NEXT,
            Features.PREVIOUS,
            Features.VOLUME,
            Features.VOLUME_UP_DOWN,
            Features.SELECT_SOURCE,
            Features.SELECT_SOUND_MODE,
        ]

        active_device = next((d["name"] for d in (devices or []) if d.get("is_active")), "")
        attributes = {
            Attributes.STATE: States.OFF,
            Attributes.MEDIA_TITLE: "",
            Attributes.MEDIA_ARTIST: "",
            Attributes.MEDIA_ALBUM: "",
            Attributes.MEDIA_DURATION: 0,
            Attributes.MEDIA_POSITION: 0,
            Attributes.MEDIA_IMAGE_URL: "",
            Attributes.VOLUME: 50,
            Attributes.MUTED: False,
            Attributes.SOURCE: "",
            Attributes.SOURCE_LIST: list(self._playlist_uris.keys()),
            Attributes.SOUND_MODE: active_device,
            Attributes.SOUND_MODE_LIST: list(self._device_ids.keys()),
        }
        
        self.entity = ucapi.MediaPlayer(
            identifier="spotify_media_player_main",
            name={"en": "Spotify Player"},
            features=features,
            attributes=attributes,
            cmd_handler=self.cmd_handler
        )
        
        _LOG.info("Spotify media player entity created with %d features", len(features))

    async def start_polling(self):
        """Start the background polling task."""
        if not self._polling_task or self._polling_task.done():
            polling_interval = self._config.get_polling_interval()
            self._polling_task = asyncio.create_task(self._poll_playback_state(polling_interval))
            _LOG.info(f"Started polling Spotify every {polling_interval} seconds.")

    async def stop_polling(self):
        """Stop the background polling task."""
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            _LOG.info("Stopped polling Spotify.")
        self._polling_task = None
        
    async def _poll_playback_state(self, interval_seconds: int):
        """Periodically poll for playback state."""
        cycle = 0
        while True:
            try:
                if self._client.is_authenticated():
                    await self._refresh_devices()
                    # Playlists change rarely - refresh every 10th cycle
                    if cycle % 10 == 0:
                        await self._refresh_playlists()

                    track_data = await self._client.get_currently_playing()
                    if track_data:
                        await self.update_current_track(track_data)
                    else:
                        await self.clear_current_track()
                else:
                    _LOG.debug("Polling skipped: client not authenticated.")
            except Exception as e:
                _LOG.error(f"Error during polling: {e}", exc_info=True)

            cycle += 1
            await asyncio.sleep(interval_seconds)

    async def _refresh_devices(self) -> None:
        """Refresh the list of available Spotify Connect devices (sound modes)."""
        devices = await self._client.get_devices()
        if not devices and not self._device_ids:
            return

        device_ids = {d["name"]: d["id"] for d in devices if d.get("id")}
        # Keep previously seen devices selectable even when they temporarily
        # drop out of the list (e.g. speakers in network standby).
        for name, device_id in self._config.get_cached_devices().items():
            device_ids.setdefault(name, device_id)
        self._device_ids = device_ids

        new_list = list(self._device_ids.keys())
        if self.entity.attributes.get(Attributes.SOUND_MODE_LIST) != new_list:
            self._api.configured_entities.update_attributes(
                self.entity.id, {Attributes.SOUND_MODE_LIST: new_list}
            )
            _LOG.debug("Updated device list: %s", new_list)

    async def _refresh_playlists(self) -> None:
        """Refresh the list of saved playlists (sources)."""
        playlists = await self._client.get_playlists()
        if not playlists:
            return

        self._playlist_uris = {p["name"]: p["uri"] for p in playlists}
        new_list = list(self._playlist_uris.keys())
        if self.entity.attributes.get(Attributes.SOURCE_LIST) != new_list:
            self._api.configured_entities.update_attributes(
                self.entity.id, {Attributes.SOURCE_LIST: new_list}
            )
            _LOG.debug("Updated playlist list: %d playlists", len(new_list))

    async def cmd_handler(self, entity: ucapi.Entity, cmd_id: str, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        """Handle media player commands."""
        _LOG.info("Media player command: %s %s", cmd_id, params)
        
        if not self._client or not self._client.is_authenticated():
            _LOG.warning("Spotify client not authenticated")
            return ucapi.StatusCodes.SERVICE_UNAVAILABLE
        
        try:
            if cmd_id == Commands.ON:
                return await self._handle_on()
            elif cmd_id == Commands.OFF:
                return await self._handle_off()
            elif cmd_id == Commands.PLAY_PAUSE:
                return await self._handle_play_pause()
            elif cmd_id == Commands.NEXT:
                return await self._handle_next()
            elif cmd_id == Commands.PREVIOUS:
                return await self._handle_previous()
            elif cmd_id == Commands.VOLUME:
                return await self._handle_volume(params)
            elif cmd_id == Commands.VOLUME_UP:
                return await self._handle_volume_up()
            elif cmd_id == Commands.VOLUME_DOWN:
                return await self._handle_volume_down()
            elif cmd_id == Commands.SELECT_SOURCE:
                return await self._handle_select_source(params)
            elif cmd_id == Commands.SELECT_SOUND_MODE:
                return await self._handle_select_sound_mode(params)
            else:
                _LOG.info("Unhandled command %s - ignoring", cmd_id)
                return ucapi.StatusCodes.OK
                
        except Exception as e:
            _LOG.error("Error handling command %s: %s", cmd_id, e)
            return ucapi.StatusCodes.SERVER_ERROR
    
    async def _handle_play_pause(self) -> ucapi.StatusCodes:
        """Handle play/pause command."""
        success = await self._client.play_pause()
        return ucapi.StatusCodes.OK if success else ucapi.StatusCodes.SERVER_ERROR
    
    async def _handle_next(self) -> ucapi.StatusCodes:
        """Handle next track command."""
        success = await self._client.next_track()
        return ucapi.StatusCodes.OK if success else ucapi.StatusCodes.SERVER_ERROR
    
    async def _handle_previous(self) -> ucapi.StatusCodes:
        """Handle previous track command."""
        success = await self._client.previous_track()
        return ucapi.StatusCodes.OK if success else ucapi.StatusCodes.SERVER_ERROR
    
    async def _handle_select_source(self, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        """Handle source selection: start playing the chosen playlist."""
        if not params or "source" not in params:
            return ucapi.StatusCodes.BAD_REQUEST

        playlist_name = params["source"]
        context_uri = self._playlist_uris.get(playlist_name)
        if not context_uri:
            _LOG.warning("Unknown playlist selected: %s", playlist_name)
            return ucapi.StatusCodes.BAD_REQUEST

        device_id = await self._client.resolve_target_device()
        success = await self._client.start_playback(context_uri=context_uri, device_id=device_id)
        if success:
            self._api.configured_entities.update_attributes(
                self.entity.id,
                {Attributes.SOURCE: playlist_name, Attributes.STATE: States.PLAYING}
            )
        return ucapi.StatusCodes.OK if success else ucapi.StatusCodes.SERVER_ERROR

    async def _handle_select_sound_mode(self, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        """Handle sound mode selection: transfer playback to the chosen device."""
        device_name = (params or {}).get("mode") or (params or {}).get("sound_mode")
        if not device_name:
            return ucapi.StatusCodes.BAD_REQUEST

        device_id = self._device_ids.get(device_name)
        if not device_id:
            _LOG.warning("Unknown device selected: %s", device_name)
            return ucapi.StatusCodes.BAD_REQUEST

        success = await self._client.transfer_playback(device_id)
        if success:
            self._api.configured_entities.update_attributes(
                self.entity.id, {Attributes.SOUND_MODE: device_name}
            )
        return ucapi.StatusCodes.OK if success else ucapi.StatusCodes.SERVER_ERROR

    async def _handle_volume(self, params: dict[str, Any] | None) -> ucapi.StatusCodes:
        """Handle volume set command."""
        if not params or "volume" not in params:
            return ucapi.StatusCodes.BAD_REQUEST
        
        volume = params["volume"]
        success = await self._client.set_volume(volume)
        
        if success:
            self._api.configured_entities.update_attributes(self.entity.id, {Attributes.VOLUME: volume})
        
        return ucapi.StatusCodes.OK if success else ucapi.StatusCodes.SERVER_ERROR
    
    async def _handle_volume_up(self) -> ucapi.StatusCodes:
        """Handle volume up command."""
        current_volume = self.entity.attributes.get(Attributes.VOLUME, 50)
        new_volume = min(100, current_volume + 5)
        return await self._handle_volume({"volume": new_volume})
    
    async def _handle_volume_down(self) -> ucapi.StatusCodes:
        """Handle volume down command."""
        current_volume = self.entity.attributes.get(Attributes.VOLUME, 50)
        new_volume = max(0, current_volume - 5)
        return await self._handle_volume({"volume": new_volume})
    
    async def _handle_on(self) -> ucapi.StatusCodes:
        """Handle turn on command."""
        success = await self._client.play()
        if success:
            self._api.configured_entities.update_attributes(self.entity.id, {Attributes.STATE: States.PLAYING})
        return ucapi.StatusCodes.OK if success else ucapi.StatusCodes.SERVER_ERROR
    
    async def _handle_off(self) -> ucapi.StatusCodes:
        """Handle turn off command."""
        success = await self._client.pause()
        if success:
            self._api.configured_entities.update_attributes(self.entity.id, {Attributes.STATE: States.PAUSED})
        return ucapi.StatusCodes.OK if success else ucapi.StatusCodes.SERVER_ERROR
    
    async def update_current_track(self, track_data: Dict[str, Any]) -> None:
        """Update media player with current track information."""
        try:
            attributes = {}
            
            if track_data.get("is_playing", False):
                attributes[Attributes.STATE] = States.PLAYING
            else:
                attributes[Attributes.STATE] = States.PAUSED
            
            attributes[Attributes.MEDIA_TITLE] = track_data.get("title", "")
            attributes[Attributes.MEDIA_ARTIST] = ", ".join(track_data.get("artists", []))
            attributes[Attributes.MEDIA_ALBUM] = track_data.get("album", "")
            attributes[Attributes.MEDIA_DURATION] = track_data.get("duration_ms", 0) // 1000
            attributes[Attributes.MEDIA_POSITION] = track_data.get("progress_ms", 0) // 1000
            attributes[Attributes.MEDIA_IMAGE_URL] = track_data.get("image_url", "")

            volume_percent = track_data.get("volume_percent")
            if volume_percent is not None:
                attributes[Attributes.VOLUME] = volume_percent
                attributes[Attributes.MUTED] = volume_percent == 0

            device_name = track_data.get("device_name")
            if device_name:
                attributes[Attributes.SOUND_MODE] = device_name

            context_uri = track_data.get("context_uri")
            if context_uri:
                source = next(
                    (name for name, uri in self._playlist_uris.items() if uri == context_uri), ""
                )
                attributes[Attributes.SOURCE] = source

            # Only send update if attributes have changed
            changed_attrs = {k: v for k, v in attributes.items() if self.entity.attributes.get(k) != v}

            if changed_attrs:
                self._api.configured_entities.update_attributes(self.entity.id, changed_attrs)
                _LOG.debug(f"Updated track info: {changed_attrs}")
            
        except Exception as e:
            _LOG.error("Error updating current track: %s", e)
    
    async def clear_current_track(self) -> None:
        """Clear current track information when nothing is playing."""
        try:
            attributes = {
                Attributes.STATE: States.OFF,
                Attributes.MEDIA_TITLE: "",
                Attributes.MEDIA_ARTIST: "",
                Attributes.MEDIA_ALBUM: "",
                Attributes.MEDIA_DURATION: 0,
                Attributes.MEDIA_POSITION: 0,
                Attributes.MEDIA_IMAGE_URL: ""
            }
            
            if self.entity.attributes.get(Attributes.STATE) != States.OFF:
                self._api.configured_entities.update_attributes(self.entity.id, attributes)
                _LOG.debug("Cleared current track information")
            
        except Exception as e:
            _LOG.error("Error clearing current track: %s", e)