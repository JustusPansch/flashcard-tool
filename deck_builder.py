"""Build a self-contained Anki .apkg deck from stored cards."""

from __future__ import annotations

import zlib
from pathlib import Path

import genanki

MODEL_ID = 1607392319
DECK_ID_BASE = 2059400110

MODEL = genanki.Model(
    MODEL_ID,
    "script2anki Card",
    fields=[{"name": "Question"}, {"name": "Answer"}],
    templates=[
        {
            "name": "Card 1",
            "qfmt": "{{Question}}",
            "afmt": '{{FrontSide}}<hr id="answer">{{Answer}}',
        }
    ],
)


def _deck_id(deck_name: str) -> int:
    return DECK_ID_BASE + (zlib.crc32(deck_name.encode()) % 1_000_000)


def _front_html(c, question: str) -> str:
    parts = [str(c["type"]) + (f" {c['number']}" if c["number"] else "")]
    if c["chapter_title"]:
        parts.append(f"Ch. {c['chapter_number']} ({c['chapter_title']})" if c["chapter_number"] else c["chapter_title"])
    if c["section_title"]:
        parts.append(f"§{c['section_number']} ({c['section_title']})" if c["section_number"] else c["section_title"])
    context = " &middot; ".join(parts)
    return f'<div style="color:#888;font-size:0.8em;margin-bottom:8px">{context}</div><div>{question}</div>'


def build_apkg(cards: list, output_path: str, deck_name: str, assets_dir: str, language: str = "en") -> int:
    """cards: rows with guid_key, question_en/question_de, image_filename,
    type, chapter/section context. Returns the number of notes written.
    """
    deck = genanki.Deck(_deck_id(deck_name), deck_name)
    media_files = []
    for c in cards:
        tags = [str(c["type"])]
        if c["chapter_title"]:
            tags.append(str(c["chapter_title"]).replace(" ", "_"))
        question = c["question_de"] if language == "de" and c["question_de"] else c["question_en"]
        note = genanki.Note(
            model=MODEL,
            fields=[_front_html(c, question), f'<img src="{c["image_filename"]}">'],
            guid=genanki.guid_for(c["guid_key"]),
            tags=tags,
        )
        deck.add_note(note)
        media_files.append(str(Path(assets_dir) / c["image_filename"]))

    package = genanki.Package(deck)
    package.media_files = media_files
    package.write_to_file(output_path)
    return len(cards)
