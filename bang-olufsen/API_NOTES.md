# Bang & Olufsen — hardware & API reference notes

Everything verified against **real hardware** during testing on 2026-06-14, on the
LAN with the speakers. This is the ground-truth companion to the code in
`uc_intg_bang_olufsen/`; where the code and these notes disagree, trust the notes
(they were measured) and fix the code.

> IP addresses are DHCP and may change; identify devices by serial/JID/friendly
> name, not IP.

## The two test devices

| | Beosound Emerge | Beoplay A9 (4th gen) |
|---|---|---|
| Platform | **Mozart** (modern) | **Legacy** ("ASE" / BeoZone / BeoNetRemote) |
| IP (at test time) | 192.168.86.29 | 192.168.86.9 |
| Friendly name | Alex's Emerge | **Davies9** |
| Serial | Beosound-Emerge-36434229 | 36069564 |
| JID | 2738.1273701.36434229@products.bang-olufsen.com | 3071.1200530.36069564@products.bang-olufsen.com |
| Type / item | — | typeNumber 3071, item 1200530, "A9, 4. gen, Google Assistant" |
| mDNS service | `_bangolufsen._tcp` | `_beoremote._tcp` (+ `_beozone`, `_products`) |
| API | `mozart-api` 6.2.0.44.0 (REST + WebSocket) | Raw REST on `:8080` |
| Auth | None on LAN | None on LAN |
| Chromecast | Yes (`_googlecast._tcp`) | Yes (`_googlecast._tcp`) + Google Assistant |

Radio station ids are **shared "airable" ids across platforms** — e.g. BBC Radio 2
= `2972408424131572` is the same id on the Emerge's presets and the A9's
favourites. This is the key to cross-device radio.

---

## Mozart (Beosound Emerge)

- **Discovery:** mDNS `_bangolufsen._tcp.local.`; TXT carries serial/friendlyName.
- **Notifications:** `connect_notifications(reconnect=True)` after registering
  `get_playback_state/metadata/progress_notifications`, `get_volume_notifications`,
  `get_source_change_notifications`. WebSocket works reliably.
- **Sources:** `get_available_sources(target_remote=False)`. 17 selectable;
  `playable=True` for `spotify`, `lineIn`, `spdif` (Optical), `netRadio` (B&O Radio).
- **Volume:** `get_current_volume()` → `VolumeState`, values **nested**:
  `level.level` (int), `maximum.level` (int, =100), `muted.muted` (bool). Set via
  `set_current_volume_level(VolumeLevel(level=int))`. *Do not* treat `.level`/
  `.maximum` as ints directly — they are wrapper objects (this caused a bug).
- **Presets (radio favourites):** `get_presets()` returns a dict keyed `'1'..'4'`.
  **The dict key is the integer preset number** that `activate_preset(id=int)`
  needs. `preset.id` is an unrelated **UUID** — never use it to activate.
  `preset.title` is the station name; `contentUri` is `netRadio://<airable-id>`.
- **Active source:** no getter — derive from `playback.metadata.source`.
- **Play from idle:** `post_playback_command("play")` only *resumes*. After
  `stop` there is nothing to resume; Spotify Connect cannot be originated locally.
  Starting playback always requires selecting a source/station.
- **Skip on Spotify Connect:** no-op. Spotify owns transport; `next/skipToNext/
  skipForward/forward` all do nothing. play/pause *do* propagate.
- **Play an arbitrary stream URL:** `post_uri_source(Uri(location="<http url>"))`
  → switches to the `uriStreamer` source and plays it. **Verified** (triple j AAC
  stream played). `netRadio://<id>` does **not** work here (treated as a URL,
  fails to a silent uriStreamer).

---

## Legacy A9 (ASE / BeoZone) — REST on `:8080`

### Transport & basics (all working after fixes)
- **Content-Type quirk:** every POST/PUT must send `Content-Type: application/json`.
  A bodyless command 400s with `Content-Type must be "application/json"` — send an
  empty `{}` body so aiohttp sets the header.
