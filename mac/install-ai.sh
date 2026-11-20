#!/bin/bash
# Instal·la la separació d'instruments amb IA (Demucs) per al Practice Player.
# Va en un entorn de Python a part (~/.practice-player-ai-venv), perquè PyTorch
# necessita Python 3.10-3.12 (en un Mac Intel, com a màxim 3.12).
set -e
VENV="$HOME/.practice-player-ai-venv"
ARCH="$(uname -m)"
DEMUCS_SRC="https://github.com/facebookresearch/demucs/archive/refs/tags/v4.0.1.zip"
PY_PKG="https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg"

echo "== Separació amb IA (Demucs) =="
echo "Mac: $ARCH"

# 1. Un Python compatible amb PyTorch
PY=""
for v in 3.12 3.11 3.10; do
  for c in "/Library/Frameworks/Python.framework/Versions/$v/bin/python3" "/opt/homebrew/bin/python$v" "/usr/local/bin/python$v" "$(command -v "python$v" 2>/dev/null)"; do
    if [ -n "$c" ] && [ -x "$c" ]; then PY="$c"; break 2; fi
  done
done
if [ -z "$PY" ]; then
  echo
  echo "Cal Python 3.12: PyTorch (la base de Demucs) encara no funciona amb la teva versió de Python."
  echo "Ara es baixarà l'instal·lador oficial de Python 3.12 i s'obrirà."
  echo "Instal·la'l (no esborra el Python que ja tens) i després torna a executar aquesta mateixa ordre."
  if curl -fL --progress-bar "$PY_PKG" -o /tmp/python-3.12.pkg; then
    open /tmp/python-3.12.pkg
  else
    open "https://www.python.org/downloads/macos/"
    echo "Baixa el 'macOS 64-bit universal2 installer' de Python 3.12."
  fi
  exit 1
fi
echo "Python per a la IA: $PY ($("$PY" -V))"

# 2. Entorn a part
if [ ! -x "$VENV/bin/python" ]; then
  echo "Creant l'entorn $VENV ..."
  "$PY" -m venv "$VENV"
fi
VPY="$VENV/bin/python"
"$VPY" -m pip install -q -U pip

# 3. PyTorch (uns 300-800 MB) i Demucs
echo "Instal·lant PyTorch (pot trigar uns minuts) ..."
if [ "$ARCH" = "x86_64" ]; then
  # Darrera versió de PyTorch per a Mac Intel
  "$VPY" -m pip install "torch==2.2.2" "torchaudio==2.2.2" "numpy<2"
else
  "$VPY" -m pip install "torch<2.9" "torchaudio<2.9" numpy
fi
echo "Instal·lant Demucs ..."
"$VPY" -m pip install certifi "demucs @ $DEMUCS_SRC"

# 4. Comprova-ho i baixa el model ara (uns 80 MB), perquè la primera separació no hagi d'esperar
echo "Baixant el model de separació ..."
"$VPY" - <<'PYCODE'
import os
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass
import torch
from demucs.pretrained import get_model
get_model("htdemucs_6s")
print("PyTorch", torch.__version__, "· model htdemucs_6s a punt")
PYCODE

echo
echo "Fet! Tanca el Taller musical i torna'l a obrir: al Practice Player, al costat del tipus de saxo, sortirà la casella AI."
