#!/usr/bin/env python3
"""
garmin_sync.py
==============
Fetch Garmin Connect health metrics and write garmin_data.json for the
training dashboard.

Fetches (last DAYS_BACK days):
  - Sleep score + duration
  - HRV (last-night avg, ms)
  - Resting heart rate (bpm)

Joins each sleep entry with the previous day's TSS from training_analytics.json
so the dashboard can plot the TSS → sleep correlation without a browser-side join.

Usage
-----
  # First time — logs in and saves garth OAuth tokens to ~/.garth
  GARMIN_EMAIL=you@example.com GARMIN_PASSWORD=secret python garmin_sync.py

  # Subsequent runs reuse saved tokens (no password needed)
  python garmin_sync.py

  # GitHub Actions: set GARMIN_EMAIL + GARMIN_PASSWORD as repo secrets,
  # OR encode your ~/.garth directory and store it as GARMIN_TOKENS_B64
  # (see .github/workflows/update-garmin.yml for details).

Env vars
--------
  GARMIN_EMAIL       Garmin Connect account email
  GARMIN_PASSWORD    Garmin Connect account password
  GARMIN_TOKENSTORE  Path to garth token directory (default: ~/.garth)
  PMC_FILE           Path to training_analytics.json (default: ./training_analytics.json)
  OUTPUT             Output path (default: ./garmin_data.json)
"""

import os
import sys
import json
import argparse
from datetime import date, timedelta

try:
    from garminconnect import Garmin, GarminConnectAuthenticationError
except ImportError:
    print("ERROR: garminconnect not installed.  Run:  pip install garminconnect")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
DAYS_BACK  = 60
PMC_FILE   = os.environ.get("PMC_FILE",  "training_analytics.json")
OUTPUT     = os.environ.get("OUTPUT",    "garmin_data.json")
TOKENSTORE = os.environ.get("GARMIN_TOKENSTORE", os.path.expanduser("~/.garth"))


# ── Auth ──────────────────────────────────────────────────────────────────────
def login() -> Garmin:
    """Login to Garmin Connect, preferring saved garth tokens."""
    if os.path.isdir(TOKENSTORE):
        try:
            api = Garmin()
            api.login(TOKENSTORE)
            print(f"Logged in via saved tokens ({TOKENSTORE}).")
            return api
        except Exception:
            print("Saved tokens expired or invalid — falling back to password auth.")

    email    = os.environ.get("GARMIN_EMAIL", "")
    password = os.environ.get("GARMIN_PASSWORD", "")
    if not email or not password:
        print(
            "ERROR: No saved garth tokens found and GARMIN_EMAIL / GARMIN_PASSWORD "
            "are not set.\n"
            "Run once with credentials to save tokens, then subsequent runs are "
            "credential-free."
        )
        sys.exit(1)

    api = Garmin(email=email, password=password)
    try:
        api.login()
    except GarminConnectAuthenticationError as exc:
        print(
            f"ERROR: Garmin authentication failed — {exc}\n"
            "If your account has 2FA enabled, run this script interactively once so "
            "garth can complete the MFA flow and save tokens."
        )
        sys.exit(1)

    os.makedirs(TOKENSTORE, exist_ok=True)
    api.garth.dump(TOKENSTORE)
    print(f"Tokens saved to {TOKENSTORE} — future runs won't need your password.")
    return api


# ── Data fetchers ─────────────────────────────────────────────────────────────
def get_sleep(api: Garmin, d: date) -> dict | None:
    """Return sleep score + duration for date d, or None if unavailable."""
    try:
        raw    = api.get_sleep_data(d.isoformat())
        dto    = raw.get("dailySleepDTO", {})
        scores = dto.get("sleepScores", {})
        # Score field location varies across Garmin Connect API versions
        score = (
            scores.get("overall", {}).get("value")
            or scores.get("totalScore")
            or dto.get("sleepScore")
        )
        if score is None:
            return None
        dur_s = dto.get("sleepTimeSeconds") or dto.get("sleepDuration")
        return {
            "date":         d.isoformat(),
            "score":        int(score),
            "duration_hrs": round(dur_s / 3600, 2) if dur_s else None,
            "prev_tss":     0,   # joined with PMC after fetch
        }
    except Exception as exc:
        print(f"    sleep error {d}: {exc}")
        return None


