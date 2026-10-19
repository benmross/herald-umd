#!/usr/bin/env python
"""Keep the `UMD Campus Events` Google Calendar current, deterministically.

This is the writing half of umd-calendar-sync, folded into Herald. The user's
old service scraped UMD's event sites (`collectors/umd_events.py` still runs
that half), then paid a Claude session every day to decide which events
belonged on the calendar. On 9 September 2026 they asked for the whole thing
to live here and to stop spending tokens on it: **every** scraped event goes
on the calendar, no judgment step, and the calendar is Herald's to keep true
from now on. It started as a two-week window like the old service; the user
removed that the same night ("far out events are useful to know about"), so
the window is now a year, which is a bound on the API reads rather than a
policy -- nothing on campus publishes further ahead than that.

So this is a collector in the scaffolding sense (a cadence, failure streaks,
one function) but it writes outward rather than inward. It is the only
collector that does, and every write goes through `lib/herald/gwrite.py`,
which records each one in the `actions` table for the morning digest.

What it does each pass:

  1. Desired = `umd_events/event` facts starting in [today, today + window)
     that the model review kept (`cycles/campus_review.py`, 18 Oct 2026,
     which replaced the keyword filters for religious events, Greek life and
     recurring meetings at the user's request). Unreviewed events are held back
     while the review is healthy.
  2. Live = everything on the calendar in the same window.
  3. Match each desired event to a live one by `syncKey` (the scraper's
     stable key, e.g. `terplink:12707004`), then by the alternate keys the
     de-duplicator says are the same event, then by title + start against
     any live event nobody owns (adoption -- so hand-added or pre-Herald
     events are stamped rather than duplicated).
  4. Create what is missing; update what changed; for a hand-edited event
     keep their summary/time/description and refresh only location and link.
  5. An event the source now marks cancelled, or that has vanished from a
     source that was scraped successfully, is retitled `CANCELLED - ` and
     greyed. **Nothing cancelled is ever deleted.** Calendar has no trash, and a
     greyed event on a firehose calendar they keep toggled off costs nothing.
  6. An event already on the calendar that the review dropped, or that the
     de-duplicator folded into another, is removed, not greyed -- it was
     never cancelled, it is simply unwanted, and a calendar of false
     cancellations is worse than none. Only clean scraper-written events
     nobody has edited qualify; see `gwrite.calendar_delete_synced`.
  7. Scout's `email:` events are never created or updated here, but one
     the review dropped or called a duplicate is removed. `manual:`
     schedules are left alone entirely.

`python collectors/campus_calendar.py --dry-run` prints the plan and writes
nothing; `--audit` adds the full live-versus-desired comparison.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import shutil
import sys

# An extension lives outside the program, so it locates it rather than assuming
# a relative path to it. `herald collect` already puts both on PYTHONPATH; this
# is what makes running the file directly work too.
_EXT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_EXT / "lib"))
_root = os.environ.get("HERALD_ROOT")
if not _root and (_herald := shutil.which("herald")):
    _root = str(pathlib.Path(_herald).resolve().parents[1])
if _root:
    sys.path.insert(0, str(pathlib.Path(_root) / "lib"))

from herald import calsync, collector, config, db, gwrite  # noqa: E402
import umd_review  # noqa: E402

NAME = "campus_calendar"
REQUIRES = ('google', 'campus_events')
# Four passes a day. The scrape behind it runs once a day; the extra passes
# pick up scout's email-derived events and anything the user edits by hand.
CADENCE_MINUTES = 360

ACTOR = f"collector:{NAME}"
CALENDAR_NAME = config.get("calendars.write.campus_event", "UMD Campus Events")
WINDOW_DAYS = int(config.get("calendars.campus_events.window_days", 14) or 14)
COLORS = {"social": "6", "academic": "9", "cancelled": "8"}
COLORS.update(config.get("calendars.campus_events.colors", {}) or {})

# syncKeys this collector is allowed to remove: scraper sources, and since
# 18 Oct 2026 scout's `email:` events, which the review now judges too (the user
# wanted every candidate reviewed, and two emails about one event had put it
# on the calendar twice). Never `manual:`.
PURGEABLE_PREFIXES = ("terplink:", "calendar:", "clarice:", "careers:",
                      "umd:", "events:", "stamp:", "athletics:", "arts:",
                      "umdcal:", "cs:", "email:")

# Same reasoning as MAX_CANCELS_PER_PASS: a category filter that suddenly
# wants to remove half the calendar has a bug, not a mandate.
MAX_PURGES_PER_PASS = 60

# Only a source that publishes its *whole* list can say an event is gone by
# omission. TerpLink's ICS is complete with stable ids; calendar.umd.edu's
# GraphQL and the Clarice's paginated list are partial by construction, so an
# event missing from them today is usually just beyond what was fetched.
VANISH_SOURCES = {"terplink"}

# If a pass wants to cancel more than this many events at once, the input is
# wrong, not the calendar. On 9 Sep 2026 the first timer run planned against
# half-migrated facts and greyed 78 live events in four minutes. A real source
# never drops that many at once; a stale or mis-keyed snapshot does.
MAX_CANCELS_PER_PASS = 15
ACADEMIC_HINTS = ("academic", "career", "professional", "research", "lecture",
                  "workshop", "training", "info session", "information session")
# The scrapers' circuit breaker (lib/umd_scrape), kept by umd_events.
BREAKER = config.LEDGER / "raw" / "umd" / "state" / "breaker.json"


def _healthy_sources() -> set[str]:
    """Sources whose last scrape succeeded, so their silence means something."""
    try:
        state = json.loads(BREAKER.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return {name for name, s in state.items() if not (s or {}).get("fails")}


def _category(d: dict) -> str:
    cats = " ".join(str(c) for c in (d.get("categories") or [])).lower()
    if d.get("source") == "careers" or any(h in cats for h in ACADEMIC_HINTS):
        return "academic"
    return "social"


def _description(row, d: dict) -> str:
    parts = [(row["body"] or "").strip()]
    if d.get("host"):
        parts.append(f"Host: {d['host']}")
    if d.get("url"):
        parts.append(d["url"])
    return "\n\n".join(p for p in parts if p)[:2000]


def desired_events(con, lo: dt.datetime, hi: dt.datetime,
                   live_keys: set[str], hold: bool) -> tuple[dict, set[str], dict, int]:
    """key -> event body, the keys the source says are cancelled, a key ->
    reason map of the ones that must go, and how many are held for review.

    Since 18 Oct 2026 what belongs is the review's call (`cycles/
    campus_review.py`): a kept event goes on, a dropped one comes off. An
    event with no current verdict is held off the calendar while the review
    is healthy, unless it is already there, in which case it stays as it is
    until judged. Held events are wanted, never "ignored", so nothing is
    removed or greyed on the strength of a verdict that does not exist yet.
    """
    rows = con.execute("""
        SELECT * FROM facts WHERE source='umd_events' AND kind='event'
          AND ts >= ? AND ts < ? ORDER BY ts
    """, (lo.isoformat(), hi.isoformat())).fetchall()
    verdicts = umd_review.verdicts(con)
    want: dict[str, dict] = {}
    cancelled: set[str] = set()
    ignored: dict[str, str] = {}
    held = 0
    for r in rows:
        d = json.loads(r["data"] or "{}")
        key = r["external_id"]
        v = verdicts.get(key)
        h = umd_review.content_hash(r["title"], r["ts"], d, r["body"])
        if umd_review.current(v, h) and not v.get("keep"):
            ignored[key] = _why(v)
        elif not umd_review.current(v, h) and hold and key not in live_keys:
            held += 1
            continue
        # The de-duplicator folded these other records into this one. Any
        # calendar copy still filed under one of them is the duplicate, so it
        # goes; otherwise a TerpLink alias would read as vanished and be greyed
        # CANCELLED, and an umdcal or careers one would sit beside it forever.
        for alt in d.get("merged_keys") or []:
            ignored.setdefault(alt, f"duplicate of {key}")
        if key in ignored:
            continue
        if (d.get("status") or "confirmed") == "cancelled":
            cancelled.add(key)
            continue
        body = gwrite.build_event(
            summary=r["title"], start=r["ts"], end=d.get("end"),
            all_day=bool(d.get("all_day")), location=d.get("location") or "",
            description=_description(r, d), color_id=COLORS[_category(d)],
            sync_key=key, source_url=d.get("url"),
            source_title=(d.get("source") or "source").title(),
        )
        body["_aliases"] = list(d.get("merged_keys") or [])
        body["_source"] = d.get("source") or key.split(":", 1)[0]
        want[key] = body
    return want, cancelled, ignored, held


def _why(v: dict) -> str:
    if v.get("duplicate_of"):
        return f"review: duplicate of {v['duplicate_of']}"
    return f"review: {v.get('reason') or 'dropped'}"


def _dropped_live(live: list[dict], want: dict, ignored: dict,
                  verdicts: dict) -> dict[str, str]:
    """Remove live events the review dropped whose facts are gone.

    TerpLink drops an event from its feed once it starts, so on 18 Oct 2026
    the noon "Dubbers Meeting" was still on the calendar at 6:57 PM: its fact
    had left the scrape, and an event dated today is never treated as
    vanished. Verdicts outlive the feed, so the verdict on its key decides.
    The same path removes a stale copy left by a source that renamed or
    moved an event, and a scout `email:` event, once the review has called
    either a duplicate.
    """
    out = {}
    for e in live:
        k = e.get("syncKey") or ""
        if not k.startswith(PURGEABLE_PREFIXES) or k in want or k in ignored:
            continue
        v = verdicts.get(k)
        if v and not v.get("keep"):
            out[k] = _why(v)
    return out


# The diff, the adoption rules, the caps and the writing all live in
# lib/herald/calsync.py now: none of that was ever about this campus, and
# anything that produces a list of dated things wants exactly the same engine.
# What stays here is the part that genuinely is about UMD -- which facts count
# as desired, which the review dropped, and which are duplicates.

def collect(con) -> dict:
    dry = "--dry-run" in sys.argv or "--audit" in sys.argv
    audit = "--audit" in sys.argv
    tz = config.tz()
    today = dt.datetime.now(tz).date()
    lo = dt.datetime.combine(today, dt.time.min, tzinfo=tz)
    hi = lo + dt.timedelta(days=WINDOW_DAYS)

    cal_id = gwrite.calendar_id(CALENDAR_NAME)
    if not cal_id:
        raise RuntimeError(f"no calendar named {CALENDAR_NAME!r} on this account")

    live = gwrite.calendar_window(cal_id, lo, hi)
    hold = umd_review.healthy(con, dt.datetime.now(tz))
    want, cancelled, ignored, held = desired_events(
        con, lo, hi, {e["syncKey"] for e in live if e["syncKey"]}, hold)
    ignored.update(_dropped_live(live, want, ignored, umd_review.verdicts(con)))
    if not hold:
        print("  campus review has not succeeded in 36h; unreviewed events go on",
              file=sys.stderr)
    ops = calsync.plan(live, want, cancelled, VANISH_SOURCES, today, ignored)
    # `--max-purges N` lifts the cap for one supervised pass, as when a new
    # rule has a backlog to clear (18 Oct 2026: about 600 recurring meetings).
    cap = MAX_PURGES_PER_PASS
    if "--max-purges" in sys.argv:
        cap = int(sys.argv[sys.argv.index("--max-purges") + 1])
    ops = calsync.guard(ops, max_cancels=MAX_CANCELS_PER_PASS,
                        max_purges=cap,
                        report=lambda m: print(f"  {m}", file=sys.stderr))

    if audit:
        managed = sum(1 for e in live if e["managed"])
        print(f"window {lo.date()} → {hi.date()}: {len(want)} desired, "
              f"{len(live)} live ({managed} managed, {len(live) - managed} unmanaged, "
              f"{sum(1 for e in live if e['user_modified'])} hand-edited), "
              f"{len(cancelled)} cancelled at source, {len(ignored)} dropped or "
              f"duplicate, {held} held for review")
        for line in calsync.describe(ops):
            print(f"  {line}")

    counts = calsync.apply(
        con, ops, actor=ACTOR, cal_id=cal_id, cal_name=CALENDAR_NAME,
        cancelled_color=COLORS["cancelled"], purgeable_prefixes=PURGEABLE_PREFIXES,
        dry_run=dry, report=lambda m: print(f"  {m}", file=sys.stderr))

    if dry:
        print(f"dry run: would create {counts['created']}, update {counts['updated']}, "
              f"merge {counts['merged']}, adopt {counts['adopted']}, "
              f"cancel {counts['cancelled']}, remove {counts['removed']}; "
              f"{counts['unchanged']} unchanged")
    return counts


if __name__ == "__main__":
    collector.main(NAME, collect)
