#!/usr/bin/env bash
# One-shot: compare -> interactive report -> e2e walk of every section.
#   ./run.sh <baseline.pdf> <candidate.pdf> [out-dir]
set -uo pipefail
cd "$(dirname "$0")"
BASE=${1:?baseline pdf}; CAND=${2:?candidate pdf}; OUT=${3:-reports/$(date +%Y%m%d-%H%M%S)}
PY=.venv/bin/python
[ -x "$PY" ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt && .venv/bin/playwright install chromium; }

$PY -m pdfval compare --baseline "$BASE" --candidate "$CAND" --out "$OUT" --fail-on never
$PY e2e/walk_sections.py "$OUT"; rc=$?
echo "Open the viewer:  $PY -m pdfval serve $OUT"
exit $rc
