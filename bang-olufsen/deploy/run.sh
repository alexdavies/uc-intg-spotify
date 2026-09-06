#!/bin/sh
# Launcher for the Bang & Olufsen integration as a background service (macOS).
# Auto-detects the LAN IP for the mDNS advert so it survives DHCP changes.
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"   # the bang-olufsen directory

# Primary LAN interface's IPv4 (en0 on most Macs; Ethernet on a mini may differ).
IFACE="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')"
IP="$(ipconfig getifaddr "${IFACE:-en0}" 2>/dev/null || true)"
[ -n "$IP" ] || IP="$(ipconfig getifaddr en1 2>/dev/null || true)"

export UC_INTEGRATION_INTERFACE="$IP"
export UC_INTEGRATION_HTTP_PORT="${UC_INTEGRATION_HTTP_PORT:-9090}"
# config.json lives in the integration dir unless UC_CONFIG_HOME is already set
# (a driver started by hand with ucapi's default keeps it in $HOME).
export UC_CONFIG_HOME="${UC_CONFIG_HOME:-$HERE}"
export PYTHONPATH="$HERE"

echo "$(date '+%Y-%m-%d %H:%M:%S') starting B&O integration on ${IP}:${UC_INTEGRATION_HTTP_PORT}"
# caffeinate keeps a laptop from idle-sleeping while the driver runs (a sleeping
# Mac = "not responding" on the Remote). Harmless on an always-on mini.
exec caffeinate -i -s "$HERE/.venv/bin/python" -u "$HERE/uc_intg_bang_olufsen/driver.py"
