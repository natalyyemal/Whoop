# WHOOP data + health dashboard

Standard-library Python only.

```bash
cp .env.example .env              # add WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET
python3 whoop_sync.py             # OAuth in your browser, pulls 180 days -> whoop_data.json
python3 build_dashboard.py        # -> dashboard.html in Spanish (add --lang en for English); open it in a browser
```

Preview without a WHOOP account (clearly marked synthetic data):

```bash
python3 make_demo_data.py && python3 build_dashboard.py --data demo_whoop_data.json --out demo_dashboard.html
```

Tests: `python3 -m unittest test_whoop_sync test_build_dashboard`

`whoop_data.json`, `.whoop_tokens.json`, `.env` and `dashboard.html` hold personal data or secrets and are gitignored.
