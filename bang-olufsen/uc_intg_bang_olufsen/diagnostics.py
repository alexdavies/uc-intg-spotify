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

import aiohttp

from uc_intg_bang_olufsen.client import BeoClient
from uc_intg_bang_olufsen.discovery import discover_devices
from uc_intg_bang_olufsen.factory import create_client, detect_protocol

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
        from uc_intg_bang_olufsen import client, config, discovery, player, remote, setup, driver  # noqa: F401
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
        from uc_intg_bang_olufsen.player import BeoPlayer
        speaker = {
            "serial": "TEST", "name": "Test Speaker", "client": c,
            "sources": [{"id": "lineIn", "name": "Line-In"}],
        }
        stations = [{"name": "Radio One", "url": "http://example/stream", "content_type": "audio/mpeg"}]
        player = BeoPlayer(api, speaker, stations, None, [{"name": "My Mix", "uri": "spotify:playlist:1"}])
        src = player.entity.attributes["source_list"]
        assert "Radio: Radio One" in src and "My Mix" in src
        assert player.entity.id == "beo_player_TEST"
        from uc_intg_bang_olufsen.remote import BeoRemote
        rem = BeoRemote(api, player, "Test Speaker", "TEST", stations)
        assert rem.entity.id == "beo_remote_TEST"
        assert "PLAY_PAUSE" in rem.entity.options["simple_commands"]
        _ok("per-speaker player + remote entities construct and wire correctly")
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

    protocol = await detect_protocol(args.host)
    if protocol == "legacy":
        _info("this is a LEGACY speaker - running the common-interface probe")
        _info("(use 'legacy-probe' for the full ASE endpoint dump)")
        return await _info_via_interface(args.host)

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

    # 2c. sources (show raw flags so we can see what is selectable)
    try:
        sources = await bc.get_sources()
        if sources:
            _ok(f"{len(sources)} selectable source(s): " + ", ".join(s["name"] for s in sources))
        else:
            _info("no selectable sources reported (may be normal if idle)")
        raw = await bc._client.get_available_sources(target_remote=False)
        for s in (raw.items or []):
            print(f"           src id={s.id!r} name={s.name!r} enabled={s.is_enabled} playable={s.is_playable}")
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


async def _info_via_interface(host: str) -> int:
    """Backend-agnostic read-only probe using the common client interface."""
    client = create_client(host, protocol="legacy")
    failures = 0
    try:
        if await client.connect():
            _ok("device reachable")
        else:
            _fail("device not reachable")
            failures += 1

        sources = await client.get_sources()
        if sources:
            _ok(f"{len(sources)} source(s): " + ", ".join(s["name"] for s in sources))
        else:
            _info("no sources reported")

        state = await client.get_state()
        _ok(f"state snapshot: {state if state else '(empty)'}")
    finally:
        await client.close()

    print()
    print("Stage 2 PASSED." if not failures else f"Stage 2 finished with {failures} problem(s).")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Identify: which B&O protocol does a speaker speak? (Phase 2 prep)
# ---------------------------------------------------------------------------

# Legacy "ASE"/BeoNetRemote speakers (older Beoplay A9, Beosound 35, ...) answer
# a device descriptor at :8080/BeoDevice. Mozart speakers do not.
LEGACY_PATHS = [
    "http://{host}:8080/BeoDevice",
    "http://{host}/BeoDevice",
]
# mDNS service types worth checking, by protocol family.
SERVICE_TYPES = {
    "_bangolufsen._tcp.local.": "Mozart",
    "_beoremote._tcp.local.": "Legacy (ASE/BeoNetRemote)",
    "_beozone._tcp.local.": "Legacy (ASE/BeoZone)",
    "_products._tcp.local.": "Legacy (B&O products)",
}


