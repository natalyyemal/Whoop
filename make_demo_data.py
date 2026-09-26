#!/usr/bin/env python3
"""Write demo_whoop_data.json: 180 days of SYNTHETIC WHOOP data in whoop_data.json's format.

For previewing and testing the dashboard without a real pull. The data is generated as raw
v2 API records and passed through whoop_sync's own normalisers, so the shape is identical.
Known effects are planted so the analysis can be checked against them:
  - workout ending < 3 h before bed: -10 recovery, -8% HRV, -15 min deep sleep
  - under 7 h asleep: -7 recovery
  - Friday/Saturday nights: bed ~75 min later, 35 min less sleep, -5 recovery
  - >= 30 min zone 2 the day before: +3 recovery, +5% HRV, +12 min deep sleep
  - strain above 14 the day before: -3 recovery per strain point
  - last 30 days: HRV drifts down ~8%, resting HR up ~2 bpm
  - naps: no effect (should come out as noise)
"""

import json
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import whoop_sync as ws

TZ = "-03:00"
LOCAL = timezone(timedelta(hours=-3))


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main(out=Path(__file__).resolve().parent / "demo_whoop_data.json", days=180, end=date(2026, 9, 25)):
    rnd = random.Random(7)
    start = end - timedelta(days=days - 1)
    missing = {start + timedelta(d) for d in (41, 42, 97, 131)}  # strap not worn

    # Per-day behaviour for the day BEFORE each night.
    plan = {}
    for i in range(-1, days + 1):
        d = start + timedelta(i)
        wk = []
        if rnd.random() < 0.68:
            kind = rnd.choices(["zone2 ride", "running", "hiit"], [0.35, 0.4, 0.25])[0]
            evening = rnd.random() < 0.55
            hour = rnd.uniform(18.0, 21.5) if evening else rnd.uniform(6.0, 8.5)
            dur = rnd.uniform(40, 90) if kind == "zone2 ride" else rnd.uniform(30, 60)
            wk.append((kind, hour, dur))
        plan[d] = wk

    # Day strain for each date, from that day's workouts.
    strain_of = {}
    for d, wk in plan.items():
        st = 7.0 + rnd.gauss(0, 1.5)
        for kind, _, dur in wk:
            st += {"zone2 ride": 5.5, "running": 7.5, "hiit": 9.0}[kind] * (dur / 60) + rnd.gauss(0, 1)
        strain_of[d] = round(min(20.8, max(2.0, st)), 1)

    cycles, recs, sleeps, workouts = [], [], [], []
    for i in range(days + 1):
        d = start + timedelta(i)          # wake-up date
        prev = d - timedelta(1)
        weekend = d.weekday() in (5, 6)   # Friday / Saturday nights
        late_rel = 0.0
        bed_h = 23.25 + rnd.gauss(0, 0.45) + (1.25 if weekend else 0)
        # A workout on the previous evening pushes bedtime a little later.
        wk = plan.get(prev, [])
        ends = [h + dur / 60 for _, h, dur in wk]
        if ends:
            bed_h = max(bed_h, max(ends) + rnd.uniform(0.8, 3.8))
        late = any(0 <= bed_h - e < 3 for e in ends)
        zone2 = any(k == "zone2 ride" and dur >= 45 for k, _, dur in wk)
        onset = datetime(prev.year, prev.month, prev.day, tzinfo=LOCAL) + timedelta(hours=bed_h)
        asleep_h = max(4.8, rnd.gauss(7.45, 0.55) - (0.6 if weekend else 0) - (0.3 if late else 0))
        awake_min = rnd.uniform(20, 50)
        wake = onset + timedelta(hours=asleep_h, minutes=awake_min)

        strain, prev_strain = strain_of.get(d, 8.0), strain_of[prev]

        drift = max(0, i - (days - 30)) / 30          # 0 -> 1 over the last 30 days
        short = asleep_h < 7
        rec = (64 - 10 * late - 7 * short - 5 * weekend + 3 * zone2
               - 3 * max(0.0, prev_strain - 14) - 6 * drift + rnd.gauss(0, 7.5))
        rec = int(min(99, max(4, rec)))
        hrv = 66 * (1 - 0.08 * late + 0.05 * zone2 - 0.03 * weekend - 0.08 * drift) * rnd.gauss(1, 0.07)
        rhr = 52 + 1.8 * late + 2 * drift + rnd.gauss(0, 1.3)
        deep = max(35, rnd.gauss(98, 13) - 15 * late + 12 * zone2)
        rem = asleep_h * 60 * rnd.uniform(0.2, 0.26)
        light = asleep_h * 60 - deep - rem

        if d in missing or i == days:
            continue
        cid, sid = i + 1, f"sleep-{i}"
        cycles.append({"id": cid, "start": iso(onset), "end": None, "timezone_offset": TZ,
                       "score_state": "SCORED",
                       "score": {"strain": strain, "kilojoule": 4184 * rnd.uniform(1.9, 2.3) + 900 * strain,
                                 "average_heart_rate": 64 + int(strain), "max_heart_rate": 120 + int(3 * strain)}})
        ms = lambda m: int(m * 60000)
        sleeps.append({"id": sid, "cycle_id": cid, "start": iso(onset), "end": iso(wake), "timezone_offset": TZ,
                       "nap": False, "score_state": "SCORED",
                       "score": {"stage_summary": {
                           "total_in_bed_time_milli": ms(asleep_h * 60 + awake_min),
                           "total_awake_time_milli": ms(awake_min), "total_no_data_time_milli": 0,
                           "total_light_sleep_time_milli": ms(light),
                           "total_slow_wave_sleep_time_milli": ms(deep),
                           "total_rem_sleep_time_milli": ms(rem),
                           "sleep_cycle_count": int(asleep_h / 1.5), "disturbance_count": rnd.randint(4, 16)},
                           "sleep_needed": {"baseline_milli": ms(460), "need_from_sleep_debt_milli": ms(15),
                                            "need_from_recent_strain_milli": ms(strain), "need_from_recent_nap_milli": 0},
                           "respiratory_rate": rnd.gauss(15.1, 0.35),
                           "sleep_performance_percentage": int(min(100, asleep_h / 7.9 * 100)),
                           "sleep_consistency_percentage": int(max(40, 88 - 25 * weekend + rnd.gauss(0, 6))),
                           "sleep_efficiency_percentage": asleep_h * 60 / (asleep_h * 60 + awake_min) * 100}})
        recs.append({"cycle_id": cid, "sleep_id": sid, "score_state": "SCORED",
                     "score": {"user_calibrating": False, "recovery_score": rec, "resting_heart_rate": round(rhr),
                               "hrv_rmssd_milli": hrv, "spo2_percentage": rnd.gauss(96.3, 0.7),
                               "skin_temp_celsius": rnd.gauss(33.6, 0.25)}})
        if rnd.random() < 0.12:
            n0 = datetime(d.year, d.month, d.day, 15, tzinfo=LOCAL)
            sleeps.append({"id": f"nap-{i}", "cycle_id": cid, "start": iso(n0), "end": iso(n0 + timedelta(minutes=30)),
                           "timezone_offset": TZ, "nap": True, "score_state": "SCORED", "score": {}})

    for i, (d, wk) in enumerate(sorted(plan.items())):
        if d < start or d in missing:
            continue
        for kind, hour, dur in wk:
            s0 = datetime(d.year, d.month, d.day, tzinfo=LOCAL) + timedelta(hours=hour)
            z = {"zone2 ride": [2, 8, dur - 14, 3, 1, 0], "running": [1, 4, 10, dur - 25, 8, 2],
                 "hiit": [2, 5, 6, 8, dur - 31, 10]}[kind]
            workouts.append({"id": f"w-{d}-{kind}", "sport_name": "cycling" if kind == "zone2 ride" else kind,
                             "start": iso(s0), "end": iso(s0 + timedelta(minutes=dur)), "timezone_offset": TZ,
                             "score_state": "SCORED",
                             "score": {"strain": {"zone2 ride": 9, "running": 12, "hiit": 13}[kind] + rnd.gauss(0, 1.5),
                                       "average_heart_rate": 140, "max_heart_rate": 175, "kilojoule": 11 * 4.184 * dur,
                                       "distance_meter": 25000.0 if kind == "zone2 ride" else (8000.0 if kind == "running" else None),
                                       "zone_durations": {f"zone_{n}_milli": int(max(0, m) * 60000)
                                                          for n, m in zip(ws.ZONES, z)}}})

    s_date, e_date = start.isoformat(), end.isoformat()
    day_map = ws.build_days(cycles, recs, sleeps, workouts, s_date, e_date)
    out_obj = {
        "demo": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "SYNTHETIC demo data (make_demo_data.py) - not a real WHOOP account",
        "coverage": ws.coverage(day_map, s_date, e_date),
        "errors": [],
        "profile": {"user_id": 0, "first_name": "Demo", "last_name": "User", "email": None},
        "body_measurement": {"height_m": 1.75, "height_cm": 175, "weight_kg": 72.0, "max_heart_rate": 190},
        "counts": {"recovery": len(recs), "cycles": len(cycles), "sleep": len(sleeps), "workouts": len(workouts)},
        "days": day_map,
        "workouts": sorted((ws.norm_workout(w) for w in workouts), key=lambda w: w["start"]),
    }
    out.write_text(json.dumps(out_obj, indent=1))
    print(f"Wrote {out} ({len(day_map)} days, SYNTHETIC)")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "demo_whoop_data.json")
