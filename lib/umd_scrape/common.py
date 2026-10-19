"""Shared plumbing for the UMD campus event scrapers.

Politeness is a hard requirement here (see the plan): every outbound request goes
through `fetch()`, which enforces per-host spacing, conditional GETs, exponential
backoff, and a circuit breaker. Nothing else in this project should call requests
directly.
"""

import hashlib
import json
import logging
import os
import pathlib
import re
import time
import datetime as dt

import requests

# Where the HTTP cache and the per-source circuit breaker live. `configure()`
# points these at $HERALD_HOME/ledger/raw/umd/ before any fetch; the module
# defaults only matter to someone importing it on its own.
STATE_DIR = pathlib.Path(os.environ.get("UMD_SCRAPE_STATE", ".")) / "state"
HTTP_CACHE = STATE_DIR / "http_cache.json"
BREAKER = STATE_DIR / "breaker.json"

# Identify the scraper honestly so a UMD webmaster can email rather than block.
# The address is the installing user's (`umd.contact_email`), set by configure().
UA = "herald-umd/1.0 (personal student calendar aggregator)"


def configure(state_dir, contact=""):
    """Point the cache and breaker at this install's state, and name a contact."""
    global STATE_DIR, HTTP_CACHE, BREAKER, UA
    STATE_DIR = pathlib.Path(state_dir)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HTTP_CACHE = STATE_DIR / "http_cache.json"
    BREAKER = STATE_DIR / "breaker.json"
    UA = ("herald-umd/1.0 (personal student calendar aggregator"
          + (f"; contact {contact}" if contact else "") + ")")


REQUEST_SPACING_SEC = 2.0
BREAKER_THRESHOLD = 3          # consecutive failures before a source is skipped
TIMEOUT = 30

log = logging.getLogger("umd-sync")
_last_request_at = {}


# --------------------------------------------------------------------------- #
# small json helpers
# --------------------------------------------------------------------------- #
def load_json(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    p = pathlib.Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, default=str))
    tmp.replace(p)          # atomic; a killed run never leaves a half-written state file


# --------------------------------------------------------------------------- #
# circuit breaker
# --------------------------------------------------------------------------- #
def breaker_state():
    return load_json(BREAKER, {})


def breaker_ok(source):
    return breaker_state().get(source, {}).get("fails", 0) < BREAKER_THRESHOLD


def breaker_record(source, ok, detail=""):
    st = breaker_state()
    cur = st.setdefault(source, {"fails": 0, "last_error": ""})
    if ok:
        cur["fails"] = 0
        cur["last_error"] = ""
    else:
        cur["fails"] = cur.get("fails", 0) + 1
        cur["last_error"] = detail[:400]
    cur["updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_json(BREAKER, st)


def tripped_sources():
    return [s for s, v in breaker_state().items() if v.get("fails", 0) >= BREAKER_THRESHOLD]


# --------------------------------------------------------------------------- #
# polite fetch
# --------------------------------------------------------------------------- #
def fetch(url, source, force=False, retries=3):
    """GET a URL politely.

    Returns (text, changed). `changed` is False when the server answered 304,
    meaning callers can reuse whatever they parsed last time.
    Returns (None, False) if the source's breaker is tripped or the fetch failed.
    """
    if not breaker_ok(source):
        log.warning("[%s] breaker tripped, skipping %s", source, url)
        return None, False

    host = requests.utils.urlparse(url).netloc
    delta = time.monotonic() - _last_request_at.get(host, 0)
    if delta < REQUEST_SPACING_SEC:
        time.sleep(REQUEST_SPACING_SEC - delta)

    cache = load_json(HTTP_CACHE, {})
    entry = cache.get(url, {})
    headers = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
    if not force:
        if entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]
        if entry.get("last_modified"):
            headers["If-Modified-Since"] = entry["last_modified"]

    delay = 5
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
            _last_request_at[host] = time.monotonic()

            if r.status_code == 304:
                breaker_record(source, True)
                return entry.get("body", ""), False

            if r.status_code in (429, 503):
                wait = int(r.headers.get("Retry-After", delay))
                log.warning("[%s] %d from %s, backing off %ss", source, r.status_code, host, wait)
                time.sleep(min(wait, 120))
                delay *= 2
                continue

            r.raise_for_status()
            cache[url] = {
                "etag": r.headers.get("ETag", ""),
                "last_modified": r.headers.get("Last-Modified", ""),
                "body": r.text,
                "fetched": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            save_json(HTTP_CACHE, cache)
            breaker_record(source, True)
            return r.text, True

        except requests.RequestException as e:
            log.warning("[%s] attempt %d/%d failed: %s", source, attempt + 1, retries, e)
            time.sleep(delay)
            delay *= 2

    breaker_record(source, False, f"all {retries} attempts failed for {url}")
    return None, False


# --------------------------------------------------------------------------- #
# candidate events
# --------------------------------------------------------------------------- #
CANDIDATE_FIELDS = ("key", "title", "start", "end", "all_day", "location", "host",
                    "description", "url", "source", "category_hint", "status",
                    "revision", "estimated")

# Ranking used when two sources describe the same event. Whichever source wins,
# the KEY comes from the highest-priority one present, so identity stays stable
# instead of flipping between runs and duplicating the event.
SOURCE_PRIORITY = ("terplink", "umdcal", "cs", "careers", "stamp", "clarice")


def source_rank(name):
    try:
        return SOURCE_PRIORITY.index(name)
    except ValueError:
        return len(SOURCE_PRIORITY)


def make_candidate(key, title, start, end, source, url="", location="", host="",
                   description="", all_day=False, category_hint="",
                   status="confirmed", revision="", estimated=()):
    """Build a normalized candidate. `revision` drives change detection.

    `estimated` names fields this source did not actually publish and that the
    collector guessed (e.g. The Clarice gives no end time, so we assume two
    hours). Merging must never let a guess overwrite a real value.
    """
    c = {
        "key": key,
        "title": clean_text(title),
        "start": start,
        "end": end,
        "all_day": bool(all_day),
        "location": clean_text(location),
        "host": clean_text(host),
        "description": clean_text(description),
        "url": url,
        "source": source,
        "category_hint": category_hint,
        "status": status,
        "estimated": sorted(estimated),
    }
    # If a source gives no native revision (e.g. scraped HTML), derive one from
    # content so an edited event still registers as changed.
    c["revision"] = revision or content_hash(c)
    return c


def content_hash(c):
    basis = "|".join(str(c.get(f, "")) for f in
                     ("title", "start", "end", "location", "description", "status"))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


def clean_text(s):
    if not s:
        return ""
    s = _TAG.sub(" ", str(s))
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&rsquo;", "'"), ("&lsquo;", "'"),
                 ("&ldquo;", '"'), ("&rdquo;", '"'), ("&mdash;", "--"), ("&ndash;", "-"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&eacute;", "e"), ("&uacute;", "u")):
        s = s.replace(a, b)
    s = _WS.sub(" ", s)
    return "\n".join(line.strip() for line in s.splitlines()).strip()


def setup_logging(verbose=False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
