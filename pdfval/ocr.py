"""Text drawn inside images, read with the tesseract command line (OCR).

Prod may set a figure's labels ("50 cm", "60±10 cm", dimension callouts) as text over the
drawing while stage has them baked into the picture: text extraction then sees them as
missing. Before calling such text missing, the stage images are read with OCR - near the
section first, then anywhere in the document (the picture may sit on another page, at
another size and with its own line breaks).
"""
from __future__ import annotations

import re
import shutil
import subprocess

import pymupdf

_DOCS: dict[str, pymupdf.Document] = {}
_TEXT: dict[tuple, str] = {}


def available() -> bool:
    return shutil.which("tesseract") is not None


def reset() -> None:
    for d in _DOCS.values():
        d.close()
    _DOCS.clear()
    _TEXT.clear()


def image_text(path: str, page: int, bbox: tuple, dpi: int = 300, scales: tuple = (1, 2)) -> str:
    """OCR text of one picture on a page, read at dpi x each of `scales` (cached per reading), in
    grayscale with the contrast stretched (thin grey labels on a line drawing), the readings joined.
    Tiny labels ("0.43cm ~ 6cm" at 5 pt) need a 3x reading."""
    return "\n".join(_read(path, page, bbox, dpi * k) for k in scales)


def _cache_key(path: str, page: int, bbox: tuple, dpi: int) -> tuple:
    return (path, page, tuple(round(v, 1) for v in bbox), dpi)


def _render(path: str, page: int, bbox: tuple, dpi: int) -> bytes | None:
    """The picture as a grayscale PNG with the contrast stretched (thin grey labels on a line drawing);
    None when it is too large to read at this dpi (a page-size picture is read at lower scales only)."""
    import io

    from PIL import Image as PILImage, ImageOps

    rect = pymupdf.Rect(bbox)
    if max(rect.width, rect.height) * dpi / 72 > 7000:
        return None
    doc = _DOCS.get(path) or _DOCS.setdefault(path, pymupdf.open(path))
    png = doc[page].get_pixmap(clip=rect, dpi=dpi).tobytes("png")
    im = ImageOps.autocontrast(PILImage.open(io.BytesIO(png)).convert("L"), cutoff=1)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _tesseract(png: bytes) -> str:
    # --psm 11: sparse text - labels scattered over a drawing, not a paragraph
    r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "11"], input=png, capture_output=True, timeout=120)
    return r.stdout.decode("utf-8", "replace")


def _read(path: str, page: int, bbox: tuple, dpi: int) -> str:
    key = _cache_key(path, page, bbox, dpi)
    if key not in _TEXT:
        _TEXT[key] = ""
        try:
            png = _render(path, page, bbox, dpi)
            if png:
                _TEXT[key] = _tesseract(png)
        except Exception:
            pass
    return _TEXT[key]


def prefetch(path: str, pictures: list[tuple[int, tuple]], dpi: int, scales: tuple, progress=None) -> None:
    """Read many pictures at once: rendered one after another (pymupdf is not thread-safe), each
    rendering handed to tesseract on its own core as soon as it is ready. Fills the cache that
    image_text reads, so the per-finding lookups that follow cost nothing."""
    import os
    from concurrent.futures import ThreadPoolExecutor

    keys = []
    for page, bbox in pictures:
        for k in scales:
            key = _cache_key(path, page, bbox, dpi * k)
            if key not in _TEXT and key not in keys:
                keys.append(key)
    if not keys:
        return
    import threading

    done, lock = [0], threading.Lock()

    def finished(key, fut=None):
        if fut is not None:
            try:
                _TEXT[key] = fut.result()
            except Exception:
                pass
        with lock:
            done[0] += 1
            n = done[0]
        if progress and (n % 5 == 0 or n == len(keys)):
            progress(n, len(keys))

    if progress:
        progress(0, len(keys))
    with ThreadPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1)) as pool:
        for key in keys:
            _TEXT[key] = ""
            try:
                png = _render(key[0], key[1], key[2], key[3])
            except Exception:
                png = None
            if png:
                pool.submit(_tesseract, png).add_done_callback(lambda fut, key=key: finished(key, fut))
            else:
                finished(key)


_DIGITS = str.maketrans("oqdilzsbg", "000112569")  # what OCR reads for a digit in a label


def _key(t: str) -> str:
    return re.sub(r"[^0-9a-z]", "", t.lower())


def _near(key: str, hay: str, tol: int) -> bool:
    """`key` occurs in `hay` with at most `tol` edits (approximate substring match)."""
    if key in hay:
        return True
    if not tol:
        return False
    prev = [0] * (len(hay) + 1)
    for i, c in enumerate(key, 1):
        cur = [i] + [0] * len(hay)
        for j, h in enumerate(hay, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (c != h))
        prev = cur
    return min(prev) <= tol


def found(text: str, ocr: str, ratio: float = 0.8) -> bool:
    """Are the words of `text` in the OCR text? Compared on letters and digits only ("60±10" is
    "6010", whatever OCR makes of "±"), allowing OCR's misreadings: a letter read for a digit
    ("6O1O") and about one wrong character in four. The telling words (with a digit, or 3+
    letters; not single characters) must be there, >= ratio of them, and at least one of 3+
    characters: a lone "1" or "A" is in any picture. Short words must be a whole word of the
    OCR text."""
    low = re.sub(r"(?<=\d)[.,](?=\d)", "", ocr.lower())  # "1.8" is the word "18", as in the key
    hay, hay_d = _key(low), _key(low).translate(_DIGITS)
    words = set(re.findall(r"\d+|[a-z]+", low)) | set(re.findall(r"\d+", low.translate(_DIGITS)))
    keys = [k for k in (_key(t) for t in text.split()) if k]
    # a lone character ("B", "2" of a circled callout ❷) is not read reliably: it counts only
    # when there is nothing else - and then the text never counts as found
    strong = [k for k in keys if (re.search(r"\d", k) or len(k) >= 3) and len(k) > 1]
    if not hay or not strong or max(len(k) for k in strong) < 3:
        return False

    def hit(k):
        if len(k) < 3:
            return k in words or all(p in words for p in re.findall(r"\d+|[a-z]+", k))
        tol = len(k) // 4 or (1 if len(k) >= 4 else 0)
        return _near(k, hay, tol) or (bool(re.search(r"\d", k)) and _near(k.translate(_DIGITS), hay_d, tol))

    return sum(hit(k) for k in strong) >= ratio * len(strong)
