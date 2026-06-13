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

- **Network discovery** of Mozart speakers via mDNS (`_bangolufsen._tcp`), plus
  manual IP entry for speakers on another subnet.
- **One media player + one remote entity per speaker.**
- **Real-time state** (now playing, album art, volume, transport) pushed over the
  Mozart notification WebSocket — no polling, no Spotify rate-limit concerns.
- **Transport / volume / mute / source selection.**
- **Radio favourites**: each speaker preset (the physical favourite buttons,
  typically radio stations) is exposed both as a media-player source
  (`Radio: ...`) and as a remote button / simple command (`RADIO_*`).
- **Beolink multiroom**: "Play on <peer>" expands the current experience to
  another configured speaker; "Leave multiroom" detaches.

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
- **Legacy radio favourites:** capture the currently-playing station on the A9
  (via the play queue / `NOW_PLAYING_NET_RADIO`) and store named favourites that
  can be replayed, since the A9 4th gen exposes no Favorites API endpoint.
- **Multiroom across backends:** "play on both" is reliable between Mozart
  speakers; Mozart↔legacy expansion is limited by the older protocol.

## Open items to verify against real devices

- Whether the Emerge's Mozart API needs a pairing credential on your LAN (the
  client currently assumes open local access).
- Exact `total_duration_seconds` vs `progress` units for the position bar.
- Beolink behaviour when expanding a Mozart speaker to a legacy peer (expected to
  be limited until Phase 2).
