# Running the B&O integration as a background service on a Mac mini

This makes the integration start automatically, restart if it crashes, and run
without keeping a terminal open — using macOS's `launchd`. It's the simplest way
to make everything permanent (no aarch64 resident build needed).

## 1. Get the code onto the mini
Either clone it:
```sh
git clone <this-repo-url> ~/uc-intg-spotify
cd ~/uc-intg-spotify && git checkout <branch>
```
…or copy the `bang-olufsen/` folder over (e.g. `rsync`/AirDrop). You do **not**
need to copy `.venv` — the installer rebuilds it.

## 2. Bring your config (keeps Spotify login + speakers)
Copy your existing `config.json` to `bang-olufsen/config.json` on the mini:
```sh
scp youroldmac:~/config.json ~/uc-intg-spotify/bang-olufsen/config.json
```
(If you skip this, just run the integration's setup again from the Remote.)

## 3. Install the service
```sh
sh ~/uc-intg-spotify/bang-olufsen/deploy/install.sh
```
This creates the venv, installs dependencies, writes a LaunchAgent, and starts it.
The Remote will discover **Bang & Olufsen (Local)** over the network as before.

## 4. Stop the mini from sleeping
A laptop/desktop will sleep and kill the service. For an always-on mini:
```sh
sudo pmset -a sleep 0          # never auto-sleep the system
sudo pmset -a womp 1           # wake for network access
```
(Display sleep is fine.) Also set **System Settings → Energy** → "Prevent
automatic sleeping" and, if it's headless, enable **automatic login** so the
LaunchAgent runs after a reboot.

## Managing it
```sh
tail -f ~/Library/Logs/beo-bang-olufsen.log     # logs
launchctl unload ~/Library/LaunchAgents/com.beo.bang-olufsen.plist   # stop
launchctl load   ~/Library/LaunchAgents/com.beo.bang-olufsen.plist   # start
```

## Updating later
```sh
cd ~/uc-intg-spotify && git pull
launchctl unload ~/Library/LaunchAgents/com.beo.bang-olufsen.plist
launchctl load   ~/Library/LaunchAgents/com.beo.bang-olufsen.plist
```

## Boot-time without login (optional)
The LaunchAgent above starts when the user logs in (pairs well with automatic
login). For a true boot-time service independent of login, move the plist to
`/Library/LaunchDaemons/` (owned by root) and add a `<key>UserName</key>` so it
runs as your user. The LaunchAgent + auto-login route is simpler and usually
enough for a home mini.