async def cmd_identify(args) -> int:
    _header(f"Identify protocol for {args.host}")
    verdict = "unknown"

    # 1. Mozart? Reuse the same client the integration uses.
    bc = BeoClient(args.host)
    try:
        if await bc.connect():
            _ok("responds to the Mozart API -> this is a MOZART speaker")
            verdict = "mozart"
        else:
            _info("no Mozart response (expected for an older Beoplay A9)")
    except Exception as e:
        _info(f"no Mozart response: {type(e).__name__}")
    finally:
        await bc.close()

    # 2. Legacy ASE / BeoNetRemote descriptor?
    legacy_body = None
    async with aiohttp.ClientSession() as session:
        for template in LEGACY_PATHS:
            url = template.format(host=args.host)
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    text = await resp.text()
                    if resp.status == 200 and text:
                        _ok(f"legacy descriptor at {url} (HTTP 200)")
                        legacy_body = text
                        if verdict == "unknown":
                            verdict = "legacy"
                        break
                    _info(f"{url} -> HTTP {resp.status}")
            except Exception as e:
                _info(f"{url} -> {type(e).__name__}")

    if legacy_body:
        for field in ("productType", "productName", "FriendlyName", "name", "softwareVersion", "typeNumber"):
            value = _extract(legacy_body, field)
            if value:
                print(f"           {field}: {value}")
        print(f"           (raw snippet) {legacy_body[:200].strip()}")

    # 3. What does it advertise over mDNS?
    _header("mDNS services advertised nearby")
    seen = await _scan_services(args.timeout)
    if not seen:
        _info("no B&O mDNS services seen (network may block multicast)")
    for stype, entries in seen.items():
        family = SERVICE_TYPES.get(stype, stype)
        for name, addrs in entries:
            marker = " <-- THIS HOST" if args.host in addrs else ""
            _ok(f"{family}: {name} {addrs}{marker}")

    # Verdict
    _header("Verdict")
    if verdict == "mozart":
        print("  MOZART speaker - already supported by this integration (Phase 1).")
    elif verdict == "legacy":
        print("  LEGACY (ASE/BeoNetRemote) speaker - this is the Phase 2 target.")
        print("  Paste this whole output back and I'll build the legacy backend to match.")
    else:
        print("  Could not classify. Paste this output back and we'll work it out.")
        print("  (Confirm the IP is correct and the speaker is awake.)")
    return 0


async def _scan_services(timeout: float) -> dict:
    """Browse several B&O mDNS service types; return {type: [(name, [addrs])]}."""
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    results: dict = {}
    azc = AsyncZeroconf()
    pending = []

    async def _resolve(stype, name):
        info = AsyncServiceInfo(stype, name)
        if await info.async_request(azc.zeroconf, 3000):
            addrs = info.parsed_addresses() or []
            results.setdefault(stype, []).append((name.split(".")[0], addrs))

    def _on_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            pending.append(asyncio.ensure_future(_resolve(service_type, name)))

    browsers = [AsyncServiceBrowser(azc.zeroconf, stype, handlers=[_on_change]) for stype in SERVICE_TYPES]
    try:
        await asyncio.sleep(timeout)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        for b in browsers:
            await b.async_cancel()
        await azc.async_close()
    return results


