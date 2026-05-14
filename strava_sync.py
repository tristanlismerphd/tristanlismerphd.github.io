#!/usr/bin/env python3
"""
strava_sync.py
==============
Fetch Strava activities and write:
  strava_data.json        -- year stats, heatmap, recent activities (for personal.html)
  training_analytics.json -- PMC (ATL/CTL/TSB), weekly splits, long runs

Env vars
--------
  STRAVA_CLIENT_ID
  STRAVA_CLIENT_SECRET
  STRAVA_REFRESH_TOKEN
"""

import os
import math
import json
import requests
from datetime import datetime, timedelta, date
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LTHR      = 175                         # Lactate threshold HR (bpm)
K_ATL     = 1 - math.exp(-1 / 7)       # 7-day decay
K_CTL     = 1 - math.exp(-1 / 42)      # 42-day decay
RACE_DATE = date(2026, 10, 17)
RACE_NAME = "Bromont 80K Ultra"
YEAR      = date.today().year
LONG_RUN_MIN_KM = 20

TRAIL_TYPES     = {"TrailRun"}
ROAD_RUN_TYPES  = {"Run"}
RUN_TYPES       = TRAIL_TYPES | ROAD_RUN_TYPES
ROAD_RIDE_TYPES = {"Ride", "VirtualRide"}
GRAVEL_TYPES    = {"GravelRide"}
MTB_TYPES       = {"MountainBikeRide"}
RIDE_TYPES      = ROAD_RIDE_TYPES | GRAVEL_TYPES | MTB_TYPES


