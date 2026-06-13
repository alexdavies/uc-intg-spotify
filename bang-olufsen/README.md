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

> **Status: Phase 1.** Covers Mozart speakers (incl. the Emerge). An older
> Beoplay A9 (pre-5th-gen) uses a different *legacy* protocol and is not yet
> supported here — see the roadmap below. This package lives inside the
> `uc-intg-spotify` repo for now and is designed to be lifted into its own repo
> (`uc-intg-bang-olufsen`) later.

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

- **Phase 2 — legacy Beoplay A9 (pre-5th-gen):** add a `BeoNetRemote` backend
  behind the same entity layer so the older A9 lives in the same integration.
- **Phase 3 — Spotify bridge:** use the local API to wake / select the Spotify
  source on a speaker so the `uc-intg-spotify` plugin can reliably start a
  chosen playlist on an otherwise-idle Emerge.

## Open items to verify against real devices

- Whether the Emerge's Mozart API needs a pairing credential on your LAN (the
  client currently assumes open local access).
- Exact `total_duration_seconds` vs `progress` units for the position bar.
- Beolink behaviour when expanding a Mozart speaker to a legacy peer (expected to
  be limited until Phase 2).
