#!/usr/bin/env bash
# Start the web UI at http://localhost:8700 (keep this terminal open; Ctrl+C stops it).
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Run ./setup.sh first"; exit 1; }
exec .venv/bin/python -m pdfval ui "$@"
