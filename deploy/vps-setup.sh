#!/usr/bin/env bash
# One-command install on a fresh Debian/Ubuntu server.
#
#   curl -fsSL https://raw.githubusercontent.com/hami9/Astolfo/main/deploy/vps-setup.sh | sudo bash
#
# Installs the bot under /opt/astolfo, runs it as a dedicated user, and keeps it
# alive with systemd. Credentials are asked for interactively and written to a
# root-only environment file; they are never stored in the repository.
set -euo pipefail

REPO="${REPO:-https://github.com/hami9/Astolfo}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/astolfo}"
APP_USER="${APP_USER:-astolfo}"
ENV_FILE="$APP_DIR/.env"
SERVICE="/etc/systemd/system/astolfo.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "run this as root (use sudo)" >&2
  exit 1
fi

# When the script is piped in (curl ... | bash) stdin is the pipe, so prompts
# would read the script itself instead of the keyboard. Reattach the terminal.
# Probe first: /dev/tty exists as a node even when no controlling terminal is
# attached, and a failed exec redirection kills a non-interactive shell.
if [ ! -t 0 ] && { : < /dev/tty; } 2>/dev/null; then
  exec < /dev/tty
fi

echo "==> installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git ffmpeg python3 python3-venv python3-pip ca-certificates

# A 1 GB box has little headroom once apt, pip and ffmpeg run; a small swapfile
# keeps the OOM killer away. Containerised VPS types often forbid this, so any
# failure here is reported and ignored rather than aborting the install.
RAM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$RAM_MB" -lt 2048 ] && [ -z "$(swapon --show --noheadings 2>/dev/null)" ]; then
  echo "==> adding a 1 GB swapfile (detected ${RAM_MB} MB of RAM)"
  if { fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none; } \
     && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile; then
    grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
  else
    rm -f /swapfile
    echo "    could not enable swap on this server type, continuing without it"
  fi
fi

echo "==> creating service user"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> fetching the code"
# The tree belongs to the service user, so git run as root refuses it as
# "dubious ownership". Trust it for these commands only, no global config.
GIT="git -c safe.directory=$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  $GIT -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  $GIT -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
else
  rm -rf "$APP_DIR"
  $GIT clone --quiet --branch "$BRANCH" --depth 1 "$REPO" "$APP_DIR"
fi

echo "==> installing python dependencies"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
  echo "==> credentials"
  # Already-exported values win, which makes an unattended install possible.
  TG_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
  OR_KEY="${OPENROUTER_API_KEY:-}"
  LANG_CHOICE="${BOT_LANG:-}"
  BUDGET="${DAILY_BUDGET_USD:-}"

  # Prompting is only safe on a real terminal. Piping the script into bash makes
  # stdin the script itself, and `read` would silently consume the next line of
  # source as the answer, so refuse instead of storing garbage.
  if { [ -z "$TG_TOKEN" ] || [ -z "$OR_KEY" ]; } && [ ! -t 0 ]; then
    echo "cannot ask for credentials: stdin is not a terminal." >&2
    echo "download the script and run it, rather than piping it:" >&2
    echo "  curl -fsSL $REPO/raw/$BRANCH/deploy/vps-setup.sh -o setup.sh && bash setup.sh" >&2
    echo "or export TELEGRAM_BOT_TOKEN and OPENROUTER_API_KEY before running it." >&2
    exit 1
  fi

  while [ -z "$TG_TOKEN" ]; do read -rp "TELEGRAM_BOT_TOKEN: " TG_TOKEN || exit 1; done
  while [ -z "$OR_KEY" ]; do read -rp "OPENROUTER_API_KEY: " OR_KEY || exit 1; done
  [ -n "$LANG_CHOICE" ] || read -rp "command language [fa/en] (default fa): " LANG_CHOICE || true
  [ -n "$BUDGET" ] || read -rp "daily budget in USD, 0 for unlimited (default 0): " BUDGET || true

  cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=$TG_TOKEN
OPENROUTER_API_KEY=$OR_KEY
BOT_LANG=${LANG_CHOICE:-fa}
DAILY_BUDGET_USD=${BUDGET:-0}
DATA_DIR=$APP_DIR/data
KEEPALIVE=0
EOF
else
  echo "==> keeping the existing environment file"
fi

chmod 600 "$ENV_FILE"
mkdir -p "$APP_DIR/data/control"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 700 "$APP_DIR/data"

# The panel shows the service log. Reading the journal needs group membership,
# which is a good deal cheaper than running the bot with more privileges.
if getent group systemd-journal >/dev/null; then
  usermod -aG systemd-journal "$APP_USER"
fi

echo "==> installing the systemd service"
cat > "$SERVICE" <<EOF
[Unit]
Description=Astolfo Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/main.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

echo "==> installing the privileged helper"
# The bot cannot restart or update itself: it runs unprivileged, and it stays
# that way. It leaves one word in data/control/request and this helper, started
# by the path unit below, is the only thing that acts on it.
chmod 755 "$APP_DIR/deploy/astolfo-agent.sh"
sed "s#/opt/astolfo#$APP_DIR#g" "$APP_DIR/deploy/astolfo-agent.service" \
  > /etc/systemd/system/astolfo-agent.service
sed "s#/opt/astolfo#$APP_DIR#g" "$APP_DIR/deploy/astolfo-agent.path" \
  > /etc/systemd/system/astolfo-agent.path

systemctl daemon-reload
systemctl enable --now astolfo-agent.path
systemctl enable astolfo
# restart, not `enable --now`: --now only starts a stopped service, so re-running
# this script would pull new code and leave the old process serving it.
systemctl restart astolfo
sleep 3

echo
echo "==> status"
systemctl --no-pager --lines=15 status astolfo || true
if ! systemctl is-active --quiet astolfo; then
  echo
  echo "the service is not running; see: journalctl -u astolfo -n 50 --no-pager" >&2
  exit 1
fi
echo "running $($GIT -C "$APP_DIR" log --oneline -1)"
echo
echo "Done. Useful commands:"
echo "  journalctl -u astolfo -f      # follow the log"
echo "  systemctl restart astolfo     # restart"
echo "  bash $APP_DIR/deploy/vps-setup.sh   # re-run to update to the latest code"
