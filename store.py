"""SQLite persistence for cards, their spaced-repetition state, and
the user's active-type filter setting.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import scheduler

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guid_key TEXT UNIQUE NOT NULL,
    source_pdf TEXT NOT NULL,
    deck_name TEXT NOT NULL,
    type TEXT NOT NULL,
    number TEXT,
    paren_title TEXT,
    chapter_number TEXT,
    chapter_title TEXT,
    section_number TEXT,
    section_title TEXT,
    question_en TEXT NOT NULL DEFAULT '',
    question_de TEXT NOT NULL DEFAULT '',
    image_filename TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'new',
    due_at TEXT NOT NULL,
    interval_days REAL NOT NULL DEFAULT 0,
    ease REAL NOT NULL DEFAULT 2.5,
    reps INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0,
    last_reviewed_at TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS review_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL REFERENCES cards(id),
    rating TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    prev_interval REAL,
    new_interval REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Best-effort migration for DBs created before bilingual questions:
    add question_en/question_de and backfill from the old single
    "question" column (assumed English) if it's still there.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(cards)").fetchall()}
    if not cols:
        return  # fresh DB, SCHEMA already created the current shape
    if "question_en" not in cols:
        conn.execute("ALTER TABLE cards ADD COLUMN question_en TEXT NOT NULL DEFAULT ''")
    if "question_de" not in cols:
        conn.execute("ALTER TABLE cards ADD COLUMN question_de TEXT NOT NULL DEFAULT ''")
    if "question" in cols:
        conn.execute("UPDATE cards SET question_en = question WHERE question_en = ''")
    if "enabled" not in cols:
        conn.execute("ALTER TABLE cards ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
    conn.commit()


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def guid_key_for(source_pdf: str, el_type: str, number: Optional[str], index: int) -> str:
    if number:
        return f"{source_pdf}::{el_type}::{number}"
    return f"{source_pdf}::{el_type}::idx{index}"


def upsert_card(conn: sqlite3.Connection, **fields) -> None:
    existing = conn.execute("SELECT id FROM cards WHERE guid_key = ?", (fields["guid_key"],)).fetchone()
    if existing:
        conn.execute(
            """UPDATE cards SET source_pdf=:source_pdf, deck_name=:deck_name, type=:type,
               number=:number, paren_title=:paren_title, chapter_number=:chapter_number,
               chapter_title=:chapter_title, section_number=:section_number,
               section_title=:section_title, question_en=:question_en, question_de=:question_de,
               image_filename=:image_filename
               WHERE guid_key=:guid_key""",
            fields,
        )
    else:
        conn.execute(
            """INSERT INTO cards (guid_key, source_pdf, deck_name, type, number, paren_title,
               chapter_number, chapter_title, section_number, section_title, question_en, question_de,
               image_filename, created_at, state, due_at)
               VALUES (:guid_key, :source_pdf, :deck_name, :type, :number, :paren_title,
               :chapter_number, :chapter_title, :section_number, :section_title, :question_en, :question_de,
               :image_filename, :created_at, 'new', :due_at)""",
            {**fields, "created_at": now_iso(), "due_at": now_iso()},
        )


def prune_stale(conn: sqlite3.Connection, source_pdf: str, seen_guid_keys: list) -> int:
    """Remove cards for this source PDF that weren't produced by the
    latest import (detection changed, element renumbered/removed, ...).
    """
    if not seen_guid_keys:
        placeholders = ""
        rows = conn.execute("SELECT id FROM cards WHERE source_pdf = ?", (source_pdf,)).fetchall()
    else:
        placeholders = ",".join("?" * len(seen_guid_keys))
        rows = conn.execute(
            f"SELECT id FROM cards WHERE source_pdf = ? AND guid_key NOT IN ({placeholders})",
            (source_pdf, *seen_guid_keys),
        ).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        id_placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM review_log WHERE card_id IN ({id_placeholders})", ids)
        conn.execute(f"DELETE FROM cards WHERE id IN ({id_placeholders})", ids)
    return len(ids)


def all_types(conn: sqlite3.Connection) -> list:
    rows = conn.execute("SELECT DISTINCT type FROM cards ORDER BY type").fetchall()
    return [r["type"] for r in rows]


def get_active_types(conn: sqlite3.Connection) -> Optional[list]:
    row = conn.execute("SELECT value FROM settings WHERE key='active_types'").fetchone()
    return json.loads(row["value"]) if row else None


def set_active_types(conn: sqlite3.Connection, types: list) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES ('active_types', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(types),),
    )
    conn.commit()


def get_language(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key='language'").fetchone()
    return row["value"] if row else "en"


def set_language(conn: sqlite3.Connection, language: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES ('language', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (language,),
    )
    conn.commit()


def _type_filter(types: Optional[list]):
    if not types:
        return "", []
    return f" AND type IN ({','.join('?' * len(types))})", list(types)


def get_cards(conn: sqlite3.Connection, types: Optional[list] = None, include_disabled: bool = False) -> list:
    clause, params = _type_filter(types)
    enabled_clause = "" if include_disabled else " AND enabled = 1"
    q = "SELECT * FROM cards WHERE 1=1" + enabled_clause + clause + " ORDER BY chapter_number, section_number, number"
    return conn.execute(q, params).fetchall()


def get_next_due(conn: sqlite3.Connection, types: Optional[list] = None):
    clause, params = _type_filter(types)
    q = "SELECT * FROM cards WHERE due_at <= ? AND enabled = 1" + clause + " ORDER BY due_at ASC LIMIT 1"
    return conn.execute(q, [now_iso(), *params]).fetchone()


def due_count(conn: sqlite3.Connection, types: Optional[list] = None) -> int:
    clause, params = _type_filter(types)
    q = "SELECT COUNT(*) AS n FROM cards WHERE due_at <= ? AND enabled = 1" + clause
    return conn.execute(q, [now_iso(), *params]).fetchone()["n"]


def disabled_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM cards WHERE enabled = 0").fetchone()["n"]


def set_card_enabled(conn: sqlite3.Connection, card_id: int, enabled: bool) -> None:
    conn.execute("UPDATE cards SET enabled = ? WHERE id = ?", (1 if enabled else 0, card_id))
    conn.commit()


def counts_by_type(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT type, COUNT(*) AS n FROM cards GROUP BY type ORDER BY type").fetchall()
    return {r["type"]: r["n"] for r in rows}


def counts_by_state(conn: sqlite3.Connection, types: Optional[list] = None) -> dict:
    clause, params = _type_filter(types)
    q = "SELECT state, COUNT(*) AS n FROM cards WHERE 1=1" + clause + " GROUP BY state"
    rows = conn.execute(q, params).fetchall()
    return {r["state"]: r["n"] for r in rows}


def apply_grade(conn: sqlite3.Connection, card_id: int, rating: str) -> None:
    card = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if not card:
        return
    now = datetime.now(timezone.utc)
    updates = scheduler.grade(dict(card), rating, now)
    conn.execute(
        """UPDATE cards SET state=:state, due_at=:due_at, interval_days=:interval_days,
           ease=:ease, reps=:reps, lapses=:lapses, last_reviewed_at=:reviewed_at
           WHERE id=:id""",
        {**updates, "reviewed_at": now.isoformat(), "id": card_id},
    )
    conn.execute(
        "INSERT INTO review_log(card_id, rating, reviewed_at, prev_interval, new_interval) "
        "VALUES (?,?,?,?,?)",
        (card_id, rating, now.isoformat(), card["interval_days"], updates["interval_days"]),
    )
    conn.commit()
