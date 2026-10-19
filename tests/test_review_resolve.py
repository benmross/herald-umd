"""How the review cycle turns the model's answers into verdicts, offline."""

import importlib.util
import pathlib
import sys
import unittest

_EXT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_EXT / "lib"))
_spec = importlib.util.spec_from_file_location("campus_review", _EXT / "cycles" / "campus_review.py")
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)


def n(key, title="t"):
    return {"key": key, "ts": "2026-10-21T13:00:00-04:00", "title": title, "hash": "h"}


def by_key(rows):
    return {r[0]: r for r in rows}


class Resolve(unittest.TestCase):
    def test_duplicate_of_another_new_listing(self):
        new = {"N0": n("terplink:1"), "N1": n("umdcal:1")}
        raw = {"N0": {"keep": True, "duplicate_of": ""},
               "N1": {"keep": True, "duplicate_of": "N0"}}
        rows = by_key(cr.resolve(raw, new, {}, set()))
        self.assertTrue(rows["terplink:1"][3])
        self.assertEqual(rows["umdcal:1"][3:5], (False, "terplink:1"))

    def test_pair_marked_both_ways_keeps_the_first(self):
        new = {"N0": n("a:1"), "N1": n("b:1")}
        raw = {"N0": {"keep": True, "duplicate_of": "N1"},
               "N1": {"keep": True, "duplicate_of": "N0"}}
        rows = by_key(cr.resolve(raw, new, {}, set()))
        self.assertTrue(rows["a:1"][3])
        self.assertFalse(rows["b:1"][3])

    def test_duplicate_of_current_calendar_event_drops_the_new_one(self):
        new = {"N0": n("email:cipher-tech-lobby-day")}
        ctx = {"C0": {"syncKey": "email:cipher-tech-solutions-lobby-day",
                      "start": "2026-10-21T12:30:00-04:00", "summary": "Cipher Tech Solutions"}}
        raw = {"N0": {"keep": True, "duplicate_of": "C0"}}
        rows = by_key(cr.resolve(raw, new, ctx, set()))
        self.assertEqual(rows["email:cipher-tech-lobby-day"][3:5],
                         (False, "email:cipher-tech-solutions-lobby-day"))

    def test_stale_calendar_copy_goes_and_fresh_listing_stays(self):
        new = {"N0": n("umdcal:new")}
        ctx = {"C0": {"syncKey": "umdcal:old", "start": "2026-10-19T18:30:00-04:00",
                      "summary": "2026 SPH Convocation: The Pictures"}}
        raw = {"N0": {"keep": True, "duplicate_of": "C0"}}
        rows = by_key(cr.resolve(raw, new, ctx, facts={"umdcal:new"}))
        self.assertTrue(rows["umdcal:new"][3])
        self.assertEqual(rows["umdcal:old"][3:5], (False, "umdcal:new"))

    def test_batches_never_split_a_day(self):
        todo = [{"ts": f"2026-10-{d:02d}T10:00:00-04:00"} for d in (20,) * 150 + (21,) * 100]
        plan = cr.batches(todo, None)
        self.assertEqual(plan, [["2026-10-20"], ["2026-10-21"]])


if __name__ == "__main__":
    unittest.main()
