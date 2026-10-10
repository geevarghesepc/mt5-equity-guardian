#!/usr/bin/env bash
# Start the MT5 Guardian in a tmux session named "guardian", as the Wine user.
# Closing the terminal does not stop it. This does not install anything.
#
# Inside the container:
#   bash /config/.wine/drive_c/guardian/deploy/run.sh
# Attach:
#   su -s /bin/bash abc -c "tmux attach -t guardian"
# Detach with Ctrl-b then d. Stop with Ctrl-c inside the session.

set -euo pipefail

WINEPREFIX="${WINEPREFIX:-/config/.wine}"
WINE_USER="${WINE_USER:-abc}"
PY_EXE="${PY_EXE:-C:/Python314/python.exe}"
SESSION="${SESSION:-guardian}"
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP=$(cd "$SCRIPT_DIR/.." && pwd)

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed. As root: apt-get update && apt-get install -y tmux" >&2
  exit 1
fi
if [[ ! -f "$APP/src/guardian_bot.py" ]]; then
  echo "No guardian at $APP. Run deploy/install.sh first." >&2
  exit 1
fi

su -s /bin/bash "$WINE_USER" -c "tmux has-session -t $(printf '%q' "$SESSION") 2>/dev/null" \
  && already=1 || already=0

if [[ "$already" -eq 1 ]]; then
  echo "Session $SESSION is already running."
  echo "Attach: su -s /bin/bash $WINE_USER -c \"tmux attach -t $SESSION\""
  exit 0
fi

mkdir -p "$APP/logs" "$APP/state"
if [[ "$(id -u)" -eq 0 ]]; then
  chown -R "$WINE_USER:$WINE_USER" "$APP/logs" "$APP/state"
  if [[ -f "$APP/config.yaml" ]]; then
    chown "$WINE_USER:$WINE_USER" "$APP/config.yaml"
  fi
fi
LOG="$APP/logs/guardian.log"
CONSOLE_LOG="$APP/logs/console.log"
if [[ -f "$CONSOLE_LOG" ]]; then
  size=$(wc -c < "$CONSOLE_LOG")
  if [[ "$size" -gt 5000000 ]]; then
    mv "$CONSOLE_LOG" "$CONSOLE_LOG.1"
  fi
fi
start_lines=0
if [[ -f "$LOG" ]]; then
  start_lines=$(wc -l < "$LOG")
fi
start_console_lines=0
if [[ -f "$CONSOLE_LOG" ]]; then
  start_console_lines=$(wc -l < "$CONSOLE_LOG")
fi

su -s /bin/bash "$WINE_USER" -c \
  "cd $(printf '%q' "$APP") && tmux new-session -d -s $(printf '%q' "$SESSION") $(printf '%q' "WINEPREFIX=$WINEPREFIX wine $PY_EXE src/guardian_bot.py >>$CONSOLE_LOG 2>&1")"

ready=0
for _ in $(seq 1 60); do
  su -s /bin/bash "$WINE_USER" -c "tmux has-session -t $(printf '%q' "$SESSION") 2>/dev/null" \
    && alive=1 || alive=0
  if [[ "$alive" -eq 0 ]]; then
    echo "The guardian exited and the tmux session closed with it." >&2
    if [[ -f "$CONSOLE_LOG" ]]; then
      echo "Last lines of $CONSOLE_LOG:" >&2
      tail -n 40 "$CONSOLE_LOG" >&2
    elif [[ -f "$LOG" ]]; then
      echo "Last lines of $LOG:" >&2
      tail -n 40 "$LOG" >&2
    fi
    exit 1
  fi
  if [[ -f "$LOG" ]] && tail -n +$((start_lines + 1)) "$LOG" | grep -q "Started in "; then
    ready=1
    break
  fi
  if [[ -f "$CONSOLE_LOG" ]] && tail -n +$((start_console_lines + 1)) "$CONSOLE_LOG" | grep -q "Started in "; then
    ready=1
    break
  fi
  sleep 1
done

if [[ "$ready" -eq 0 ]]; then
  echo "The guardian is still starting after 60 seconds. Last lines of $CONSOLE_LOG:" >&2
  if [[ -f "$CONSOLE_LOG" ]]; then
    tail -n 40 "$CONSOLE_LOG" >&2
  elif [[ -f "$LOG" ]]; then
    tail -n 40 "$LOG" >&2
  fi
  exit 1
fi

echo "Started. Attach: su -s /bin/bash $WINE_USER -c \"tmux attach -t $SESSION\""
echo "Detach with Ctrl-b then d. The guardian keeps running."
echo "Log: $LOG"
