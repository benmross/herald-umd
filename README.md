# umd

UMD campus events, on a Google calendar Herald
keeps for you.

Once a day it scrapes TerpLink's and the CS department's public ICS feeds,
calendar.umd.edu's event search, and the STAMP and Careers listing pages, and
merges listings of the same event. A model then reads each new or changed
event once and decides whether it belongs on your calendar, by criteria you
write in plain sentences, and whether it is a duplicate of something already
there. Events it keeps go on the calendar; ones that are cancelled at the
source are greyed and retitled, never silently deleted. A run with nothing new
costs nothing.

## Install

Send your Herald this repository's URL and ask it to install it. It will ask
you for the settings below and walk you through the steps. By hand:

```bash
herald ext install https://github.com/benmross/herald-umd
~/herald/venv/bin/pip install beautifulsoup4 lxml
herald ext setup umd
herald ext enable umd
herald services install
```

It needs Herald's Google access (the `google` capability) and a calendar you
created for it.

## Settings

| key | what |
|---|---|
| `calendars.write.campus_event` | the calendar's name, default `UMD Campus Events` |
| `umd.review_criteria` | what you want kept and dropped, in plain sentences. A `## Campus events calendar review` section in `identity/preferences.md` takes its place if present |
| `umd.contact_email` | your UMD address, in the scrapers' User-Agent |
| `calendars.campus_events.window_days` | how far ahead, default 365 |
| `calendars.campus_events.review_model` | the model that judges events |
| `umd.extra_sources` | optional: paths to extra source modules, each with `collect(cfg, start, end, known_keys)` |

## Being a good guest

Every request goes through one polite fetcher: two seconds between requests
to a host, conditional GETs, backoff, and a circuit breaker that stops trying
a source after three failures in a row. Its state lives in
`ledger/raw/umd/state/`.

The Clarice's site asks scrapers not to crawl it, and this repository does
not include a scraper for it. Use `umd.extra_sources` only for sources you
have permission to read.
