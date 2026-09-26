"""Offline tests for whoop_sync.py: the WHOOP API is mocked, nothing hits the network."""

import json
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import whoop_sync as ws


def cycle(i, day, tz="-03:00", strain=10.5):
    return {"id": i, "start": f"{day}T10:30:00.000Z", "end": None, "timezone_offset": tz,
            "score_state": "SCORED",
            "score": {"strain": strain, "kilojoule": 8368.0, "average_heart_rate": 68, "max_heart_rate": 170}}


def sleep(sid, cid, day, nap=False):
    return {"id": sid, "cycle_id": cid, "start": f"{day}T02:00:00.000Z", "end": f"{day}T10:00:00.000Z",
            "timezone_offset": "-03:00", "nap": nap, "score_state": "SCORED",
            "score": {"stage_summary": {"total_in_bed_time_milli": 28_800_000,
                                        "total_awake_time_milli": 1_800_000,
                                        "total_no_data_time_milli": 0,
                                        "total_light_sleep_time_milli": 12_600_000,
                                        "total_slow_wave_sleep_time_milli": 6_300_000,
                                        "total_rem_sleep_time_milli": 8_100_000,
                                        "sleep_cycle_count": 4, "disturbance_count": 9},
                      "sleep_needed": {"baseline_milli": 27_000_000, "need_from_sleep_debt_milli": 600_000,
                                       "need_from_recent_strain_milli": 300_000, "need_from_recent_nap_milli": 0},
                      "respiratory_rate": 15.23, "sleep_performance_percentage": 91,
                      "sleep_consistency_percentage": 80, "sleep_efficiency_percentage": 93.75}}


def recovery(cid, sid):
    return {"cycle_id": cid, "sleep_id": sid, "score_state": "SCORED",
            "score": {"user_calibrating": False, "recovery_score": 66, "resting_heart_rate": 52,
                      "hrv_rmssd_milli": 61.234, "spo2_percentage": 96.4, "skin_temp_celsius": 33.71}}


WORKOUT = {"id": "w1", "sport_name": "running", "start": "2026-09-20T12:00:00.000Z",
           "end": "2026-09-20T12:45:00.000Z", "timezone_offset": "-03:00", "score_state": "SCORED",
           "score": {"strain": 12.3, "average_heart_rate": 150, "max_heart_rate": 182, "kilojoule": 2092.0,
                     "distance_meter": 8000.0,
                     "zone_durations": {f"zone_{z}_milli": 300_000 for z in ws.ZONES}}}


