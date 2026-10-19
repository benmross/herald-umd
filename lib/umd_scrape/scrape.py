"""Run every UMD event source, merge, and return the candidates.

Deterministic: no model is involved. The review of what belongs on the
calendar happens later, in the campus_review cycle.

This came from umd-calendar-sync, a standalone service that predates Herald,
and was moved into the extension on 18 Oct 2026 so that the extension could be
installed by someone else. Sources are the public ICS feeds in sources.json,
calendar.umd.edu's GraphQL endpoint, and the STAMP and Careers listing pages.
`umd.extra_sources` may name more source modules (each a file with a
`collect(cfg, start, end, known_keys)` function); that is how a scraper that
runs under one person's own permission stays out of the published code.
"""

import datetime as dt
import importlib.util
import json
import logging
import pathlib
import re

from zoneinfo import ZoneInfo

from umd_scrape.common import source_rank, tripped_sources
from umd_scrape.sources import ics_source, umdcal_graphql, umd_component

log = logging.getLogger("umd-sync.collect")
ET = ZoneInfo("America/New_York")
DEFAULTS = pathlib.Path(__file__).with_name("sources.json")


def default_config():
    return json.loads(DEFAULTS.read_text())


def window(cfg, days=None):
    now = dt.datetime.now(ET)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=days or cfg.get("window_days", 14))


def merge(cands):
    """Collapse cross-source duplicates.

    Same key wins outright. Otherwise treat same start-minute + very similar
    title as the same event, and prefer the record carrying more detail --
    which is how a stamp event with no location inherits umdcal's location.
    """
    import re

    def norm(t):
        return re.sub(r"[^a-z0-9]", "", t.lower())[:40]

    by_key, order = {}, []
    for c in cands:
        if c["key"] in by_key:
            continue
        by_key[c["key"]] = c
        order.append(c["key"])

    merged, seen_sig = [], {}
    for k in order:
        c = by_key[k]
        # status is part of the signature on purpose: a cancelled and a confirmed
        # record for the same slot must NEVER collapse, or we would silently drop
        # the cancellation. Two such records mean something changed at the source
        # (typically a cancelled original plus a rescheduled replacement), which
        # is exactly the case Claude should adjudicate rather than us guessing.
        sig = (c["start"][:16], norm(c["title"]), c["status"])
        if sig in seen_sig:
            prev = seen_sig[sig]
            winner = _combine(prev, c)
            idx = merged.index(prev)
            merged[idx] = winner
            seen_sig[sig] = winner
            continue
        seen_sig[sig] = c
        merged.append(c)
    return merged


def _combine(a, b):
    """Fold two records for the same event into one.

    Two rules matter here, both learned the hard way:

    * A field a source GUESSED must never overwrite one it actually published.
      The Clarice supplies no end time, so its collector assumes two hours; on
      field-count alone that record outscored calendar.umd.edu and would have
      rewritten NextNOW Fest from its real 13:00-23:00 down to 13:00-15:00.
    * The surviving KEY comes from the highest-priority source, not from
      whichever record happens to look richer. Otherwise the key flips as
      sources come and go, and since a differing syncKey blocks adoption, the
      event gets duplicated rather than updated.
    """
    keyholder = a if source_rank(a["source"]) <= source_rank(b["source"]) else b
    other = b if keyholder is a else a

    out = dict(keyholder)
    out["estimated"] = list(keyholder.get("estimated") or [])
    # An all-day listing of something another source gives a time for is a
    # date without a time, so the timed record's clock wins.
    if keyholder.get("all_day") and not other.get("all_day"):
        out["estimated"] = sorted(set(out["estimated"]) | {"start", "end", "all_day"})

    for field in ("start", "end", "location", "description", "url", "host",
                  "category_hint", "all_day"):
        mine_est = field in out["estimated"]
        theirs_est = field in (other.get("estimated") or [])
        take_theirs = (
            (mine_est and not theirs_est and other.get(field) not in (None, ""))
            # False is a value for all_day, not a gap to fill.
            or (field != "all_day" and not out.get(field) and other.get(field))
        )
        if take_theirs:
            out[field] = other[field]
            if field in out["estimated"]:
                out["estimated"].remove(field)

    seen = set(out.get("also_seen_in") or [])
    seen.add(other["source"])
    seen.discard(out["source"])
    out["also_seen_in"] = sorted(seen)
    # Remember the discarded identity so apply.py can re-key an event already on
    # the calendar under it instead of creating a second copy.
    alts = set(out.get("merged_keys") or []) | set(other.get("merged_keys") or [])
    alts.add(other["key"])
    alts.discard(out["key"])
    out["merged_keys"] = sorted(alts)
    return out


def _detail_score(c):
    return sum(bool(c.get(f)) for f in ("location", "description", "url", "host", "category_hint"))


def _load_extra(path):
    spec = importlib.util.spec_from_file_location(f"umd_extra_{pathlib.Path(path).stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(cfg, days=None, known=(), extra_sources=()):
    """{"candidates": [...], "counts": {...}, ...}, the shape candidates.json had."""
    start, end = window(cfg, days)
    log.info("window %s -> %s", start.date(), end.date())
    known = set(known)

    candidates, exclusions = [], []
    ics_c, ics_x = ics_source.collect(cfg, start, end)
    candidates += ics_c
    exclusions += ics_x
    candidates += umdcal_graphql.collect(cfg, start, end, known)
    candidates += umd_component.collect(cfg, start, end, known)
    for path in extra_sources or ():
        try:
            candidates += _load_extra(pathlib.Path(path).expanduser()).collect(cfg, start, end, known)
        except Exception as e:                                # noqa: BLE001
            log.error("extra source %s failed: %s", path, e)

    before = len(candidates)
    candidates = [c for c in candidates
                  if not ics_source.is_athletics_duplicate(c, exclusions)]
    suppressed = before - len(candidates)
    candidates = merge(candidates)

    by_source = {}
    for c in candidates:
        by_source[c["source"]] = by_source.get(c["source"], 0) + 1
    return {
        "generated": dt.datetime.now(ET).isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "counts": by_source,
        "athletics_suppressed": suppressed,
        "breaker_tripped": tripped_sources(),
        "candidates": sorted(candidates, key=lambda x: x["start"]),
    }
