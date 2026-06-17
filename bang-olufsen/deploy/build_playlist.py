#!/usr/bin/env python3
"""
Create/refresh a curated "◆" playlist in your Spotify account.

Spotify blocks playlist *writes* for development-mode apps, so we read track data
with the official API (search) and do the *write* via SpotAPI, which uses your
web-player session (sp_dc cookie) — the same permissions as the Spotify web app.

Session file (default ./.spotapi_session.json, or $SPOTAPI_SESSION), git-ignored:
  {"identifier": "<your Spotify email or username>", "cookies": {"sp_dc": "<value>"}}

Usage:
  .venv/bin/python deploy/build_playlist.py            # builds "◆ Discovery"
  .venv/bin/python deploy/build_playlist.py --name "◆ X" --tracks file.txt
"""
import argparse
import asyncio
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # the bang-olufsen dir

from uc_intg_bang_olufsen.config import BeoConfig
from uc_intg_bang_olufsen.spotify import SpotifyClient

DISCOVERY = [
    "Stars - Your Ex-Lover Is Dead", "Phoenix - 1901", "Spoon - The Way We Get By",
    "Cut Copy - Lights & Music", "LCD Soundsystem - All My Friends",
    "Stars - Take Me to the Riot", "Phoenix - Lisztomania", "Spoon - I Turn My Camera On",
    "Cut Copy - Hearts on Fire", "LCD Soundsystem - Dance Yrself Clean",
    "Rolling Blackouts Coastal Fever - French Press", "Spacey Jane - Booster Seat",
    "Turnstile - MYSTERY", "Fontaines D.C. - Starburster", "Parcels - Tieduprightnow",
    "Rolling Blackouts Coastal Fever - Talking Straight", "Spacey Jane - Lunchtime",
    "Turnstile - Blackout", "Fontaines D.C. - Boys in the Better Land", "Parcels - Lightenup",
]


async def resolve_ids(lines):
    cfg_path = os.path.join(os.environ.get("UC_CONFIG_HOME", os.path.expanduser("~")), "config.json")
    c = SpotifyClient(BeoConfig(cfg_path))
    ids = []
    for line in lines:
        q = urllib.parse.quote(line)
        r = await c._request("GET", f"/search?q={q}&type=track&limit=1")
        items = ((r or {}).get("tracks") or {}).get("items", [])
        if items:
            ids.append(items[0]["id"])
        else:
            print(f"  ! not found, skipped: {line}")
    await c.close()
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="◆ Discovery")
    ap.add_argument("--tracks", help="text file of 'Artist - Title' lines (default: built-in Discovery)")
    ap.add_argument("--session", default=os.environ.get("SPOTAPI_SESSION",
                                                        os.path.join(os.path.dirname(HERE), ".spotapi_session.json")))
    args = ap.parse_args()

    lines = [l.strip() for l in open(args.tracks)] if args.tracks else DISCOVERY
    lines = [l for l in lines if l]

    if not os.path.exists(args.session):
        sys.exit(f"Session file not found: {args.session}\n"
                 'Create it as {"identifier": "<spotify email>", "cookies": {"sp_dc": "<value>"}}')
    dump = json.load(open(args.session))

    print(f"Resolving {len(lines)} tracks via the official API...")
    ids = asyncio.run(resolve_ids(lines))
    print(f"  resolved {len(ids)} track ids")

    from spotapi import Config, Login, NoopLogger, PrivatePlaylist, Song
    login = Login.from_cookies(dump, Config(NoopLogger()))
    pid = PrivatePlaylist(login).create_playlist(args.name)
    print(f"Created playlist {args.name!r} -> {pid}")
    Song(PrivatePlaylist(login, pid)).add_songs_to_playlist(ids)
    print(f"Added {len(ids)} tracks. Done.")


if __name__ == "__main__":
    main()
