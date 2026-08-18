# Deployment

The bot uses long polling, so it needs no public URL, no webhook and no TLS certificate.
It does need a process that stays running.

## Before anything else

1. Create the bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. **Turn off Group Privacy**: `/mybots` → your bot → *Bot Settings* → *Group Privacy* →
   *Turn off*. Without this Telegram only forwards commands and replies, so the bot cannot
   see the conversation. Remove and re-add the bot to existing groups afterwards.
3. Create an API key at [openrouter.ai](https://openrouter.ai/keys) and add credit.

## Replit

1. **Create Repl** → *Import from GitHub* → this repository.
2. Add two **Secrets** (the lock icon): `TELEGRAM_BOT_TOKEN` and `OPENROUTER_API_KEY`.
   Never put keys in the code or in `.replit`.
3. Press **Run**. `start.sh` installs dependencies on first boot and starts the bot;
   `replit.nix` provides ffmpeg.
4. Keep it alive with a **Reserved VM Deployment** (the reliable option for a 24/7 bot),
   or point an uptime pinger at the Repl URL — the bot serves a small HTTP endpoint on
   `PORT` for exactly that.

Note that a Repl's filesystem is reset on some redeploys; `data/` holds only settings,
notes and usage history, so losing it costs nothing except accumulated budget counters.

## A plain VPS (one command)

On a fresh Debian or Ubuntu server:

```bash
curl -fsSL https://raw.githubusercontent.com/hami9/Astolfo/main/deploy/vps-setup.sh | sudo bash
```

It installs ffmpeg and the Python dependencies, creates a dedicated service user,
asks for the two credentials, writes them to a root-only environment file, and
registers a systemd service that restarts on failure and starts at boot. Re-run the
same script to update to the latest code.

The smallest plan any provider sells is enough: the bot idles at well under 300 MB of
RAM and spends almost all its time waiting on network calls. One shared core and 1 GB
of RAM is comfortable.

**Pick a region outside Iran.** Telegram's Bot API and OpenRouter are not reliably
reachable from Iranian IP ranges, and OpenRouter geo-blocks some of them outright, so a
European datacenter (Frankfurt, Amsterdam, Helsinki) is the practical choice even when
buying from an Iranian reseller. Prefer Ubuntu LTS: `ffmpeg` and `python3-venv` are one
`apt install` away.

## Docker

```bash
docker build -t astolfo .
docker run -d --name astolfo --restart unless-stopped \
  -e TELEGRAM_BOT_TOKEN=... \
  -e OPENROUTER_API_KEY=... \
  -e DAILY_BUDGET_USD=1 \
  -v astolfo-data:/data \
  astolfo
```

The image includes ffmpeg and writes state to the `/data` volume.

## systemd by hand

If you would rather not run the setup script, this is what it configures:

```ini
[Unit]
Description=Astolfo Telegram bot
After=network-online.target

[Service]
WorkingDirectory=/opt/astolfo
EnvironmentFile=/opt/astolfo/.env
ExecStart=/opt/astolfo/.venv/bin/python main.py
Restart=always
RestartSec=5
User=astolfo

[Install]
WantedBy=multi-user.target
```

```bash
sudo apt install ffmpeg
sudo systemctl enable --now astolfo
journalctl -u astolfo -f
```

## Operating notes

- **One instance only.** Two processes polling the same token fight over updates; Telegram
  returns 409 conflicts. Stop the old instance before starting a new one.
- **Startup log.** The bot logs the resolved model for each role and warns when ffmpeg is
  missing or a model id is unknown. Check it after the first boot.
- **Health check.** `GET /` on `PORT` returns 200 while the process is alive.
- **Backups.** `data/state.json` (settings and notes) and `data/usage.json` (cost history)
  are the only stateful files.
- **Key rotation.** Revoke the Telegram token with `/revoke` in BotFather and delete the
  OpenRouter key in its dashboard; both are read from the environment at startup, so
  rotating means updating the secret and restarting.
