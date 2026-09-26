#!/usr/bin/env python3
"""Build dashboard.html - one self-contained file - from whoop_data.json.

Every number on the page is computed here from the data file; the HTML only draws it.

Usage:
    python3 build_dashboard.py                              # whoop_data.json -> dashboard.html
    python3 build_dashboard.py --data demo_whoop_data.json --out demo_dashboard.html

How the analysis reads the data
-------------------------------
Each row is one wake-up date D. Its recovery, HRV, resting HR and sleep describe the night
that ended on the morning of D; "behaviours" are what happened the day before (D-1) and
that night (bedtime, time asleep). Comparisons are with/without the behaviour, reported
with the number of days on each side and a permutation-test p-value. Groups with fewer than
MIN_N days are reported as "too little data" instead of being ranked. These are
associations in observational data, not controlled experiments.
"""

import argparse
import json
import math
import random
import statistics as st
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import i18n

HERE = Path(__file__).resolve().parent
MIN_N = 8            # minimum days on each side of a comparison
PERMUTATIONS = 2000
BASELINE_DAYS = 30
T = i18n.text("es")  # set by analyse()

# key, label, unit, which direction is good, decimals
TILES = [
    ("recovery", "Recovery", "%", "up", 0),
    ("hrv", "HRV", "ms", "up", 0),
    ("rhr", "Resting HR", "bpm", "down", 0),
    ("sleep_perf", "Sleep performance", "%", "up", 0),
    ("sleep_hours", "Hours slept", "h", "up", 1),
    ("strain", "Day strain", "", None, 1),
    ("spo2", "SpO₂", "%", "up", 1),
    ("resp", "Respiratory rate", "rpm", "down", 1),
]
EXTRA_TREND = [
    ("deep_min", "Deep sleep", "min", "up", 0),
    ("rem_min", "REM sleep", "min", "up", 0),
    ("sleep_consistency", "Sleep consistency", "%", "up", 0),
    ("disturbances", "Sleep disturbances", "", "down", 0),
    ("skin_temp", "Skin temperature", "°C", None, 2),
]


# --------------------------------------------------------------------------- rows

def parse(ts):
    return datetime.fromisoformat(ts) if ts else None


def clock_min(dt):
    """Minutes after local noon of the evening before, so 23:30 -> 690 and 00:30 -> 750."""
    m = dt.hour * 60 + dt.minute
    return m - 720 if m >= 720 else m + 720


