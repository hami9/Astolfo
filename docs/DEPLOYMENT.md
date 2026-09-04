# Deployment

The bot uses long polling, so it needs no public URL, no webhook and no TLS certificate.
It does need a process that stays running.

## Before anything else

1. Create the bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. **Turn off Group Privacy**: `/mybots` → your bot → *Bot Settings* → *Group Privacy* →
   *Turn off*. Without this Telegram only forwards commands and replies, so the bot cannot
   see the conversation. Remove and re-add the bot to existing groups afterwards.
3. Get one API key. Any service in the table in [COST.md](COST.md) works, and several of
   them have a free tier — [openrouter.ai](https://openrouter.ai/keys) with
   `FREE_MODE=1`, or [aistudio.google.com](https://aistudio.google.com/apikey), or
   [console.groq.com](https://console.groq.com/keys). More keys can be added later from
   the bot's own panel without touching the server.
4. Know your numeric Telegram id, from [@userinfobot](https://t.me/userinfobot). It goes in
   `MASTER_ID` and is what makes `/panel` yours.

## A plain VPS (one command)

On a fresh Debian or Ubuntu server:

```bash
curl -fsSL https://raw.githubusercontent.com/hami9/Astolfo/main/deploy/vps-setup.sh -o setup.sh
sudo bash setup.sh
```

The script is downloaded first rather than piped into bash: it asks for the credentials,
and a piped script has no terminal to read the answers from. For an unattended install,
export `TELEGRAM_BOT_TOKEN` and `OPENROUTER_API_KEY` beforehand and it will not prompt.

What it sets up:

- ffmpeg, `python3-venv` and the Python dependencies, in a virtualenv under `/opt/astolfo`
- a dedicated unprivileged `astolfo` user, added to `systemd-journal` so the panel can show
  the log without the bot needing any privilege of its own
- `/opt/astolfo/.env`, mode 600, owned by root — the only place credentials are written
- `data/`, mode 700, owned by the service user
- a systemd service with `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full` and
  `ProtectHome`, restarting on failure and starting at boot
- the privileged helper and its path unit, which is what makes **update** and **restart**
  work from the panel

Re-run the same script to update to the latest code; it restarts rather than merely
starting, so a re-run never leaves the old process serving new code.

The smallest plan any provider sells is enough: the bot idles at well under 300 MB of RAM
and spends almost all its time waiting on network calls. One shared core and 1 GB of RAM is
comfortable, and on a server that small the script adds a 1 GB swapfile so that apt, pip and
ffmpeg have room to work.

**Pick a region outside Iran.** Telegram's Bot API and most model providers are not reliably
reachable from Iranian IP ranges, and some geo-block them outright, so a European datacenter
(Frankfurt, Amsterdam, Helsinki) is the practical choice even when buying from an Iranian
reseller. Prefer Ubuntu LTS.

### After it starts

Open a private chat with the bot and send `/panel`. Everything else — more keys, the
settings, the groups, the limits, update and restart — happens there rather than over SSH.
See [ADMIN.md](ADMIN.md).

## Docker

Every release publishes an image, so there is nothing to build:

```bash
docker run -d --name astolfo --restart unless-stopped \
  -e TELEGRAM_BOT_TOKEN=... \
  -e OPENROUTER_API_KEY=... \
  -e MASTER_ID=... \
  -e DAILY_BUDGET_USD=1 \
  -v astolfo-data:/data \
  ghcr.io/hami9/astolfo:latest
```

Pin a version (`ghcr.io/hami9/astolfo:2.5.1`) if you would rather decide when to move.
`docker build -t astolfo .` builds the same image from a clone.

With the bundled [docker-compose.yml](../docker-compose.yml), put the credentials in a
`.env` beside it and run `docker compose up -d`. It sets a restart policy, a health check
against the keepalive endpoint, log rotation and the named volume.

The image includes ffmpeg and sets `DATA_DIR=/data`, so the volume carries the database,
the encryption key and the usage history. The panel's **update** and **restart** buttons do
not apply here: pull the new image and recreate the container instead.

## As a package

```bash
pip install "astolfo-bot @ git+https://github.com/hami9/Astolfo@v2.5.1"
astolfo --version
astolfo
```

The console script reads the same environment and `.env` as `python main.py`. Each release
also attaches a built wheel and sdist, so an air-gapped install can take the file rather
than the repository.

## Replit

1. **Create Repl** → *Import from GitHub* → this repository.
2. Add **Secrets** (the lock icon): `TELEGRAM_BOT_TOKEN`, one provider key, and
   `MASTER_ID`. Never put keys in the code or in `.replit`.
3. Press **Run**. `start.sh` installs dependencies on first boot and starts the bot;
   `replit.nix` provides ffmpeg.
4. Keep it alive with a **Reserved VM Deployment** (the reliable option for a 24/7 bot), or
   point an uptime pinger at the Repl URL — the bot serves a small HTTP endpoint on `PORT`
   for exactly that.

A Repl's filesystem is reset on some redeploys, which takes `data/` with it: the database
holds your stored keys, per-group settings and notes, so back it up from **panel → data**
if you are running this way.

## systemd by hand

If you would rather not run the setup script, this is what it configures:

```ini
[Unit]
Description=Astolfo Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=astolfo
WorkingDirectory=/opt/astolfo
EnvironmentFile=/opt/astolfo/.env
ExecStart=/opt/astolfo/.venv/bin/python /opt/astolfo/main.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo apt install ffmpeg python3-venv
sudo systemctl enable --now astolfo
journalctl -u astolfo -f
```

`deploy/astolfo-agent.service` and `deploy/astolfo-agent.path` are the optional second
half: a path unit watching `data/control/request`, and a oneshot root helper that acts on
the one word it finds there. Install them only if you want **update** and **restart** in the
panel; the bot works without them and simply hides those buttons.

## Operating notes

- **One instance only.** Two processes polling the same token fight over updates; Telegram
  returns 409 conflicts. Stop the old instance before starting a new one.
- **Startup log.** The bot logs the resolved model for each role, the services it will draw
  on, and a warning when ffmpeg is missing or a model id is unknown. Check it after the
  first boot.
- **Health check.** `GET /` on `PORT` returns 200 while the process is alive.
- **Backups.** `data/astolfo.db` is the whole state — chats, people, settings, encrypted
  keys and the audit trail — and `data/secret.key` is what decrypts the keys in it.
  `data/usage.json` holds the cost history. **panel → data** downloads the database
  without an SSH session; keep the copy private, and keep `secret.key` out of it if you
  would rather the copy be useless on its own.
- **Rotating a provider key.** Revoke it at the provider, then set the new one from
  **panel → services**. It takes effect on the next message: no file to edit, no restart,
  and the running conversations are not disturbed.
- **Rotating the Telegram token.** `/revoke` in BotFather, then update `.env` and restart —
  this one is read from the environment at startup and is deliberately not settable from
  the panel.
- **Updating.** **panel → server → update**, or on the server:
  ```bash
  sudo -u astolfo git -C /opt/astolfo pull && sudo systemctl restart astolfo
  ```
  An update from the panel fetches, resets to the remote branch, reinstalls requirements
  and restarts; if the new version fails to start within twelve seconds it rolls back to
  the previous commit on its own.
- **Migrations run themselves.** The schema version is checked at startup and upgraded in
  place; new columns are added only if they are missing, so a re-run is harmless. A
  database written by a *newer* version than the code is left untouched with a warning in
  the log rather than migrated backwards — if you roll the code back, expect that line and
  restore a matching backup if anything misbehaves.
