"""PDF structure detection for script2anki.

Scans a math lecture PDF with PyMuPDF and detects two kinds of structure:

- Headings: numbered chapter/section/subsection titles, recognized by a
  bold font noticeably larger than body text (e.g. "2.3 Reversibilitaet").
- Elements: Satz/Definition/Beispiel/... blocks, recognized by a
  configurable keyword at the very start of a line, styled distinctly
  from body text (bold, or italic for "Beweis").

An element's region runs until the next element/heading starts, until a
large vertical gap appears, or until end of document; it can span a page
break, in which case it collects one bounding box per page.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import fitz  # PyMuPDF

DEFAULT_KEYWORDS = [
    "Satz",
    "Definition",
    "Beispiel",
    "Lemma",
    "Korollar",
    "Proposition",
    "Bemerkung",
    "Beweis",
]

LEVEL_NAMES = {0: "chapter", 1: "section", 2: "subsection"}

_HEADING_NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(.*)$")
_PAGE_NUMBER_RE = re.compile(r"^[ivxlcdmIVXLCDM\d]+$")


@dataclass
class Line:
    page: int
    bbox: tuple
    text: str
    size: float
    bold: bool
    italic: bool


@dataclass
class Heading:
    number: Optional[str]
    title: str
    level: int
    page: int
    bbox: tuple

    @property
    def level_name(self) -> str:
        return LEVEL_NAMES.get(self.level, f"level{self.level}")


@dataclass
class Element:
    type: str
    number: Optional[str]
    paren_title: Optional[str]
    style: str
    heading_context: dict = field(default_factory=dict)
    start_page: int = 0
    end_page: int = 0
    boxes: list = field(default_factory=list)  # [{"page": int, "bbox": tuple}]
    end_reason: str = ""
    text: str = ""

    @property
    def key(self) -> str:
        num = (self.number or "x").replace(".", "_")
        return f"{self.type.lower()}_{num}"

    @property
    def flags(self) -> list:
        flags = []
        if self.number is None:
            flags.append("no_number")
        pages = {b["page"] for b in self.boxes}
        if len(pages) > 1:
            flags.append("spans_pages")
        n = len(self.text)
        if n < 30:
            flags.append("short_text")
        if n > 4000:
            flags.append("long_text")
        return flags


@dataclass
class ParseResult:
    source_pdf: str
    page_count: int
    body_font_size: float
    keywords: list
    headings: list
    elements: list


def _span_style(spans) -> tuple:
    """Style of the line's leading text, not the whole line -- a keyword
    match must be styled distinctly right at the start, otherwise a body
    line that merely contains an italic/bold word later on (e.g. an
    inline term) would be misdetected as a new element.
    """
    lead = next((s for s in spans if s["text"].strip()), spans[0])
    font = lead["font"].lower()
    bold = "bold" in font
    italic = "italic" in font or "oblique" in font
    return bold, italic


def extract_lines(doc: "fitz.Document") -> list:
    """Flatten every page into simple text lines with font metadata."""
    lines = []
    for pno in range(len(doc)):
        raw = doc[pno].get_text("dict")
        for block in raw["blocks"]:
            if block.get("type") != 0:
                continue  # skip images/drawings, text only
            for line in block["lines"]:
                spans = line["spans"]
                text = "".join(s["text"] for s in spans)
                if not text.strip():
                    continue
                size = round(max(s["size"] for s in spans), 1)
                bold, italic = _span_style(spans)
                lines.append(
                    Line(
                        page=pno,
                        bbox=tuple(round(v, 2) for v in line["bbox"]),
                        text=text,
                        size=size,
                        bold=bold,
                        italic=italic,
                    )
                )
    return lines


def _outer_gap_cutoff(sorted_values: list, from_high: bool) -> Optional[float]:
    """Find the boundary of a separate outlier column (e.g. right-margin
    lecture markers at x0=~524 vs. body text maxing out at x0=~508) in a
    sorted 1-D distribution of line start positions.

    A real margin column is a small, *tightly packed* group of points cut
    off from the body by a clear gap -- lecture markers all sit at nearly
    the same x0 on every page. A handful of legitimately rare positions
    (e.g. a few wide diagram labels) can create an even bigger gap without
    being a real column: those points stay spread out beyond the gap
    rather than clustering. So for each candidate split, besides requiring
    a clear gap, require that most of the far side is packed close to the
    boundary -- that combination is what a genuine separate column looks
    like, and it lets the search skip past sparse-but-legitimate gaps to
    find the real one further out.
    """
    n = len(sorted_values)
    if n < 20:
        return None
    vals = sorted_values
    max_outer = max(8, int(n * 0.08))
    min_outer = 5
    for count_above in range(min_outer, max_outer + 1):
        if from_high:
            i = n - 1 - count_above
            a, b = vals[i], vals[i + 1]
            outer = vals[i + 1:]
            anchor = outer[0]
        else:
            i = count_above
            a, b = vals[i - 1], vals[i]
            outer = vals[:i]
            anchor = outer[-1]
        gap = b - a
        if gap < 10.0:
            continue
        density = sum(1 for v in outer if abs(v - anchor) <= 5.0) / len(outer)
        if density >= 0.6:
            return (a + b) / 2
    return None


def _filter_margin_noise(lines: list, doc: "fitz.Document") -> list:
    """Drop running headers/footers (page numbers) and outer-margin
    annotations (e.g. "Vorl.5" lecture markers) that sit in their own
    column outside the main text body, so they don't pollute headings,
    elements, or bounding boxes.
    """
    if not lines:
        return lines
    x0s = sorted(l.bbox[0] for l in lines)
    lo_cut = _outer_gap_cutoff(x0s, from_high=False)
    hi_cut = _outer_gap_cutoff(x0s, from_high=True)

    kept = []
    for ln in lines:
        page_h = doc[ln.page].rect.height
        in_footer_zone = ln.bbox[1] < 32 or ln.bbox[3] > page_h - 32
        is_number_like = bool(_PAGE_NUMBER_RE.fullmatch(ln.text.strip()))
        if in_footer_zone and is_number_like:
            continue
        if hi_cut is not None and ln.bbox[0] > hi_cut:
            continue
        if lo_cut is not None and ln.bbox[0] < lo_cut:
            continue
        kept.append(ln)
    return kept


def _body_font_size(lines: list) -> float:
    sizes = [l.size for l in lines]
    return Counter(sizes).most_common(1)[0][0]


def _page_figure_rects(doc: "fitz.Document") -> dict:
    """Bounding rects of vector drawings and images per page, excluding
    the header/footer margin. Lecture-note diagrams (e.g. Markov-chain
    transition graphs) are usually drawn as vector paths with sparse text
    labels, which otherwise look like a big blank gap to the line-based
    detector and cause it to cut an element off mid-figure.
    """
    rects_by_page: dict = {}
    for pno in range(len(doc)):
        page = doc[pno]
        page_h = page.rect.height
        rects = []
        for d in page.get_drawings():
            r = d["rect"]
            if r.is_empty or r.y1 < 32 or r.y0 > page_h - 32:
                continue
            rects.append(r)
        for info in page.get_image_info():
            r = fitz.Rect(info["bbox"])
            if r.is_empty or r.y1 < 32 or r.y0 > page_h - 32:
                continue
            rects.append(r)
        rects_by_page[pno] = rects
    return rects_by_page


def _figure_rect_in_gap(rects: list, y0: float, y1: float) -> Optional[tuple]:
    """Union bbox of figure rects whose vertical center falls within a
    text gap (y0, y1), or None if the gap is genuinely empty.
    """
    hits = [r for r in rects if y0 <= (r.y0 + r.y1) / 2 <= y1]
    if not hits:
        return None
    x0 = min(r.x0 for r in hits)
    y0_ = min(r.y0 for r in hits)
    x1 = max(r.x1 for r in hits)
    y1_ = max(r.y1 for r in hits)
    return (x0, y0_, x1, y1_)


def build_keyword_pattern(keywords: list) -> "re.Pattern":
    kw_alt = "|".join(re.escape(k) for k in keywords)
    return re.compile(
        rf"^(?P<keyword>{kw_alt})\b"
        rf"(?:\s*(?P<number>\d+(?:\.\d+)*))?"
        rf"(?:\s*\((?P<paren>[^)]*)\))?"
        rf"\.?"
    )


def _merge_heading_fragments(fragments: list) -> list:
    """Merge number+title pairs that share a baseline (same page, same
    size, overlapping y-range) into single logical heading lines, e.g.
    "2" + "Eigenschaften von Markovketten" -> "2 Eigenschaften von
    Markovketten".
    """
    fragments = sorted(fragments, key=lambda l: (l.page, l.bbox[1], l.bbox[0]))
    merged = []
    i = 0
    while i < len(fragments):
        cur = fragments[i]
        bbox = list(cur.bbox)
        text = cur.text.strip()
        j = i + 1
        while j < len(fragments):
            nxt = fragments[j]
            same_page = nxt.page == cur.page
            same_size = abs(nxt.size - cur.size) < 0.3
            y_overlap = same_page and not (
                nxt.bbox[3] <= bbox[1] or nxt.bbox[1] >= bbox[3]
            )
            if same_page and same_size and y_overlap:
                text += " " + nxt.text.strip()
                bbox[0] = min(bbox[0], nxt.bbox[0])
                bbox[1] = min(bbox[1], nxt.bbox[1])
                bbox[2] = max(bbox[2], nxt.bbox[2])
                bbox[3] = max(bbox[3], nxt.bbox[3])
                j += 1
            else:
                break
        merged.append(
            Line(page=cur.page, bbox=tuple(bbox), text=text, size=cur.size, bold=True, italic=False)
        )
        i = j
    return merged


def _heading_from_line(ln: Line, size_to_level: dict) -> Heading:
    m = _HEADING_NUMBERED_RE.match(ln.text)
    if m:
        number, title = m.group(1), m.group(2).strip()
        level = number.count(".")
    else:
        number, title = None, ln.text.strip()
        level = size_to_level.get(round(ln.size, 1), 0)
    return Heading(number=number, title=title, level=level, page=ln.page, bbox=ln.bbox)


def parse_pdf(
    path: str,
    keywords: Optional[list] = None,
    gap_factor: float = 8.0,
) -> ParseResult:
    keywords = keywords or DEFAULT_KEYWORDS
    pattern = build_keyword_pattern(keywords)

    doc = fitz.open(path)
    raw_lines = extract_lines(doc)
    lines = _filter_margin_noise(raw_lines, doc)
    lines.sort(key=lambda l: (l.page, l.bbox[1], l.bbox[0]))
    body_size = _body_font_size(lines)
    figure_rects = _page_figure_rects(doc)

    heading_fragments = [l for l in lines if l.bold and l.size > body_size + 0.5]
    merged_headings = _merge_heading_fragments(heading_fragments)
    distinct_sizes = sorted({round(l.size, 1) for l in merged_headings}, reverse=True)
    size_to_level = {s: idx for idx, s in enumerate(distinct_sizes)}

    # Typical single-line vertical gap, used to detect "clear whitespace" breaks.
    gaps = []
    prev = None
    for ln in lines:
        if prev is not None and prev.page == ln.page:
            gap = ln.bbox[1] - prev.bbox[3]
            if 0 < gap < 50:
                gaps.append(gap)
        prev = ln
    median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 4.0
    gap_threshold = max(median_gap * gap_factor, 8.0)

    headings: list = []
    elements: list = []
    current_headings: dict = {}
    heading_buffer: list = []
    open_el: Optional[Element] = None
    prev_line: Optional[Line] = None

    def flush_heading_buffer():
        if not heading_buffer:
            return
        merged = _merge_heading_fragments(heading_buffer)
        for ln in merged:
            h = _heading_from_line(ln, size_to_level)
            headings.append(h)
            current_headings[h.level] = h
            for deeper in [lvl for lvl in current_headings if lvl > h.level]:
                del current_headings[deeper]
        heading_buffer.clear()

    def snapshot_context() -> dict:
        return {
            h.level_name: {"number": h.number, "title": h.title}
            for h in sorted(current_headings.values(), key=lambda h: h.level)
        }

    def close_element(reason: str):
        nonlocal open_el
        if open_el is None:
            return
        open_el.end_reason = reason
        open_el.end_page = open_el.boxes[-1]["page"] if open_el.boxes else open_el.start_page
        elements.append(open_el)
        open_el = None

    def extend_box(el: Element, page: int, bbox: tuple):
        if not el.boxes or el.boxes[-1]["page"] != page:
            el.boxes.append({"page": page, "bbox": bbox})
        else:
            b = el.boxes[-1]["bbox"]
            el.boxes[-1]["bbox"] = (
                min(b[0], bbox[0]),
                min(b[1], bbox[1]),
                max(b[2], bbox[2]),
                max(b[3], bbox[3]),
            )

    def append_to_box(el: Element, ln: Line):
        extend_box(el, ln.page, ln.bbox)

    for ln in lines:
        is_heading_fragment = ln.bold and ln.size > body_size + 0.5
        page_changed = prev_line is not None and prev_line.page != ln.page

        if is_heading_fragment:
            close_element("heading")
            heading_buffer.append(ln)
            prev_line = ln
            continue

        flush_heading_buffer()

        m = pattern.match(ln.text.strip())
        is_element_start = bool(m) and (ln.bold or ln.italic)

        if is_element_start:
            close_element("new_element")
            open_el = Element(
                type=m.group("keyword"),
                number=m.group("number"),
                paren_title=m.group("paren"),
                style="bold" if ln.bold else "italic",
                heading_context=snapshot_context(),
                start_page=ln.page,
                text=ln.text.strip(),
            )
            append_to_box(open_el, ln)
            prev_line = ln
            continue

        if ln.bold:
            # A bold line-start is this style of script's general
            # convention for "new block" (formal or informal: asides
            # like "Motivation.", recurring study-guide summaries, or
            # unrecognized numbered types like "Aufgabe"), even when it
            # doesn't match a tracked keyword. Close the open element so
            # such asides don't get silently absorbed into it, but don't
            # start tracking a new one since it's not a card-worthy type.
            close_element("other_block")
            prev_line = ln
            continue

        # Plain body line.
        if open_el is not None:
            if not page_changed:
                gap = ln.bbox[1] - prev_line.bbox[3]
                if gap > gap_threshold:
                    figure = _figure_rect_in_gap(
                        figure_rects.get(ln.page, []), prev_line.bbox[3], ln.bbox[1]
                    )
                    if figure is not None:
                        extend_box(open_el, ln.page, figure)
                    else:
                        close_element("whitespace_gap")
                        prev_line = ln
                        continue
            open_el.text += "\n" + ln.text.strip()
            append_to_box(open_el, ln)
        prev_line = ln

    flush_heading_buffer()
    close_element("end_of_document")

    doc.close()
    return ParseResult(
        source_pdf=path,
        page_count=prev_line.page + 1 if prev_line else 0,
        body_font_size=body_size,
        keywords=keywords,
        headings=headings,
        elements=elements,
    )


def to_debug_dict(result: ParseResult) -> dict:
    summary = Counter(el.type for el in result.elements)
    return {
        "source_pdf": result.source_pdf,
        "page_count": result.page_count,
        "body_font_size": result.body_font_size,
        "keywords": result.keywords,
        "summary": dict(summary),
        "headings": [
            {
                "number": h.number,
                "title": h.title,
                "level": h.level,
                "level_name": h.level_name,
                "page": h.page + 1,
            }
            for h in result.headings
        ],
        "elements": [
            {
                "index": i,
                "type": el.type,
                "number": el.number,
                "paren_title": el.paren_title,
                "style": el.style,
                "heading_context": el.heading_context,
                "start_page": el.start_page + 1,
                "end_page": el.end_page + 1,
                "end_reason": el.end_reason,
                "flags": el.flags,
                "char_count": len(el.text),
                "boxes": [
                    {"page": b["page"] + 1, "bbox": list(b["bbox"])} for b in el.boxes
                ],
                "text": el.text,
            }
            for i, el in enumerate(result.elements)
        ],
    }
