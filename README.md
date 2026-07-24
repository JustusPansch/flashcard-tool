# script2anki

Turns a math lecture script (PDF) into a spaced-repetition flashcard deck
where each card's back is a screenshot of the actual Satz/Definition/
Beispiel/... passage, and the front is a question generated from its
title and surrounding context. Study it in a local web app with an
Anki-style spaced-repetition algorithm, or export a self-contained
`.apkg` and study it in real Anki instead -- either at any time, and
they share the same underlying card store.

Everything runs locally; no network access or paid API is used.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional, for genuinely content-aware questions instead of the
structural-metadata template fallback ("What is Definition 1.1? Give the
definition."): install [Ollama](https://ollama.com) and pull a model.

```bash
curl -fsSL https://ollama.com/install.sh | sh   # needs sudo
ollama pull llama3.2:3b
```

If Ollama isn't installed or the model isn't pulled, `import` still works
end to end -- it just falls back to the template question for every card
and says so on the console.

**No sudo / low disk space on your main partition?** The standard
installer needs root (it registers a systemd service) and stores models
under `~/.ollama`. If that's not available, install the portable binary
instead and point it at whatever partition has room:

```bash
curl -fL "https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst" \
  -o /path/with/space/ollama.tar.zst
mkdir -p /path/with/space/ollama-portable && cd /path/with/space/ollama-portable
tar --zstd -xf ../ollama.tar.zst && rm ../ollama.tar.zst

export OLLAMA_MODELS=/path/with/space/ollama-portable/models
nohup ./bin/ollama serve > serve.log 2>&1 &   # runs until you kill it or reboot

OLLAMA_MODELS=/path/with/space/ollama-portable/models ./bin/ollama pull llama3.2:3b
```

This is exactly how it's set up on this machine right now: the root
filesystem had ~80MB free, so Ollama lives under
`/mnt/data/ollama-portable` instead, running as a plain background
process (`ollama serve`, started via `nohup`) rather than a systemd
service. It does **not** restart automatically on reboot -- if
`question_gen.ollama_available()` returns false after a reboot, re-run
the `nohup ... serve &` line above (with the same `OLLAMA_MODELS`).
To stop it: `pkill -f 'ollama-portable/bin/ollama serve'`.

## Usage

### 1. Check detection quality

```bash
python script2anki.py detect input.pdf --debug-json debug.json
```

Fast, no images or LLM calls -- just parses the PDF and dumps every
detected Satz/Definition/Beispiel/... plus the chapter/section outline to
`debug.json`. Review this before importing (see "Reviewing debug.json"
below); `import` (next step) writes the same debug.json too, so this is
mainly useful for quickly iterating on `--keywords`/`--gap-factor`
without regenerating screenshots.

### 2. Import: build the card set

```bash
python script2anki.py import input.pdf --ollama-model llama3.2:3b
```

Parses the PDF, renders a cropped screenshot per element into `assets/`,
generates a question **in both English and German** per card (two Ollama
calls per card when a model is given), and stores everything in
`flashcards.db` (SQLite). Re-running `import` on the same PDF updates
existing cards in place (by type+number, so your study progress is
preserved) and removes cards no longer detected, rather than duplicating.

Questions are deliberately short and don't mention chapter/section
context in the text itself (that's shown as a separate badge in the UI
and on the exported card's front). Statements with several bullet/
lettered sub-conditions get asked directly -- "What are the three
conditions in Satz 1.21?" -- and a Beweis is linked back to the theorem
it proves ("How do you prove Satz 1.10?") rather than asked generically.

An LLM response that isn't actually phrased as a question, runs long, or
contains raw LaTeX gets rejected and replaced with the template
fallback for that card; `import` prints how often this happened per
language. **This does not catch every failure mode** -- a small model
can still phrase a yes/no or "why does formula X hold?" question that
states the statement's conclusion outright, or occasionally hallucinate
content unrelated to the actual card. `llama3.2:3b` (2GB, CPU-friendly)
does this often enough to be worth knowing about; a larger model (e.g.
`mistral:7b`) is noticeably more reliable at "ask about X without
revealing X" if you run into this -- just pull it and re-run `import`
with `--ollama-model mistral:7b`. Either way, skim new cards in Browse
before relying on the deck for exam prep.

Options: `--keywords`, `--gap-factor` (see below), `--dpi` (screenshot
resolution, default 200), `--ollama-model` / `--ollama-url`, `--db`,
`--assets-dir`, `--deck-name` (defaults to the PDF's filename),
`--debug-json`.

You can import multiple PDFs into the same `flashcards.db` -- they're
kept apart internally by source file but studied and exported together
(filter by type if you only want one deck's material at a time).

### 3. Study

```bash
python script2anki.py serve
```

Opens a local web app at `http://127.0.0.1:5000`:

- **EN/DE toggle** (top right, every page) -- switches which stored
  question language is shown in Study/Browse and defaults the Export
  language. Persisted, so it stays put until you switch it again.
- **Dashboard** -- card counts per type and by scheduling state, and the
  type filter ("what do I want to study?" -- check off Satz, Definition,
  Beispiel, Lemma, Korollar, Bemerkung, Beweis, or any subset). This
  filter is persisted and drives both the study queue and the Export
  page's default selection.
- **Study** -- one question at a time (chapter/section shown as a small
  subtitle above it, not part of the question text). Press space (or
  click) to reveal the answer screenshot, then grade your recall with
  Again/Hard/Good/Easy (keys 1-4), same as Anki's four-button review.
  Grading schedules the card's next appearance using a simplified SM-2
  algorithm (see below) and immediately serves the next due card. A
  "Disable card" button next to the badge pulls a card out of the study
  rotation (and export) without deleting it -- for a card whose question
  is garbled or whose content you don't want to be quizzed on.
- **Browse** -- table of every card with its type, chapter/section,
  question (in whichever language is toggled), scheduling state, and a
  link to view the full screenshot. Check new cards here for LLM
  leaks/hallucinations before trusting them (see the caveat above).
  Disabled cards show dimmed with a re-enable link.
- **Export** -- pick a type subset, deck name, and language, download a
  self-contained `.apkg` with images bundled, importable into Anki on
  any device. The exported card's front includes the chapter/section
  context (real Anki has no equivalent of the web app's separate badge).

### 4. Export from the command line

```bash
python script2anki.py export --output deck.apkg --types Satz,Definition --language de
```

Same export the web app's Export page produces, for scripting. Omit
`--types` to export everything in the DB; omit `--language` for English.

## The spaced-repetition algorithm

A simplified SM-2 -- the same family of algorithm classic Anki uses, not
a byte-for-byte clone (Anki's scheduler has changed across versions, and
current Anki defaults to FSRS, which needs trained per-user weights).
What's implemented (`scheduler.py`):

- Every card tracks an ease factor (starts at 2.5) and an interval in
  days, same as Anki.
- New cards graduate out of a short "learning" state on any rating other
  than Again (Hard -> 1 day, Good -> 3 days, Easy -> 4 days); Again keeps
  it in learning and brings it back in 10 minutes.
- Once graduated, each grade scales the interval: Good multiplies it by
  the ease factor, Easy also boosts the ease factor, Hard shrinks the
  ease factor and grows the interval more slowly, and Again ("lapse")
  halves the interval and drops the ease factor rather than resetting to
  zero.

## Reviewing `debug.json`

Before trusting the deck, check `debug.json` (written by both `detect`
and `import`):

- `headings`: the detected chapter/section outline -- compare against the
  script's table of contents.
- `elements`: one entry per detected Satz/Definition/etc., with its type,
  number, parenthetical title, chapter/section context, page range,
  bounding box(es), the reason detection ended that element
  (`new_element` / `heading` / `other_block` / `whitespace_gap` /
  `end_of_document`), and the extracted text.
- Text order for complex math layouts (arrays, piecewise definitions,
  stacked fractions) can look jumbled in the extracted `text` field --
  that's just how left-to-right/top-to-bottom text extraction linearizes
  a 2-D layout. It doesn't affect the screenshot, which crops the
  original page region as an image.

## How detection works

- **Headings** (chapter/section): lines noticeably larger and bolder than
  body text. Numbering dots determine nesting (`2` = chapter, `2.3` =
  section, `2.3.1` = subsection).
- **Elements**: lines starting with a configured keyword (`--keywords`,
  default `Satz,Definition,Beispiel,Lemma,Korollar,Proposition,Bemerkung,Beweis`),
  styled distinctly right at the start of the line (bold, or italic for
  "Beweis") -- checked on the line's *leading* span specifically, so an
  inline bold/italic word later in an unrelated sentence can't cause a
  false match, and a mid-sentence reference like "nach Satz 1.19" won't
  trigger one either.
- **Element boundaries**: an element ends when the next element or
  heading starts, when a bold line-start signals some other kind of
  block (an unnumbered aside, a recurring section summary, an
  unrecognized numbered type like "Aufgabe") even if it's not one of the
  tracked keywords, when a vertical gap far exceeds the document's normal
  line spacing and isn't just a diagram (`--gap-factor`, default 8x the
  median line gap), or at end of document.
- **Multi-page elements**: if an element is still open when a page ends
  and the next page doesn't start with a new element/heading, it keeps
  reading onto the next page; the screenshot stitches one crop per page
  into a single image.
- Running headers/footers (page numbers) and outer-margin annotations
  (e.g. per-page lecture markers) are filtered out so they don't pollute
  headings, elements, or bounding boxes.

## Project layout

```
pdf_parser.py     structure detection (headings + Satz/Definition/... elements)
screenshot.py     crops detected elements to PNGs, stitching multi-page ones
question_gen.py   Ollama-based question generation, with template fallback
deck_builder.py   genanki .apkg export with bundled media
store.py          SQLite persistence: cards, scheduling state, settings
scheduler.py      the spaced-repetition algorithm
webapp/           Flask study/browse/export app
script2anki.py    CLI: detect / import / export / serve
```
