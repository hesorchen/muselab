# Install muselab on macOS

One-shot installer for personal Mac. Runs as a **user-level LaunchAgent** —
no `sudo`, autostarts on login, restarts on crash.

## Prerequisites

- macOS 12 (Monterey) or newer (Apple Silicon or Intel)
- `uv` ([install](https://docs.astral.sh/uv/getting-started/installation/)):
  ```bash
  brew install uv
  # or:  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- (For Anthropic models) `claude` CLI logged in once:
  ```bash
  claude login
  ```
  This stores OAuth in `~/.claude/` which the agent reuses. Non-Claude providers
  (DeepSeek / GLM / MiniMax / Kimi) only need API keys via Settings UI.

## Install

```bash
git clone https://github.com/hesorchen/muselab && cd muselab
bash scripts/install-macos.sh
```

The script will:

1. Verify `uv` (and warn if `claude` missing)
2. Run `uv sync`
3. **Ask you** for the archive directory (where Muse can read/write),
   defaults to `~/muselab-archive`
4. Generate `.env` with a random `MUSELAB_TOKEN` and `MUSELAB_HOST=127.0.0.1`
5. Write `~/Library/LaunchAgents/com.muselab.plist` and `launchctl load -w`
6. Curl `localhost:8765` to confirm it's up

If `.env` already exists, the script keeps it (re-running is safe).

## Verify

```bash
launchctl list | grep muselab        # should show a PID
open http://localhost:8765            # browser
grep MUSELAB_TOKEN .env               # paste at login
```

## Survives reboot?

Yes — `RunAtLoad=true` in the plist. macOS launches the agent at login. No
extra setup needed (unlike Linux's `loginctl enable-linger`).

If you want it to start **before** you log in (rare; e.g. headless Mac mini),
move from `LaunchAgents` to `LaunchDaemons` and run as root — out of scope for
this installer; ping me if you need it.

## Common commands

```bash
launchctl list | grep muselab                          # check loaded
launchctl kickstart -k gui/$UID/com.muselab            # restart (preserves state)
launchctl unload  ~/Library/LaunchAgents/com.muselab.plist   # stop (until next login)
launchctl load -w ~/Library/LaunchAgents/com.muselab.plist   # start again
tail -f ~/Library/Logs/muselab/stderr.log              # tail logs
```

## Expose to LAN (optional)

Default binds to `127.0.0.1`. To let your phone / iPad on the same WiFi connect:

1. Edit `.env`:
   ```
   MUSELAB_HOST=0.0.0.0
   ```
2. Restart: `launchctl kickstart -k gui/$UID/com.muselab`
3. Find your Mac's LAN IP: `ipconfig getifaddr en0` (WiFi) or `en1` (Ethernet)
4. From another device: `http://<that-ip>:8765`

macOS firewall: System Settings → Network → Firewall. If on, you may get an
"accept incoming connections" prompt for `python` — allow it.

⚠ Token leak ≈ shell access. Don't expose to networks you don't fully trust
without HTTPS + an auth layer in front (nginx basic-auth, Tailscale, …).

## Uninstall

```bash
bash scripts/uninstall-macos.sh
```

Unloads and removes the plist. `.env`, `sessions/`, your archive directory, and
the log dir are **not** touched. Delete the repo to remove fully.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `agent failed to load` | `cat ~/Library/Logs/muselab/stderr.log` — usually `.env` missing or invalid |
| Port already in use | `lsof -iTCP:8765 -sTCP:LISTEN` → kill the offender or change `MUSELAB_PORT` |
| Anthropic models 401 | `~/.claude` missing or stale — `claude login` again |
| `claude` not found by agent | The plist sets a hardcoded `PATH` including `/opt/homebrew/bin` and `/usr/local/bin`. If your `claude` is elsewhere, edit `EnvironmentVariables/PATH` in the plist and `launchctl unload && load -w` it. |
| Plist installed but no autostart at login | Check `launchctl print gui/$UID/com.muselab` — look for `state = running` |
