"""Collector for UMD sites built on the university design-system event component.

stamp.umd.edu and careers.umd.edu both render event lists as server-side
<umd-element-event> custom elements, so one parser covers both.
(calendar.umd.edu uses the same component but is handled by umdcal_graphql,
which reaches its Solspace API directly for far better coverage.)

Two quirks these Drupal sites have that calendar.umd.edu does not:

  * The headline slot is a bare <h2> with no link -- the URL lives in the
    `actions` (or `image`) slot and is site-relative, so it needs resolving.
  * The `location` slot frequently carries the TIME as free text
    ("Time: 9 to 10:30 a.m.") while start-date-iso holds a placeholder noon.
    We parse that text to recover the real time; if parsing fails we keep the
    date, drop the bogus time, and flag the event for Claude to resolve.
"""

import datetime as dt
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from umd_scrape.common import fetch, make_candidate, clean_text

log = logging.getLogger("umd-sync.component")
ET = ZoneInfo("America/New_York")

MAX_DETAIL_FETCHES = 4          # politeness cap, per source, per run

SITES = {
    "stamp":   {"url": "https://stamp.umd.edu/upcoming_events",
                "base": "https://stamp.umd.edu/", "prefix": "stamp"},
    "careers": {"url": "https://careers.umd.edu/events/event-calendar",
                "base": "https://careers.umd.edu/", "prefix": "careers"},
}

# NB: no "Online Registration.*" alternative here. These pages render the venue
# and the sign-up link adjacently ("Location Online Registration Link: ..."), so
# that pattern would swallow the word "Online" -- which IS the venue for a Zoom
# event -- and leave the location blank.
_LOC_NOISE = re.compile(
    r"\s*(Get Directions.*|View Map.*|Registration Link.*|Register\b.*|"
    r"Add to Calendar.*)$", re.I | re.S)
_TIME_SLOT = re.compile(r"^\s*time\s*:", re.I)

# "9 to 10:30 a.m." / "8 p.m. to 1 a.m." / "Noon-4 p.m." / "10 a.m. - 3 p.m."
_T = r"(noon|midnight|\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)"
_RANGE = re.compile(_T + r"\s*(?:to|-|--|–|—)\s*" + _T, re.I)
_SINGLE = re.compile(_T, re.I)


def _tidy_location(s):
    s = _LOC_NOISE.sub("", clean_text(s)).strip(" ,;|")
    parts, seen, keep = [p.strip() for p in s.split(",")], set(), []
    for p in parts:
        if p and p.lower() not in seen:
            seen.add(p.lower())
            keep.append(p)
    return ", ".join(keep)


def _clock(tok, meridiem_hint=None):
    """Parse one time token into (hour, minute) on a 24h clock, or None."""
    tok = tok.strip().lower().replace(".", "")
    if tok == "noon":
        return 12, 0
    if tok == "midnight":
        return 0, 0
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", tok)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    mer = m.group(3) or meridiem_hint
    if mer == "pm" and h != 12:
        h += 12
    elif mer == "am" and h == 12:
        h = 0
    elif mer is None and h < 8:
        h += 12          # "1 to 3" on a campus calendar means afternoon
    return h % 24, mi


def parse_time_range(text, day):
    """Recover (start, end) datetimes on `day` from free text like 'Time: 9 to 10:30 a.m.'"""
    text = re.sub(r"^\s*time\s*:\s*", "", clean_text(text), flags=re.I)
    m = _RANGE.search(text)
    if m:
        a_raw, b_raw = m.group(1), m.group(2)
        # a trailing meridiem applies to both halves: "9 to 10:30 a.m."
        hint = None
        mm = re.search(r"(am|pm)", b_raw.lower().replace(".", ""))
        if mm:
            hint = mm.group(1)
        a, b = _clock(a_raw, hint), _clock(b_raw)
        if a and b:
            start = dt.datetime.combine(day, dt.time(*a), tzinfo=ET)
            end = dt.datetime.combine(day, dt.time(*b), tzinfo=ET)
            if end <= start:                       # crossed midnight
                end += dt.timedelta(days=1)
            return start, end
    m = _SINGLE.search(text)
    if m:
        a = _clock(m.group(1))
        if a:
            start = dt.datetime.combine(day, dt.time(*a), tzinfo=ET)
            return start, start + dt.timedelta(hours=1)
    return None


def _slot_el(block, name):
    return block.find(attrs={"slot": name})


def _slot(block, name):
    el = _slot_el(block, name)
    return clean_text(el.get_text(" ", strip=True)) if el else ""


def _find_url(block, base):
    for name in ("headline", "actions", "image"):
        el = _slot_el(block, name)
        if el is not None:
            a = el if el.name == "a" else el.find("a")
            if a and a.get("href"):
                return urljoin(base, a["href"])
    return ""


