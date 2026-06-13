"""
Staged diagnostics / verification CLI for the Bang & Olufsen integration.

Run these IN ORDER when you are on the same LAN as the speakers. Each stage
verifies one layer and prints clear [OK]/[FAIL] results, so problems are caught
close to their cause instead of inside the full integration.

    # Stage 0 - no hardware needed (run anywhere):
    python -m uc_intg_bang_olufsen.diagnostics selftest

    # Stage 1 - discover speakers on the network:
    python -m uc_intg_bang_olufsen.diagnostics discover

    # Stage 2 - read-only probe of one speaker (validates API + auth):
    python -m uc_intg_bang_olufsen.diagnostics info 192.168.1.50

    # Stage 3 - watch real-time push events for ~20s:
    python -m uc_intg_bang_olufsen.diagnostics listen 192.168.1.50

    # Stage 4 - exercise individual commands:
    python -m uc_intg_bang_olufsen.diagnostics play    192.168.1.50
    python -m uc_intg_bang_olufsen.diagnostics volume  192.168.1.50 25
    python -m uc_intg_bang_olufsen.diagnostics preset  192.168.1.50 1
    python -m uc_intg_bang_olufsen.diagnostics source  192.168.1.50 spotify

Only once these pass should you run the full driver (driver.py).

:copyright: (c) 2024
:license: MPL-2.0, see LICENSE for more details.
"""

import argparse
import asyncio
import logging
import sys
from typing import Optional

from uc_intg_bang_olufsen.client import BeoClient
from uc_intg_bang_olufsen.discovery import discover_devices

# Quiet the library; the diagnostics print their own clear output.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(name)s | %(message)s")


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def _info(msg: str) -> None:
    print(f"  [INFO] {msg}")


