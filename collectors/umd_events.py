#!/usr/bin/env python
"""UMD campus events, scraped into the ledger.

The scrapers (lib/umd_scrape) read TerpLink's and the CS department's ICS
feeds, calendar.umd.edu's GraphQL endpoint, and the STAMP and Careers listing
pages, then merge records of the same event. Everything is deterministic and
spends no tokens; whether an event belongs on the user's calendar is decided
later by the campus_review cycle, and `collectors/campus_calendar.py` writes
what it kept.

Until 18 Oct 2026 this shelled out to umd-calendar-sync, the standalone
service the scrapers came from. They were moved in so the extension can be
installed by someone else. The candidate `key` is still the fact's
external_id because it is also the `syncKey` stamped on every calendar event,
which is what lets the calendar sync recognise its own earlier work.
"""
from __future__ import annotations

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

from herald import collector, config, db  # noqa: E402
from umd_scrape import common as scrape_common, scrape  # noqa: E402

NAME = "umd_events"
REQUIRES = ('campus_events',)

# How often this is worth running:
# About a minute of scraping the UMD sources, all of them gently. Running this
# every half hour would be 48 scrapes a day of somebody else's servers to
# learn nothing new.
CADENCE_MINUTES = 1440
#: The HTTP cache and circuit breaker: disposable, like everything in raw/.
STATE = config.LEDGER / "raw" / "umd"

# The upstream category hint arrives as a repr of icalendar objects, which is
# unreadable and enormous. Pull the actual words out.
_VTEXT = re.compile(r"vText\(b'([^']+)'\)")


def _categories(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    found = _VTEXT.findall(str(raw))
    return found or [str(raw)[:80]]


def collect(con) -> dict:
    scrape_common.configure(STATE / "state", config.get("umd.contact_email", ""))
    cfg = scrape.default_config()
    cfg["window_days"] = int(config.get("calendars.campus_events.window_days", 365) or 365)
    for key, value in (config.get("umd.sources") or {}).items():
        cfg[key] = value
    known = [r[0] for r in con.execute(
        "SELECT external_id FROM facts WHERE source=? AND kind='event'", (NAME,))]
    payload = scrape.run(cfg, known=known,
                         extra_sources=config.get("umd.extra_sources") or [])
    events = payload.get("candidates", [])

    tripped = payload.get("breaker_tripped") or []
    db.clear(con, NAME, "event")

    for e in events:
        # `key` is the de-duplicator's stable id ("terplink:12707004") and the
        # syncKey on the calendar. It used to fall through to source:title,
        # which is neither stable nor what the calendar carries.
        uid = (e.get("key") or e.get("uid") or e.get("id")
               or f"{e.get('source')}:{e.get('title')}")
        db.put_fact(
            con, NAME, "event", external_id=str(uid), ts=e.get("start"),
            title=e.get("title") or "(untitled)",
            body=(e.get("description") or "")[:1500] or None,
            data={
                "end": e.get("end"),
                "all_day": e.get("all_day"),
                "location": e.get("location"),
                "source": e.get("source"),
                "url": e.get("url") or e.get("source_url"),
                "categories": _categories(e.get("category_hint")),
                "host": e.get("host"),
                "status": e.get("status") or "confirmed",
                "merged_keys": e.get("merged_keys") or [],
                "also_seen_in": e.get("also_seen_in") or [],
                "estimated": e.get("estimated") or [],
            },
        )

    # Upstream reports this as a count in some versions and a list in others.
    suppressed = payload.get("athletics_suppressed") or 0
    counts = {"events": len(events),
              "suppressed as duplicate athletics":
                  suppressed if isinstance(suppressed, int) else len(suppressed)}
    if tripped:
        # A source that keeps failing is switched off upstream. Herald should
        # say so rather than quietly reporting fewer events.
        counts["sources tripped"] = len(tripped)
        print(f"  circuit breaker open on: {', '.join(map(str, tripped))}",
              file=sys.stderr)
    return counts


if __name__ == "__main__":
    collector.main(NAME, collect)
