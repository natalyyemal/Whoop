#!/usr/bin/env python3
"""Pull the last N days of WHOOP data through the official v2 API.

Steps: OAuth 2.0 authorization-code flow -> paginated pull of recovery, cycles,
sleep, workouts, profile and body measurements -> normalise units -> join per
local calendar day -> write whoop_data.json and print a coverage report.

Standard library only. Credentials come from the environment or a .env file:
    WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET, WHOOP_REDIRECT_URI (optional)

Usage:
    python3 whoop_sync.py                # 180 days, opens the browser if needed
    python3 whoop_sync.py --days 30
    python3 whoop_sync.py --no-browser   # print the auth URL instead of opening it
    python3 whoop_sync.py --paste        # paste the redirect URL instead of running a server
"""

import argparse
import http.server
import json
import os
import secrets
import string
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKENS_FILE = HERE / ".whoop_tokens.json"
OUTPUT_FILE = HERE / "whoop_data.json"

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer/v2"
SCOPES = [
    "read:recovery", "read:cycles", "read:sleep", "read:workout",
    "read:profile", "read:body_measurement", "offline",
]
# Which scope each endpoint needs, so a 401/403 can name the missing one.
ENDPOINT_SCOPE = {
    "/recovery": "read:recovery",
    "/cycle": "read:cycles",
    "/activity/sleep": "read:sleep",
    "/activity/workout": "read:workout",
    "/user/profile/basic": "read:profile",
    "/user/measurement/body": "read:body_measurement",
}
PAGE_LIMIT = 25
USER_AGENT = "whoop-sync/1.0 (+personal data export)"
KJ_PER_KCAL = 4.184


class ScopeError(Exception):
    pass


# --------------------------------------------------------------------------- config

def load_dotenv(path=HERE / ".env"):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def config():
    load_dotenv()
    cid = os.environ.get("WHOOP_CLIENT_ID")
    secret = os.environ.get("WHOOP_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET (env vars or a .env file next to this script).")
    redirect = os.environ.get("WHOOP_REDIRECT_URI", "http://localhost:8080/callback")
    return cid, secret, redirect


# --------------------------------------------------------------------------- http

def http_json(method, url, *, headers=None, form=None, timeout=30):
    """Return (status, parsed_json_or_text, response_headers). Never raises on HTTP errors."""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if form is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    # WHOOP's edge (Cloudflare) rejects urllib's default "Python-urllib/x.y" agent with error 1010.
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, body, hdrs = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        status, body, hdrs = e.code, e.read(), e.headers
    text = body.decode("utf-8", "replace")
    try:
        return status, json.loads(text) if text else None, hdrs
    except json.JSONDecodeError:
        return status, text, hdrs


# --------------------------------------------------------------------------- tokens

def save_tokens(payload, previous=None):
    tokens = {
        "access_token": payload["access_token"],
        # WHOOP rotates refresh tokens; keep the old one only if a new one wasn't sent.
        "refresh_token": payload.get("refresh_token") or (previous or {}).get("refresh_token"),
        "token_type": payload.get("token_type", "bearer"),
        "scope": payload.get("scope", (previous or {}).get("scope", "")),
        "expires_at": int(time.time()) + int(payload.get("expires_in", 3600)),
    }
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    try:
        TOKENS_FILE.chmod(0o600)
    except OSError:
        pass
    return tokens


def load_tokens():
    if TOKENS_FILE.exists():
        return json.loads(TOKENS_FILE.read_text())
    return None


def exchange_code(code, cid, secret, redirect):
    status, body, _ = http_json("POST", TOKEN_URL, form={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect,
        "client_id": cid,
        "client_secret": secret,
    })
    if status != 200:
        sys.exit(f"Token exchange failed ({status}): {body}")
    if "refresh_token" not in body:
        print("WARNING: no refresh_token returned - was the 'offline' scope granted?")
    return save_tokens(body)