class FakeWhoop:
    """Stands in for http_json. Serves pages of 2 records and can simulate 401s / missing scopes."""

    def __init__(self, data, expire_first_call=True, forbidden=()):
        self.data, self.forbidden = data, set(forbidden)
        self.valid_token = None if expire_first_call else "access-1"
        self.issued = 1
        self.calls = []

    def __call__(self, method, url, headers=None, form=None, timeout=30):
        self.calls.append((method, url))
        if url == ws.TOKEN_URL:
            assert form["grant_type"] == "refresh_token"
            assert form["refresh_token"] == f"refresh-{self.issued}"  # rotated token is reused
            self.issued += 1
            self.valid_token = f"access-{self.issued}"
            return 200, {"access_token": self.valid_token, "refresh_token": f"refresh-{self.issued}",
                         "expires_in": 3600, "scope": " ".join(ws.SCOPES)}, {}
        if headers["Authorization"] != f"Bearer {self.valid_token}":
            return 401, {"error": "expired"}, {}
        u = urllib.parse.urlparse(url)
        path = u.path[len("/developer/v2"):]
        if path in self.forbidden:
            return 401, {"error": "insufficient scope"}, {}
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        payload = self.data[path]
        if isinstance(payload, dict):
            return 200, payload, {}
        assert q["limit"] == "25" and q["start"].endswith("Z") and q["end"].endswith("Z")
        i = int(q.get("nextToken", 0))
        nxt = str(i + 2) if i + 2 < len(payload) else None
        return 200, {"records": payload[i:i + 2], "next_token": nxt}, {}


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(ws, "TOKENS_FILE", self.tmp / ".whoop_tokens.json"),
            mock.patch.object(ws, "OUTPUT_FILE", self.tmp / "whoop_data.json"),
        ]
        for p in self.patches:
            p.start()
        ws.TOKENS_FILE.write_text(json.dumps({
            "access_token": "access-1", "refresh_token": "refresh-1",
            "scope": " ".join(ws.SCOPES), "expires_at": int(time.time()) + 3600}))
        self.data = {
            "/cycle": [cycle(1, "2026-09-18"), cycle(2, "2026-09-19"), cycle(3, "2026-09-20"),
                       cycle(5, "2026-09-23")],
            "/recovery": [recovery(1, "s1"), recovery(2, "s2"), recovery(5, "s5")],
            "/activity/sleep": [sleep("s1", 1, "2026-09-18"), sleep("s2", 2, "2026-09-19"),
                                sleep("s3", 3, "2026-09-20"), sleep("n3", 3, "2026-09-20", nap=True)],
            "/activity/workout": [WORKOUT],
            "/user/profile/basic": {"user_id": 7, "email": "x@y.z", "first_name": "Ana", "last_name": "B"},
            "/user/measurement/body": {"height_meter": 1.68, "weight_kilogram": 61.4, "max_heart_rate": 190},
        }

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def run_main(self, fake, days=8):
        env = {"WHOOP_CLIENT_ID": "id", "WHOOP_CLIENT_SECRET": "secret"}
        fixed_now = ws.datetime(2026, 9, 24, 15, 0, tzinfo=ws.timezone.utc)
        dt = mock.MagicMock(wraps=ws.datetime)
        dt.now.return_value = fixed_now
        dt.fromisoformat = ws.datetime.fromisoformat
        with mock.patch.dict("os.environ", env), mock.patch.object(ws, "http_json", fake), \
                mock.patch.object(ws, "datetime", dt), mock.patch.object(ws, "load_dotenv"), \
                mock.patch.object(sys, "argv", ["whoop_sync.py", "--days", str(days)]), \
                mock.patch("builtins.print"):
            ws.main()
        return json.loads(ws.OUTPUT_FILE.read_text())

    def test_end_to_end(self):
        fake = FakeWhoop(self.data)
        out = self.run_main(fake)

        # 401 on the first request -> exactly one refresh, rotated tokens persisted.
        self.assertEqual(sum(1 for _, u in fake.calls if u == ws.TOKEN_URL), 1)
        saved = json.loads(ws.TOKENS_FILE.read_text())
        self.assertEqual((saved["access_token"], saved["refresh_token"]), ("access-2", "refresh-2"))

        # Pagination followed next_token (2 records/page). /recovery is fetched first, so it
        # also carries the one rejected 401 call that triggered the refresh.
        self.assertEqual(len([u for _, u in fake.calls if "/recovery?" in u]), 3)
        self.assertEqual(len([u for _, u in fake.calls if "/cycle?" in u]), 2)
        self.assertIn("nextToken=2", [u for _, u in fake.calls if "/cycle?" in u][1])
        self.assertEqual(out["counts"], {"recovery": 3, "cycles": 4, "sleep": 4, "workouts": 1})

        day = out["days"]["2026-09-18"]
        self.assertEqual(day["cycle"]["strain"], 10.5)
        self.assertEqual(day["cycle"]["calories"], 2000)            # 8368 kJ / 4.184
        self.assertEqual(day["recovery"]["recovery_score"], 66)
        self.assertEqual(day["recovery"]["hrv_rmssd_ms"], 61.2)
        self.assertEqual(day["sleep"]["stages"]["deep"], "1h 45m")  # 6_300_000 ms
        self.assertEqual(day["sleep"]["stages"]["light"], "3h 30m")
        self.assertEqual(day["sleep"]["stages"]["rem"], "2h 15m")
        self.assertEqual(day["sleep"]["total_asleep"], "7h 30m")
        self.assertEqual(day["sleep"]["sleep_needed"], "7h 45m")
        self.assertEqual(day["sleep"]["disturbance_count"], 9)
        self.assertTrue(day["cycle"]["start"].endswith("-03:00"))

        # Cycle 3 has sleep (via cycle_id fallback) and a nap but no recovery; workout joins on 09-20.
        d20 = out["days"]["2026-09-20"]
        self.assertIsNone(d20["recovery"])
        self.assertEqual(d20["sleep"]["sleep_id"], "s3")
        self.assertEqual(len(d20["naps"]), 1)
        w = d20["workouts"][0]
        self.assertEqual((w["calories"], w["distance_km"], w["duration"]), (500, 8.0, "0h 45m"))
        self.assertEqual(w["zone_durations"]["zone_three"], "0h 05m")

        cov = out["coverage"]
        self.assertEqual((cov["window_start"], cov["window_end"]), ("2026-09-16", "2026-09-24"))
        self.assertEqual(cov["days_with_cycle"], 4)
        self.assertEqual(cov["gaps"]["no_data_at_all"],
                         ["2026-09-16..2026-09-17 (2 days)", "2026-09-21..2026-09-22 (2 days)", "2026-09-24"])
        self.assertEqual(cov["gaps"]["cycle_but_no_recovery"], ["2026-09-20"])
        self.assertEqual(cov["gaps"]["cycle_but_no_sleep"], ["2026-09-23"])
        self.assertEqual(out["profile"]["first_name"], "Ana")
        self.assertEqual(out["body_measurement"]["height_cm"], 168)
        self.assertEqual(out["errors"], [])

    def test_missing_scope_is_named(self):
        fake = FakeWhoop(self.data, expire_first_call=False, forbidden={"/activity/workout"})
        out = self.run_main(fake)
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("Missing scope: read:workout", out["errors"][0])
        self.assertEqual(out["counts"]["workouts"], 0)
        self.assertEqual(out["days"]["2026-09-18"]["recovery"]["recovery_score"], 66)

    def test_expired_token_refreshed_before_first_call(self):
        ws.TOKENS_FILE.write_text(json.dumps({"access_token": "access-1", "refresh_token": "refresh-1",
                                              "scope": " ".join(ws.SCOPES), "expires_at": 0}))
        fake = FakeWhoop(self.data)
        with mock.patch.object(ws, "http_json", fake), mock.patch("builtins.print"):
            api = ws.Whoop("id", "secret", "http://localhost:8080/callback")
            api.get("/user/profile/basic")
        self.assertEqual(api.tokens["access_token"], "access-2")
        # Refreshed proactively, so the API call itself never saw a 401.
        self.assertEqual([u for _, u in fake.calls][0], ws.TOKEN_URL)
        self.assertEqual(len(fake.calls), 2)

    def test_state_is_8_chars(self):
        s = ws.random_state()
        self.assertEqual(len(s), 8)
        self.assertTrue(s.isalnum())


if __name__ == "__main__":
    unittest.main()