- **Empty 200 bodies:** commands return an empty 200 that parses to `None`; treat
  any 2xx as success (don't read the body as a failure signal).
- **Transport:** `POST /BeoZone/Zone/Stream/{Play,Pause,Stop,Forward,Backward}`.
  `GET /BeoZone/Zone/Stream` → `{"features":["PAUSE","PLAY","SKIP","STOP"]}`.
  `/Stream/Skip` is **404** — use `Forward`/`Backward`.
- **Volume:** range **0–90**. `GET/PUT /BeoZone/Zone/Sound/Volume/Speaker/{Level,Muted}`.
- **Power:** `PUT /BeoDevice/powerManagement/standby {"standby":{"powerState":"on"|"standby"}}`.
- **Descriptor:** `GET /BeoDevice` → `beoDevice.productFriendlyName.productFriendlyName`
  ("Davies9"), `beoDevice.productId.serialNumber` (36069564).
- **Notifications:** chunked stream `GET /BeoNotify/Notifications`. Types seen:
  `SOURCE`, `PROGRESS_INFORMATION` (`state`: play/stop/**preparing**, `playQueueId`),
  `VOLUME` (`speaker.level/muted`), `NOW_PLAYING_NET_RADIO` (`name`, `stationId`,
  `playQueueItemId`), `NOW_PLAYING_ENDED`, `SOFTWARE_UPDATE_STATE`, etc.

### Power / standby quirks (verified 2026-09-06)
- **Volume writes are silently ignored in standby.** `PUT .../Speaker/Level`
  answers **200** but the level does not change while `powerState=standby`;
  after `powerState=on` the same PUT applies. Accepted writes are applied
  **asynchronously** — an immediate GET can still return the old level for
  ~100–300 ms. The driver therefore polls the level back briefly and only
  reports failure when it never changes *and* the speaker is in standby.
- **No power-on notification.** Entering standby emits
  `SHUTDOWN {"reason":"standby"}` on `/BeoNotify`. Waking emits only
  `SOURCE {}` / `NOW_PLAYING_ENDED` / `PROGRESS_INFORMATION {"state":"stop"}`;
  a *real* `SOURCE` (with an id) or a `play`/`preparing` progress state is what
  tells you it's on. Read `/BeoDevice/powerManagement/standby` for the initial
  state.
- **Casting sequence** on `/BeoNotify`: `SOURCE` (googlecast) → `PROGRESS
  preparing` → `stop` → `NOW_PLAYING_STORED_MUSIC {name, trackImage[], ...}`
  (the A9 echoes the cast title + artwork) → `play` → `preparing` … Treat
  `preparing` as playing/buffering or the card flaps to PAUSED.

### Sources
- `GET /BeoZone/Zone/Sources` — 13 sources. Switch with
  `POST /BeoZone/Zone/ActiveSources {"primaryExperience":{"source":{"id":<id>}}}`.
- **Two radio sources, mutually exclusive:**
  - `beoradio:<jid>` — **B&O Radio** (BEO RADIO), `inUse=true` (the one in use).
  - `radio:<jid>` — **TuneIn**, `inUse=false` (disabled).
  - ⚠️ **Enabling one disables the other.** Enabling TuneIn silently disabled
    B&O Radio (`"beoradio is disabled"`) and broke radio until reverted.
- **Enable/disable a source:** `PUT /BeoZone/Zone/Sources/<url-encoded-id>` with
  the **unwrapped** source object (set `inUse`). A wrapped `{"source":{...}}` body
  returns 200 but is silently ignored. A source can't be disabled while it's the
  active source — switch away first.
- Activating `beoradio` **resumes the last-played station** (not a chosen one).

### Radio favourites (read-only)
- `GET /BeoContent/radio/netRadioProfile/favoriteList/` → two lists:
  `favorite` (15 stations) and `presetDefault` (4).
- `.../favoriteList/favorite/favoriteListStation` → each entry has `number`
  (fixed order), `station.id` (airable id), `name`, `image[]`, `beoradio.stationId`.
- The A9's 15 `favorite` stations (number : name : id):

  | # | Station | airable id |
  |---|---|---|
  | 0 | Energy Zürich | 3787747871130705 |
  | 1 | **triple j (New South Wales)** | 2554623176809400 |
  | 2 | BBC Radio 2 | 2972408424131572 |
  | 3 | BBC Radio 4 | 1022963300812989 |
  | 4 | BBC Radio 6 Music | 8785094880964608 |
  | 5 | Classic FM | 4888751676771790 |
  | 6–10 | (Russian stations) | … |
  | 11 | Radio 24 | … |
  | 12 | 92.7 MIX FM | … |
  | 13 | Triple M Brisbane 104.5 | … |
  | 14 | Nova 106.9 | … |

### ❌ What does NOT work for selecting a station (all tried, all failed)
- `POST /BeoZone/Zone/PlayQueue?instantplay` → **403 "PQ with id radio doesn't
  exist"** while B&O Radio is active (B&O Radio has no local PlayQueue; the main
  PlayQueue is `"music"` and stays empty during radio).
- `contentUri` in `ActiveSources` (4 shapes incl. `netRadio://`, `radio://`):
  **200 but ignored** (no station change).
- `PUT`/`POST` the favourite station resource, `POST /BeoZone/Zone/Stream` with a
  station: **405 Method Not Allowed**.
- `playQueueItem` must be an **array** (`[{...}]`) when a PQ does exist — but for
  radio the PQ doesn't exist anyway.

### ⚠️ The TuneIn "instant" trap (false positive — do not pursue)
- Enabling TuneIn (`inUse=true`, unwrapped PUT) makes the `"radio"` PlayQueue
  exist, so `instantplay` returns **200** and `NOW_PLAYING` shows the station name.
- **But there is no audio** — playback sticks in `"preparing"`. The `NOW_PLAYING`
  name is just the device echoing back the name you submitted, not real playback.
- Reasons it's a dead end: (1) TuneIn uses its own station ids, and the airable /
  B&O Radio ids do **not** resolve on it; (2) there is **no API to search TuneIn**
  for valid ids; (3) enabling TuneIn **disables B&O Radio**.
- After testing, the A9 was restored: B&O Radio enabled, TuneIn disabled, playing.

### ✅ The only API way to change station on the A9
**`Forward`/`Backward`**, which cycle the 15 favourites in `number` order
(wrapping). ~1–2s per step. There is no instant/direct tune on this firmware.

---

## B&O Radio = airable (not TuneIn), and how the app tunes stations

**The "B&O Radio" stations are [airable](https://www.airablenow.com/) stations,
not TuneIn.** Confirmed:
- All artwork is served from `static.airable.io`.
- B&O Radio **replaced** TuneIn (B&O support); airable is B&O's radio catalog
  backend (B&O is an airable customer, via Frontier Silicon). airable and TuneIn
  are competing aggregators with **separate catalogs and id namespaces** — which
  is exactly why feeding airable/B&O-Radio ids to the (old, now-disabled) TuneIn
  source hangs in "preparing".
- The airable station id is **identical across platforms**: on Mozart it's
  `playback.metadata.sourceInternalId` (e.g. BBC Radio 2 = `2972408424131572`);
  on the A9 it's the favourite's `station.id` — same number.

**How the app plays a station:** it browses airable's catalog (via B&O's airable
partner endpoint) and the device resolves the airable id to a live stream through
**airable's cloud**, then streams it natively. This is *not* Chromecast and *not*
a local "tune" endpoint — it's a cloud-backed catalog. That's why there's no
local station-select API and why the community wrappers
([`ha-beoplay`](https://github.com/martonborzak/ha-beoplay) /
[`pybeoplay`](https://pypi.org/project/pybeoplay/), `giachello/beoplay`) only do
source-switch + transport.

**Can we just push an airable id to the A9 to play natively?** No. B&O Radio
(`beoradio`) has **no local play queue and no tune endpoint** — `instantplay`
consistently 403s with *"PQ with id radio doesn't exist"* (that queue only exists
for the separate, disabled TuneIn source), the source exposes only
`modify`/`move`/`reset` links, there's no `playNow`, `contentUri` is ignored, and
the favourite resources are read-only. The airable id is meaningful only to the
device's **cloud-backed** native playback (the app/airable cloud populates the
single `beoradio` slot); there is no local "here's an id, play it" entry point.

**Can we get the actual stream URL?** The device never exposes it — only the
airable id + artwork. airable's catalog *does* contain the stream links ("multiple
links per station"), but the **airable API is partner-gated** (needs B&O's
namespace/credentials), so we can't cleanly resolve an airable id → URL.
(`rhaamo/pyrable` is a community airable-compatible server, i.e. the API shape is
partly known, but relying on it is fragile/grey-area.) Practical path: hand-curate
a real stream URL per station — most big stations publish their own public streams
anyway (BBC, ABC/triple j, etc.).

## The viable instant path: Chromecast — ✅ PROVEN (real audio, 2026-06-14)
Both speakers advertise `_googlecast._tcp` ("Alex's Emerge", "Davies9"). Casting a
**stream URL** via `pychromecast` (v14) gives instant, arbitrary-station playback,
bypassing the B&O radio API entirely. **Verified with real audio on both
speakers** through the integration's own path (`BeoPlayer._select_source` →
`BeoCast`): triple j confirmed *audibly* playing on the A9 **and** the Emerge,
BBC Radio 6 Music audible on the A9 (not just a PLAYING status).

How it works:
- `pychromecast.get_listed_chromecasts(friendly_names=["Davies9"], discovery_timeout=12)`
  (discovery is flaky — retry 2-3×; or connect by host). `cast.wait()`.
- `cast.set_volume(0.4)`, then `cast.media_controller.play_media(url, content_type,
  title=..., stream_type="LIVE")`. The default media receiver app (`CC1AD845`)
  launches automatically.
- The A9 then switches its **active source to "Chromecast built-in" (GC4A)** and
  outputs audio — verified via the B&O API (power on, source=Chromecast, device
  volume 36, unmuted). *This source switch is the real proof, vs. the TuneIn
  false-positive which never switched and stuck in "preparing".*

Gotchas / lessons:
- **One receiver = one stream.** `play_media` replaces the current media; stations
  are not simultaneous. (An early test cast 3 in a row, ~5s each — looked like
  nothing played; it was just too brief + sequential.)
- **PLAYING status ≠ audible.** Always confirm with a human, or at least check the
  B&O API shows source=Chromecast + unmuted + sane volume.
- Polling can race a socket reconnect (`NotConnected: ... is connecting`); wrap
  `update_status()` and retry, or rely on pushed status.
- Building this means adding the `pychromecast` dependency (not yet in
  `requirements.txt`) and a cast control path in the integration.
- Prior art: [`giachello/netradio`](https://github.com/giachello/netradio).

## Collected stream/station data (for a cast-based custom radio list)
`airable id` is for the speakers' own native radio (read-only / not locally
pushable — see above). `stream URL` is what we actually use for **casting** (and
Mozart `post_uri_source`).

| Station | airable id | cast stream URL | content_type | verified |
|---|---|---|---|---|
| triple j (NSW) | 2554623176809400 | `https://mediaserviceslive.akamaized.net/hls/live/2038308/triplejnsw/masterhq.m3u8` | application/vnd.apple.mpegurl | ✅ cast PLAYING (2026-09-06); the old `live-radio01.mediahubaustralia.com/2TJW/aac/` now returns **403** and the cast fails with a receiver error |
| BBC Radio 6 Music | 8785094880964608 | `https://lsn.lv/bbcradio.m3u8?station=bbc_6music&bitrate=320000` | application/x-mpegurl | ✅ audible |
| Energy Zürich | 3787747871130705 | `https://energyzuerich.ice.infomaniak.ch/energyzuerich-high.mp3` | audio/mpeg | ✅ PLAYING |
| BBC Radio 2 | 2972408424131572 | `https://lsn.lv/bbcradio.m3u8?station=bbc_radio_two&bitrate=320000` | application/x-mpegurl | (lsn.lv, untested) |
| BBC Radio 4 | 1022963300812989 | `https://lsn.lv/bbcradio.m3u8?station=bbc_radio_fourfm&bitrate=320000` | application/x-mpegurl | (lsn.lv, untested) |
| Classic FM | 4888751676771790 | (tbd) | | |

BBC notes: the old `as-hls-ww-live.akamaized.net/pool_904/...` URLs are **dead
(HTTP 410)** — BBC rotates the pool number. Use the **`lsn.lv`** resolver
(`https://lsn.lv/bbcradio.m3u8?station=<id>&bitrate=320000`), which returns a
master playlist pointing at the *current* akamai pool. Test location is **London
(UK)**, so BBC is local and unrestricted; for a non-UK location add `&uk=0` /
use the `ww` worldwide variant. `lsn.lv` also accepts `&uk=1` for the UK-only
high-quality variant. Source for current BBC URLs:
<https://garfnet.org.uk/cms/bbc-national-and-local-radio-hls-streams/>.

## Unfolded Circle Remote — driving it from a laptop (verified 2026-09-06)
The Remote's REST API (basic auth `web-configurator:<PIN>`, OpenAPI at
`http://<remote>/doc/core-rest/openapi.yaml`) is enough for a full debug loop
without touching the Remote's screen:
- **On-device vs external driver.** A custom integration *installed on the
  Remote* (`driver_type: CUSTOM`) runs its own copy of the code on the Remote;
  a driver running on a Mac is a separate **EXTERNAL** driver. Both use the same
  `driver_id`, so the Remote will not connect to the Mac while the on-device
  copy exists. To iterate locally: `DELETE /api/intg/drivers/bang_olufsen_local`
  (removes the on-device copy, its instance, entities and activity references),
  then register the external one with `POST /api/intg/drivers` sending the full
  `driver.json` plus `"driver_url": "ws://<mac-hostname>.local:9090"` — the
  hostname form survives DHCP changes (a long-running driver bound to a stale
  IP is exactly how the June setup silently stopped working).
- **Setup without the UI:** `POST /api/intg/setup {"driver_id": ..., "setup_data":
  {every field of setup_data_schema, "" is fine}}`, poll `GET /api/intg/setup/<id>`
  until `OK`, then `POST /api/intg/instances/<id>.main/entities ["<entity id>", ...]`.
- **Reconnect:** `PUT /api/intg/instances/<id>.main?cmd=DISCONNECT|CONNECT`.
- **Commands:** `PUT /api/entities/<entity id>/command {"cmd_id":
  "media_player.volume_up"}` (params e.g. `{"source": "Radio: triple j"}`).
- **Activities:** `PATCH /api/activities/<id>` (`options.entity_ids`,
  `options.touch_slider {enabled, target:{entity_id, feature:"volume"}}`),
  `POST .../buttons` (replace physical button mapping; each `short_press` needs
  `entity_id` + `cmd_id`), `PATCH .../ui/pages/<page>` / `POST .../ui/pages`
  (items: `media_player` widget via `media_player_id`, `text`/`icon` with an
  `EntityCommand`). An activity with **no** button mapping is why the physical
  volume keys did nothing.

## Spotify playlists (optional, via Spotify Connect — ✅ live-tested 2026-09-06 on the A9)
Spotify playback can't be originated on the speaker locally, so the optional
"Spotify" page drives it through the **Spotify Web API**: `PUT /me/player/play?
device_id=<speaker>` with the playlist `context_uri`, targeting the speaker as a
Spotify Connect device. Auth is the speaker integration's *own* OAuth (vendored
`spotify.py`, reusing the same app + `https://example.com/callback` redirect);
tokens live in the B&O config. Setup asks for Client ID/Secret, then an auth code.
Playback flow (`BeoPlayer.play_spotify_playlist`): wake the speaker's Spotify
source (so it registers as a Connect device) → `resolve_device_id(speaker_name)`
→ start the playlist.
- ⚠️ **Device-name matching is the risk to verify.** We match the speaker's
  friendly name against Spotify's `/me/player/devices` names (contains, either
  direction) + a cached map. If B&O exposes a *different* Spotify Connect name
  than the speaker's friendly name, resolution fails ("no matching device") —
  check the logged device list and, if needed, add a name override.
- Connect devices may only appear when awake; we select the Spotify source first
  to nudge it. Spotify playback also needs Premium.

## Related fixes already committed (see git log)
- Mozart: preset id (key not UUID), volume parsing (nested `.level`), `get_state`
  active-source resolution.
- Legacy: Content-Type on bodyless commands, empty-200 → success.
- Discovery: also browse `_beoremote._tcp` so legacy speakers appear named.

## References
- Mozart open API — https://bang-olufsen.github.io/mozart-open-api/
- B&O "Radio replaces TuneIn" — https://support.bang-olufsen.com/hc/en-us/articles/15815194248977-Bang-Olufsen-Radio-replaces-TuneIn
- BeoNetRemote Client API (Postman) — https://documenter.getpostman.com/view/1053298/T1LTe4Lt
- ha-beoplay / pybeoplay — https://github.com/martonborzak/ha-beoplay
- giachello/netradio (Cast radio) — https://github.com/giachello/netradio
</content>