def refresh(tokens, cid, secret):
    if not tokens or not tokens.get("refresh_token"):
        return None
    status, body, _ = http_json("POST", TOKEN_URL, form={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": cid,
        "client_secret": secret,
        "scope": "offline",
    })
    if status != 200:
        print(f"Refresh failed ({status}): {body}")
        return None
    print("Access token refreshed.")
    return save_tokens(body, previous=tokens)


def random_state(n=8):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def authorize(cid, secret, redirect, open_browser=True, paste=False):
    state = random_state(8)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": redirect,
        "scope": " ".join(SCOPES),
        "state": state,
    })

    if paste:
        print("\nOpen this URL, approve, then paste the full URL your browser was redirected to\n"
              "(the page itself may fail to load - that's fine, copy it from the address bar):\n")
        print(url, "\n")
        params = urllib.parse.parse_qs(urllib.parse.urlparse(input("Redirect URL: ").strip()).query)
        result = {k: v[0] for k, v in params.items()}
    else:
        result = wait_for_callback(url, redirect, open_browser)

    if "error" in result:
        sys.exit(f"Authorization failed: {result.get('error')} - {result.get('error_description', '')}")
    if result.get("state") != state:
        sys.exit("State mismatch - aborting (possible CSRF or stale redirect).")
    if "code" not in result:
        sys.exit(f"No authorization code in redirect: {result}")
    return exchange_code(result["code"], cid, secret, redirect)


def wait_for_callback(url, redirect, open_browser, timeout=300):
    parsed = urllib.parse.urlparse(redirect)
    host, port, path = parsed.hostname or "localhost", parsed.port or 80, parsed.path or "/"
    result = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            req = urllib.parse.urlparse(self.path)
            if req.path != path:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in urllib.parse.parse_qs(req.query).items()})
            ok = "code" in result
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "WHOOP connected. You can close this tab." if ok else f"Authorization failed: {result}"
            self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())
            done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Listening on {redirect}")
    if open_browser and webbrowser.open(url):
        print("Opened the WHOOP login page in your browser.")
    else:
        print("Open this URL in your browser:\n\n" + url + "\n")
    got = done.wait(timeout)
    server.shutdown()
    if not got:
        sys.exit("Timed out waiting for the OAuth redirect.")
    return result


def report_granted_scopes(tokens):
    granted = set((tokens.get("scope") or "").split())
    if not granted:
        return
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        print("WARNING: these scopes were NOT granted:", ", ".join(missing))


# --------------------------------------------------------------------------- api client

class Whoop:
    def __init__(self, cid, secret, redirect, open_browser=True, paste=False):
        self.cid, self.secret, self.redirect = cid, secret, redirect
        tokens = load_tokens()
        if tokens and tokens.get("expires_at", 0) - 60 <= time.time():
            tokens = refresh(tokens, cid, secret)
        if not tokens:
            tokens = authorize(cid, secret, redirect, open_browser, paste)
        self.tokens = tokens
        report_granted_scopes(tokens)

    def get(self, path, params=None):
        if self.tokens.get("expires_at", 0) - 60 <= time.time():
            self._refresh_or_die()
        url = API_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        retried_auth = False
        attempts_429 = 0
        while True:
            status, body, hdrs = http_json("GET", url, headers={
                "Authorization": f"Bearer {self.tokens['access_token']}"})
            if status == 200:
                return body
            if status == 401 and not retried_auth:
                retried_auth = True
                self._refresh_or_die()
                continue
            if status in (401, 403):
                scope = ENDPOINT_SCOPE.get(path, "unknown")
                raise ScopeError(f"GET {path} -> {status}. Missing scope: {scope}. Body: {body}")
            if status == 429 and attempts_429 < 5:
                attempts_429 += 1
                wait = int(hdrs.get("Retry-After") or 2 ** attempts_429)
                print(f"Rate limited on {path}; waiting {wait}s")
                time.sleep(wait)
                continue
            if status >= 500 and attempts_429 < 3:
                attempts_429 += 1
                time.sleep(2 ** attempts_429)
                continue
            raise RuntimeError(f"GET {path} failed ({status}): {body}")

    def _refresh_or_die(self):
        new = refresh(self.tokens, self.cid, self.secret)
        if not new:
            sys.exit("Could not refresh the access token. Delete .whoop_tokens.json and run again to re-authorise.")
        self.tokens = new

    def collection(self, path, start, end):
        records, token, pages = [], None, 0
        while True:
            params = {"limit": PAGE_LIMIT, "start": start, "end": end}
            if token:
                params["nextToken"] = token
            page = self.get(path, params) or {}
            records.extend(page.get("records", []))
            pages += 1
            token = page.get("next_token")
            if not token:
                break
        print(f"  {path:<20} {len(records):>4} records ({pages} pages)")
        return records


