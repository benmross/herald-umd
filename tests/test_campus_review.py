"""Verdict-driven calendar membership, and the review's duplicate handling, offline."""

import datetime as dt
import importlib.util
import json
import pathlib
import sqlite3
import sys
import unittest

_EXT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_EXT / "lib"))
import umd_review  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "campus_calendar", _EXT / "collectors" / "campus_calendar.py")
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

TS = "2026-10-20T19:00:00-04:00"


def _db(events, verdicts):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, source, kind, external_id,"
                " ts, title, body, data, ingested_at)")
    for key, title, data in events:
        con.execute("INSERT INTO facts (source, kind, external_id, ts, title, body, data)"
                    " VALUES ('umd_events','event',?,?,?,?,?)",
                    (key, TS, title, "", json.dumps(data)))
    for key, v in verdicts.items():
        con.execute("INSERT INTO facts (source, kind, external_id, data) VALUES (?,?,?,?)",
                    (umd_review.SOURCE, umd_review.KIND, key, json.dumps(v)))
    return con


def _h(title, data):
    return umd_review.content_hash(title, TS, data, "")


LO = dt.datetime(2026, 10, 18, tzinfo=dt.timezone.utc)
HI = LO + dt.timedelta(days=30)


class Membership(unittest.TestCase):
    def test_kept_goes_on_dropped_comes_off(self):
        a, b = {"host": "X"}, {"host": "Pre-Dental Society"}
        con = _db([("terplink:1", "Fall Fest", a), ("terplink:2", "Pre Dental Social", b)],
                  {"terplink:1": {"keep": True, "v": umd_review.VERSION, "hash": _h("Fall Fest", a)},
                   "terplink:2": {"keep": False, "reason": "pre-dental only",
                                  "v": umd_review.VERSION, "hash": _h("Pre Dental Social", b)}})
        want, _, ignored, held = cc.desired_events(con, LO, HI, set(), True)
        self.assertIn("terplink:1", want)
        self.assertNotIn("terplink:2", want)
        self.assertIn("pre-dental", ignored["terplink:2"])
        self.assertEqual(held, 0)

    def test_unreviewed_is_held_while_review_is_healthy(self):
        con = _db([("terplink:3", "New Thing", {})], {})
        want, _, ignored, held = cc.desired_events(con, LO, HI, set(), True)
        self.assertEqual((want, ignored, held), ({}, {}, 1))

    def test_unreviewed_already_live_stays(self):
        con = _db([("terplink:3", "New Thing", {})], {})
        want, _, ignored, _ = cc.desired_events(con, LO, HI, {"terplink:3"}, True)
        self.assertIn("terplink:3", want)
        self.assertEqual(ignored, {})

    def test_unreviewed_goes_on_when_review_is_broken(self):
        con = _db([("terplink:3", "New Thing", {})], {})
        want, _, _, held = cc.desired_events(con, LO, HI, set(), False)
        self.assertIn("terplink:3", want)
        self.assertEqual(held, 0)

    def test_changed_listing_is_not_judged_by_old_verdict(self):
        con = _db([("terplink:4", "Renamed", {})],
                  {"terplink:4": {"keep": False, "v": umd_review.VERSION, "hash": _h("Old Name", {})}})
        want, _, ignored, _ = cc.desired_events(con, LO, HI, {"terplink:4"}, True)
        self.assertIn("terplink:4", want)
        self.assertNotIn("terplink:4", ignored)


class LiveOrphans(unittest.TestCase):
    def test_started_event_dropped_by_review_is_removed(self):
        live = [{"syncKey": "terplink:1", "summary": "Dubbers Meeting"}]
        out = cc._dropped_live(live, {}, {}, {"terplink:1": {"keep": False, "reason": "r"}})
        self.assertIn("terplink:1", out)

    def test_orphan_without_verdict_is_left(self):
        live = [{"syncKey": "terplink:1", "summary": "x"}]
        self.assertEqual(cc._dropped_live(live, {}, {}, {}), {})

    def test_scout_email_event_called_a_duplicate_is_removed(self):
        live = [{"syncKey": "email:x", "summary": "Cipher Tech Lobby Day"}]
        out = cc._dropped_live(live, {}, {}, {"email:x": {"keep": False,
                                                          "duplicate_of": "email:y"}})
        self.assertIn("duplicate of email:y", out["email:x"])

    def test_manual_events_are_never_touched(self):
        live = [{"syncKey": "manual:x", "summary": "x"}]
        self.assertEqual(cc._dropped_live(live, {}, {}, {"manual:x": {"keep": False}}), {})

    def test_old_version_verdict_does_not_count_as_current(self):
        a = {"host": "X"}
        con = _db([("terplink:9", "Fair", a)],
                  {"terplink:9": {"keep": False, "hash": _h("Fair", a), "v": 1}})
        want, _, ignored, _ = cc.desired_events(con, LO, HI, {"terplink:9"}, True)
        self.assertIn("terplink:9", want)


if __name__ == "__main__":
    unittest.main()
