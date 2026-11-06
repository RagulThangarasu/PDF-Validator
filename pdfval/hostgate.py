"""How many page loads one web server is asked for at the same time, counted across every run.

A batch of ten publications is ten worker processes, and each of them opens `html.concurrency` browsers of
its own, so "six pages at a time" quietly becomes sixty at a time against a single AEM instance. The server
does not fail that cleanly: it takes every request, serves them all slowly, and pages start passing the 90 s
navigation timeout - the run then reports an error although the site is merely overloaded by this tool.

The processes share no memory, so the cap is held in the filesystem: `n` slot files, each locked with
flock(LOCK_EX|LOCK_NB). A slot is held only while a page is navigating, never while it is being read, and
the kernel drops the lock when a worker exits or is killed, so a stopped run leaves nothing behind.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import tempfile
import time
from pathlib import Path

_DIR = Path(os.environ.get("PDFVAL_HOSTGATE_DIR") or (Path(tempfile.gettempdir()) / "pdfval-hostgate"))


@contextlib.contextmanager
def host_slot(host: str, limit: int, timeout: float = 600.0):
    """Hold one of `limit` slots for `host` for the body of the with-block.

    Waiting here is not lost time: the request would otherwise have gone to a server that is already queueing
    it. If no slot comes free within `timeout` the body runs anyway - a slow guide must still finish, and the
    gate is a throttle, not a lock that may deadlock a run.
    """
    if not host or limit <= 0:
        yield
        return
    key = hashlib.sha1(host.encode("utf-8", "replace")).hexdigest()[:16]
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
    except OSError:  # no shared directory: never hold the run up over a throttle
        yield
        return
    fh, deadline, delay = None, time.monotonic() + timeout, 0.05
    try:
        while fh is None and time.monotonic() < deadline:
            for i in range(limit):
                try:
                    f = open(_DIR / f"{key}.{i}", "a+b")
                except OSError:
                    continue
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    f.close()
                    continue
                fh = f
                break
            if fh is None:
                time.sleep(delay)
                delay = min(delay * 1.6, 0.5)  # back off, so waiters do not spin on the same slots together
        yield
    finally:
        if fh is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()
