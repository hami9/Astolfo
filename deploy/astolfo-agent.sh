#!/usr/bin/env bash
# The only part of the bot that runs as root.
#
# A systemd path unit starts this whenever the bot writes data/control/request.
# The file may contain exactly one of the words below, and nothing from it is
# ever passed to a command, so a message in Telegram cannot become a shell
# argument. Installed by vps-setup.sh; it is small on purpose, read it before
# trusting it.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/astolfo}"
APP_USER="${APP_USER:-astolfo}"
SERVICE="${SERVICE:-astolfo}"
CONTROL="$APP_DIR/data/control"
REQUEST="$CONTROL/request"
RESULT="$CONTROL/result"

# The tree belongs to the service user; root touching it needs the exception, and
# writes are done as that user so nothing ends up owned by root.
git_root() { git -c safe.directory="$APP_DIR" -C "$APP_DIR" "$@"; }
as_app() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$APP_USER" -- "$@"
  else
    sudo -u "$APP_USER" -- "$@"
  fi
}
git_app() { as_app git -c safe.directory="$APP_DIR" -C "$APP_DIR" "$@"; }

report() {
  printf '%s: %s\n' "$(date -u '+%Y-%m-%d %H:%M UTC')" "$*" > "$RESULT"
  chown "$APP_USER:$APP_USER" "$RESULT" 2>/dev/null || true
  echo "$*"
}

[ -f "$REQUEST" ] || exit 0

# Read a bounded amount and keep only lowercase letters: whatever the file holds,
# what reaches the case below is one short word.
action=$(head -c 32 "$REQUEST" | tr -dc 'a-z')
rm -f "$REQUEST"

case "$action" in
  restart)
    systemctl restart "$SERVICE"
    report "restarted, running $(git_root log --oneline -1)"
    ;;

  update)
    previous=$(git_root rev-parse HEAD)
    branch=$(git_root rev-parse --abbrev-ref HEAD)

    if ! git_app fetch --quiet origin "$branch"; then
      report "update failed: could not reach the remote"
      exit 0
    fi
    if [ "$(git_root rev-list --count "HEAD..origin/$branch")" = "0" ]; then
      report "already up to date at $(git_root log --oneline -1)"
      exit 0
    fi

    git_app reset --quiet --hard "origin/$branch"
    as_app "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
    systemctl restart "$SERVICE"

    # A version that cannot start would leave the owner with no way back in, so
    # the server checks and undoes it without being asked.
    sleep 12
    if systemctl is-active --quiet "$SERVICE"; then
      report "updated to $(git_root log --oneline -1)"
    else
      git_app reset --quiet --hard "$previous"
      as_app "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
      systemctl restart "$SERVICE"
      report "the new version did not start; rolled back to $(git_root log --oneline -1)"
    fi
    ;;

  *)
    report "ignored an unknown request"
    ;;
esac
