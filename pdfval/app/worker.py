"""Run one comparison of the UI in its own process: python -m pdfval.app.worker <runs dir> <run id>.

The UI server starts one worker per run, up to [ui] parallel_runs at a time; the worker writes its
progress and result to <runs>/<id>/job.json. Passwords arrive in the environment (PDFVAL_AEM_PASSWORD,
PDFVAL_HTML_PASSWORD), never on the command line or on disk.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse


def main(argv: list[str]) -> int:
    from .server import Jobs
    runs, jid = Path(argv[0]), argv[1]
    jobs = Jobs(runs, recover=False, parallel=1)
    jobs.aem_password = os.environ.get("PDFVAL_AEM_PASSWORD", "") or jobs.aem_password
    job = jobs.get(jid)
    o = job.get("options", {})
    if o.get("mode") == "html" and os.environ.get("PDFVAL_HTML_PASSWORD"):
        jobs.passwords[(urlparse(job["candidate"]).netloc, o.get("html_user", ""))] = os.environ["PDFVAL_HTML_PASSWORD"]
    jobs.execute(jid)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
