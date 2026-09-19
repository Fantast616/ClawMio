#!/usr/bin/env bash
# Linux launcher. Never source .env: dotenv values are data, not shell commands.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT"
VENV="$ROOT/.venv-linux"
RUN="$ROOT/data/run"
PIDFILE="$RUN/server.pid"
LOG="$ROOT/logs/server.log"
ACTION="${1:-run}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
PYTHON="${PYTHON:-python3}"

fail() { printf '%s\n' "$*" >&2; exit 1; }
help_text() {
  cat <<'HELP'
Usage: bash start.sh [run|start|stop|restart|status|logs|install]
  run      Install if needed, then run in foreground (default)
  start    Install if needed, then run in background
  stop     Gracefully stop this project's server
  restart  Stop, then start in background
  status   Show running state
  logs     Follow logs/server.log
  install  Create Linux venv and install/update dependencies

Options: HOST=127.0.0.1 PORT=8000 PYTHON=python3.11 bash start.sh start
Requires Linux, Python >= 3.11 with venv, and flock (util-linux).
Configure credentials and admin password in .env before starting.
HELP
}

running() {
  [[ -r "$PIDFILE" ]] || return 1
  read -r SERVER_PID < "$PIDFILE" || return 1
  [[ "$SERVER_PID" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 "$SERVER_PID" 2>/dev/null || return 1
  # A stale PID must never cause an unrelated process to be stopped.
  [[ "$(readlink -f "/proc/$SERVER_PID/cwd" 2>/dev/null || true)" == "$ROOT" ]] || return 1
  [[ -r "/proc/$SERVER_PID/cmdline" ]] || return 1
  local command_line
  command_line="$(tr '\0' ' ' < "/proc/$SERVER_PID/cmdline")"
  [[ "$command_line" == *"$VENV/bin/python -m uvicorn app.main:app "* ]]
}

stop_server() {
  if ! running; then
    printf '%s\n' 'Server is not running (or PID does not belong to this launcher).'
    return
  fi
  local pid="$SERVER_PID"
  kill -TERM "$pid"
  for ((i=0; i<30; i++)); do
    if ! running; then
      rm -f -- "$PIDFILE"
      printf '%s\n' 'Server stopped.'
      return
    fi
    sleep 1
  done
  fail "Server is still shutting down (PID $pid). Check logs; no forced kill was performed."
}

case "$ACTION" in
  -h|--help|help) help_text; exit 0 ;;
  status)
    if running; then printf 'Running: PID %s\n' "$SERVER_PID"; else printf '%s\n' 'Not running.'; fi
    exit 0 ;;
  logs) [[ -f "$LOG" ]] || fail 'No log file yet. Start with: bash start.sh start'; exec tail -n 100 -f "$LOG" ;;
  stop) stop_server; exit 0 ;;
  restart) stop_server ;;
  run|start|install) ;;
  *) help_text; exit 2 ;;
esac

[[ "$(uname -s)" == Linux ]] || fail 'This script is for Linux. Use start.ps1 on Windows.'
command -v "$PYTHON" >/dev/null || fail "Python not found: $PYTHON"
command -v flock >/dev/null || fail 'Install util-linux to provide flock.'
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else "Python 3.11+ is required")'
mkdir -p -- "$RUN" "$ROOT/logs"
# Inherited by uvicorn, preventing a second instance of this launcher.
exec 9>"$RUN/server.lock"
flock -n 9 || fail 'Another server or installation is active for this project.'

if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON" -m venv "$VENV" || fail 'Cannot create venv. On Ubuntu/Debian install python3-venv.'
fi
REQ_HASH="$("$VENV/bin/python" -c 'import hashlib; print(hashlib.sha256(open("requirements.txt","rb").read()).hexdigest())')"
OLD_HASH="$(cat "$VENV/.requirements.sha256" 2>/dev/null || true)"
if [[ "$ACTION" == install || "$REQ_HASH" != "$OLD_HASH" ]]; then
  "$VENV/bin/python" -m pip install -r "$ROOT/requirements.txt"
  printf '%s\n' "$REQ_HASH" > "$VENV/.requirements.sha256"
fi
if [[ "$ACTION" == install ]]; then
  printf '%s\n' 'Dependencies installed. Configure .env, then run: bash start.sh start'
  exit 0
fi

if [[ ! -f "$ROOT/.env" ]]; then
  cp -- "$ROOT/.env.example" "$ROOT/.env"
  fail '.env created from template. Fill API key, workspace, ADMIN_PASSWORD and COOKIE_SECRET, then rerun.'
fi
chmod 600 "$ROOT/.env"
"$VENV/bin/python" - <<'PY'
from pathlib import Path
from dotenv import load_dotenv
import os
load_dotenv(Path.cwd()/'.env')
required = ('DASHSCOPE_API_KEY','BAILIAN_WORKSPACE_ID','ADMIN_PASSWORD','COOKIE_SECRET')
missing = [key for key in required if not os.getenv(key) or os.environ[key].startswith('replace-with-')]
if missing:
    raise SystemExit('Configure these .env entries: '+', '.join(missing))
path = os.getenv('DATABASE_PATH','data/claw.db')
if len(path)>1 and path[1]==':':
    raise SystemExit('DATABASE_PATH is a Windows path. Use data/claw.db or a Linux absolute path.')
Path(path).parent.mkdir(parents=True,exist_ok=True)
PY
[[ "$PORT" =~ ^[0-9]{1,5}$ ]] || fail 'PORT must be an integer from 1 to 65535.'
PORT="$((10#$PORT))"
((PORT>=1 && PORT<=65535)) || fail 'PORT must be from 1 to 65535.'

ARGS=(-m uvicorn app.main:app --host "$HOST" --port "$PORT" --workers 1 --no-access-log)
if [[ "$ACTION" == run ]]; then
  printf '%s\n' "$$" > "$PIDFILE"
  printf 'Starting on %s:%s (Ctrl+C to stop).\n' "$HOST" "$PORT"
  exec "$VENV/bin/python" "${ARGS[@]}"
fi

nohup "$VENV/bin/python" "${ARGS[@]}" >> "$LOG" 2>&1 < /dev/null &
SERVER_PID=$!
printf '%s\n' "$SERVER_PID" > "$PIDFILE"
# Wait for startup without printing secrets or assuming process existence is health.
CHECK_HOST="$HOST"
[[ "$CHECK_HOST" == 0.0.0.0 ]] && CHECK_HOST=127.0.0.1
[[ "$CHECK_HOST" == :: ]] && CHECK_HOST=::1
for ((i=0; i<20; i++)); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    fail "Startup failed. Check: $LOG"
  fi
  if "$VENV/bin/python" - "$CHECK_HOST" "$PORT" <<'PY'
import sys, urllib.request
host=sys.argv[1]
if ':' in host: host='['+host+']'
try:
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://{host}:{sys.argv[2]}/',timeout=1) as r:
        sys.exit(0 if r.status==200 else 1)
except Exception:
    sys.exit(1)
PY
  then
    if running; then
      printf 'Started: PID %s, listen %s:%s\nLog: %s\n' "$SERVER_PID" "$HOST" "$PORT" "$LOG"
      exit 0
    fi
  fi
  sleep 1
done
fail "Server process started but readiness is unconfirmed. Check: $LOG"
