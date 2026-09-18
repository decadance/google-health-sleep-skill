"""Fetch sleep sessions + nightly recovery metrics from the Google Health API and print a compact JSON summary.

Usage: python fetch_sleep.py [--days 30] [--raw-out PATH]

stdout: JSON {period, nights[], daily_metrics{date:{...}}, exercise[], errors{}, raw_dump}
The full raw API responses are written to --raw-out (default: temp dir) for debugging schema surprises.
"""
import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from gh_common import get_access_token, list_all

DAILY_TYPES = [
    "daily-resting-heart-rate",
    "daily-heart-rate-variability",
    "daily-oxygen-saturation",
    "daily-respiratory-rate",
    "daily-sleep-temperature-derivations",
]


def snake(kebab):
    return kebab.replace("-", "_")


def source_label(dp):
    ds = dp.get("dataSource") or {}
    dev = (ds.get("device") or {}).get("displayName")
    app = (ds.get("application") or {}).get("packageName")
    return f"{ds.get('platform', '?')}:{dev or app or '?'}"


def source_rank(label):
    """Lower = preferred when two sources report the same thing."""
    return 0 if label.startswith("FITBIT") else 1 if label.startswith("HEALTH_CONNECT") else 2


def parse_ts(s):
    if not s:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # trim nanoseconds
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_offset(v):
    """'10800s' / '+03:00' / 10800 -> timezone, else None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return timezone(timedelta(seconds=v))
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)s", str(v))
    if m:
        return timezone(timedelta(seconds=float(m.group(1))))
    m = re.fullmatch(r"([+-])(\d{2}):(\d{2})", str(v))
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
    return None


def first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def numeric_leaves(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("name", "dataSource", "metadata", "date", "year", "month", "day"):
                continue
            out.update(numeric_leaves(v, f"{prefix}{k}."))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out[prefix.rstrip(".")] = obj
    elif isinstance(obj, str) and re.fullmatch(r"-?\d+(\.\d+)?", obj):
        out[prefix.rstrip(".")] = float(obj)
    return out


def find_date(obj):
    if isinstance(obj, dict):
        d = obj.get("date")
        if isinstance(d, str):
            return d[:10]
        if isinstance(d, dict) and {"year", "month", "day"} <= d.keys():
            return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"
        for v in obj.values():
            r = find_date(v)
            if r:
                return r
    return None


def summarize_sleep(dp, local_tz):
    s = dp.get("sleep", dp)
    interval = s.get("interval", s)
    start = parse_ts(first(interval, "startTime", "start_time"))
    end = parse_ts(first(interval, "endTime", "end_time"))
    if not start or not end:
        return None
    tz = parse_offset(first(interval, "endUtcOffset", "startUtcOffset", "utcOffset")) or local_tz
    start_l, end_l = start.astimezone(tz), end.astimezone(tz)

    stage_min = defaultdict(float)
    stages = first(s, "stages", "sleepStages", "levels") or []
    for st in stages:
        a, b = parse_ts(first(st, "startTime", "start_time")), parse_ts(first(st, "endTime", "end_time"))
        kind = str(first(st, "type", "stage", "level") or "UNKNOWN").upper().replace("SLEEP_STAGE_", "")
        if a and b:
            stage_min[kind] += (b - a).total_seconds() / 60
    wake_bouts = [st for st in stages if "AWAKE" in str(first(st, "type", "stage", "level") or "").upper()
                  or "WAKE" == str(first(st, "type", "stage", "level") or "").upper()]
    short_awakenings = first(s, "shortAwakenings") or []

    in_bed = (end - start).total_seconds() / 60
    awake = stage_min.get("AWAKE", 0) + stage_min.get("WAKE", 0) + stage_min.get("RESTLESS", 0)
    asleep = sum(v for k, v in stage_min.items() if k not in ("AWAKE", "WAKE", "RESTLESS", "UNKNOWN")) or None
    if asleep is None and stage_min:
        asleep = in_bed - awake

    meta = s.get("metadata") or {}
    night = {
        "source": source_label(dp),
        "api_main_sleep": meta.get("mainSleep"),
        "wake_date": end_l.date().isoformat(),
        "bedtime": start_l.strftime("%Y-%m-%d %H:%M"),
        "waketime": end_l.strftime("%Y-%m-%d %H:%M"),
        "weekday_of_bedtime": start_l.strftime("%a"),
        "utc_offset": start_l.strftime("%z"),
        "sleep_type": first(s, "type", "sleepType"),
        "in_bed_min": round(in_bed),
        "asleep_min": round(asleep) if asleep is not None else None,
        "efficiency_pct": round(asleep / in_bed * 100) if asleep and in_bed else None,
        "stages_min": {k: round(v) for k, v in sorted(stage_min.items())},
        "stages_pct_of_asleep": {k: round(v / asleep * 100, 1) for k, v in stage_min.items()
                                 if asleep and k not in ("AWAKE", "WAKE", "RESTLESS", "UNKNOWN")},
        "wake_bouts": len(wake_bouts),
        "short_awakenings": len(short_awakenings),
    }
    for k in ("minutesToFallAsleep", "minutesAfterWakeup"):
        if s.get(k) is not None:
            night[k] = s[k]
    summary = first(s, "summary", "sleepSummary")
    if summary:
        night["api_summary"] = numeric_leaves(summary)
    return night


def summarize_exercise(dp, local_tz):
    e = dp.get("exercise", dp)
    interval = e.get("interval", e)
    start, end = parse_ts(first(interval, "startTime")), parse_ts(first(interval, "endTime"))
    if not start or not end:
        return None
    tz = parse_offset(first(interval, "startUtcOffset")) or local_tz
    return {
        "source": source_label(dp),
        "type": first(e, "exerciseType", "activityType", "type", "displayName"),
        "start": start.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "end": end.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "duration_min": round((end - start).total_seconds() / 60),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--raw-out", default=os.path.join(tempfile.gettempdir(), "google_health_sleep_raw.json"))
    args = ap.parse_args()

    local_tz = datetime.now().astimezone().tzinfo
    token = get_access_token()
    since_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    since_date = (date.today() - timedelta(days=args.days)).isoformat()

    raw, errors = {}, {}

    sleep_pts, err = list_all("sleep", f'sleep.interval.end_time >= "{since_iso}"', token, page_size=25)
    raw["sleep"] = sleep_pts
    if err:
        errors["sleep"] = err

    ex_pts, err = list_all("exercise", f'exercise.interval.civil_start_time >= "{since_date}"', token, page_size=25)
    raw["exercise"] = ex_pts
    if err:
        errors["exercise"] = err

    daily = defaultdict(dict)
    for dt in DAILY_TYPES:
        pts, err = list_all(dt, f'{snake(dt)}.date >= "{since_date}"', token, page_size=1000)
        raw[dt] = pts
        if err:
            errors[dt] = err
        for p in pts:
            d = find_date(p)
            vals = numeric_leaves(p)
            if not d or not vals:
                continue
            src = source_label(p)
            prev = daily[d].get(dt)
            # Keep one value per day per metric, preferring the Fitbit reading
            if prev is None or source_rank(src) < source_rank(prev["source"]):
                daily[d][dt] = {"source": src, **vals}

    nights = [n for n in (summarize_sleep(p, local_tz) for p in sleep_pts) if n]
    # Drop sessions duplicated by another source (overlap > 50% of the shorter one), preferring Fitbit
    nights.sort(key=lambda n: (source_rank(n["source"]), -n["in_bed_min"]))
    kept = []
    for n in nights:
        a0, a1 = n["bedtime"], n["waketime"]
        dup = False
        for k in kept:
            b0, b1 = k["bedtime"], k["waketime"]
            lo, hi = max(a0, b0), min(a1, b1)
            if lo < hi:
                ov = (datetime.fromisoformat(hi) - datetime.fromisoformat(lo)).total_seconds() / 60
                if ov > 0.5 * min(n["in_bed_min"], k["in_bed_min"]):
                    dup = True
                    break
        if not dup:
            kept.append(n)
    duplicates_dropped = len(nights) - len(kept)
    nights = sorted(kept, key=lambda n: n["bedtime"])
    # Mark the longest session per wake date as main sleep, the rest as naps
    by_day = defaultdict(list)
    for n in nights:
        by_day[n["wake_date"]].append(n)
    for group in by_day.values():
        flagged = [n for n in group if n["api_main_sleep"]]
        main_n = flagged[0] if flagged else max(group, key=lambda n: n["in_bed_min"])
        for n in group:
            n["is_main"] = n is main_n

    with open(args.raw_out, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)

    out = {
        "period": {"days": args.days, "since": since_date, "until": date.today().isoformat(), "local_tz": str(local_tz)},
        "nights": nights,
        "daily_metrics": dict(sorted(daily.items())),
        "exercise": [e for e in (summarize_exercise(p, local_tz) for p in ex_pts) if e],
        "counts": {k: len(v) for k, v in raw.items()},
        "duplicate_sleep_sessions_dropped": duplicates_dropped,
        "nights_by_source": dict(Counter(n["source"] for n in nights)),
        "errors": errors,
        "raw_dump": args.raw_out,
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