# --------------------------------------------------------------------------- normalisation

def ms_to_hm(ms):
    if ms is None:
        return None
    total_min = round(ms / 60000)
    return f"{total_min // 60}h {total_min % 60:02d}m"


def ms_to_min(ms):
    return None if ms is None else round(ms / 60000, 1)


def ms_to_hours(ms):
    return None if ms is None else round(ms / 3_600_000, 2)


def kj_to_kcal(kj):
    return None if kj is None else round(kj / KJ_PER_KCAL)


def rnd(x, n=1):
    return None if x is None else round(x, n)


def parse_offset(offset):
    if not offset:
        return None
    sign = -1 if offset.startswith("-") else 1
    hh, mm = offset.lstrip("+-").split(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))


def to_local(ts, offset=None):
    """ISO UTC timestamp -> aware datetime in the record's own timezone (else this machine's)."""
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    tz = parse_offset(offset)
    return dt.astimezone(tz) if tz else dt.astimezone()


def local_iso(ts, offset=None):
    dt = to_local(ts, offset)
    return dt.isoformat(timespec="minutes") if dt else None


def norm_cycle(c):
    s = c.get("score") or {}
    return {
        "cycle_id": c.get("id"),
        "start": local_iso(c.get("start"), c.get("timezone_offset")),
        "end": local_iso(c.get("end"), c.get("timezone_offset")),
        "score_state": c.get("score_state"),
        "strain": rnd(s.get("strain"), 2),
        "calories": kj_to_kcal(s.get("kilojoule")),
        "kilojoule": rnd(s.get("kilojoule")),
        "average_heart_rate": s.get("average_heart_rate"),
        "max_heart_rate": s.get("max_heart_rate"),
    }


def norm_recovery(r):
    s = r.get("score") or {}
    return {
        "score_state": r.get("score_state"),
        "recovery_score": s.get("recovery_score"),
        "resting_heart_rate": s.get("resting_heart_rate"),
        # HRV RMSSD is a physiological value measured in ms, not a duration: keep it in ms.
        "hrv_rmssd_ms": rnd(s.get("hrv_rmssd_milli")),
        "spo2_percentage": rnd(s.get("spo2_percentage")),
        "skin_temp_celsius": rnd(s.get("skin_temp_celsius"), 2),
        "user_calibrating": s.get("user_calibrating"),
    }


STAGES = [
    ("in_bed", "total_in_bed_time_milli"),
    ("light", "total_light_sleep_time_milli"),
    ("deep", "total_slow_wave_sleep_time_milli"),
    ("rem", "total_rem_sleep_time_milli"),
    ("awake", "total_awake_time_milli"),
    ("no_data", "total_no_data_time_milli"),
]


