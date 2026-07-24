"""Generate exam-style flashcard questions for detected elements, in
English and German.

Uses a local Ollama model when available, falling back to a plain
template when it isn't -- the tool must work fully offline with no LLM
installed, just with less natural-sounding questions. Chapter/section
context is deliberately NOT part of the question text -- the caller
displays that separately in the UI -- which keeps questions short and
direct.
"""

from __future__ import annotations

import re
from typing import Optional

import requests

from pdf_parser import Element

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2:3b"

LANGUAGES = ("en", "de")

PROVABLE_TYPES = {"Satz", "Lemma", "Korollar", "Proposition"}
BEWEIS_ANTECEDENT_TYPES = PROVABLE_TYPES | {"Bemerkung"}

_LEADING_HEADER_RE = re.compile(r"^\S+\s*[\d.]*\s*(\([^)]*\))?\.?\s*")
_BULLET_RE = re.compile(r"(?m)^\s*•")
_LETTERED_RE = re.compile(r"\(([a-h])\)")

_NUMBER_WORDS = {
    "en": {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"},
    "de": {2: "zwei", 3: "drei", 4: "vier", 5: "fünf", 6: "sechs", 7: "sieben", 8: "acht"},
}

_LANG_NAMES = {"en": "English", "de": "German"}


def ollama_available(base_url: str = DEFAULT_OLLAMA_URL, model: Optional[str] = None, timeout: float = 2.0) -> bool:
    try:
        r = requests.get(f"{base_url}/api/tags", timeout=timeout)
        r.raise_for_status()
    except requests.RequestException:
        return False
    if model is None:
        return True
    names = {m.get("name", "") for m in r.json().get("models", [])}
    return model in names or any(n.split(":")[0] == model.split(":")[0] for n in names)


def find_beweis_antecedent(elements: list, index: int, max_back: int = 4) -> Optional[Element]:
    """Walk back from a Beweis to the statement it's proving. Usually
    that's the immediately preceding element, but a short aside
    (Bemerkung, Beispiel) can sit in between -- so scan a few elements
    back, stopping if we cross into a different section (a proof won't
    reference a claim from an unrelated topic).
    """
    cur_section = (elements[index].heading_context or {}).get("section", {}).get("number")
    for j in range(index - 1, max(-1, index - 1 - max_back), -1):
        cand = elements[j]
        cand_section = (cand.heading_context or {}).get("section", {}).get("number")
        if cand_section != cur_section:
            break
        if cand.type in BEWEIS_ANTECEDENT_TYPES:
            return cand
    return None


def _identifier(el: Element) -> str:
    ident = f"{el.type} {el.number}" if el.number else el.type
    if el.paren_title:
        ident += f' ("{el.paren_title}")'
    return ident


def _heading_label(el: Element) -> str:
    ctx = el.heading_context or {}
    parts = []
    for level in ("chapter", "section"):
        h = ctx.get(level)
        if not h:
            continue
        parts.append(f"{level.capitalize()} {h['number']} ({h['title']})" if h.get("number") else h["title"])
    return ", ".join(parts) if parts else "the script"


def _context_snippet(el: Element, max_chars: int = 200) -> str:
    """The opening framing clause only (notation setup), with the
    keyword/number/title header itself stripped off -- not the result.
    """
    body = _LEADING_HEADER_RE.sub("", el.text, count=1)
    body = body.strip().replace("\n", " ")
    return body[:max_chars]


def _enumerated_count(el: Element) -> Optional[int]:
    """How many bullet/lettered sub-parts a statement has (e.g. three
    "(a)/(b)/(c)" equivalent conditions), if it looks confidently
    enumerated. Only meaningful for statement types -- a Beispiel's
    bullets are usually reasoning steps, not formal conditions.
    """
    if el.type not in PROVABLE_TYPES:
        return None
    bullets = len(_BULLET_RE.findall(el.text))
    lettered = len(set(_LETTERED_RE.findall(el.text)))
    n = max(bullets, lettered)
    return n if 2 <= n <= 8 else None


def _number_word(n: int, language: str) -> str:
    return _NUMBER_WORDS[language].get(n, str(n))


def template_question(el: Element, prev_el: Optional[Element] = None, language: str = "en") -> str:
    """Phrase a short, direct question from structural metadata only
    (type, number, parenthetical title, bullet count) -- no free-text
    extraction from the body, so it can't garble or leak content. This
    is inherently limited compared to an LLM: it can name and place a
    Definition/Satz precisely and note how many conditions it has, but
    can't ask about its specific content.
    """
    ident = _identifier(el)
    n = _enumerated_count(el)

    if language == "de":
        if el.type == "Beweis":
            if prev_el is not None and prev_el.type in BEWEIS_ANTECEDENT_TYPES:
                return f"Wie beweist man {_identifier(prev_el)}?"
            return f"Was wird in {ident} bewiesen?"
        if n:
            return f"Was sind die {_number_word(n, 'de')} Bedingungen in {ident}?"
        if el.type == "Definition":
            return f"Was besagt {ident}?"
        if el.type in PROVABLE_TYPES:
            return f"Was besagt {ident}?"
        if el.type == "Beispiel":
            return f"Was zeigt {ident}?"
        if el.type == "Bemerkung":
            return f"Worauf weist {ident} hin?"
        return f"Was besagt {ident}?"

    # English
    if el.type == "Beweis":
        if prev_el is not None and prev_el.type in BEWEIS_ANTECEDENT_TYPES:
            return f"How do you prove {_identifier(prev_el)}?"
        return f"What is proved in {ident}?"
    if n:
        return f"What are the {_number_word(n, 'en')} conditions in {ident}?"
    if el.type == "Definition":
        return f"What does {ident} say?"
    if el.type in PROVABLE_TYPES:
        return f"What does {ident} say?"
    if el.type == "Beispiel":
        return f"What does {ident} show?"
    if el.type == "Bemerkung":
        return f"What does {ident} point out?"
    return f"What does {ident} say?"


def _is_usable_question(text: str, max_len: int = 160) -> bool:
    """A small local model doesn't reliably follow instructions: it can
    answer in a flat declarative sentence instead of asking a question
    (an outright leak of the content), drift into raw LaTeX ($...$) that
    won't render in the plain-HTML card, or ignore the length budget
    entirely. Reject those rather than store them.
    """
    if not text or len(text) > max_len:
        return False
    if "?" not in text:
        return False
    if "$" in text:
        return False
    return True


def _ollama_question(el: Element, model: str, base_url: str, language: str, prev_el: Optional[Element] = None) -> Optional[str]:
    focus = _identifier(el)
    if el.type == "Beweis" and prev_el is not None and prev_el.type in BEWEIS_ANTECEDENT_TYPES:
        focus = f"the proof of {_identifier(prev_el)}"

    n = _enumerated_count(el)
    hint = ""
    if n:
        hint = (
            f" This statement has {n} distinct parts/conditions -- if that's natural, "
            f"ask specifically for the {_number_word(n, 'en')} conditions/properties rather than a generic question."
        )

    prompt = (
        "You write exam-style flashcard prompts for a German university math course. "
        f"Write ONE short question in {_LANG_NAMES[language]} (at most 15 words) that asks the student to recall "
        f"{focus} from {_heading_label(el)}."
        f"{hint} "
        "Name the type and number so the question is specific, but do NOT reveal the actual mathematical content, "
        "formula, or conclusion, and do NOT mention the chapter or section (that's shown separately). "
        f'Opening context (setup only, not the result): "{_context_snippet(el)}"\n'
        f"Respond with only the question itself in {_LANG_NAMES[language]}, one short sentence, no preamble or quotes."
    )
    try:
        resp = requests.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip().strip('"')
        return text if _is_usable_question(text) else None
    except (requests.RequestException, ValueError, KeyError):
        return None


def generate_question(
    el: Element, model: Optional[str], base_url: str, language: str, prev_el: Optional[Element] = None
) -> tuple:
    """Returns (question_text, source) where source is 'ollama' or
    'template' -- callers can tally how often the LLM output got
    rejected (not a question, too long, stray LaTeX) to gauge whether
    the chosen model is actually reliable enough for this use.
    """
    if model:
        q = _ollama_question(el, model, base_url, language, prev_el)
        if q:
            return q, "ollama"
    return template_question(el, prev_el, language), "template"


def generate_questions(
    el: Element, model: Optional[str], base_url: str = DEFAULT_OLLAMA_URL, prev_el: Optional[Element] = None
) -> dict:
    """Generate both language variants for a card: {lang: (text, source)}."""
    return {lang: generate_question(el, model, base_url, lang, prev_el) for lang in LANGUAGES}
