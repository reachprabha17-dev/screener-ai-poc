#!/usr/bin/env bash
#
# Local development runner (spec §22, §24.6).
#
# One script instead of three remembered command lines, because the *order*
# matters and getting it wrong is silent rather than loud:
#
#   - the worker stops first and starts last. It is the only process that writes
#     candidate results, so stopping it first means no job is half-written while
#     the code underneath changes, and starting it last means the API has already
#     passed the migration gate (§12.1).
#   - the schema is migrated before anything serves. The API refuses to start
#     against a stale schema; the worker does not, and would happily judge
#     candidates against one.
#
# PID files, not `pkill -f`. A `pkill -f 'Screener/...'` matches the shell that
# is running it whenever the pattern appears in that shell's own command line —
# it has killed this repo's operator twice (exit 144), leaving a stopped worker
# and a terminal that looked like it had crashed.
#
# Usage:
#   scripts/dev.sh start [api|worker|ui]
#   scripts/dev.sh stop  [api|worker|ui]
#   scripts/dev.sh restart [api|worker|ui]
#   scripts/dev.sh status
#   scripts/dev.sh logs [api|worker|ui]
#   scripts/dev.sh check          # the four gates, same as CI
#
# RELOAD=1 scripts/dev.sh start api   — uvicorn watches the source tree. Handy
# while editing routes or prompts; never used for measurement, because a reload
# mid-run changes the code a batch is running under without changing app_version.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv/bin"
RUN_DIR="$ROOT/data/run"
LOG_DIR="$ROOT/data/logs"

# 8010, not 8000: `onprem-rag` already holds 8000 on this host, and the failure
# mode of a clash is a UI that talks to the wrong service rather than an error.
API_PORT="${SCREENER_API_PORT:-8010}"
UI_PORT="${SCREENER_UI_PORT:-8501}"
API_URL="http://127.0.0.1:${API_PORT}"

SERVICES=(api worker ui)

mkdir -p "$RUN_DIR" "$LOG_DIR"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# Logs are appended, never truncated. A process that dies seconds after starting
# leaves its only explanation in this file, and the next `restart` would erase
# exactly the evidence needed to explain the restart.
banner() { printf '\n==== %s started %s ====\n' "$1" "$(date -Is)" >>"$(log_file "$1")"; }

pid_file() { echo "$RUN_DIR/$1.pid"; }
log_file() { echo "$LOG_DIR/dev-$1.log"; }

# Connectable means occupied. Checked because a pid file can be missing while
# the port is not — a process started by hand, or one this script lost track of.
# Without it, the second uvicorn fails inside a redirected log and `start` looks
# like it worked while the UI talks to whichever server got there first.
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1; }

pid_of() {
  local file; file="$(pid_file "$1")"
  [[ -f "$file" ]] || return 1
  local pid; pid="$(cat "$file")"
  # A stale pid file outlives a crash. Verify the process is really ours before
  # reporting it up, or `restart` waits forever on a pid the kernel reused.
  if kill -0 "$pid" 2>/dev/null; then echo "$pid"; else rm -f "$file"; return 1; fi
}

# --- start -------------------------------------------------------------------

start_api() {
  pid_of api >/dev/null && { warn "api already running (pid $(pid_of api))"; return; }
  port_busy "$API_PORT" && die "port $API_PORT is already in use by something this script did not start"
  local reload=()
  [[ "${RELOAD:-0}" == "1" ]] && reload=(--reload)
  banner api
  nohup "$VENV/uvicorn" screener.api.app:create_app --factory \
      --host 127.0.0.1 --port "$API_PORT" "${reload[@]}" \
      >>"$(log_file api)" 2>&1 &
  echo $! >"$(pid_file api)"
  wait_for_health
}

start_worker() {
  pid_of worker >/dev/null && { warn "worker already running (pid $(pid_of worker))"; return; }
  banner worker
  nohup "$VENV/python" "$ROOT/worker.py" >>"$(log_file worker)" 2>&1 &
  echo $! >"$(pid_file worker)"
  say "worker  started (pid $(cat "$(pid_file worker)"))"
}

