#!/usr/bin/env python3
"""Builds index.html: one single page with every program inside it.

Run it again after changing any of the programs:
    python3 build-hub.py
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Order = order of the tabs. "tuner": shown in the side panel instead of a tab.
APPS = [
    ("practice-player", "practice-player.html", "Practice Player", "Reprodueix cançons (també de YouTube) a la velocitat que vulguis, amb bucles."),
    ("chord-practice", "chord-practice.html", "Chord Practice", "Practica canvis d'acords."),
    ("neo-soul-generator", "neo-soul-generator.html", "Neo Soul Generator", "Genera progressions neo soul."),
    ("scale-recognizer", "scale-recognizer.html", "Scale Recognizer", "Toca una escala i et diu quina és."),
    ("analitzador", "analitzador_espectral.html", "Analitzador espectral", "Espectre del so del micròfon."),
    ("guitar-rodeo", "guitar-rodeo.html", "Chord Rodeo", "Joc d'acords de guitarra."),
    ("bass-rodeo", "bass-rodeo.html", "Bass Rodeo", "Joc de baix."),
    ("afinador", "afinador.html", "Afinador", "Afinador.", True),
]


def script_json(element_id, value):
    # < avoids any "</script>" or "<!--" inside the programs breaking the page.
    data = json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/json" id="{element_id}">{data}</script>'


def main():
    meta, blocks = [], []
    for entry in APPS:
        app_id, file, name, desc = entry[:4]
        tuner = len(entry) > 4 and entry[4]
        source = (HERE / file).read_text(encoding="utf-8")
        meta.append({"id": app_id, "name": name, "desc": desc, "tuner": bool(tuner)})
        blocks.append(script_json("src-" + app_id, source))
    template = (HERE / "hub-template.html").read_text(encoding="utf-8")
    out = template.replace("<!--APPS-->", "\n".join([script_json("apps-meta", meta)] + blocks), 1)
    (HERE / "index.html").write_text(out, encoding="utf-8")
    print(f"index.html: {len(APPS)} programs, {len(out) // 1024} KB")


if __name__ == "__main__":
    main()
