#!/bin/bash
# El fa servir Taller musical.app en tancar-se: atura el servidor que va engegar.
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/.taller-musical.pid"
if [ -f "$PIDFILE" ]; then
  kill "$(cat "$PIDFILE")" 2>/dev/null
  rm -f "$PIDFILE"
fi
exit 0
