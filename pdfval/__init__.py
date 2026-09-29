"""pdfval – section-anchored PDF parity validation (content, CSS, alignment)."""
import sys

if sys.version_info < (3, 11):  # plain syntax only above this line: it must run on old Pythons too
    sys.exit("pdfval needs Python 3.11 or newer; this is Python %d.%d (%s).\n"
             "Run ./setup.sh to create .venv with a current Python, then: .venv/bin/python -m pdfval ui"
             % (sys.version_info[0], sys.version_info[1], sys.executable))

from .engine import compare, load_config  # noqa: E402,F401