def norm_sleep(sl):
    s = sl.get("score") or {}
    st = s.get("stage_summary") or {}
    need = s.get("sleep_needed") or {}
    stages = {name: ms_to_hm(st.get(key)) for name, key in STAGES}
    stages_min = {f"{name}_min": ms_to_min(st.get(key)) for name, key in STAGES}
    asleep_ms = None
    if st:
        asleep_ms = sum(st.get(k) or 0 for k in (
            "total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli", "total_rem_sleep_time_milli"))
    need_ms = sum(v for k, v in need.items() if k.endswith("_milli") and v is not None) if need else None
    return {
        "sleep_id": sl.get("id"),
        "nap": sl.get("nap"),
        "start": local_iso(sl.get("start"), sl.get("timezone_offset")),
        "end": local_iso(sl.get("end"), sl.get("timezone_offset")),
        "score_state": sl.get("score_state"),
        "sleep_performance_percentage": s.get("sleep_performance_percentage"),
        "sleep_efficiency_percentage": rnd(s.get("sleep_efficiency_percentage")),
        "sleep_consistency_percentage": s.get("sleep_consistency_percentage"),
        "respiratory_rate": rnd(s.get("respiratory_rate"), 1),
        "total_asleep": ms_to_hm(asleep_ms),
        "total_asleep_hours": ms_to_hours(asleep_ms),
        "stages": stages,
        "stages_minutes": stages_min,
        "sleep_cycle_count": st.get("sleep_cycle_count"),
        "disturbance_count": st.get("disturbance_count"),
        "sleep_needed": ms_to_hm(need_ms),
        "sleep_needed_hours": ms_to_hours(need_ms),
    }


ZONES = ["zero", "one", "two", "three", "four", "five"]


def norm_workout(w):
    s = w.get("score") or {}
    zd = s.get("zone_durations") or {}
    start, end = w.get("start"), w.get("end")
    dur_ms = None
    if start and end:
        dur_ms = (to_local(end) - to_local(start)).total_seconds() * 1000
    return {
        "workout_id": w.get("id"),
        "sport_name": w.get("sport_name"),
        "start": local_iso(start, w.get("timezone_offset")),
        "end": local_iso(end, w.get("timezone_offset")),
        "duration": ms_to_hm(dur_ms),
        "duration_min": ms_to_min(dur_ms),
        "score_state": w.get("score_state"),
        "strain": rnd(s.get("strain"), 2),
        "average_heart_rate": s.get("average_heart_rate"),
        "max_heart_rate": s.get("max_heart_rate"),
        "calories": kj_to_kcal(s.get("kilojoule")),
        "kilojoule": rnd(s.get("kilojoule")),
        "distance_km": rnd(s["distance_meter"] / 1000, 2) if s.get("distance_meter") is not None else None,
        "distance_meter": rnd(s.get("distance_meter")),
        "zone_durations": {f"zone_{z}": ms_to_hm(zd.get(f"zone_{z}_milli")) for z in ZONES} if zd else None,
        "zone_minutes": {f"zone_{z}": ms_to_min(zd.get(f"zone_{z}_milli")) for z in ZONES} if zd else None,
    }


def local_date(ts, offset):
    dt = to_local(ts, offset)
    return dt.date().isoformat() if dt else None


