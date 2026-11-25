# Música (servidor per al mòbil)

Busca cançons (YouTube, SoundCloud), les baixa i les reprodueix des del navegador del mòbil, amb llistes.

- `music-server.py`: el servidor (Python 3.9+ i `yt-dlp`; `ffmpeg` opcional, per convertir a m4a).
- `music.html`: la pàgina per al mòbil (la serveix el mateix servidor).

Les cançons es desen a `<carpeta de música>/<autor>/<disc>/<títol>.m4a`, amb un `.json` al costat
(títol, autor, disc, enllaç d'origen). Les llistes són a `<carpeta de música>/.llistes.json`.

## Mac
    curl -fsSL https://raw.githubusercontent.com/Gabrieltrial/Trial/main/mac/install-music.sh | bash
Després, doble clic a `~/Documents/AI/fitxers_claude/music/Musica.command`.

## Raspberry Pi (més endavant)
    sudo apt install python3-pip ffmpeg
    python3 -m pip install -U "yt-dlp[default]"
    python3 music-server.py --music-dir "/home/pi/Música/baixat internet"

Opcions: `--port 8770`, `--music-dir CARPETA` (es recorda), `--new-pin` (els mòbils hauran de tornar a escriure el PIN).
La configuració (PIN i mòbils autoritzats) és a `~/.music-server.json`.
