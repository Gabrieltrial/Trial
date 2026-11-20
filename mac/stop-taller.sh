#!/bin/bash
# El fa servir Taller musical.app en tancar-se: atura el servidor que va engegar (si encara funciona).
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/.taller-musical.pid"
if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE")"
  if ps -p "$PID" -o command= 2>/dev/null | grep -q "practice-player-server.py"; then
    kill "$PID" 2>/dev/null
  fi
  rm -f "$PIDFILE"
fi
exit 0
