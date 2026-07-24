"""Render detected Satz/Definition/... elements to cropped PNG screenshots.

Each element's bounding box(es) are clipped from the original PDF page(s)
at a configurable DPI, with a small margin so nothing touches the crop
edge. Elements spanning a page break get one crop per page, stacked into
a single image.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import fitz
from PIL import Image

from pdf_parser import Element

PAD_PT = 10.0
STACK_GAP_PX = 8


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def element_filename(deck_slug: str, el: Element, index: int) -> str:
    num = _slug(el.number) if el.number else "x"
    return f"{deck_slug}_{_slug(el.type)}_{num}_{index}.png"


def _clip_rect(page: "fitz.Page", bbox: tuple, pad: float = PAD_PT) -> "fitz.Rect":
    r = fitz.Rect(bbox)
    r = fitz.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad)
    return r & page.rect


def _render_box(doc: "fitz.Document", page_no: int, bbox: tuple, dpi: int) -> "Image.Image":
    page = doc[page_no]
    pix = page.get_pixmap(clip=_clip_rect(page, bbox), dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def _stack(images: list) -> "Image.Image":
    if len(images) == 1:
        return images[0]
    width = max(im.width for im in images)
    height = sum(im.height for im in images) + STACK_GAP_PX * (len(images) - 1)
    combined = Image.new("RGB", (width, height), "white")
    y = 0
    for im in images:
        combined.paste(im, (0, y))
        y += im.height + STACK_GAP_PX
    return combined


def render_elements(
    source_pdf: str,
    elements: list,
    out_dir: str,
    deck_slug: str,
    dpi: int = 200,
) -> dict:
    """Render every element's screenshot and return {index: filename}."""
    doc = fitz.open(source_pdf)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    filenames = {}
    for i, el in enumerate(elements):
        images = [_render_box(doc, b["page"], b["bbox"], dpi) for b in el.boxes]
        combined = _stack(images)
        filename = element_filename(deck_slug, el, i)
        combined.save(out / filename)
        filenames[i] = filename
    doc.close()
    return filenames