def build_days(cycles, recoveries, sleeps, workouts, start_date, end_date):
    """Key each calendar day (local time) to its cycle + recovery + main sleep (+ naps, workouts).

    A WHOOP cycle runs from one sleep onset to the next, so its start date flips depending on
    whether you fell asleep before or after midnight. The day is therefore keyed by the local
    date you woke up: the end of the cycle's main sleep, or, without a sleep, cycle start + 6 h.
    The recovery is linked by cycle_id and its sleep_id points at that night's sleep. Naps and
    workouts are attached to the day they started on.
    """
    rec_by_cycle = {r["cycle_id"]: r for r in recoveries if r.get("cycle_id") is not None}
    sleep_by_id = {s["id"]: s for s in sleeps}
    main_sleep_by_cycle, naps_by_cycle = {}, {}
    for s in sleeps:
        cid = s.get("cycle_id")
        if s.get("nap"):
            naps_by_cycle.setdefault(cid, []).append(s)
        elif cid is not None:
            main_sleep_by_cycle.setdefault(cid, s)

    days = {}
    for c in sorted(cycles, key=lambda c: c["start"]):
        rec = rec_by_cycle.get(c["id"])
        sl = sleep_by_id.get(rec.get("sleep_id")) if rec else None
        sl = sl or main_sleep_by_cycle.get(c["id"])
        if sl and sl.get("end"):
            d = local_date(sl["end"], sl.get("timezone_offset"))
        else:
            d = (to_local(c["start"], c.get("timezone_offset")) + timedelta(hours=6)).date().isoformat()
        entry = {
            "date": d,
            "cycle": norm_cycle(c),
            "recovery": norm_recovery(rec) if rec else None,
            "sleep": norm_sleep(sl) if sl else None,
            "naps": [norm_sleep(n) for n in naps_by_cycle.get(c["id"], [])],
            "workouts": [],
        }
        if d in days:  # two cycles starting the same local day (travel/tz change): keep both
            days[d].setdefault("extra_cycles", []).append(entry)
        else:
            days[d] = entry

    for w in workouts:
        d = local_date(w["start"], w.get("timezone_offset"))
        days.setdefault(d, {"date": d, "cycle": None, "recovery": None, "sleep": None,
                            "naps": [], "workouts": []})["workouts"].append(norm_workout(w))

    # Keep only the requested window, sorted by date.
    return {d: days[d] for d in sorted(days) if start_date <= d <= end_date}


def coverage(days, start_date, end_date):
    d0, d1 = date.fromisoformat(start_date), date.fromisoformat(end_date)
    all_dates = [(d0 + timedelta(n)).isoformat() for n in range((d1 - d0).days + 1)]

    def ranges(dates):
        """Collapse a sorted list of ISO dates into 'a..b' ranges."""
        out, run = [], []
        for d in dates:
            if run and date.fromisoformat(d) - date.fromisoformat(run[-1]) != timedelta(1):
                out.append(run)
                run = []
            run.append(d)
        if run:
            out.append(run)
        return [r[0] if len(r) == 1 else f"{r[0]}..{r[-1]} ({len(r)} days)" for r in out]

    no_cycle = [d for d in all_dates if not (days.get(d) or {}).get("cycle")]
    no_recovery = [d for d in all_dates if d not in no_cycle and not days[d]["recovery"]]
    no_sleep = [d for d in all_dates if d not in no_cycle and not days[d]["sleep"]]
    unscored = sorted({d for d, v in days.items() for part in ("cycle", "recovery", "sleep")
                       if v.get(part) and v[part].get("score_state") not in (None, "SCORED")})
    with_data = [d for d in all_dates if days.get(d, {}).get("cycle")]
    return {
        "window_start": start_date,
        "window_end": end_date,
        "calendar_days_in_window": len(all_dates),
        "first_day_with_data": with_data[0] if with_data else None,
        "last_day_with_data": with_data[-1] if with_data else None,
        "days_with_cycle": len(with_data),
        "days_with_recovery": sum(1 for d in all_dates if (days.get(d) or {}).get("recovery")),
        "days_with_sleep": sum(1 for d in all_dates if (days.get(d) or {}).get("sleep")),
        "gaps": {
            "no_data_at_all": ranges(no_cycle),
            "cycle_but_no_recovery": ranges(no_recovery),
            "cycle_but_no_sleep": ranges(no_sleep),
            "not_yet_scored_or_unscorable": unscored,
        },
    }


def norm_profile(p):
    return {k: p.get(k) for k in ("user_id", "first_name", "last_name", "email")} if p else None


