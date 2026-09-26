"""Checks the dashboard analysis recovers the effects planted in make_demo_data.py."""

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import build_dashboard as bd
import make_demo_data


class AnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = Path(tempfile.mkdtemp()) / "demo.json"
        with unittest.mock.patch("builtins.print"):
            make_demo_data.main(tmp)
        cls.data = json.loads(tmp.read_text())
        cls.r = bd.analyse(cls.data)

    def effect(self, block, key):
        return next(e for e in self.r[block]["ranked"] + self.r[block]["thin"] if e["key"] == key)

    def test_planted_recovery_hits_are_found_and_hold_after_adjustment(self):
        for key, lo, hi in [("late_workout", -14, -6), ("short_sleep", -14, -4), ("weekend", -12, -4)]:
            e = self.effect("hurts_recovery", key)
            self.assertEqual(e["conf"], "strong", key)
            self.assertTrue(lo < e["effect"] < hi, (key, e["effect"]))
            self.assertFalse(e["explained"], key)

    def test_no_effect_behaviour_is_not_claimed(self):
        for block in ("lifts_deep", "lifts_hrv"):
            self.assertEqual(self.effect(block, "nap")["conf"], "noise", block)

    def test_zone2_lifts_deep_sleep_and_hrv(self):
        for block in ("lifts_deep", "lifts_hrv"):
            e = self.effect(block, "zone2")
            self.assertEqual((e["conf"], e["label"].startswith("≥ 30 min")), ("strong", True), block)

    def test_strain_ceiling_and_trends(self):
        self.assertEqual(self.r["strain"]["range"][1], 14)
        status = {t["key"]: t["status"] for t in self.r["trends"]}
        self.assertEqual((status["hrv"], status["rhr"]), ("worse", "worse"))

    def test_top3_uses_evidence_only(self):
        self.assertEqual(len(self.r["top3"]), 3)
        self.assertTrue(all(c["conf"] in ("strong", "likely") and c["gain"] > 0 for c in self.r["top3"]))

    def test_thin_data_makes_no_claims(self):
        d = dict(self.data)
        keys = sorted(d["days"])[-12:]
        d["days"] = {k: d["days"][k] for k in keys}
        r = bd.analyse(d)
        self.assertEqual(r["hurts_recovery"]["ranked"], [])
        self.assertEqual(r["top3"], [])
        self.assertIsNone(r["strain"].get("range"))
        self.assertTrue(all(t["status"] == "thin" for t in r["trends"]))

    def test_render_is_self_contained(self):
        html = bd.render(self.r)
        self.assertNotIn("/*__DATA__*/null", html)
        self.assertNotIn("<script src", html)
        self.assertNotIn("<link", html)


if __name__ == "__main__":
    unittest.main()
