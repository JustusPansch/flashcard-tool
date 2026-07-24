"""Local web app for studying script2anki cards with spaced repetition,
browsing/filtering the deck by element type, and exporting an .apkg.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from flask import Flask, redirect, render_template, request, send_file, send_from_directory, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402
import deck_builder  # noqa: E402
import question_gen  # noqa: E402


def _question_for(card, language: str) -> str:
    if language == "de" and card["question_de"]:
        return card["question_de"]
    return card["question_en"] or card["question_de"]


def _context_for(card) -> dict:
    return {
        "chapter_number": card["chapter_number"],
        "chapter_title": card["chapter_title"],
        "section_number": card["section_number"],
        "section_title": card["section_title"],
    }


def create_app(db_path: str, assets_dir: str) -> Flask:
    app = Flask(__name__)
    # Resolve to absolute paths: Flask's send_from_directory and a bare
    # sqlite3.connect() both treat a relative path as relative to the
    # app's root_path (the webapp/ package dir), not the directory the
    # CLI was invoked from, so a relative "assets"/"flashcards.db" would
    # silently look in the wrong place.
    app.config["DB_PATH"] = str(Path(db_path).resolve())
    app.config["ASSETS_DIR"] = str(Path(assets_dir).resolve())

    def db():
        return store.connect(app.config["DB_PATH"])

    def selected_types(conn) -> list:
        types = store.all_types(conn)
        active = store.get_active_types(conn)
        return active if active is not None else types

    @app.context_processor
    def inject_language():
        conn = db()
        lang = store.get_language(conn)
        conn.close()
        return {"language": lang, "languages": question_gen.LANGUAGES}

    @app.route("/")
    def index():
        conn = db()
        types = store.all_types(conn)
        active = selected_types(conn)
        ctx = {
            "types": types,
            "active": set(active),
            "counts": store.counts_by_type(conn),
            "states": store.counts_by_state(conn, active),
            "due": store.due_count(conn, active),
            "total_active": sum(1 for c in store.get_cards(conn, active)),
            "disabled": store.disabled_count(conn),
        }
        conn.close()
        return render_template("index.html", **ctx)

    @app.route("/settings", methods=["POST"])
    def settings():
        conn = db()
        chosen = request.form.getlist("types")
        all_known = store.all_types(conn)
        store.set_active_types(conn, chosen if chosen else all_known)
        conn.close()
        return redirect(request.referrer or url_for("index"))

    @app.route("/language", methods=["POST"])
    def set_language():
        conn = db()
        lang = request.form.get("language")
        if lang in question_gen.LANGUAGES:
            store.set_language(conn, lang)
        conn.close()
        return redirect(request.referrer or url_for("index"))

    @app.route("/study")
    def study():
        conn = db()
        active = selected_types(conn)
        lang = store.get_language(conn)
        card = store.get_next_due(conn, active)
        remaining = store.due_count(conn, active)
        ctx = {
            "card": card,
            "remaining": remaining,
            "question": _question_for(card, lang) if card else None,
            "context": _context_for(card) if card else None,
        }
        conn.close()
        return render_template("study.html", **ctx)

    @app.route("/study/grade", methods=["POST"])
    def study_grade():
        conn = db()
        card_id = int(request.form["card_id"])
        rating = request.form["rating"]
        store.apply_grade(conn, card_id, rating)
        conn.close()
        return redirect(url_for("study"))

    @app.route("/cards/<int:card_id>/enabled", methods=["POST"])
    def card_enabled(card_id):
        conn = db()
        store.set_card_enabled(conn, card_id, request.form.get("enabled") == "1")
        conn.close()
        return redirect(request.referrer or url_for("study"))

    @app.route("/browse")
    def browse():
        conn = db()
        types = store.all_types(conn)
        lang = store.get_language(conn)
        filter_types = request.args.getlist("types") or None
        cards = store.get_cards(conn, filter_types, include_disabled=True)
        rows = [{"card": c, "question": _question_for(c, lang)} for c in cards]
        ctx = {"types": types, "filter_types": set(filter_types or types), "rows": rows}
        conn.close()
        return render_template("browse.html", **ctx)

    @app.route("/export", methods=["GET", "POST"])
    def export():
        conn = db()
        types = store.all_types(conn)
        active = selected_types(conn)
        default_lang = store.get_language(conn)
        if request.method == "POST":
            chosen = request.form.getlist("types") or types
            lang = request.form.get("language", default_lang)
            cards = store.get_cards(conn, chosen)
            deck_name = request.form.get("deck_name") or "script2anki"
            conn.close()
            if not cards:
                return render_template("export.html", types=types, active=set(chosen), error="No cards match that filter.")
            with tempfile.NamedTemporaryFile(suffix=".apkg", delete=False) as tmp:
                deck_builder.build_apkg(cards, tmp.name, deck_name, app.config["ASSETS_DIR"], lang)
                path = tmp.name
            return send_file(path, as_attachment=True, download_name=f"{deck_name}.apkg")
        conn.close()
        return render_template("export.html", types=types, active=set(active), error=None, export_lang=default_lang)

    @app.route("/assets/<path:filename>")
    def assets(filename):
        return send_from_directory(app.config["ASSETS_DIR"], filename)

    return app


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--db", default="flashcards.db")
    p.add_argument("--assets-dir", default="assets")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    args = p.parse_args()

    create_app(args.db, args.assets_dir).run(host=args.host, port=args.port, debug=False)
