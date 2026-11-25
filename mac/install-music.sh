#!/bin/bash
# Installs the music server (search, download and play music from the phone).
# Program files: ~/Documents/AI/fitxers_claude/music   Music: ~/Music/baixat internet
set -e
DIR="$HOME/Documents/AI/fitxers_claude/music"
REPO="https://raw.githubusercontent.com/Gabrieltrial/Trial/main"
mkdir -p "$DIR"
curl -fsSL "$REPO/music/music-server.py" -o "$DIR/music-server.py"
curl -fsSL "$REPO/music/music.html" -o "$DIR/music.html"
cat > "$DIR/Musica.command" <<'CMD'
#!/bin/bash
# Starts the music server. Leave this window open while you use it from the phone.
cd "$(dirname "$0")"
REPO="https://raw.githubusercontent.com/Gabrieltrial/Trial/main"
for f in music-server.py music.html; do
  if curl -fsSL --max-time 30 "$REPO/music/$f" -o "$f.new" 2>/dev/null && [ -s "$f.new" ]; then mv "$f.new" "$f"; else rm -f "$f.new"; fi
done
PY="$HOME/.practice-player-venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
if ! "$PY" -c "import yt_dlp" 2>/dev/null; then
  echo "Installing yt-dlp (needed to search and download)..."
  "$PY" -m pip install --user -U "yt-dlp[default]" || true
fi
"$PY" music-server.py
read -n 1 -s -r -p "Press any key to close"
CMD
chmod +x "$DIR/Musica.command"
echo "Fet! S'obrirà el servidor de música. A la finestra hi veuràs l'adreça per al mòbil i el PIN."
open "$DIR/Musica.command"