def _header(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# Stage 0: self-test (no hardware)
# ---------------------------------------------------------------------------

async def cmd_selftest(_args) -> int:
    _header("Stage 0: self-test (no hardware required)")
    failures = 0

    try:
        import mozart_api  # noqa: F401
        _ok(f"mozart-api importable (version {getattr(mozart_api, '__version__', '?')})")
    except Exception as e:
        _fail(f"mozart-api not installed: {e}")
        failures += 1

    try:
        import ucapi  # noqa: F401
        from uc_intg_bang_olufsen import client, config, discovery, media_player, remote, setup, driver  # noqa: F401
        _ok("all integration modules import cleanly")
    except Exception as e:
        _fail(f"integration import error: {e}")
        failures += 1

    try:
        import json
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "driver.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        assert manifest["driver_id"] and manifest["version"]
        _ok(f"driver.json valid (id={manifest['driver_id']} v{manifest['version']})")
    except Exception as e:
        _fail(f"driver.json problem: {e}")
        failures += 1

    # Build entities with a mocked client to confirm wiring.
    try:
        from unittest.mock import MagicMock
        api = MagicMock()
        c = BeoClient("0.0.0.0", "Test Speaker", "TEST")
        c._client = MagicMock()
        from uc_intg_bang_olufsen.media_player import BeoMediaPlayer
        from uc_intg_bang_olufsen.remote import BeoRemote
        mp = BeoMediaPlayer(api, c, [{"id": "spotify", "name": "Spotify"}], [{"id": 1, "name": "Radio One"}])
        rm = BeoRemote(api, c, [{"id": 1, "name": "Radio One"}], [], None)
        assert "Radio: Radio One" in mp.entity.attributes["source_list"]
        assert rm.entity.options["simple_commands"]
        _ok("media player + remote entities construct and wire correctly")
    except Exception as e:
        _fail(f"entity construction error: {e}")
        failures += 1

    print()
    if failures:
        print(f"Stage 0 FAILED with {failures} problem(s). Fix these before testing hardware.")
        return 1
    print("Stage 0 PASSED. Code is wired correctly; proceed to 'discover' on your LAN.")
    return 0


# ---------------------------------------------------------------------------
# Stage 1: discovery
# ---------------------------------------------------------------------------

async def cmd_discover(args) -> int:
    _header("Stage 1: network discovery (_bangolufsen._tcp)")
    _info(f"scanning for {args.timeout:.0f}s...")
    devices = await discover_devices(timeout=args.timeout)
    if not devices:
        _fail("no Mozart speakers found.")
        print("\n  Things to check:")
        print("   - Is this machine on the SAME subnet/VLAN as the speakers?")
        print("   - Is mDNS/multicast allowed on your network (some APs block it)?")
        print("   - The Emerge is a Mozart device and SHOULD appear here. An older")
        print("     Beoplay A9 (legacy protocol) will NOT - that's expected (Phase 2).")
        print("   - You can still proceed with a known IP: 'info <ip>'.")
        return 1
    for d in devices:
        _ok(f"{d['name']}  host={d['host']}  model={d.get('model') or '?'}  serial={d.get('serial')}")
    print(f"\nStage 1 PASSED. Found {len(devices)} speaker(s). Next: 'info <host>'.")
    return 0


# ---------------------------------------------------------------------------
# Stage 2: read-only probe (validates API surface + auth)
# ---------------------------------------------------------------------------

async def cmd_info(args) -> int:
    _header(f"Stage 2: read-only probe of {args.host}")
    bc = BeoClient(args.host)
    raw = bc._client  # the underlying mozart-api client; probe it directly so
    failures = 0      # real exceptions (incl. auth errors) surface clearly.

    # 2a. reachability
    try:
        reachable = await raw.check_device_connection(raise_error=True)
        if reachable:
            _ok("device reachable (check_device_connection)")
        else:
            _fail("check_device_connection returned False")
            failures += 1
    except Exception as e:
        _fail(f"cannot reach device: {type(e).__name__}: {e}")
        _info("If this is an auth/401/403 error, the Mozart API on your speaker")
        _info("requires a credential - tell me and I'll add auth to the client.")
        await bc.close()
        return 1

    # 2b. beolink identity
    try:
        me = await raw.get_beolink_self()
        _ok(f"Beolink JID: {me.jid}  (friendly name: {me.friendly_name})")
    except Exception as e:
        _fail(f"get_beolink_self failed: {type(e).__name__}: {e}")
        failures += 1

    # 2c. sources
    try:
        sources = await bc.get_sources()
        if sources:
            _ok(f"{len(sources)} playable source(s): " + ", ".join(s["name"] for s in sources))
        else:
            _info("no playable sources reported (may be normal if idle)")
    except Exception as e:
        _fail(f"sources probe failed: {type(e).__name__}: {e}")
        failures += 1

    # 2d. presets (radio favourites) - the key feature
    try:
        presets = await bc.get_presets()
        if presets:
            _ok(f"{len(presets)} preset/radio favourite(s):")
            for p in presets:
                print(f"           preset {p['id']}: {p['name']}")
        else:
            _info("no presets found. Set some favourites in the B&O app, then re-run.")
    except Exception as e:
        _fail(f"presets probe failed: {type(e).__name__}: {e}")
        failures += 1

    # 2e. current state snapshot
    try:
        state = await bc.get_state()
        _ok(f"state snapshot: {state if state else '(empty - speaker likely idle)'}")
    except Exception as e:
        _fail(f"state probe failed: {type(e).__name__}: {e}")
        failures += 1

    await bc.close()
    print()
    if failures:
        print(f"Stage 2 finished with {failures} problem(s). Share the [FAIL] lines with me.")
        return 1
    print("Stage 2 PASSED. API assumptions hold. Next: 'listen' then individual commands.")
    return 0


# ---------------------------------------------------------------------------
# Stage 3: real-time push (WebSocket)
# ---------------------------------------------------------------------------

async def cmd_listen(args) -> int:
    _header(f"Stage 3: real-time notifications from {args.host} ({args.seconds:.0f}s)")
    bc = BeoClient(args.host)

    async def on_update(attrs):
        print(f"  [PUSH] {attrs}")

    bc.on_update = on_update
    await bc.start_notifications()
    _info("connected. Now press play / change volume on the speaker or app...")
    _info("(no events while idle and untouched is normal)")
    try:
        await asyncio.sleep(args.seconds)
    finally:
        await bc.close()
    print("\nStage 3 done. If you saw [PUSH] lines when you touched the speaker, the")
    print("real-time WebSocket works. If nothing appeared even while changing things,")
    print("tell me - we may need to poll instead on your firmware.")
    return 0


# ---------------------------------------------------------------------------
# Stage 4: individual commands
# ---------------------------------------------------------------------------

async def _run_command(host: str, label: str, coro_factory) -> int:
    _header(f"Stage 4: {label} -> {host}")
    bc = BeoClient(host)
    try:
        ok = await coro_factory(bc)
        if ok:
            _ok(f"{label} succeeded (check the speaker responded as expected)")
            rc = 0
        else:
            _fail(f"{label} returned failure")
            rc = 1
    except Exception as e:
        _fail(f"{label} raised {type(e).__name__}: {e}")
        rc = 1
    finally:
        await bc.close()
    return rc


async def cmd_play(args):
    return await _run_command(args.host, "play", lambda bc: bc.play())

async def cmd_pause(args):
    return await _run_command(args.host, "pause", lambda bc: bc.pause())

async def cmd_stop(args):
    return await _run_command(args.host, "stop", lambda bc: bc.stop())

async def cmd_next(args):
    return await _run_command(args.host, "next", lambda bc: bc.next_track())

async def cmd_previous(args):
    return await _run_command(args.host, "previous", lambda bc: bc.previous_track())

async def cmd_volume(args):
    return await _run_command(args.host, f"set volume {args.level}", lambda bc: bc.set_volume(args.level))

async def cmd_preset(args):
    return await _run_command(args.host, f"activate preset {args.preset_id}", lambda bc: bc.activate_preset(args.preset_id))

async def cmd_source(args):
    return await _run_command(args.host, f"set source {args.source_id}", lambda bc: bc.set_source(args.source_id))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="diagnostics", description="Staged B&O integration verification.")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("selftest", help="Stage 0: no-hardware wiring check").set_defaults(func=cmd_selftest)

    d = sub.add_parser("discover", help="Stage 1: find speakers via mDNS")
    d.add_argument("--timeout", type=float, default=6.0)
    d.set_defaults(func=cmd_discover)

    i = sub.add_parser("info", help="Stage 2: read-only probe of a speaker")
    i.add_argument("host")
    i.set_defaults(func=cmd_info)

    l = sub.add_parser("listen", help="Stage 3: watch real-time push events")
    l.add_argument("host")
    l.add_argument("--seconds", type=float, default=20.0)
    l.set_defaults(func=cmd_listen)

    for name, fn in [("play", cmd_play), ("pause", cmd_pause), ("stop", cmd_stop),
                     ("next", cmd_next), ("previous", cmd_previous)]:
        s = sub.add_parser(name, help=f"Stage 4: {name}")
        s.add_argument("host")
        s.set_defaults(func=fn)

    v = sub.add_parser("volume", help="Stage 4: set volume 0-100")
    v.add_argument("host")
    v.add_argument("level", type=int)
    v.set_defaults(func=cmd_volume)

    pr = sub.add_parser("preset", help="Stage 4: activate a preset (radio favourite)")
    pr.add_argument("host")
    pr.add_argument("preset_id", type=int)
    pr.set_defaults(func=cmd_preset)

    so = sub.add_parser("source", help="Stage 4: select a source by id")
    so.add_argument("host")
    so.add_argument("source_id")
    so.set_defaults(func=cmd_source)

    return p


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
