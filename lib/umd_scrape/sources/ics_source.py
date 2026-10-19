"""iCalendar collectors.

Two roles:
  include  -- TerpLink, the primary student-org feed (245 events, stable UIDs)
  exclude  -- Maryland Athletics, used only to suppress events the user can
              get from the athletics calendar's own subscription.

TerpLink's feed carries UID / SEQUENCE / STATUS, so change and cancellation
detection here needs no LLM at all.
"""

import logging
import re
import datetime as dt

from icalendar import Calendar
from zoneinfo import ZoneInfo

from umd_scrape.common import fetch, make_candidate, clean_text

log = logging.getLogger("umd-sync.ics")
ET = ZoneInfo("America/New_York")


def _to_iso(value, all_day):
    """icalendar hands back date or datetime; normalize to ET-aware ISO strings."""
    if all_day:
        return value.isoformat()
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(ET).isoformat()
    return dt.datetime.combine(value, dt.time(0, 0), tzinfo=ET).isoformat()


def _terplink_id(uid):
    m = re.search(r"/event/(\d+)", str(uid))
    return m.group(1) if m else str(uid)


def _categories(comp):
    """CATEGORIES comes back as a vCategory object; flatten it to plain text."""
    raw = comp.get("CATEGORIES")
    if raw is None:
        return ""
    cats = getattr(raw, "cats", None)
    if cats:
        return ", ".join(str(c) for c in cats)
    try:
        return raw.to_ical().decode("utf-8", "replace")
    except AttributeError:
        return clean_text(str(raw))


def parse_ics(text, source, window_start, window_end):
    """Parse an ICS body into candidates inside [window_start, window_end)."""
    out = []
    try:
        cal = Calendar.from_ical(text)
    except Exception as e:                                    # noqa: BLE001
        log.error("[%s] unparseable ICS: %s", source, e)
        return out

    for comp in cal.walk("VEVENT"):
        try:
            dtstart = comp.get("DTSTART")
            if dtstart is None:
                continue
            raw_start = dtstart.dt
            all_day = not isinstance(raw_start, dt.datetime)

            dtend = comp.get("DTEND")
            raw_end = dtend.dt if dtend is not None else (
                raw_start + (dt.timedelta(days=1) if all_day else dt.timedelta(hours=1)))

            start_iso = _to_iso(raw_start, all_day)
            end_iso = _to_iso(raw_end, all_day)

            # window filter, compared in ET
            cmp_dt = (dt.datetime.fromisoformat(start_iso)
                      if not all_day
                      else dt.datetime.combine(raw_start, dt.time(0, 0), tzinfo=ET))
            if not (window_start <= cmp_dt < window_end):
                continue

            uid = str(comp.get("UID", ""))
            status = str(comp.get("STATUS", "CONFIRMED")).lower()

            desc = clean_text(comp.get("DESCRIPTION", ""))
            # TerpLink appends its own boilerplate; keep the useful part only.
            host = clean_text(comp.get("X-HOSTS", ""))
            if not host:
                m = re.search(r"Hosted by:\s*(.+)", desc)
                host = m.group(1).strip() if m else ""
            desc = re.sub(r"\n*Hosted by:.*", "", desc, flags=re.S).strip()
            desc = re.sub(r"\n*Additional Information can be found at:.*", "",
                          desc, flags=re.S).strip()

            url = str(comp.get("URL", "")) or (uid if uid.startswith("http") else "")
            key = (f"terplink:{_terplink_id(uid)}" if source == "terplink"
                   else f"{source}:{uid}")

            out.append(make_candidate(
                key=key,
                title=clean_text(comp.get("SUMMARY", "(untitled)")),
                start=start_iso,
                end=end_iso,
                all_day=all_day,
                location=clean_text(comp.get("LOCATION", "")),
                host=host,
                description=desc,
                url=url,
                source=source,
                category_hint=_categories(comp),
                status="cancelled" if status == "cancelled" else "confirmed",
                # revision="" -> make_candidate derives a content hash.
                # We deliberately do NOT use SEQUENCE or DTSTAMP: in TerpLink's
                # feed SEQUENCE is 0 on every event and DTSTAMP is the feed's
                # generation time, which changes on every fetch and would make
                # every event look modified on every run.
                revision="",
            ))
        except Exception as e:                                # noqa: BLE001
            log.warning("[%s] skipping malformed VEVENT: %s", source, e)
    return out


def collect(cfg, window_start, window_end):
    """Return (candidates, exclusion_index)."""
    candidates, exclusions = [], []
    for name, spec in cfg["ics_sources"].items():
        text, changed = fetch(spec["url"], name)
        if not text:
            log.error("[%s] no data", name)
            continue
        events = parse_ics(text, name, window_start, window_end)
        # A department feed has no per-event host. Naming the department lets
        # the calendar sync tell its own committee meetings apart.
        for e in events:
            if not e.get("host") and spec.get("host"):
                e["host"] = spec["host"]
        log.info("[%s] %d events in window (%s)", name, len(events),
                 "fresh" if changed else "cached/304")
        if spec.get("role") == "exclude":
            exclusions.extend(events)
        else:
            candidates.extend(events)
    return candidates, exclusions


# --------------------------------------------------------------------------- #
# athletics de-duplication
# --------------------------------------------------------------------------- #
_SPORTS = ("football", "soccer", "basketball", "volleyball", "field hockey",
           "lacrosse", "baseball", "softball", "tennis", "golf", "wrestling",
           "gymnastics", "cross country", "track", "swimming", "diving")


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())


def is_athletics_duplicate(cand, exclusions):
    """True if this candidate is a Terps game already on the athletics calendar.

    Matches on same ET calendar date plus a shared sport name, which is what
    actually distinguishes 'Field Hockey vs. Temple' from a movie screening.
    Deliberately conservative: a false negative just means one duplicate event,
    while a false positive would silently hide something the user wanted.
    """
    ctitle = _norm(cand["title"])
    if not any(sp in ctitle for sp in _SPORTS):
        return False
    cdate = cand["start"][:10]
    for ex in exclusions:
        if ex["start"][:10] != cdate:
            continue
        etitle = _norm(ex["title"])
        for sp in _SPORTS:
            if sp in ctitle and sp in etitle:
                return True
    return False
