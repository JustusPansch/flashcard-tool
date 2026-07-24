#!/usr/bin/env python3
"""CLI entry point for script2anki.

Subcommands:
  detect  -- parse a PDF and dump detected structure to debug.json (fast, no images/LLM/DB)
  import  -- full pipeline: parse, screenshot, generate questions, store in the card DB
  export  -- build a self-contained .apkg from the stored cards
  serve   -- launch the local study/browse/export web app
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import deck_builder
import pdf_parser
import question_gen
import screenshot
import store
from pdf_parser import DEFAULT_KEYWORDS, parse_pdf, to_debug_dict


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def cmd_detect(args) -> int:
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    result = parse_pdf(args.input_pdf, keywords=keywords, gap_factor=args.gap_factor)
    debug = to_debug_dict(result)

    with open(args.debug_json, "w", encoding="utf-8") as f:
        json.dump(debug, f, ensure_ascii=False, indent=2)

    print(f"Parsed {args.input_pdf} ({result.page_count} pages, body font size {result.body_font_size}pt)")
    print(f"Detected {len(result.headings)} headings and {len(result.elements)} elements:")
    for kind, count in sorted(debug["summary"].items(), key=lambda kv: -kv[1]):
        print(f"  {kind}: {count}")

    flagged = [el for el in debug["elements"] if el["flags"]]
    if flagged:
        print(f"\n{len(flagged)} elements have review flags (see debug.json), e.g.:")
        for el in flagged[:10]:
            print(f"  [{el['index']}] {el['type']} {el['number']} (p.{el['start_page']}): {el['flags']}")

    print(f"\nWrote debug output to {args.debug_json}.")
    return 0


def cmd_import(args) -> int:
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    deck_name = args.deck_name or args.input_pdf.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    deck_slug = _slug(deck_name)

    result = parse_pdf(args.input_pdf, keywords=keywords, gap_factor=args.gap_factor)
    if args.debug_json:
        with open(args.debug_json, "w", encoding="utf-8") as f:
            json.dump(to_debug_dict(result), f, ensure_ascii=False, indent=2)

    print(f"Parsed {args.input_pdf}: {len(result.elements)} elements, {len(result.headings)} headings.")

    print(f"Rendering screenshots to {args.assets_dir}/ ...")
    filenames = screenshot.render_elements(args.input_pdf, result.elements, args.assets_dir, deck_slug, dpi=args.dpi)

    model = args.ollama_model
    if model:
        if question_gen.ollama_available(args.ollama_url, model):
            print(f"Using Ollama model '{model}' for question generation (English + German, two calls per card).")
        else:
            print(f"Ollama model '{model}' not reachable at {args.ollama_url} -- falling back to template questions.")
            model = None
    else:
        print("No --ollama-model given -- using template questions.")

    conn = store.connect(args.db)
    seen_keys = []
    fallback_count = {"en": 0, "de": 0}
    ollama_count = {"en": 0, "de": 0}
    for i, el in enumerate(result.elements):
        antecedent = question_gen.find_beweis_antecedent(result.elements, i) if el.type == "Beweis" else None
        questions = question_gen.generate_questions(el, model, args.ollama_url, antecedent)
        for lang, (_, source) in questions.items():
            (ollama_count if source == "ollama" else fallback_count)[lang] += 1
        ctx = el.heading_context or {}
        chapter = ctx.get("chapter") or {}
        section = ctx.get("section") or {}
        guid_key = store.guid_key_for(args.input_pdf, el.type, el.number, i)
        seen_keys.append(guid_key)
        store.upsert_card(
            conn,
            guid_key=guid_key,
            source_pdf=args.input_pdf,
            deck_name=deck_name,
            type=el.type,
            number=el.number,
            paren_title=el.paren_title,
            chapter_number=chapter.get("number"),
            chapter_title=chapter.get("title"),
            section_number=section.get("number"),
            section_title=section.get("title"),
            question_en=questions["en"][0],
            question_de=questions["de"][0],
            image_filename=filenames[i],
        )
    conn.commit()
    removed = store.prune_stale(conn, args.input_pdf, seen_keys)
    conn.commit()
    conn.close()

    if model:
        for lang in question_gen.LANGUAGES:
            total = ollama_count[lang] + fallback_count[lang]
            if fallback_count[lang]:
                print(
                    f"{lang.upper()}: {fallback_count[lang]}/{total} questions fell back to the template "
                    f"(Ollama output rejected as not-a-question, too long, or containing raw LaTeX)."
                )

    print(f"Stored {len(result.elements)} cards in {args.db} (deck '{deck_name}').")
    if removed:
        print(f"Removed {removed} stale card(s) no longer detected in this PDF.")
    print("Run 'python script2anki.py serve' to study, or 'python script2anki.py export' to get an .apkg.")
    return 0


def cmd_export(args) -> int:
    conn = store.connect(args.db)
    types = [t.strip() for t in args.types.split(",")] if args.types else None
    cards = store.get_cards(conn, types)
    conn.close()
    if not cards:
        print("No cards match that filter -- nothing to export.")
        return 1
    n = deck_builder.build_apkg(cards, args.output, args.deck_name, args.assets_dir, args.language)
    print(f"Wrote {n} cards to {args.output}")
    return 0


def cmd_serve(args) -> int:
    sys.path.insert(0, "webapp")
    from webapp.app import create_app

    app = create_app(args.db, args.assets_dir)
    print(f"Serving at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Turn a math lecture PDF into a spaced-repetition Anki deck.")
    sub = p.add_subparsers(dest="command", required=True)

    common_detect = dict(
        keywords=",".join(DEFAULT_KEYWORDS),
        gap_factor=8.0,
    )

    detect = sub.add_parser("detect", help="Parse a PDF and dump detected structure to debug.json")
    detect.add_argument("input_pdf")
    detect.add_argument("--keywords", default=common_detect["keywords"])
    detect.add_argument("--debug-json", default="debug.json")
    detect.add_argument("--gap-factor", type=float, default=common_detect["gap_factor"])
    detect.set_defaults(func=cmd_detect)

    imp = sub.add_parser("import", help="Parse, screenshot, generate questions, and store cards")
    imp.add_argument("input_pdf")
    imp.add_argument("--keywords", default=common_detect["keywords"])
    imp.add_argument("--gap-factor", type=float, default=common_detect["gap_factor"])
    imp.add_argument("--dpi", type=int, default=200)
    imp.add_argument("--ollama-model", default=question_gen.DEFAULT_OLLAMA_MODEL)
    imp.add_argument("--ollama-url", default=question_gen.DEFAULT_OLLAMA_URL)
    imp.add_argument("--db", default="flashcards.db")
    imp.add_argument("--assets-dir", default="assets")
    imp.add_argument("--deck-name", default=None)
    imp.add_argument("--debug-json", default="debug.json")
    imp.set_defaults(func=cmd_import)

    exp = sub.add_parser("export", help="Build a self-contained .apkg from stored cards")
    exp.add_argument("--db", default="flashcards.db")
    exp.add_argument("--assets-dir", default="assets")
    exp.add_argument("--output", default="deck.apkg")
    exp.add_argument("--deck-name", default="script2anki")
    exp.add_argument("--types", default=None, help="Comma-separated types to include (default: all)")
    exp.add_argument("--language", choices=question_gen.LANGUAGES, default="en")
    exp.set_defaults(func=cmd_export)

    serve = sub.add_parser("serve", help="Launch the local study/browse/export web app")
    serve.add_argument("--db", default="flashcards.db")
    serve.add_argument("--assets-dir", default="assets")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5000)
    serve.set_defaults(func=cmd_serve)

    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
