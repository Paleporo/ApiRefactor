#!/usr/bin/env bash
# Setup completo in un comando: .venv + dipendenze Python + tool Node locali (Spectral, swagger2openapi).
# Usa uv se disponibile, altrimenti python -m venv + pip.
set -euo pipefail
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
  echo "==> uv sync (crea .venv e installa dipendenze + dev)"
  uv sync --extra server
else
  PY="${PYTHON:-}"
  if [ -z "$PY" ]; then
    for c in python3.13 python3.12 python3; do
      if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then PY="$c"; break; fi
    done
  fi
  [ -n "$PY" ] || { echo "Serve Python >= 3.12 (oppure installa uv: https://docs.astral.sh/uv/)"; exit 1; }
  echo "==> uv non trovato: fallback $PY -m venv + pip"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements-dev.txt
  .venv/bin/python -m pip install -e . --no-deps
fi

if command -v npm >/dev/null 2>&1; then
  echo "==> npm install (Spectral + swagger2openapi in ./node_modules/.bin)"
  npm install --no-fund --no-audit
else
  echo "ATTENZIONE: npm non trovato. Installa Node.js >= 18, poi: npm install (oppure npm install -g @stoplight/spectral-cli swagger2openapi)"
fi
echo "==> Fatto. Verifica: .venv/bin/python -m pytest"
