#!/usr/bin/env bash
# One-time setup: ./setup.sh   then start the UI with   ./start.sh
# Creates .venv with Python 3.11+ (uses one that is installed, else downloads one with uv - no admin
# rights needed), installs requirements.txt and the Chromium browser for web-page runs.
set -euo pipefail
cd "$(dirname "$0")"

ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }

if [ -x .venv/bin/python ] && ok .venv/bin/python; then
  echo "Using the existing .venv ($(.venv/bin/python --version))"
else
  [ -d .venv ] && { echo "Removing the old .venv (not Python 3.11+ or copied from another machine)"; rm -rf .venv; }
  PY=""
  for c in python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 && ok "$(command -v "$c")"; then PY="$(command -v "$c")"; break; fi
  done
  if [ -n "$PY" ]; then
    echo "Creating .venv with $PY ($("$PY" --version))"
    "$PY" -m venv .venv
    .venv/bin/python -m pip install -q --upgrade pip
    .venv/bin/python -m pip install -q -r requirements.txt
  else
    echo "No Python 3.11+ found - installing uv to download Python 3.12 (into your home folder)"
    command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -r requirements.txt
  fi
fi

# a copied or older .venv may lack newer requirements: install whatever is missing
.venv/bin/python -m pip install -q -r requirements.txt 2>/dev/null \
  || uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -c "import pdfval, pdfval.app.server, pdfval.report.pdf_report" \
  && echo "Setup OK ($(.venv/bin/python --version)). Start the UI with: ./start.sh"
