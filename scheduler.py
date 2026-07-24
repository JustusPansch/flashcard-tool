"""Simplified SM-2 / Anki-style spaced repetition scheduler.

Not a byte-for-byte clone of Anki's scheduler (which itself has changed
across versions, and newer Anki defaults to FSRS) -- this implements the
same recognizable shape: an ease factor and a growing interval, four
grading buttons, cards graduate from a short "learning" phase into a
long-lived "review" phase, and failing a review card knocks its interval
back down rather than starting over from scratch.

State machine: new -> learning (cycles on "again") -> review (stays
review afterwards; interval grows/shrinks with each grade, never demotes
back to learning).
"""

from __future__ import annotations

from datetime import timedelta

LEARNING_AGAIN_MINUTES = 10
GRADUATE_HARD_DAYS = 1
GRADUATE_GOOD_DAYS = 3
GRADUATE_EASY_DAYS = 4

MIN_EASE = 1.3
EASE_STEP = 0.15
LAPSE_EASE_STEP = 0.20
LAPSE_INTERVAL_FACTOR = 0.5

RATINGS = ("again", "hard", "good", "easy")


def grade(card: dict, rating: str, now) -> dict:
    if rating not in RATINGS:
        raise ValueError(f"unknown rating: {rating!r}")

    state = card.get("state") or "new"
    ease = card.get("ease") or 2.5
    interval = card.get("interval_days") or 0.0
    reps = card.get("reps") or 0
    lapses = card.get("lapses") or 0

    if state != "review":
        if rating == "again":
            new_state, new_interval = "learning", 0.0
            due = now + timedelta(minutes=LEARNING_AGAIN_MINUTES)
        else:
            new_state = "review"
            new_interval = {
                "hard": GRADUATE_HARD_DAYS,
                "good": GRADUATE_GOOD_DAYS,
                "easy": GRADUATE_EASY_DAYS,
            }[rating]
            due = now + timedelta(days=new_interval)
            reps += 1
        new_ease = ease
    else:
        if rating == "again":
            lapses += 1
            new_ease = max(MIN_EASE, ease - LAPSE_EASE_STEP)
            new_interval = max(1.0, interval * LAPSE_INTERVAL_FACTOR)
            new_state = "review"
            due = now + timedelta(days=new_interval)
        elif rating == "hard":
            new_ease = max(MIN_EASE, ease - EASE_STEP)
            new_interval = max(interval * 1.2, interval + 1)
            new_state = "review"
            due = now + timedelta(days=new_interval)
        elif rating == "good":
            new_ease = ease
            new_interval = max(1.0, interval * ease)
            new_state = "review"
            due = now + timedelta(days=new_interval)
            reps += 1
        else:  # easy
            new_ease = ease + EASE_STEP
            new_interval = max(1.0, interval * ease * 1.3)
            new_state = "review"
            due = now + timedelta(days=new_interval)
            reps += 1

    return {
        "state": new_state,
        "due_at": due.isoformat(),
        "interval_days": round(new_interval, 3),
        "ease": round(new_ease, 3),
        "reps": reps,
        "lapses": lapses,
    }