# ---------------------------------------------------------------------------
# Strava auth + fetch
# ---------------------------------------------------------------------------
def get_access_token() -> str:
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id":     os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
        "grant_type":    "refresh_token",
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_activities(token: str) -> list[dict]:
    all_acts, page = [], 1
    while True:
        resp = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers={"Authorization": f"Bearer {token}"},
            params={"per_page": 200, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_acts.extend(batch)
        if len(batch) < 200:
            break
        page += 1
    return all_acts


# ---------------------------------------------------------------------------
# TSS
# ---------------------------------------------------------------------------
def hr_tss(duration_s: float, avg_hr: Optional[float]) -> float:
    if avg_hr and avg_hr > 0:
        return (duration_s / 3600) * (avg_hr / LTHR) ** 2 * 100
    return (duration_s / 3600) * 60  # fallback: flat 60 TSS/hr


# ---------------------------------------------------------------------------
# PMC
# ---------------------------------------------------------------------------
def compute_pmc(activities: list[dict]) -> list[dict]:
    day_tss: dict[str, float] = {}
    for act in activities:
        dt  = datetime.fromisoformat(act["start_date_local"].replace("Z", ""))
        ds  = dt.strftime("%Y-%m-%d")
        tss = hr_tss(act.get("moving_time", 0), act.get("average_heartrate"))
        day_tss[ds] = day_tss.get(ds, 0) + tss

    if not day_tss:
        return []

    start = min(datetime.strptime(d, "%Y-%m-%d").date() for d in day_tss)
    end   = RACE_DATE
    pmc, atl, ctl = [], 0.0, 0.0
    cur = start
    while cur <= end:
        ds  = cur.strftime("%Y-%m-%d")
        tss = day_tss.get(ds, 0.0)
        atl = atl + (tss - atl) * K_ATL
        ctl = ctl + (tss - ctl) * K_CTL
        pmc.append({"date": ds, "tss": round(tss, 1),
                    "atl": round(atl, 1), "ctl": round(ctl, 1),
                    "tsb": round(ctl - atl, 1)})
        cur += timedelta(days=1)
    return pmc


# ---------------------------------------------------------------------------
# Weekly summary (split by sport type for the stacked bar charts)
# ---------------------------------------------------------------------------
def weekly_summary(activities: list[dict]) -> list[dict]:
    weeks: dict[str, dict] = {}
    for act in activities:
        dt  = datetime.fromisoformat(act["start_date_local"].replace("Z", ""))
        wk  = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
        t   = act.get("sport_type", act.get("type", ""))
        dk  = act["distance"] / 1000
        el  = act.get("total_elevation_gain", 0)
        dur = act.get("moving_time", 0)
        hr  = act.get("average_heartrate")
        tss = hr_tss(dur, hr)
        if wk not in weeks:
            weeks[wk] = {"week": wk, "trail_run_km": 0, "road_run_km": 0,
                         "road_ride_km": 0, "gravel_km": 0, "mtb_km": 0,
                         "vert_m": 0, "tss": 0}
        w = weeks[wk]
        w["tss"]    += tss
        w["vert_m"] += el
        if t in TRAIL_TYPES:       w["trail_run_km"] += dk
        elif t in ROAD_RUN_TYPES:  w["road_run_km"]  += dk
        elif t in ROAD_RIDE_TYPES: w["road_ride_km"] += dk
        elif t in GRAVEL_TYPES:    w["gravel_km"]    += dk
        elif t in MTB_TYPES:       w["mtb_km"]       += dk

    return [{
        "week":          v["week"],
        "trail_run_km":  round(v["trail_run_km"], 1),
        "road_run_km":   round(v["road_run_km"],  1),
        "road_ride_km":  round(v["road_ride_km"], 1),
        "gravel_km":     round(v["gravel_km"],    1),
        "mtb_km":        round(v["mtb_km"],       1),
        "vert_m":        round(v["vert_m"],        0),
        "tss":           round(v["tss"],           0),
    } for v in sorted(weeks.values(), key=lambda x: x["week"])]


# ---------------------------------------------------------------------------
# Long runs
# ---------------------------------------------------------------------------
def long_runs(activities: list[dict]) -> list[dict]:
    runs = []
    for act in activities:
        t  = act.get("sport_type", act.get("type", ""))
        dk = act["distance"] / 1000
        if t not in RUN_TYPES or dk < LONG_RUN_MIN_KM:
            continue
        dt   = datetime.fromisoformat(act["start_date_local"].replace("Z", ""))
        dur  = act.get("moving_time", 0)
        hr   = act.get("average_heartrate") or 0
        tss  = hr_tss(dur, hr)
        pace = (dur / 60) / dk if dk > 0 else 0
        runs.append({
            "date":        dt.strftime("%Y-%m-%d"),
            "name":        act.get("name", ""),
            "distance_km": round(dk, 1),
            "elevation_m": round(act.get("total_elevation_gain", 0), 0),
            "pace_min_km": round(pace, 2),
            "hr":          round(hr, 0),
            "tss":         round(tss, 0),
        })
    return sorted(runs, key=lambda r: r["date"])


# ---------------------------------------------------------------------------
# Year stats
# ---------------------------------------------------------------------------
def year_stats(activities: list[dict]) -> dict:
    s: dict = {"run_km": 0.0, "ride_km": 0.0, "run_count": 0, "ride_count": 0,
               "run_time_hrs": 0.0, "ride_time_hrs": 0.0,
               "run_elevation": 0.0, "ride_elevation": 0.0}
    for act in activities:
        dt = datetime.fromisoformat(act["start_date_local"].replace("Z", ""))
        if dt.year != YEAR:
            continue
        t        = act.get("sport_type", act.get("type", ""))
        dk       = act["distance"] / 1000
        el       = act.get("total_elevation_gain", 0)
        dur_hrs  = act.get("moving_time", 0) / 3600
        if t in RUN_TYPES:
            s["run_km"]        += dk
            s["run_count"]     += 1
            s["run_time_hrs"]  += dur_hrs
            s["run_elevation"] += el
        elif t in RIDE_TYPES:
            s["ride_km"]        += dk
            s["ride_count"]     += 1
            s["ride_time_hrs"]  += dur_hrs
            s["ride_elevation"] += el
    return {
        "run_km":         round(s["run_km"], 1),
        "ride_km":        round(s["ride_km"], 1),
        "run_count":      s["run_count"],
        "ride_count":     s["ride_count"],
        "run_time_hrs":   round(s["run_time_hrs"], 1),
        "ride_time_hrs":  round(s["ride_time_hrs"], 1),
        "run_elevation":  round(s["run_elevation"], 0),
        "ride_elevation": round(s["ride_elevation"], 0),
    }


# ---------------------------------------------------------------------------
# Heatmap (one entry per activity)
# ---------------------------------------------------------------------------
def heatmap(activities: list[dict]) -> list[dict]:
    return [
        {
            "date":        datetime.fromisoformat(
                               act["start_date_local"].replace("Z", "")
                           ).strftime("%Y-%m-%d"),
            "type":        act.get("sport_type", act.get("type", "")),
            "distance_km": round(act["distance"] / 1000, 1),
        }
        for act in activities
    ]


# ---------------------------------------------------------------------------
# Recent activities (with polyline for mini-maps)
# ---------------------------------------------------------------------------
def recent_activities(activities: list[dict], n: int = 10) -> list[dict]:
    sorted_acts = sorted(activities, key=lambda a: a["start_date_local"], reverse=True)
    result = []
    for act in sorted_acts[:n]:
        dt   = datetime.fromisoformat(act["start_date_local"].replace("Z", ""))
        poly = (act.get("map") or {}).get("summary_polyline") or ""
        result.append({
            "name":           act.get("name", ""),
            "date":           dt.isoformat(),
            "type":           act.get("sport_type", act.get("type", "")),
            "distance_km":    round(act["distance"] / 1000, 1),
            "moving_time":    act.get("moving_time", 0),
            "elevation_gain": round(act.get("total_elevation_gain", 0), 0),
            "polyline":       poly,
        })
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Fetching Strava activities...")
    token      = get_access_token()
    activities = fetch_activities(token)
    print(f"Fetched {len(activities)} activities")

    today     = date.today()
    today_str = today.strftime("%Y-%m-%d")

    pmc           = compute_pmc(activities)
    current_pmc   = next((p for p in reversed(pmc) if p["date"] <= today_str), None)
    weeks_to_race = max(0, (RACE_DATE - today).days // 7)

    training_data = {
        "lthr":          LTHR,
        "race_date":     str(RACE_DATE),
        "race_name":     RACE_NAME,
        "current":       {
            "ctl": current_pmc["ctl"],
            "atl": current_pmc["atl"],
            "tsb": current_pmc["tsb"],
        } if current_pmc else {"ctl": 0, "atl": 0, "tsb": 0},
        "weeks_to_race": weeks_to_race,
        "pmc":           pmc,
        "weekly":        weekly_summary(activities),
        "long_runs":     long_runs(activities),
    }
    with open("training_analytics.json", "w") as f:
        json.dump(training_data, f, indent=2)
    print("Saved training_analytics.json")

    strava_data = {
        "generated":         today_str,
        "year":              YEAR,
        "year_stats":        year_stats(activities),
        "heatmap":           heatmap(activities),
        "recent_activities": recent_activities(activities),
    }
    with open("strava_data.json", "w") as f:
        json.dump(strava_data, f, indent=2)
    print("Saved strava_data.json")

    today_pmc = current_pmc or {"ctl": 0, "atl": 0, "tsb": 0}
    print(f"\nCurrent PMC (LTHR = {LTHR} bpm):")
    print(f"  CTL (Fitness): {today_pmc['ctl']:.1f}")
    print(f"  ATL (Fatigue): {today_pmc['atl']:.1f}")
    print(f"  TSB (Form):    {today_pmc['tsb']:+.1f}")
    print(f"  Weeks to race: {weeks_to_race}")
