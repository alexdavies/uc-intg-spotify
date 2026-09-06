# Bang & Olufsen (Mozart) Integration for Unfolded Circle Remote 2/3

Local control of Bang & Olufsen **Mozart-platform** speakers — Beosound Emerge,
Balance, Level, A5, A9 5th gen, Beolab 8/28, and others — from an Unfolded
Circle remote.

This is the companion to `uc-intg-spotify`. It exists because the Spotify Web
API and the UC built-in B&O integration **cannot** reach these speakers well:

- The Spotify Web API can't see or wake an idle Spotify Connect speaker, and has
  no radio capability at all.
- The UC built-in B&O integration speaks the **legacy** Beo protocol, so it finds
  older speakers (e.g. an early Beoplay A9) but **not** Mozart speakers like the
  Beosound Emerge.

This integration talks the **Mozart local API** (`mozart-api`) directly over the
LAN, so it discovers the Emerge, controls it in real time, and can trigger radio
favourites and multiroom — none of which Spotify can do.

> **Status: Phase 1 + 2.** Covers both Mozart speakers (e.g. Beosound Emerge)
> **and** older "ASE"/BeoNetRemote speakers (e.g. Beoplay A9 4th gen) via two
> backends behind one entity layer. The integration auto-selects the backend per
> speaker. This package lives inside the `uc-intg-spotify` repo for now and is
> designed to be lifted into its own repo (`uc-intg-bang-olufsen`) later.
>
> **Radio favourites caveat:** Mozart speakers expose presets over the API, so
> radio favourites work as buttons/sources on those. The Beoplay A9 4th gen does
> **not** expose a Favorites endpoint, so on it radio is reached by selecting the
> "B&O Radio" source (which resumes the last station). Per-station favourites on
> legacy speakers are a planned follow-up (capture & replay via the play queue).

## Features

- **One media-player entity per speaker — and that's the whole UI.** Each
  speaker (e.g. *Davies9*, *Alex's Emerge*) is a single media-player entity:
  the now-playing card (title / artist / artwork / progress), transport, volume
  and mute, plus a **source list** made of the custom radio stations and your
  curated Spotify playlists. Drop it on a page or into an activity — there is no
  separate "controls" remote entity any more.
- **Physical buttons.** In an activity map VOLUME_UP/DOWN, MUTE, PLAY, PREV/NEXT,
  STOP and POWER to the media player (`media_player.volume_up`, `.toggle`, ...);
  on the Remote 3 the touch slider can target the player's `volume` feature.
- **Radio** stations are cast to the speaker's Chromecast (the only way to tune an
  arbitrary station on the legacy A9 — see `API_NOTES.md`). Selecting
  `Radio: <station>` from the source list plays it and fills the card.
- **Radio now-playing text + artwork.** A cast carries no track info back from
  the speaker, so each station can name a metadata provider (`nowplaying` in
  the station config): `abc` (ABC Radio's plays API — triple j etc.), `icy`
  (Icecast in-band metadata, e.g. Energy Zürich, incl. cover art) or `bbc`
  (BBC Sounds' segments API — composer / work, or the programme between
  tracks). The card shows track / artist, the station name as "album", and the
  track or cover art; polled every 20 s while the station is playing.
- **Station logos** ship inside the driver (`uc_intg_bang_olufsen/logos/`,
  512 px, referenced as `logo:<file>`) and are sent as base64 data URLs, so
  nothing depends on an external image host.
- **Spotify playlists** (optional) start on the speaker via Spotify Connect; while
  Spotify is the source, next/previous/play-pause are routed through the Spotify
  Web API (the speakers' own skip commands are no-ops on Connect).
- **Network discovery** of Mozart (`_bangolufsen._tcp`) and legacy
  (`_beoremote._tcp`) speakers via mDNS, plus manual IP entry.
- **Real-time state** pushed from each speaker's notification stream.
- **Honest volume handling:** the legacy A9 silently ignores volume writes while
  in standby (it still answers HTTP 200); the driver reads the level back and
  reports the command as failed instead of pretending. Volume up/down step is
  `volume_step` percent in `config.json` (default 2).

## Setup

1. Install the integration on the remote (or run via Docker).
2. Start configuration — it scans the network for Mozart speakers.
3. Select the speaker(s) to add, or type an IP for any that didn't appear.
4. Entities are created per speaker.

## Testing against real hardware

This package was built against the documented `mozart-api` and verified with
mock tests, but not yet against a physical speaker. **[TESTING.md](TESTING.md)**
is a staged runbook to verify it on your network, layer by layer, using the
built-in diagnostics CLI:

```bash
python -m uc_intg_bang_olufsen.diagnostics selftest         # Stage 0 (no hardware)
python -m uc_intg_bang_olufsen.diagnostics discover         # Stage 1: find the Emerge
python -m uc_intg_bang_olufsen.diagnostics info <ip>        # Stage 2: read-only probe
python -m uc_intg_bang_olufsen.diagnostics listen <ip>      # Stage 3: real-time push
python -m uc_intg_bang_olufsen.diagnostics play <ip>        # Stage 4: commands
```

## Development

```bash
cd bang-olufsen
pip install -r requirements.txt
python -m pytest tests/ -v
python -u uc_intg_bang_olufsen/driver.py   # run locally on the speaker's LAN
```

The Mozart client wrapper (`client.py`) returns plain dicts/booleans so the
entity layer carries no `mozart-api` types, which keeps it unit-testable without
hardware (see `tests/`).

## Roadmap

- **Phase 3 — Spotify bridge:** use the local API to wake / select the Spotify
  source on a speaker so the `uc-intg-spotify` plugin can reliably start a
  chosen playlist on an otherwise-idle Emerge.
- **Legacy radio favourites:** the A9 4th gen exposes no Favorites API; custom
  stations are cast instead (done). Native-favourite capture/replay is parked.
- **Multiroom across backends:** "play on both" is reliable between Mozart
  speakers; Mozart↔legacy expansion is limited by the older protocol.

## Open items to verify against real devices

- Whether the Emerge's Mozart API needs a pairing credential on your LAN (the
  client currently assumes open local access).
- Exact `total_duration_seconds` vs `progress` units for the position bar.
- Beolink behaviour when expanding a Mozart speaker to a legacy peer (expected to
  be limited until Phase 2).
