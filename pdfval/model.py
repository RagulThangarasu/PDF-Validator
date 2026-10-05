"""Core data model shared by the extractor, the checks and the reporters."""
from __future__ import annotations

from dataclasses import dataclass, field

Rect = tuple[float, float, float, float]


@dataclass(frozen=True)
class Style:
    family: str
    weight: int  # 100..900, CSS-like
    italic: bool
    size: float  # pt
    color: str  # #rrggbb

    def css(self) -> str:
        it = " italic" if self.italic else ""
        return f"{self.family} {self.weight}{it} {self.size:g}pt {self.color}"


@dataclass
class Word:
    text: str  # raw text as drawn
    norm: str  # normalised token used for content comparison ("" = ignored)
    page: int  # 0-based
    bbox: Rect
    style: Style
    line: int  # index into Doc.lines
    line_start: bool = False
    role: str = "body"  # h1/h2/.../body/text-10pt, assigned by the engine
    space_after: int | None = None  # whitespace chars before the next word on the same line (None = line end)
    script: str = ""  # per character: "^" superscript, "_" subscript, "." normal; "" = all normal


@dataclass
class Line:
    page: int
    bbox: Rect
    text: str
    size: float
    first_word: int = -1
    block: tuple[int, int] = (-1, -1)  # (page, text block) – blocks approximate paragraphs / cells


@dataclass
class Image:
    page: int
    bbox: Rect
    broken: bool = False  # failed to load (web page <img> with no pixels)
    stretch: float = 1.0  # drawn shape / pixel shape ((box w/h) / (px w/h)); 1 = drawn in its own proportions
    px: tuple = (0, 0)  # the embedded bitmap's size in pixels (0 = unknown: vector, web capture)


@dataclass
class PageInfo:
    width: float
    height: float
    left: float = 0.0  # content-box left edge (body text margin)
    right: float = 0.0  # content-box right edge


@dataclass
class Anchor:
    """A section start located in the document's word stream."""
    title: str
    norm: str
    level: int
    page: int
    y: float
    word: int  # index of first word of the section heading
    located: bool = True  # False when the heading text was not found on the page


@dataclass
class Doc:
    path: str
    label: str
    pages: list[PageInfo]
    words: list[Word]
    lines: list[Line]
    images: list[Image]
    outline: list[tuple[int, str, int]]  # (level, title, 1-based page)
    body_size: float = 0.0
    removed_lines: int = 0  # header/footer/ignored lines dropped
    raw_tables: dict | None = None  # page -> tables from a structured source (HTML DOM); None = detect in the PDF
    picture_text: dict = field(default_factory=dict)  # (page, picture box) -> word indices of its labels
    outline_to: list = field(default_factory=list)  # per outline entry: the y its bookmark lands on (or None)
    # running headers / footers taken out of the text comparison, kept to compare them on their own:
    # [{page, band ('header' | 'footer'), text, bbox, words: [(text, bbox, Style)]}]
    furniture: list = field(default_factory=list)

    def left(self, page: int) -> float:
        return self.pages[page].left

    def right(self, page: int) -> float:
        return self.pages[page].right


@dataclass
class Loc:
    page: int
    bbox: Rect

    def to_json(self):
        return {"page": self.page, "bbox": [round(v, 1) for v in self.bbox]}


@dataclass
class Finding:
    check: str  # structure | content | style | layout | assets
    severity: str  # error | warning | info
    message: str
    baseline: list[Loc] = field(default_factory=list)
    candidate: list[Loc] = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    # For one-sided findings (text/heading/image only on one side): the aligned
    # position on the other side, derived from the surrounding matched text.
    baseline_at: Loc | None = None
    candidate_at: Loc | None = None
    critical: bool = False  # breaking issue (missing section/row/image/file, broken link/glyph, ...)
    types: list[str] = field(default_factory=list)  # sub-types for filtering, e.g. ["case"], ["missing row"]
    # (prod box, stage box) of the same text, one per line pair: lets a screenshot
    # highlight exactly the same words on both sides
    links: list[tuple[Loc, Loc]] = field(default_factory=list)

    def to_json(self):
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "baseline": [l.to_json() for l in self.baseline],
            "candidate": [l.to_json() for l in self.candidate],
            "detail": self.detail,
            "baseline_at": self.baseline_at.to_json() if self.baseline_at else None,
            "candidate_at": self.candidate_at.to_json() if self.candidate_at else None,
            "critical": self.critical,
            "types": self.types,
            "links": [[a.to_json(), b.to_json()] for a, b in self.links],
        }


SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}
