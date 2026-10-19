"""calendar.umd.edu via its Solspace Calendar GraphQL endpoint.

Discovered by driving the site in headless Chromium and intercepting its own
network calls, because none of the obvious routes worked:

  * /search?start=&end=  -- date params are ignored; always the same 12 upcoming
  * "Load More"          -- JS-driven; no limit/page/perPage variant helps
  * XML sitemap          -- stale since 2022
  * Craft GraphQL root   -- events are NOT at the query root; they live under a
                            nested `solspace_calendar` field, which is why schema
                            introspection of the root turned up nothing

The site ships TWO public bearer tokens in its homepage HTML and they are not
interchangeable:
  * token="..."                          -> Craft schema (does NOT see events)
  * <umd-feed-events-grouped token=...>  -> Solspace schema (the one we need)

Both are scraped fresh each run so token rotation doesn't break us.

Yield: 258 events, ~25 in a 2-week window, against 12 from the HTML list page.
"""

import datetime as dt
import logging
import re

from zoneinfo import ZoneInfo

from umd_scrape.common import fetch, make_candidate, clean_text, breaker_record

log = logging.getLogger("umd-sync.umdcal")
ET = ZoneInfo("America/New_York")

HOME = "https://calendar.umd.edu/"
GRAPHQL = "https://calendar.umd.edu/graphql"
PAGE_SIZE = 300

# The Solspace token appears as an unquoted attribute on the feed component.
_SOLSPACE_TOKEN = re.compile(
    r"<umd-feed-events-grouped[^>]*\btoken=\"?([A-Za-z0-9_-]{16,})\"?", re.I)

_FIELDS = """
    id title url
    startStamp: startDate @formatDateTime(format: "Y-m-d")
    startIso:   startDate @formatDateTime(format: "Y-m-d\\\\TH:i:s")
    endIso:     endDate   @formatDateTime(format: "Y-m-d\\\\TH:i:s")
    allDay
    summary: commonRichTextTwo
    location: categoriesCampusBuildingSingle { title }
"""

QUERY = """
query getEvents($startDate: String!, $limit: Int, $offset: Int) {
  entries: solspace_calendar {
    events(loadOccurrences: true, rangeStart: $startDate,
           limit: $limit, offset: $offset) {
      ... on communications_Event { %s }
      ... on submission_Event { %s }
    }
  }
}
""" % (_FIELDS, _FIELDS)


def _token(html):
    m = _SOLSPACE_TOKEN.search(html or "")
    return m.group(1) if m else None


def _post(token, start_date, offset=0):
    """POST the GraphQL query. Uses requests directly but honours our UA/politeness."""
    import requests
    from umd_scrape.common import UA, REQUEST_SPACING_SEC
    import time
    time.sleep(REQUEST_SPACING_SEC)
    r = requests.post(
        GRAPHQL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": UA, "Referer": HOME},
        json={"query": QUERY,
              "variables": {"startDate": start_date, "limit": PAGE_SIZE, "offset": offset}},
        timeout=45,
    )
    r.raise_for_status()
    return r.json()


def _location(ev):
    loc = ev.get("location")
    if isinstance(loc, list):
        return ", ".join(x.get("title", "") for x in loc if x.get("title"))
    if isinstance(loc, dict):
        return loc.get("title", "")
    return ""


def collect(cfg, window_start, window_end, known_keys=()):
    html, _ = fetch(HOME, "umdcal")
    token = _token(html)
    if not token:
        log.error("[umdcal] could not scrape Solspace token from homepage")
        breaker_record("umdcal", False, "token scrape failed")
        return []

    try:
        data = _post(token, window_start.strftime("%Y-%m-%d"))
    except Exception as e:                                    # noqa: BLE001
        log.error("[umdcal] graphql failed: %s", e)
        breaker_record("umdcal", False, str(e))
        return []

    if data.get("errors"):
        log.error("[umdcal] graphql errors: %s", str(data["errors"])[:300])
        breaker_record("umdcal", False, str(data["errors"])[:300])
        return []

    events = ((data.get("data") or {}).get("entries") or {}).get("events") or []
    log.info("[umdcal] graphql returned %d events", len(events))

    out = []
    for ev in events:
        try:
            siso, eiso = ev.get("startIso"), ev.get("endIso")
            if not siso:
                continue
            sdt = dt.datetime.fromisoformat(siso).replace(tzinfo=ET)
            edt = (dt.datetime.fromisoformat(eiso).replace(tzinfo=ET)
                   if eiso else sdt + dt.timedelta(hours=1))
            if not (window_start <= sdt < window_end):
                continue
            slug = (ev.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            key = f"umdcal:{slug or ev.get('id')}"
            out.append(make_candidate(
                key=key,
                title=clean_text(ev.get("title", "")),
                start=sdt.isoformat(),
                end=edt.isoformat(),
                all_day=bool(ev.get("allDay")),
                location=clean_text(_location(ev)),
                description=clean_text(ev.get("summary", "")),
                url=ev.get("url", ""),
                source="umdcal",
            ))
        except Exception as e:                                # noqa: BLE001
            log.warning("[umdcal] skipping malformed event %s: %s", ev.get("id"), e)

    breaker_record("umdcal", True)
    log.info("[umdcal] %d in window", len(out))
    return out
