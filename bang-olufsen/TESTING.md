# Bang & Olufsen integration — staged testing runbook

This integration was built against the documented `mozart-api` and verified with
mock tests, but **not yet against real hardware**. Run the stages below **in
order** from a machine on the **same network as your speakers** (a laptop is
fine). Each stage proves one layer works before the next, so any problem is
caught close to its cause.

After each stage, if you hit a `[FAIL]`, copy the `[FAIL]`/`[INFO]` lines back to
me — they tell me exactly what to adjust (e.g. add API auth, switch from push to
polling, fix a field name).

## One-time setup

```bash
cd bang-olufsen
pip install -r requirements.txt
```

## Stage 0 — wiring check (no hardware, you can run this anywhere)

```bash
python -m uc_intg_bang_olufsen.diagnostics selftest
```

Proves: the library is installed, all modules import, `driver.json` is valid, and
the entities construct. Already passing in CI — run it to confirm your install.

## Stage 1 — discovery (proves we can find the Emerge)

```bash
python -m uc_intg_bang_olufsen.diagnostics discover
```

Expect your **Beosound Emerge** to be listed with its IP. Your older **Beoplay
A9 will NOT appear** — it uses the legacy protocol and is Phase 2; that's
expected. Note the Emerge's IP for the next stages.

If nothing is found: check you're on the same subnet/VLAN and that your
access point isn't blocking mDNS/multicast. You can still continue with a known
IP from your router.

## Stage 2 — read-only probe (proves the API + auth assumptions) ⭐ most important

```bash
python -m uc_intg_bang_olufsen.diagnostics info <EMERGE_IP>
```

This is the key blind-spot check. It reads — and changes nothing — and tells us:

- whether the speaker is reachable and whether the Mozart API needs a
  **credential** (if you see a 401/403/auth error here, tell me — I'll add auth);
- your **Beolink JID** (needed for multiroom);
- the list of **playable sources** (e.g. Spotify, TuneIn, Line-In);
- your **presets / radio favourites** with their numeric IDs (set some in the
  B&O app first if none show);
- a current **state snapshot**.

If this stage is clean, the riskiest assumptions in the code are confirmed.

## Stage 3 — real-time updates (proves the push WebSocket)

```bash
python -m uc_intg_bang_olufsen.diagnostics listen <EMERGE_IP>
```

While it runs (~20s), press play / change volume on the speaker or in the B&O
app. You should see `[PUSH]` lines reflecting those changes. No events while you
leave it untouched is normal. If you change things and still see nothing, tell me
and I'll switch that speaker to polling.

## Stage 4 — individual commands (proves control)

Run these one at a time and watch the speaker react:

```bash
python -m uc_intg_bang_olufsen.diagnostics play     <EMERGE_IP>
python -m uc_intg_bang_olufsen.diagnostics pause    <EMERGE_IP>
python -m uc_intg_bang_olufsen.diagnostics volume   <EMERGE_IP> 25
python -m uc_intg_bang_olufsen.diagnostics preset   <EMERGE_IP> 1     # a radio favourite from Stage 2
python -m uc_intg_bang_olufsen.diagnostics source   <EMERGE_IP> spotify
python -m uc_intg_bang_olufsen.diagnostics next     <EMERGE_IP>
python -m uc_intg_bang_olufsen.diagnostics previous <EMERGE_IP>
```

Use a preset ID and source ID that Stage 2 actually reported. `preset` is the
radio-favourite path — getting a station playing here is the headline feature.

## Stage 5 — the full integration on the remote

Only after Stages 1–4 pass:

```bash
python -u uc_intg_bang_olufsen/driver.py
```

Then add the integration on the Remote, run setup (it scans and lists the
Emerge), and confirm one media-player entity per speaker appears and works.

## Phase 2 prep — identify the Beoplay A9's protocol

Before building local control for the older A9, confirm what it actually speaks.
Get the A9's IP (from your router, or it's the device the Spotify integration
sees), then:

```bash
python -m uc_intg_bang_olufsen.diagnostics identify <A9_IP>
```

This is read-only. It tests the Mozart API, the legacy `:8080/BeoDevice`
descriptor, and the B&O mDNS service types, then prints a verdict
(Mozart / Legacy-ASE / Unknown) plus the evidence.

If the verdict is **Legacy**, follow up with the deeper probe so the backend is
built against the device's real API shapes (especially radio favourites and the
notification format, which vary by firmware). Start radio playing on the A9, then:

```bash
python -m uc_intg_bang_olufsen.diagnostics legacy-probe <A9_IP>
```

This is also read-only: it dumps the `/BeoZone` endpoints and listens to the
`/BeoNotify` stream for ~12s. Paste the whole output back and I'll build the
Phase 2 backend to match exactly what your A9 exposes.

## Testing the legacy A9 backend

The diagnostics commands auto-detect the protocol, so the same commands work on
the A9 (they route to the legacy backend automatically):

```bash
python -m uc_intg_bang_olufsen.diagnostics info    <A9_IP>   # sources + state
python -m uc_intg_bang_olufsen.diagnostics listen  <A9_IP>   # live BeoNotify events
python -m uc_intg_bang_olufsen.diagnostics play    <A9_IP>
python -m uc_intg_bang_olufsen.diagnostics pause   <A9_IP>
python -m uc_intg_bang_olufsen.diagnostics volume  <A9_IP> 20
python -m uc_intg_bang_olufsen.diagnostics source  <A9_IP> <source_id_from_info>
```

`info` will list the A9's source IDs; use one of those with `source` to switch
inputs (e.g. select "B&O Radio"). `preset` will report that the A9 has no preset
API — that's expected. Watch the speaker react and confirm `listen` shows
`[PUSH]` lines when you change volume or track.

---

### What I most need back from you

After Stage 2 (`info`), paste its output. That single result confirms or
corrects the assumptions everything else is built on, and tells me whether to
start Phase 2 (legacy A9) or first fix anything on the Emerge path.
