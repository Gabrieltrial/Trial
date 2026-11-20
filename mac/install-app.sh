#!/bin/bash
# Crea "Taller musical.app" (per posar al Dock).
# Els fitxers del programa es desen a ~/Documents/AI/fitxers_claude/practice_player.
set -e
DIR="$HOME/Documents/AI/fitxers_claude/practice_player"
REPO="https://raw.githubusercontent.com/Gabrieltrial/Trial/main"
if [ -w /Applications ]; then APPS="/Applications"; else APPS="$HOME/Applications"; fi
APP="$APPS/Taller musical.app"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "1/4  Baixant els fitxers a $DIR ..."
mkdir -p "$DIR" "$APPS"
for f in mac/start-taller.sh mac/stop-taller.sh index.html practice-player-server.py; do
  curl -fsSL "$REPO/$f" -o "$DIR/$(basename "$f")"
done
chmod +x "$DIR/start-taller.sh" "$DIR/stop-taller.sh"
curl -fsSL "$REPO/mac/icon.png" -o "$TMP/icon.png"

echo "2/4  Creant l'aplicació ..."
cat > "$TMP/app.applescript" <<'APPLESCRIPT'
property folderPath : ""

on run
	startTaller()
end run

on reopen
	startTaller()
end reopen

on startTaller()
	set folderPath to (POSIX path of (path to documents folder)) & "AI/fitxers_claude/practice_player/"
	try
		do shell script "/bin/bash " & quoted form of (folderPath & "start-taller.sh")
	on error errMsg
		display dialog "No s'ha pogut engegar el Taller musical:" & return & return & errMsg buttons {"D'acord"} default button 1 with icon caution
		return
	end try
	open location "http://127.0.0.1:8765/"
end startTaller

on idle
	-- Quan es tanca la pàgina, el servidor s'atura sol; llavors l'app també es tanca.
	try
		do shell script "curl -fs --max-time 3 http://127.0.0.1:8765/api/ping"
	on error
		quit
	end try
	return 5
end idle

on quit
	try
		do shell script "/bin/bash " & quoted form of ((POSIX path of (path to documents folder)) & "AI/fitxers_claude/practice_player/stop-taller.sh")
	end try
	continue quit
end quit
APPLESCRIPT
rm -rf "$APP"
osacompile -s -o "$APP" "$TMP/app.applescript"

echo "3/4  Posant-hi la icona ..."
ICONSET="$TMP/icon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$TMP/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$TMP/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/applet.icns"
/usr/libexec/PlistBuddy -c "Set :CFBundleName Taller musical" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.gabrieltrial.tallermusical" "$APP/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.gabrieltrial.tallermusical" "$APP/Contents/Info.plist"
codesign --force --deep --sign - "$APP" 2>/dev/null || true
touch "$APP"

echo "4/4  Fet!"
echo
echo "L'aplicació és a: $APP"
echo "Ara s'obrirà. Per deixar-la al Dock: clic dret a la seva icona del Dock > Opcions > Mantenir al Dock."
open "$APP"