def _extract(xml_or_json: str, field: str) -> Optional[str]:
    """Best-effort scrape of <field>value</field> or "field": "value"."""
    import re
    m = re.search(rf"<{field}>([^<]+)</{field}>", xml_or_json, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(rf'"{field}"\s*:\s*"([^"]+)"', xml_or_json, re.IGNORECASE)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Legacy probe: dump the ASE/BeoZone API surface (Phase 2 backend prep)
# ---------------------------------------------------------------------------

# Read-only GET endpoints on the legacy ASE platform (Beoplay A9 4th gen etc.),
# served on port 8080. We dump these so the Phase 2 backend is built against the
# device's real JSON shapes - especially radio favourites, whose endpoint varies.
LEGACY_GET_ENDPOINTS = [
    "/BeoDevice",
    "/BeoZone/Zone/Sources",
    "/BeoZone/Zone/ActiveSources",
    "/BeoZone/Zone/Sound/Volume/Speaker/Level",
    "/BeoZone/Zone/Sound/Volume/Speaker/Muted",
    "/BeoZone/Zone/Stream",
    "/BeoZone/Zone/PlayQueue",
    # Radio favourites / presets are exposed differently across firmwares; probe
    # every known candidate and report which one answers.
    "/BeoZone/Zone/Favorites",
    "/BeoZone/Zone/Favorites/",
    "/BeoZone/Zone/Lists",
    "/BeoZone/Zone/Lists/FavoriteLists",
    "/BeoContent/RadioFavorites",
    "/BeoOneWay/Favorites",
]


async def cmd_legacy_probe(args) -> int:
    _header(f"Legacy ASE/BeoZone probe of {args.host}")
    base = f"http://{args.host}:8080"

    async with aiohttp.ClientSession() as session:
        for path in LEGACY_GET_ENDPOINTS:
            url = base + path
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=4, connect=3, sock_connect=3)) as resp:
                    text = (await resp.text()).strip()
                    if resp.status == 200 and text:
                        _ok(f"{path}")
                        print(f"           {text[:600]}")
                    else:
                        _info(f"{path} -> HTTP {resp.status}")
            except Exception as e:
                _info(f"{path} -> {type(e).__name__}")

    _header(f"Live notifications ({args.seconds:.0f}s) - touch the speaker / play radio now")
    await _stream_legacy_notifications(base, args.seconds)

    _header("Done")
    print("  Paste this whole output back. I especially need:")
    print("   - which Favorites/Lists endpoint returned 200 (radio favourites)")
    print("   - the notification 'type' values you saw when you changed things")
    print("   - the volume Level JSON shape (for correct scaling)")
    return 0


async def _stream_legacy_notifications(base: str, seconds: float) -> None:
    """Read the BeoNotify long-poll stream briefly and print event types seen."""
    url = base + "/BeoNotify/Notifications"
    seen_types: dict = {}

    async def _reader():
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=None, connect=4, sock_connect=4)) as resp:
                async for raw in resp.content:
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    ntype = _extract(line, "type") or "(unknown)"
                    if ntype not in seen_types:
                        seen_types[ntype] = line[:300]
                        _ok(f"notification type: {ntype}")
                        print(f"           {line[:300]}")

    try:
        await asyncio.wait_for(_reader(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        _info(f"notification stream error: {type(e).__name__}: {e}")
    if not seen_types:
        _info("no notifications captured (try again while pressing play / changing volume)")


async def cmd_listen(args) -> int:
    _header(f"Stage 3: real-time notifications from {args.host} ({args.seconds:.0f}s)")
    bc = await _client_for(args.host)
    if bc is None:
        _fail(f"{args.host} not reachable as a Mozart or legacy speaker")
        return 1

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

async def _client_for(host: str):
    """Detect the speaker's protocol and return the right client (or None)."""
    protocol = await detect_protocol(host)
    if not protocol:
        return None
    _info(f"detected protocol: {protocol}")
    return create_client(host, protocol=protocol)


async def _run_command(host: str, label: str, coro_factory) -> int:
    _header(f"Stage 4: {label} -> {host}")
    bc = await _client_for(host)
    if bc is None:
        _fail(f"{host} not reachable as a Mozart or legacy speaker")
        return 1
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

    idf = sub.add_parser("identify", help="Identify which B&O protocol a speaker speaks (Phase 2 prep)")
    idf.add_argument("host")
    idf.add_argument("--timeout", type=float, default=6.0)
    idf.set_defaults(func=cmd_identify)

    lp = sub.add_parser("legacy-probe", help="Dump a legacy ASE speaker's API surface (Phase 2 backend prep)")
    lp.add_argument("host")
    lp.add_argument("--seconds", type=float, default=12.0)
    lp.set_defaults(func=cmd_legacy_probe)

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