def norm_body(b):
    if not b:
        return None
    h, w = b.get("height_meter"), b.get("weight_kilogram")
    return {
        "height_m": rnd(h, 2),
        "height_cm": rnd(h * 100) if h else None,
        "weight_kg": rnd(w, 1),
        "max_heart_rate": b.get("max_heart_rate"),
    }


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--no-browser", action="store_true", help="print the auth URL instead of opening it")
    ap.add_argument("--paste", action="store_true", help="paste the redirect URL instead of running a local server")
    args = ap.parse_args()

    cid, secret, redirect = config()
    api = Whoop(cid, secret, redirect, open_browser=not args.no_browser, paste=args.paste)

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.days)
    start_iso = start.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    end_iso = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    start_date = start.astimezone().date().isoformat()
    end_date = now.astimezone().date().isoformat()

    print(f"\nPulling {start_iso} -> {end_iso}")
    raw, errors = {}, []
    for key, path in [("recovery", "/recovery"), ("cycles", "/cycle"),
                      ("sleep", "/activity/sleep"), ("workouts", "/activity/workout")]:
        try:
            raw[key] = api.collection(path, start_iso, end_iso)
        except ScopeError as e:
            errors.append(str(e))
            raw[key] = []
    for key, path in [("profile", "/user/profile/basic"), ("body", "/user/measurement/body")]:
        try:
            raw[key] = api.get(path)
        except ScopeError as e:
            errors.append(str(e))
            raw[key] = None

    days = build_days(raw["cycles"], raw["recovery"], raw["sleep"], raw["workouts"], start_date, end_date)
    cov = coverage(days, start_date, end_date)
    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "source": "WHOOP API v2",
        "units": {
            "durations": "'Xh YYm' strings, with *_min / *_hours numeric twins",
            "calories": "kcal = kilojoule / 4.184",
            "hrv_rmssd_ms": "milliseconds (HRV value, not a duration)",
            "times": "ISO 8601 in the timezone the record was captured in",
            "day_key": "local date you woke up (end of the cycle's main sleep; else cycle start + 6 h)",
        },
        "coverage": cov,
        "errors": errors,
        "profile": norm_profile(raw["profile"]),
        "body_measurement": norm_body(raw["body"]),
        "counts": {k: len(v) for k, v in raw.items() if isinstance(v, list)},
        "days": days,
        "workouts": sorted((norm_workout(w) for w in raw["workouts"]), key=lambda w: w["start"] or ""),
    }
    OUTPUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print_report(out)


def print_report(out):
    cov, c = out["coverage"], out["counts"]
    print("\n================ WHOOP PULL SUMMARY ================")
    if out["profile"]:
        p = out["profile"]
        print(f"User:        {p.get('first_name')} {p.get('last_name')} (id {p.get('user_id')})")
    if out["body_measurement"]:
        b = out["body_measurement"]
        print(f"Body:        {b['height_cm']} cm, {b['weight_kg']} kg, max HR {b['max_heart_rate']}")
    print(f"Window:      {cov['window_start']} .. {cov['window_end']} ({cov['calendar_days_in_window']} calendar days)")
    print(f"Data range:  {cov['first_day_with_data']} .. {cov['last_day_with_data']}")
    print(f"Days:        {cov['days_with_cycle']} with a cycle, {cov['days_with_recovery']} with recovery, "
          f"{cov['days_with_sleep']} with sleep")
    print(f"Raw records: {c.get('cycles', 0)} cycles, {c.get('recovery', 0)} recoveries, "
          f"{c.get('sleep', 0)} sleeps (incl. naps), {c.get('workouts', 0)} workouts")
    g = cov["gaps"]
    for label, key in [("No data at all", "no_data_at_all"),
                       ("Cycle but no recovery", "cycle_but_no_recovery"),
                       ("Cycle but no sleep", "cycle_but_no_sleep"),
                       ("Not yet scored / unscorable", "not_yet_scored_or_unscorable")]:
        print(f"{label + ':':<29}{', '.join(g[key]) if g[key] else 'none'}")
    for e in out["errors"]:
        print("ERROR:", e)
    print(f"\nSaved -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