start_ui() {
  pid_of ui >/dev/null && { warn "ui already running (pid $(pid_of ui))"; return; }
  port_busy "$UI_PORT" && die "port $UI_PORT is already in use by something this script did not start"
  banner ui
  # Streamlit resolves .streamlit/config.toml from the working directory, so the
  # settings that matter are also passed as env vars (§22.2, decision #1).
  SCREENER_API_URL="$API_URL" \
  STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
  STREAMLIT_SERVER_HEADLESS=true \
  STREAMLIT_SERVER_PORT="$UI_PORT" \
    nohup "$VENV/streamlit" run "$ROOT/ui/screener_app.py" \
      >>"$(log_file ui)" 2>&1 &
  echo $! >"$(pid_file ui)"
  say "ui      started → http://127.0.0.1:${UI_PORT}"
}

wait_for_health() {
  local pid; pid="$(cat "$(pid_file api)")"
  for _ in $(seq 1 40); do
    if curl -fsS "${API_URL}/health" >/dev/null 2>&1; then
      say "api     started → ${API_URL}  (docs at ${API_URL}/docs)"
      return
    fi
    kill -0 "$pid" 2>/dev/null || die "api died on startup — see $(log_file api)"
    sleep 0.5
  done
  die "api did not answer /health in 20s — see $(log_file api)"
}

migrate() {
  "$VENV/screener" migrate >/dev/null || die "migration failed"
}

# --- stop --------------------------------------------------------------------

stop_one() {
  local name="$1" pid
  pid="$(pid_of "$name")" || { printf '%-8s not running\n' "$name"; return; }

  # SIGTERM only. The worker's handler finishes the resume in hand and then
  # exits; SIGKILL mid-job leaves the row `in_progress` until startup reclaim
  # ages it out and the work is redone.
  kill -TERM "$pid" 2>/dev/null || true

  # 120s matches TimeoutStopSec in deploy/screener-worker.service — one full
  # screen is ~5s of inference plus a parse, and the queue may be mid-batch.
  local deadline=$(( SECONDS + 120 ))
  while kill -0 "$pid" 2>/dev/null; do
    (( SECONDS < deadline )) || die "$name did not stop within 120s (pid $pid)"
    sleep 0.5
  done
  rm -f "$(pid_file "$name")"
  printf '%-8s stopped\n' "$name"
}

# --- commands ----------------------------------------------------------------

cmd_start() {
  local only="${1:-}"
  [[ -x "$VENV/uvicorn" ]] || die "no venv at $VENV — run: uv sync"
  if [[ -z "$only" ]]; then
    migrate
    start_api; start_worker; start_ui
  else
    [[ "$only" == "api" ]] && migrate
    "start_$only"
  fi
}

cmd_stop() {
  local only="${1:-}"
  if [[ -z "$only" ]]; then
    # Reverse of start: the writer goes down before the thing that feeds it.
    stop_one worker; stop_one ui; stop_one api
  else
    stop_one "$only"
  fi
}

cmd_status() {
  for name in "${SERVICES[@]}"; do
    if pid="$(pid_of "$name")"; then
      printf '%-8s running  pid %-8s %s\n' "$name" "$pid" "$(log_file "$name")"
    else
      printf '%-8s \033[33mstopped\033[0m\n' "$name"
    fi
  done
  echo
  "$VENV/screener" health || true
}

cmd_logs() {
  local name="${1:-worker}"
  tail -f "$(log_file "$name")"
}

cmd_check() {
  # The same four gates the build order runs at every step (§20). Ordered
  # cheapest-first so a formatting slip does not cost a full test run.
  "$VENV/ruff" format --check .
  "$VENV/ruff" check .
  # No path argument: a path overrides `files` in pyproject.toml, and `mypy .`
  # then walks `ui/` twice under two module names and fails before checking
  # anything real.
  "$VENV/mypy"
  "$VENV/pytest" -q
}

case "${1:-}" in
  start)   shift; cmd_start "${1:-}" ;;
  stop)    shift; cmd_stop  "${1:-}" ;;
  restart) shift; cmd_stop "${1:-}"; cmd_start "${1:-}" ;;
  status)  cmd_status ;;
  logs)    shift; cmd_logs "${1:-}" ;;
  check)   cmd_check ;;
  *) die "usage: $0 {start|stop|restart|status|logs|check} [api|worker|ui]" ;;
esac
