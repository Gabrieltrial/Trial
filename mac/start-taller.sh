#!/bin/bash
# El fa servir Taller musical.app: actualitza els fitxers i engega el servidor en segon pla.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
export PATH="$HOME/.practice-player-venv/bin:/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"
PORT=8765
REPO="https://raw.githubusercontent.com/Gabrieltrial/Trial/main"

ping() { curl -fs --max-time 2 "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1; }

# Ja funciona (per exemple, des de l'app o des de Taller_musical.command): no cal fer res més.
if ping; then exit 0; fi

# Versions més noves de GitHub (si no hi ha internet, fa servir les que ja hi ha).
for f in index.html practice-player-server.py mac/start-taller.sh mac/stop-taller.sh; do
  local_name="$(basename "$f")"
  if curl -fsSL --max-time 30 "$REPO/$f" -o "$local_name.new" 2>/dev/null && [ -s "$local_name.new" ]; then
    mv "$local_name.new" "$local_name"
  else
    rm -f "$local_name.new"
  fi
done
for f in index.html practice-player-server.py; do
  if [ ! -f "$f" ]; then echo "No s'ha pogut baixar $f. Comprova la connexió a internet."; exit 1; fi
done

PY="$HOME/.practice-player-venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
if [ -z "$PY" ]; then echo "No s'ha trobat Python 3. Instal·la'l des de https://www.python.org/downloads/"; exit 1; fi

nohup "$PY" practice-player-server.py --port "$PORT" --no-browser --no-install-prompt > taller-musical.log 2>&1 &
echo $! > .taller-musical.pid

for _ in $(seq 1 40); do
  if ping; then exit 0; fi
  sleep 0.5
done
echo "El servidor no ha arrencat. Mira el fitxer taller-musical.log de la carpeta $DIR"
exit 1
