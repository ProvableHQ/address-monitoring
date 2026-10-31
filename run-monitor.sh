#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$ROOT/run-monitor.sh"
PYTHON="$ROOT/.venv/bin/python"
STATE_DIR="${MONITOR_STATE_DIR:-$ROOT/.runtime}"
PID_FILE="$STATE_DIR/monitor.pid"
LOG_FILE="$STATE_DIR/monitor.log"
OUTPUT_FILE="$STATE_DIR/latest.json"

usage() {
    cat <<'EOF'
Usage: ./run-monitor.sh COMMAND [OPTIONS]

  run [PYTHON_ARGS...]                  Run once in the foreground; print JSON.
  start [--interval SECONDS | --once] [ARGS...]
                                       Poll in the background (default: 10s).
  status                               Show process state and last exit code.
  stop                                 Stop the background process.
  logs                                 Follow the background log (Ctrl-C to exit).
  alerts                               Show the latest saved sender alert.
  test-alert                           Show a local test popup and sound.
  test-slack                           Send a labeled Slack test message.

Examples:
  ./run-monitor.sh run --limit 100
  ./run-monitor.sh start --limit 100
  ./run-monitor.sh start --once --limit 100
  ./run-monitor.sh start --interval 10 --limit 100

start fetches repeatedly, waiting 10 seconds after each attempt by default.
Use --once to fetch one page and exit. Background output goes
to .runtime/latest.json; errors and progress go to .runtime/monitor.log.
MONITOR_STATE_DIR overrides the runtime directory. Python arguments are passed
through unchanged. The virtual environment and PyYAML are set up if needed.
EOF
}

setup() {
    if [[ ! -x "$PYTHON" ]]; then
        "${PYTHON_BIN:-python3}" -m venv "$ROOT/.venv"
    fi
    if ! "$PYTHON" -c 'import yaml' >/dev/null 2>&1; then
        "$PYTHON" -m pip install -r "$ROOT/requirements.txt" >&2
    fi
}

running() {
    [[ -f "$PID_FILE" ]] || return 1
    IFS= read -r pid < "$PID_FILE" || return 1
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    # Check identity as well as liveness before sending signals to a saved PID.
    local command
    command="$(ps -ww -p "$pid" -o args= 2>/dev/null)" || return 1
    [[ "$command" == *"$SCRIPT __worker"* ]]
}

worker() {
    local interval="$1" child="" temp_file="$STATE_DIR/latest.$$.tmp" code=0
    shift
    cleanup() {
        if [[ -n "$child" ]]; then
            kill "$child" 2>/dev/null || true
            wait "$child" 2>/dev/null || true
        fi
        rm -f -- "$temp_file"
        printf '%s\n' "$code" > "$STATE_DIR/last-exit-code"
    }
    trap cleanup EXIT
    trap 'code=143; exit 143' TERM
    trap 'code=130; exit 130' INT
    while :; do
        printf '[%s] Fetching transactions\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        "$PYTHON" -u "$ROOT/address-monitoring.py" \
            --registry-cache-dir "$STATE_DIR/registry" \
            --alert-state-dir "$STATE_DIR" "$@" > "$temp_file" &
        child=$!
        if wait "$child"; then
            code=0
            mv -f -- "$temp_file" "$OUTPUT_FILE"
            printf '[%s] Updated %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$OUTPUT_FILE"
        else
            code=$?
            printf '[%s] Fetch failed (exit %s); previous output retained\n' \
                "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$code"
        fi
        child=""
        printf '%s\n' "$code" > "$STATE_DIR/last-exit-code"
        [[ "$interval" != 0 ]] || exit "$code"
        sleep "$interval" &
        child=$!
        wait "$child"
        child=""
    done
}

action="${1:-help}"
[[ $# -eq 0 ]] || shift
case "$action" in
    help|-h|--help) usage ;;
    run)
        setup
        exec "$PYTHON" -u "$ROOT/address-monitoring.py" \
            --registry-cache-dir "$STATE_DIR/registry" --alert-state-dir "$STATE_DIR" "$@"
        ;;
    start)
        interval=10
        if [[ "${1:-}" == --once ]]; then
            interval=0
            shift
        elif [[ "${1:-}" == --interval ]]; then
            interval="${2:-}"
            if [[ ! "$interval" =~ ^[1-9][0-9]*$ ]]; then
                echo '--interval requires a positive integer number of seconds' >&2
                exit 2
            fi
            shift 2
        fi
        [[ "${1:-}" != -- ]] || shift
        mkdir -p -- "$STATE_DIR"
        if ! mkdir -- "$STATE_DIR/start.lock" 2>/dev/null; then
            echo "Another start is in progress (lock: $STATE_DIR/start.lock)" >&2
            exit 1
        fi
        trap 'rmdir -- "$STATE_DIR/start.lock"' EXIT
        if running; then
            echo "Already running (PID $pid)"
            exit 0
        fi
        setup
        rm -f -- "$STATE_DIR/last-exit-code"
        nohup bash "$SCRIPT" __worker "$interval" "$@" >> "$LOG_FILE" 2>&1 < /dev/null &
        printf '%s\n' "$!" > "$PID_FILE"
        echo "Started PID $!"
        echo "Output: $OUTPUT_FILE"
        echo "Log: $LOG_FILE"
        ;;
    __worker) worker "$@" ;;
    test-alert)
        setup
        exec "$PYTHON" -u "$ROOT/address-monitoring.py" --test-alert
        ;;
    test-slack)
        setup
        exec "$PYTHON" -u "$ROOT/address-monitoring.py" --test-slack
        ;;
    alerts)
        if [[ -f "$STATE_DIR/latest-alert.txt" ]]; then
            cat -- "$STATE_DIR/latest-alert.txt"
        else
            echo 'No saved sender alerts'
        fi
        ;;
    status)
        if running; then
            echo "Running (PID $pid)"
        else
            echo 'Stopped'
        fi
        if [[ -f "$STATE_DIR/last-exit-code" ]]; then
            printf 'Last exit code: '
            cat -- "$STATE_DIR/last-exit-code"
        fi
        ;;
    stop)
        if running; then
            kill -TERM "$pid"
            echo "Stop requested (PID $pid)"
            for ((attempt = 0; attempt < 50; attempt++)); do
                if ! running; then
                    echo 'Stopped'
                    exit 0
                fi
                sleep 0.1
            done
            echo 'Process is still stopping; check status before starting again.' >&2
            exit 1
        else
            echo 'Already stopped'
        fi
        ;;
    logs)
        mkdir -p -- "$STATE_DIR"
        touch -- "$LOG_FILE"
        exec tail -n 50 -f "$LOG_FILE"
        ;;
    *) usage >&2; exit 2 ;;
esac