def fmt_clock(noon_min):
    m = int(round(noon_min + 720)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def build_rows(data):
    days = data["days"]
    keys = sorted(days)
    d0, d1 = date.fromisoformat(keys[0]), date.fromisoformat(keys[-1])
    rows = []
    for i in range((d1 - d0).days + 1):
        d = (d0 + timedelta(i)).isoformat()
        day = days.get(d) or {}
        c, r, s = day.get("cycle") or {}, day.get("recovery") or {}, day.get("sleep") or {}
        sm = s.get("stages_minutes") or {}
        onset, wake = parse(s.get("start")), parse(s.get("end"))
        rows.append({
            "date": d,
            "has_cycle": bool(c),
            "cycle_open": bool(c) and not c.get("end"),
            "strain": c.get("strain"),
            "calories": c.get("calories"),
            "recovery": r.get("recovery_score"),
            "hrv": r.get("hrv_rmssd_ms"),
            "rhr": r.get("resting_heart_rate"),
            "spo2": r.get("spo2_percentage"),
            "skin_temp": r.get("skin_temp_celsius"),
            "sleep_perf": s.get("sleep_performance_percentage"),
            "sleep_consistency": s.get("sleep_consistency_percentage"),
            "sleep_efficiency": s.get("sleep_efficiency_percentage"),
            "resp": s.get("respiratory_rate"),
            "sleep_hours": s.get("total_asleep_hours"),
            "deep_min": sm.get("deep_min"),
            "rem_min": sm.get("rem_min"),
            "light_min": sm.get("light_min"),
            "awake_min": sm.get("awake_min"),
            "disturbances": s.get("disturbance_count"),
            "onset": onset,
            "wake": wake,
            "bed_min": clock_min(onset) if onset else None,
            "wake_min": wake.hour * 60 + wake.minute if wake else None,
            "naps": len(day.get("naps") or []),
            "workouts": day.get("workouts") or [],
        })
    return rows


def quantile(vals, q):
    vals = sorted(vals)
    if not vals:
        return None
    k = (len(vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def zone(w, z):
    return ((w.get("zone_minutes") or {}).get(z) or 0)


# --------------------------------------------------------------------------- behaviours

def build_flags(rows):
    """Return (flag definitions, per-row flag values). Values are True/False/None (unknown)."""
    strains = [r["strain"] for r in rows if r["strain"] is not None]
    beds = [r["bed_min"] for r in rows if r["bed_min"] is not None]
    hi_strain = round(quantile(strains, 0.75), 1) if len(strains) >= 20 else None
    late_bed = st.median(beds) + 45 if len(beds) >= 20 else None
    wakes = [r["wake_min"] for r in rows if r["wake_min"] is not None]
    late_wake = st.median(wakes) + 60 if len(wakes) >= 20 else None

    dec = lambda v: str(v).replace(".", T["decimal"])
    fmt = {"hi": dec(hi_strain), "late": fmt_clock(late_bed) if late_bed else "?",
           "usual": fmt_clock(late_bed - 45) if late_bed else "?",
           "late_wake": fmt_clock(late_wake - 720) if late_wake else "?",
           "usual_wake": fmt_clock(late_wake - 780) if late_wake else "?"}
    defs = []
    for key in ("late_workout", "short_sleep", "late_bed", "bed_shift", "wake_shift", "slept_in", "weekend",
                "high_strain", "back_to_back",
                "hard_zones", "zone2", "morning_workout", "rest_day", "nap"):
        on, off, action = (x.format(**fmt) for x in T["flags"][key])
        defs.append(dict(key=key, on=on, off=off, action=action))

    vals = []
    for i, r in enumerate(rows):
        p = rows[i - 1] if i > 0 else None
        p2 = rows[i - 2] if i > 1 else None
        f = {}
        pw = p["workouts"] if p else []
        prev_known = bool(p and p["has_cycle"])
        # late_workout: any previous-day workout ending 0-3 h before this night's sleep onset
        if r["onset"] and prev_known:
            gaps = [(r["onset"] - parse(w["end"])).total_seconds() / 3600 for w in pw if w.get("end")]
            f["late_workout"] = any(0 <= g < 3 for g in gaps)
        else:
            f["late_workout"] = None
        f["short_sleep"] = None if r["sleep_hours"] is None else r["sleep_hours"] < 7
        f["late_bed"] = None if (r["bed_min"] is None or late_bed is None) else r["bed_min"] > late_bed
        f["bed_shift"] = (None if r["bed_min"] is None or not p or p["bed_min"] is None
                          else abs(r["bed_min"] - p["bed_min"]) > 60)
        f["wake_shift"] = (None if r["wake_min"] is None or not p or p["wake_min"] is None
                           else abs(r["wake_min"] - p["wake_min"]) > 60)
        # Slept in the morning before (D-1): does a late wake-up shift the next night?
        f["slept_in"] = (None if not p or p["wake_min"] is None or late_wake is None
                         else p["wake_min"] > late_wake)
        f["weekend"] = None if not r["onset"] else date.fromisoformat(r["date"]).weekday() in (5, 6)
        ps = p["strain"] if p else None
        f["high_strain"] = None if ps is None or hi_strain is None else ps >= hi_strain
        f["back_to_back"] = (None if ps is None or not p2 or p2["strain"] is None or hi_strain is None
                             else ps >= hi_strain and p2["strain"] >= hi_strain)
        if prev_known:
            f["hard_zones"] = sum(zone(w, "zone_four") + zone(w, "zone_five") for w in pw) >= 15
            f["zone2"] = sum(zone(w, "zone_two") for w in pw) >= 30
            f["morning_workout"] = any(w.get("start") and parse(w["start"]).hour < 12 for w in pw)
            f["rest_day"] = not pw
            f["nap"] = p["naps"] > 0
        else:
            for k in ("hard_zones", "zone2", "morning_workout", "rest_day", "nap"):
                f[k] = None
        vals.append(f)
    thresholds = {"late_wake": fmt_clock(late_wake - 720) if late_wake else None,
                  "high_strain": dec(hi_strain) if hi_strain is not None else None, "late_bed": fmt_clock(late_bed) if late_bed else None}
    return defs, vals, thresholds


# --------------------------------------------------------------------------- statistics

def perm_p(a, b, rng):
    """Two-sided permutation test on the difference in means."""
    obs = abs(st.mean(a) - st.mean(b))
    pool, na = a + b, len(a)
    hits = 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(pool)
        if abs(st.mean(pool[:na]) - st.mean(pool[na:])) >= obs - 1e-12:
            hits += 1
    return (hits + 1) / (PERMUTATIONS + 1)


def confidence(p, n1, n0):
    if n1 < MIN_N or n0 < MIN_N:
        return "thin"
    if p < 0.05:
        return "strong"
    if p < 0.15:
        return "likely"
    return "noise"


def compare(rows, flags, key, outcome, rng):
    on = [r[outcome] for r, f in zip(rows, flags) if f.get(key) is True and r[outcome] is not None]
    off = [r[outcome] for r, f in zip(rows, flags) if f.get(key) is False and r[outcome] is not None]
    res = {"n_on": len(on), "n_off": len(off)}
    if not on or not off:
        res.update(diff=None, conf="thin")
        return res
    m1, m0 = st.mean(on), st.mean(off)
    res.update(mean_on=m1, mean_off=m0, diff=m1 - m0, pct=(m1 - m0) / m0 * 100 if m0 else None)
    enough = len(on) >= MIN_N and len(off) >= MIN_N
    res["p"] = perm_p(list(on), list(off), rng) if enough else None
    res["conf"] = confidence(res["p"] if enough else 1, len(on), len(off))
    return res


def bh_adjust(ps):
    """Benjamini-Hochberg q-values (controls false discoveries across the behaviours tested)."""
    idx = sorted(range(len(ps)), key=lambda i: ps[i])
    q, running = [0.0] * len(ps), 1.0
    for rank in range(len(ps), 0, -1):
        i = idx[rank - 1]
        running = min(running, ps[i] * len(ps) / rank)
        q[i] = running
    return q


def effects(rows, defs, flags, outcome, rng):
    out = []
    for d in defs:
        c = compare(rows, flags, d["key"], outcome, rng)
        c.update(key=d["key"], on=d["on"], off=d["off"], action=d["action"])
        out.append(c)
    tested = [c for c in out if c.get("p") is not None]
    for c, q in zip(tested, bh_adjust([c["p"] for c in tested])):
        c["q"] = q
        c["conf"] = confidence(q, c["n_on"], c["n_off"])
    return out


def solve_inverse(a):
    """Gauss-Jordan inverse of a small square matrix (list of lists)."""
    n = len(a)
    m = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        m[col] = [v / pv for v in m[col]]
        for r in range(n):
            if r != col and m[r][col]:
                f = m[r][col]
                m[r] = [a_ - f * b_ for a_, b_ in zip(m[r], m[col])]
    return [row[n:] for row in m]


def adjusted_effects(rows, flags, effs, outcome):
    """OLS of the outcome on all well-sampled behaviour flags at once.

    Each coefficient is that behaviour's association holding the others fixed, which
    separates overlapping habits (e.g. weekend nights vs late bedtimes vs short sleep)."""
    keys = [e["key"] for e in effs if e["conf"] != "thin"]
    data = [(r[outcome], [1.0] + [1.0 if f[k] else 0.0 for k in keys]) for r, f in zip(rows, flags)
            if r[outcome] is not None and all(f.get(k) is not None for k in keys)]
    k = len(keys) + 1
    if len(data) < max(40, 4 * k):
        return {}
    xtx = [[sum(x[i] * x[j] for _, x in data) for j in range(k)] for i in range(k)]
    xty = [sum(x[i] * y for y, x in data) for i in range(k)]
    inv = solve_inverse(xtx)
    if inv is None:
        return {}
    beta = [sum(inv[i][j] * xty[j] for j in range(k)) for i in range(k)]
    resid = [y - sum(b * xi for b, xi in zip(beta, x)) for y, x in data]
    sigma2 = sum(e * e for e in resid) / (len(data) - k)
    out = {}
    for i, key in enumerate(keys, start=1):
        se = math.sqrt(max(inv[i][i] * sigma2, 1e-12))
        z = beta[i] / se
        out[key] = {"effect": beta[i], "se": se, "p": math.erfc(abs(z) / math.sqrt(2)), "n": len(data)}
    return out


def ranked(effs, want):
    """want='hurt': behaviours that lower the outcome. want='lift': the direction that raises it.

    A behaviour whose 'on' state lowers the outcome also means its 'off' state raises it; for
    lifts it is reported in whichever direction is positive, with that side's day count."""
    rows, thin = [], []
    for e in effs:
        if e["diff"] is None:
            continue
        if want == "hurt":
            if e["diff"] >= 0:
                continue
            item = dict(e, label=e["on"], effect=e["diff"], eff_pct=e["pct"], n=e["n_on"], n_other=e["n_off"],
                        adj_effect=e["adj"]["effect"] if e.get("adj") else None)
        else:
            flip = e["diff"] < 0
            pct = None
            if e.get("pct") is not None:
                pct = (e["mean_off"] - e["mean_on"]) / e["mean_on"] * 100 if flip and e["mean_on"] else e["pct"]
            adj = e["adj"]["effect"] * (-1 if flip else 1) if e.get("adj") else None
            item = dict(e, label=e["off"] if flip else e["on"], effect=abs(e["diff"]), eff_pct=pct,
                        n=e["n_off"] if flip else e["n_on"], n_other=e["n_on"] if flip else e["n_off"],
                        adj_effect=adj)
        adj = item["adj_effect"]
        # "Explained": the effect vanishes (or flips) once the other behaviours are held fixed.
        item["explained"] = bool(e.get("adj")) and (adj is None or adj <= 0 or e["adj"]["p"] > 0.1) \
            if want == "lift" else bool(e.get("adj")) and (adj >= 0 or e["adj"]["p"] > 0.1)
        (thin if e["conf"] == "thin" else rows).append(item)
    order = {"strong": 0, "likely": 1, "noise": 2}
    # Real evidence first; within it, effects that survive adjustment before explained ones.
    rows.sort(key=lambda x: (x["conf"] == "noise", x["explained"], order[x["conf"]], -abs(x["effect"])))
    thin.sort(key=lambda x: -abs(x["effect"]))
    return {"ranked": rows, "thin": thin}


def pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = st.mean(xs), st.mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else None


def strain_analysis(rows, rng):
    pairs = [(rows[i - 1]["strain"], rows[i]["recovery"], rows[i]["date"])
             for i in range(1, len(rows))
             if rows[i - 1]["strain"] is not None and rows[i]["recovery"] is not None]
    res = {"pairs": [[round(s, 1), r, d] for s, r, d in pairs], "n": len(pairs)}
    if len(pairs) < 20:
        res["verdict"] = T["few_pairs"].format(n=len(pairs))
        return res
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    r = pearson(xs, ys)
    mx, my = st.mean(xs), st.mean(ys)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    # permutation p for correlation
    hits, ys2 = 0, list(ys)
    for _ in range(PERMUTATIONS):
        rng.shuffle(ys2)
        if abs(pearson(xs, ys2)) >= abs(r) - 1e-12:
            hits += 1
    p = (hits + 1) / (PERMUTATIONS + 1)
    edges = [0, 6, 8, 10, 12, 14, 16, 18, 21.1]
    bands = []
    for lo, hi in zip(edges, edges[1:]):
        v = [y for x, y in zip(xs, ys) if lo <= x < hi]
        bands.append({"lo": lo, "hi": min(hi, 21), "n": len(v), "mean": st.mean(v) if v else None})
    avg = my
    ok = [b for b in bands if b["n"] >= 5]
    res.update(r=r, p=p, slope=slope, avg_recovery=avg, bands=bands)

    good = [b for b in ok if b["mean"] >= avg]
    if abs(r) < 0.1 or p > 0.2:
        res["verdict"] = T["no_link"].format(r=f"{r:.2f}", n=len(pairs))
        res["range"] = None
        return res
    if not good:
        res["verdict"] = T["no_band"]
        res["range"] = None
        return res
    # Optimal = the most training load that doesn't cost next-day recovery: the highest band
    # (>= 5 days) at or above your average recovery, widened to the band below it if that
    # one also qualifies. Its upper edge is your personal ceiling.
    top = max(good, key=lambda b: b["lo"])
    idx = ok.index(top)
    low = ok[idx - 1] if idx > 0 and ok[idx - 1]["mean"] >= avg and ok[idx - 1]["hi"] == top["lo"] else top
    rng_lo, rng_hi = low["lo"], top["hi"]
    inside = [y for x, y in zip(xs, ys) if x < rng_hi]
    above = [y for x, y in zip(xs, ys) if x >= rng_hi]
    res["range"] = [rng_lo, rng_hi]
    res["inside"] = {"n": len(inside), "mean": st.mean(inside) if inside else None}  # everything under the ceiling
    res["above"] = {"n": len(above), "mean": st.mean(above) if above else None,
                    "share": len(above) / len(xs)}
    if len(above) >= MIN_N:
        res["above"]["p"] = perm_p(list(above), list(inside), rng)
        res["above"]["conf"] = confidence(res["above"]["p"], len(above), len(inside))
    else:
        res["above"]["conf"] = "thin"
    return res


def trend_flags(rows, rng):
    end = date.fromisoformat(rows[-1]["date"])
    last = [r for r in rows if (end - date.fromisoformat(r["date"])).days < 30]
    prev = [r for r in rows if 30 <= (end - date.fromisoformat(r["date"])).days < 60]
    out = []
    for key, label, unit, good, dec in TILES + EXTRA_TREND:
        a = [r[key] for r in last if r[key] is not None]
        b = [r[key] for r in prev if r[key] is not None]
        label, unit = localise(key, label, unit)
        item = {"key": key, "label": label, "unit": unit, "good": good, "dec": dec, "n_last": len(a), "n_prev": len(b)}
        if len(a) < 10 or len(b) < 10:
            item["status"] = "thin"
            out.append(item)
            continue
        ma, mb = st.mean(a), st.mean(b)
        p = perm_p(list(a), list(b), rng)
        diff = ma - mb
        item.update(last=ma, prev=mb, diff=diff, pct=diff / mb * 100 if mb else None, p=p)
        if good is None:
            item["status"] = "changed" if p < 0.1 else "stable"
        elif p >= 0.1:
            item["status"] = "stable"
        else:
            better = diff > 0 if good == "up" else diff < 0
            item["status"] = "better" if better else "worse"
        out.append(item)
    return out


def tiles(rows):
    out = []
    for key, label, unit, good, dec in TILES:
        latest = next((r for r in reversed(rows) if r[key] is not None), None)
        label, unit = localise(key, label, unit)
        item = {"key": key, "label": label, "unit": unit, "good": good, "dec": dec}
        if not latest:
            out.append(item)
            continue
        ld = date.fromisoformat(latest["date"])
        base = [r[key] for r in rows
                if r[key] is not None and 1 <= (ld - date.fromisoformat(r["date"])).days <= BASELINE_DAYS]
        item.update(value=latest[key], date=latest["date"], base_n=len(base),
                    in_progress=key == "strain" and latest["cycle_open"] and latest is rows[-1],
                    baseline=st.mean(base) if len(base) >= 7 else None,
                    sd=st.stdev(base) if len(base) >= 7 else None)
        out.append(item)
    return out


def series(rows):
    """Daily values plus a trailing 30-day baseline (mean +/- 1 SD of the 30 days before)."""
    out = {}
    for key, *_ in TILES + EXTRA_TREND:
        pts = []
        for i, r in enumerate(rows):
            d = date.fromisoformat(r["date"])
            win = [x[key] for x in rows[max(0, i - BASELINE_DAYS):i]
                   if x[key] is not None and (d - date.fromisoformat(x["date"])).days <= BASELINE_DAYS]
            m = st.mean(win) if len(win) >= 7 else None
            sd = st.stdev(win) if len(win) >= 7 else None
            pts.append([r["date"], r[key], None if m is None else round(m, 2), None if sd is None else round(sd, 2)])
        out[key] = pts
    return out


def priorities(recovery_hurts, strain):
    """Top changes by expected average gain = effect when it happens x how often it happens."""
    cands = []
    for e in recovery_hurts["ranked"]:
        if e["conf"] not in ("strong", "likely") or not e["action"]:
            continue
        # Use the effect adjusted for the other behaviours when available, so overlapping
        # habits (weekends -> late bed -> short sleep) are not counted twice.
        adj = e.get("adj")
        if adj and (adj["effect"] >= 0 or adj["p"] > 0.1):
            continue  # the raw effect is explained by other behaviours
        eff = adj["effect"] if adj else e["diff"]
        freq = e["n_on"] / (e["n_on"] + e["n_off"])
        cands.append({"key": e["key"], "label": e["on"], "action": e["action"], "effect": eff,
                      "raw_effect": e["diff"], "adjusted": bool(adj), "freq": freq, "n": e["n_on"],
                      "n_other": e["n_off"], "gain": -eff * freq, "conf": e["conf"]})
    rg, ab = strain.get("range"), strain.get("above") or {}
    if rg and ab.get("conf") in ("strong", "likely") and strain["inside"]["mean"] is not None:
        eff = ab["mean"] - strain["inside"]["mean"]
        if eff < 0:
            cands.append({"key": "strain_cap", "label": T["cap_label"].format(hi=f"{rg[1]:g}".replace(".", T["decimal"])),
                          "action": T["cap_action"].format(lo=f"{rg[0]:g}".replace(".", T["decimal"]),
                                                           hi=f"{rg[1]:g}".replace(".", T["decimal"])),
                          "effect": eff, "raw_effect": eff, "adjusted": False, "freq": ab["share"], "n": ab["n"], "n_other": strain["inside"]["n"],
                          "gain": -eff * ab["share"], "conf": ab["conf"]})
    # Drop the strain cap if it duplicates the high-strain behaviour; keep the larger gain.
    keys = {c["key"] for c in cands}
    if "strain_cap" in keys and "high_strain" in keys:
        a = next(c for c in cands if c["key"] == "strain_cap")
        b = next(c for c in cands if c["key"] == "high_strain")
        cands.remove(b if a["gain"] >= b["gain"] else a)
    cands.sort(key=lambda c: -c["gain"])
    return cands[:3], len(cands)


# --------------------------------------------------------------------------- assemble

def clean(o):
    if isinstance(o, float):
        return None if math.isnan(o) else round(o, 3)
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def localise(key, label, unit):
    return T["metric"].get(key, label), T["unit"].get(unit, unit)


def analyse(data, lang="es"):
    global T
    T = i18n.text(lang)
    rng = random.Random(1234)
    rows = build_rows(data)
    defs, flags, thresholds = build_flags(rows)
    rec_eff = effects(rows, defs, flags, "recovery", rng)
    deep_eff = effects(rows, defs, flags, "deep_min", rng)
    hrv_eff = effects(rows, defs, flags, "hrv", rng)
    for effs, outcome in ((rec_eff, "recovery"), (deep_eff, "deep_min"), (hrv_eff, "hrv")):
        adj = adjusted_effects(rows, flags, effs, outcome)
        for e in effs:
            e["adj"] = adj.get(e["key"])
    strain = strain_analysis(rows, rng)
    hurts = ranked(rec_eff, "hurt")
    top3, n_cands = priorities(hurts, strain)
    stages = [[r["date"], r["deep_min"], r["rem_min"], r["light_min"], r["awake_min"]]
              for r in rows if r["deep_min"] is not None]
    table = [{k: r[k] for k in ("date", "recovery", "hrv", "rhr", "sleep_perf", "sleep_hours", "strain",
                                "spo2", "resp", "deep_min", "rem_min")} for r in rows]
    return clean({
        "demo": bool(data.get("demo")),
        "source": data.get("source"),
        "generated_at": data.get("generated_at"),
        "profile": data.get("profile"),
        "coverage": data.get("coverage"),
        "errors": data.get("errors") or [],
        "min_n": MIN_N,
        "thresholds": thresholds,
        "tiles": tiles(rows),
        "series": series(rows),
        "series_meta": [{"key": k, "label": localise(k, l, u)[0], "unit": localise(k, l, u)[1], "good": g, "dec": d}
                        for k, l, u, g, d in TILES + EXTRA_TREND],
        "stages": stages,
        "hurts_recovery": hurts,
        "lifts_deep": ranked(deep_eff, "lift"),
        "lifts_hrv": ranked(hrv_eff, "lift"),
        "strain": strain,
        "trends": trend_flags(rows, rng),
        "top3": top3,
        "n_candidates": n_cands,
        "table": table,
    })


def render(result, lang="es"):
    template = i18n.apply_template((HERE / "dashboard_template.html").read_text(), lang)
    payload = json.dumps(result, separators=(",", ":")).replace("</", "<\\/")
    return template.replace("/*__DATA__*/null", payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(HERE / "whoop_data.json"))
    ap.add_argument("--out", default=str(HERE / "dashboard.html"))
    ap.add_argument("--lang", choices=i18n.LANGS, default="es", help="dashboard language (default: es)")
    args = ap.parse_args()
    src = Path(args.data)
    if not src.exists():
        sys.exit(f"{src} not found - run whoop_sync.py first (or make_demo_data.py for a synthetic preview).")
    data = json.loads(src.read_text())
    if not data.get("days"):
        sys.exit(f"{src} has no days in it.")
    result = analyse(data, args.lang)
    Path(args.out).write_text(render(result, args.lang))
    print(f"Wrote {args.out}" + ("  [DEMO - synthetic data]" if result["demo"] else ""))


if __name__ == "__main__":
    main()