def _parse_list(html, spec):
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for block in soup.find_all("umd-element-event"):
        title = _slot(block, "headline")
        start_raw = _slot(block, "start-date-iso").split(".")[0]
        if not (title and start_raw):
            continue
        url = _find_url(block, spec["base"])
        slug = url.rstrip("/").rsplit("/", 1)[-1] if url else re.sub(r"\W+", "-", title.lower())[:60]

        end_raw = _slot(block, "end-date-iso").split(".")[0] or start_raw
        loc_slot = _slot(block, "location")

        try:
            sdt = dt.datetime.fromisoformat(start_raw).replace(tzinfo=ET)
            edt = dt.datetime.fromisoformat(end_raw).replace(tzinfo=ET)
        except ValueError:
            continue

        location, needs_review = loc_slot, False
        if _TIME_SLOT.match(loc_slot):
            # the slot holds the real time; the ISO stamp is a placeholder
            location = ""
            parsed = parse_time_range(loc_slot, sdt.date())
            if parsed:
                sdt, edt = parsed
            else:
                needs_review = True
                log.info("[%s] could not parse time %r for %r",
                         spec["prefix"], loc_slot, title)

        # No time slot and a zero-length event at exactly noon is the bare
        # placeholder, not a time. careers.umd.edu does this for most of its
        # listings, and unflagged it kept the Law School Fair at 12:00 beside
        # TerpLink's real 11:00 as a second event (18 Oct 2026).
        estimated = []
        if (needs_review or (sdt == edt and sdt.time() == dt.time(12, 0))):
            estimated = ["start", "end"]

        rows.append({
            "estimated": estimated,
            "key": f"{spec['prefix']}:{slug}",
            "title": title, "start": sdt, "end": edt,
            "all_day": False,
            "url": url,
            "description": _slot(block, "text"),
            "location": _tidy_location(location),
            "raw_time": loc_slot if _TIME_SLOT.match(loc_slot) else "",
            "needs_review": needs_review,
        })
    return rows


_PLACE_HINT = re.compile(
    r"\b(hall|building|center|centre|room|union|stamp|library|theater|theatre|"
    r"field|stadium|mall|plaza|chapel|online|virtual|zoom|lab|auditorium|"
    r"gallery|court|complex|dr(ive)?|st(reet)?|ave(nue)?|rd|road)\b", re.I)


def _looks_like_location(s):
    """Guard against grabbing description prose from a detail page.

    These Drupal pages have no reliable location slot, so a loose heading match
    happily returns the event blurb. A real location is short and names a place;
    anything else is better left empty for Claude to fill from another source.
    """
    if not s or len(s) > 120 or len(s.split()) > 18:
        return False
    if s.rstrip().endswith((".", "!", "?")) and len(s.split()) > 8:
        return False          # reads like a sentence
    return bool(_PLACE_HINT.search(s))


def _detail_location(html):
    soup = BeautifulSoup(html, "lxml")
    for attr in ("location", "event-location"):
        el = soup.find(attrs={"slot": attr})
        if el and not _TIME_SLOT.match(el.get_text(" ", strip=True)):
            cand = _tidy_location(el.get_text(" ", strip=True))
            if _looks_like_location(cand):
                return cand
    for tag in soup.find_all(["h2", "h3", "h4", "dt", "strong"]):
        if re.fullmatch(r"location", tag.get_text(strip=True) or "", re.I):
            nxt = tag.find_next(["p", "div", "dd", "span"])
            if nxt:
                cand = _tidy_location(nxt.get_text(" ", strip=True))
                if _looks_like_location(cand):
                    return cand
    return ""


def collect_site(name, spec, window_start, window_end, known_keys=()):
    html, changed = fetch(spec["url"], name)
    if not html:
        return []
    rows = _parse_list(html, spec)
    log.info("[%s] %d on list page (%s)", name, len(rows),
             "fresh" if changed else "cached/304")

    out, budget = [], MAX_DETAIL_FETCHES
    for r in rows:
        if not (window_start <= r["start"] < window_end):
            continue
        location = r["location"]
        if not location and r["key"] not in known_keys and budget > 0 and r["url"]:
            detail, _ = fetch(r["url"], name)
            budget -= 1
            if detail:
                location = _detail_location(detail)

        desc = r["description"]
        if r["needs_review"] and r["raw_time"]:
            desc = f"{desc}\n\n[sync] Unparsed time from source: {r['raw_time']}".strip()

        out.append(make_candidate(
            key=r["key"], title=r["title"],
            start=r["start"].isoformat(), end=r["end"].isoformat(),
            all_day=r["all_day"], location=location,
            description=desc, url=r["url"], source=name,
            estimated=r["estimated"],
        ))
    log.info("[%s] %d in window", name, len(out))
    return out


def collect(cfg, window_start, window_end, known_keys=()):
    out = []
    for name, spec in SITES.items():
        try:
            out += collect_site(name, spec, window_start, window_end, known_keys)
        except Exception as e:                                # noqa: BLE001
            log.error("[%s] collector failed: %s", name, e)
    return out
