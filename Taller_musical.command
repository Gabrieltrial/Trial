#!/bin/bash
# Engega el Taller musical (tots els programes en una sola aplicació)
# amb el servidor que permet cercar a YouTube des del Practice Player.
cd "$(dirname "$0")"

# 1. Baixa de GitHub les versions més noves (si no hi ha internet, fa servir les que ja hi ha).
REPO="https://raw.githubusercontent.com/Gabrieltrial/Trial/main"
for f in index.html practice-player-server.py; do
  if curl -fsSL --max-time 30 "$REPO/$f" -o "$f.new" 2>/dev/null && [ -s "$f.new" ]; then
    mv "$f.new" "$f"
  else
    rm -f "$f.new"
    if [ ! -f "$f" ]; then
      echo "No s'ha pogut baixar $f i no hi és a la carpeta. Comprova la connexió a internet."
      read -n 1 -s -r -p "Prem una tecla per tancar"
      exit 1
    fi
    echo "Sense connexió: faig servir el $f que ja hi ha."
  fi
done

# 2. Busca Python.
PY="$HOME/.practice-player-venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi
if [ -z "$PY" ]; then
  echo "Python 3 was not found. Install it from https://www.python.org/downloads/"
  read -n 1 -s -r -p "Press any key to close"
  exit 1
fi

# 3. Engega el servidor; obre el Taller musical al navegador.
"$PY" practice-player-server.py
