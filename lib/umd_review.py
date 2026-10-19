"""Model verdicts on campus events: what is stored, and how a verdict is found.

Every candidate event is reviewed by a model, with no keyword filtering beside
it, because a filter by category throws away the exceptions a person would go
to: the one event from a kind of host they usually skip that is open to
everyone. So the judgment is the model's, made once per event and stored, and
both the review cycle and the calendar sync read it from here.

A verdict is a fact, `campus_review/verdict`, keyed by the event's syncKey. It
carries a hash of what the model was shown. When the listing changes (a new
time, a new description) the hash no longer matches and the event is judged
again; otherwise it never is. Verdicts are never cleared, so an event that has
left the feed (TerpLink drops one once it starts) still has its answer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

#: Bumped when what a verdict means changes, so every event is judged again.
#: 2 (18 Oct 2026): the review also decides duplicates, with each day's
#: listings and calendar events in front of it at once.
VERSION = 2

SOURCE = "campus_review"
KIND = "verdict"
STATE = "state"

#: If no review has succeeded for this long, the calendar stops holding
#: unreviewed events back. A broken review must not quietly empty the
#: calendar of everything new; they said they never wants to miss an event.
STALE_AFTER = dt.timedelta(hours=36)


def content_hash(title: str | None, ts: str | None, data: dict, body: str | None) -> str:
    parts = [title or "", ts or "", data.get("end") or "", data.get("location") or "",
             data.get("host") or "", (body or "")[:600]]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def verdicts(con) -> dict[str, dict]:
    """syncKey -> {"keep", "duplicate_of", "reason", "hash", "v", "model",
    "reviewed_at"}. A duplicate always has keep false."""
    out = {}
    for r in con.execute("SELECT external_id, data FROM facts WHERE source=? AND kind=?",
                         (SOURCE, KIND)):
        try:
            out[r[0]] = json.loads(r[1] or "{}")
        except json.JSONDecodeError:
            continue
    return out


def current(v: dict | None, h: str) -> bool:
    """A verdict exists and was made on what the listing says now."""
    return bool(v) and v.get("hash") == h and v.get("v") == VERSION


def last_success(con) -> dt.datetime | None:
    row = con.execute("SELECT data FROM facts WHERE source=? AND kind=? AND external_id='last_success'",
                      (SOURCE, STATE)).fetchone()
    if not row:
        return None
    try:
        return dt.datetime.fromisoformat(json.loads(row[0])["at"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def healthy(con, now: dt.datetime) -> bool:
    last = last_success(con)
    return last is not None and now - last < STALE_AFTER