def get_hrv(api: Garmin, d: date) -> dict | None:
    """Return last-night HRV (ms) for date d, or None."""
    try:
        raw     = api.get_hrv_data(d.isoformat())
        summary = raw.get("hrvSummary", {})
        val = (
            summary.get("lastNight5MinHigh")
            or summary.get("lastNight")
            or summary.get("weeklyAvg")
        )
        if val is None:
            return None
        return {"date": d.isoformat(), "hrv_ms": round(float(val), 1)}
    except Exception as exc:
        print(f"    HRV error {d}: {exc}")
        return None


def get_rhr(api: Garmin, d: date) -> dict | None:
    """Return resting heart rate (bpm) for date d, or None."""
    try:
        stats = api.get_stats(d.isoformat())
        val   = stats.get("restingHeartRate")
        if val is None:
            return None
        return {"date": d.isoformat(), "bpm": int(val)}
    except Exception as exc:
        print(f"    RHR error {d}: {exc}")
        return None


# ── PMC join ──────────────────────────────────────────────────────────────────
def load_pmc_tss() -> dict[str, float]:
    """Load {date_str: daily_tss} from training_analytics.json."""
    try:
        with open(PMC_FILE) as f:
            data = json.load(f)
        tss_map = {row["date"]: row["tss"] for row in data.get("pmc", [])}
        print(f"Loaded TSS for {len(tss_map)} days from {PMC_FILE}.")
        return tss_map
    except FileNotFoundError:
        print(f"Warning: {PMC_FILE} not found — prev_tss will be 0 for all sleep entries.")
        return {}
    except Exception as exc:
        print(f"Warning: could not read {PMC_FILE} — {exc}")
        return {}


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Garmin Connect data to garmin_data.json"
    )
    parser.add_argument(
        "--days", type=int, default=DAYS_BACK,
        help=f"Days of history to fetch (default {DAYS_BACK})"
    )
    args = parser.parse_args()

    api = login()

    end   = date.today()
    start = end - timedelta(days=args.days - 1)
    dates = [start + timedelta(days=i) for i in range(args.days)]

    pmc_tss = load_pmc_tss()

    sleep_rows: list[dict] = []
    hrv_rows:   list[dict] = []
    rhr_rows:   list[dict] = []

    print(f"\nFetching {args.days} days ({start} → {end})...")
    for d in dates:
        print(f"  {d}", end=" ", flush=True)

        s = get_sleep(api, d)
        h = get_hrv(api, d)
        r = get_rhr(api, d)

        if s:
            prev_date     = (d - timedelta(days=1)).isoformat()
            s["prev_tss"] = pmc_tss.get(prev_date, 0)
            sleep_rows.append(s)

        if h:
            hrv_rows.append(h)
        if r:
            rhr_rows.append(r)

        parts = []
        if s: parts.append(f"sleep={s['score']}")
        if h: parts.append(f"hrv={h['hrv_ms']}ms")
        if r: parts.append(f"rhr={r['bpm']}bpm")
        print(", ".join(parts) if parts else "(no data)")

    out = {
        "generated":  end.isoformat(),
        "sleep":      sleep_rows,
        "hrv":        hrv_rows,
        "resting_hr": rhr_rows,
    }

    with open(OUTPUT, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\nWrote {OUTPUT}")
    print(f"  Sleep:      {len(sleep_rows)} days")
    print(f"  HRV:        {len(hrv_rows)} days")
    print(f"  Resting HR: {len(rhr_rows)} days")


if __name__ == "__main__":
    main()
