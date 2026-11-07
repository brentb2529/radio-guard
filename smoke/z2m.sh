#!/usr/bin/env bash
# Start/stop exactly one simulated Zigbee2MQTT ("the container"), like `docker start/stop zigbee2mqtt`.
#   ./z2m.sh start <data_dir>   boots the stick named by serial.port tcp://<stick>:6638 in <data_dir>/configuration.yaml
#   ./z2m.sh stop               clean stop: publishes bridge/state offline, like Z2M does
#   ./z2m.sh status
set -euo pipefail
cd "$(dirname "$0")"
PIDFILE=.fake_z2m.pid
LOG=${FAKE_Z2M_LOG:-.fake_z2m.log}

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    if running; then echo "already running (pid $(cat "$PIDFILE"))"; exit 1; fi
    if pgrep -f "fake_z2m.py" >/dev/null; then echo "a stray fake_z2m.py is running; stop it first"; exit 1; fi
    FAKE_DATA_DIR="${2:?data dir}" nohup ../.venv/bin/python fake_z2m.py >>"$LOG" 2>&1 &
    echo $! >"$PIDFILE"
    sleep 2
    grep "Started with result" "$LOG" | tail -1
    ;;
  stop)
    if running; then kill -TERM "$(cat "$PIDFILE")"; for _ in $(seq 1 20); do running || break; sleep 0.2; done; fi
    rm -f "$PIDFILE"
    if pgrep -f "fake_z2m.py" >/dev/null; then echo "WARNING: stray fake_z2m.py still running"; exit 1; fi
    echo stopped
    ;;
  status)
    running && echo "running (pid $(cat "$PIDFILE"))" || echo stopped
    ;;
  *) echo "usage: $0 start <data_dir> | stop | status"; exit 2 ;;
esac
