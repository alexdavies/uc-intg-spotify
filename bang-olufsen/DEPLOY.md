# Deploying the Bang & Olufsen integration to the Remote

## Will this brick my Remote?

No. A custom integration runs as an isolated userspace driver. The Remote
validates the uploaded archive, and a broken integration simply fails to load —
you then delete it from the web configurator. It does not touch the Remote's
firmware. And Route A below installs nothing on the device at all.

`driver_id` is `bang_olufsen_local` — deliberately distinct from the Remote's
**built-in** Bang & Olufsen integration (which uses `bang_olufsen`) and from the
Spotify integration (`spotify`). You can run all of them side by side.

---

## Route A — run it off-device first (recommended, zero risk)

The Remote can connect to an integration running on another machine on the same
network (your Mac, a Raspberry Pi, a NAS, Docker, ...). Nothing is installed on
the Remote, so there is nothing to brick and updating is just restarting the
process.

On the machine (must be on the same LAN/VLAN as the Remote and speakers):

```bash
cd bang-olufsen
pip install -r requirements.txt
export MAC_IP=$(ipconfig getifaddr en0)   # macOS Wi-Fi; Linux: hostname -I | awk '{print $1}'
UC_INTEGRATION_INTERFACE=$MAC_IP UC_INTEGRATION_HTTP_PORT=9090 \
  python -u uc_intg_bang_olufsen/driver.py
```

> **Important:** set `UC_INTEGRATION_INTERFACE` to the machine's **real LAN IP**,
> not `0.0.0.0`. ucapi puts that value straight into the mDNS record, so `0.0.0.0`
> (or leaving it unset on a multi-interface machine) makes the Remote discover the
> integration but fail to connect with *"Service is currently not available."*

The driver advertises itself over mDNS. In the Remote's **Web Configurator →
Integrations → Add new**, it should appear for you to add and run setup
(discovery of your speakers).

Keep the process running while you use it. This is the best way to iterate:
when I push a fix, you just `git pull` and restart.

> Tip: to find the Web Configurator, browse to the Remote's IP, or use the URL
> shown under Settings → Integrations & docks → Web Configurator on the Remote.

---

## Route B — install on the Remote (resident, no always-on computer)

This puts a self-contained binary on the Remote so it runs without another
machine. The archive is a `.tar.gz` with `driver.json` at the root and a
statically-linked aarch64 `driver` binary in `./bin/` — **not** the repo source.

### 1. Get the archive

Build it in CI (no local cross-compiling needed):

1. GitHub → **Actions** tab → **"Build Bang & Olufsen Integration"** → **Run
   workflow** on branch `claude/youthful-allen-cdpu5w`.
2. When it finishes, download the `uc-intg-bang_olufsen_local-<version>-aarch64`
   artifact and unzip it to get `uc-intg-bang_olufsen_local-<version>-aarch64.tar.gz`.
   **Do not unzip the inner `.tar.gz`.**

### 2. Upload it

Either:

- **Web Configurator → Integrations → Add new → Install custom** → select the
  `.tar.gz` → Upload, or
- REST API:

  ```bash
  curl --location 'http://<REMOTE_IP>/api/intg/install' \
    --user 'web-configurator:<PIN>' \
    --form 'file=@"uc-intg-bang_olufsen_local-<version>-aarch64.tar.gz"'
  ```

Then run the integration's setup from the Integrations list to discover your
speakers.

### 3. Update or remove

Custom integrations can't be updated in place. To update: **delete** it from the
Integrations menu, then upload the new `.tar.gz`. Removing it fully reverses the
install.

---

## A note on the modified Spotify integration

The Spotify integration in this repo still uses `driver_id: spotify`, so if you
install it while the official Spotify integration is present they will collide.
If you want to run our modified Spotify build alongside the official one, ask me
to rename its `driver_id` (and entity ids) to something like `spotify_multi`
first. The B&O integration needs no such change.
