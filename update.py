"""Fetch the latest Rhine data, append it to the archive, and write a snapshot for the page.

Archive (committed to the repo, plain CSV so git stores only the appended lines):
  data/observations/YYYY-MM.csv  station, qty, time (UTC), value          — hourly values
  data/forecasts/YYYY-MM.csv     fetched_at, station, qty, time, lead_h, value
                                 hourly up to 96 h lead, 6-hourly beyond; a run is
                                 skipped when it is identical to the last stored run
Snapshot (not committed, used by build_site.py):
  build/latest.csv, build/meta.json

Run:  python update.py        (RHINE_NOW=2026-10-07T10:00+02:00 overrides the clock, for testing)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

import rhine_data as rd

ROOT = Path(__file__).parent
BUILD = ROOT / "build"
DAYS_BACK, DAYS_AHEAD = 10, 15
OBS_COLS = ["station", "qty", "time", "value"]
FC_COLS = ["fetched_at", "station", "qty", "time", "lead_h", "value"]


def _now() -> pd.Timestamp:
    env = os.environ.get("RHINE_NOW")
    return pd.Timestamp(env).tz_convert(rd.TZ) if env else pd.Timestamp.now(tz=rd.TZ)


def _iso(ts) -> pd.Series:
    return pd.to_datetime(ts, utc=True).dt.strftime("%Y-%m-%dT%H:%MZ")


def _append(sub: str, df: pd.DataFrame, cols: list[str], month_col: str):
    """Append rows to monthly CSV files (header written only for new files)."""
    if df.empty:
        return 0
    folder = rd.DATA_DIR / sub
    folder.mkdir(parents=True, exist_ok=True)
    months = pd.to_datetime(df[month_col], utc=True).dt.strftime("%Y-%m")
    for month, part in df.groupby(months):
        f = folder / f"{month}.csv"
        part[cols].to_csv(f, mode="a", header=not f.exists(), index=False)
    return len(df)


def archive_observations(data, now) -> int:
    """Append hourly values newer than what is already stored, per series."""
    stored = rd.read_observations()
    last = ({} if stored.empty else
            stored.groupby(["station", "qty"])["time"].max().to_dict())
    rows = []
    for station, d in data.items():
        for qty in ("H", "Q"):
            s = d.get((qty, "obs"))
            if s is None or s.empty:
                continue
            s = s.copy()
            s.index = s.index.tz_convert("UTC")
            hourly = s.resample("1h").first().dropna()        # value at (or just after) each full hour
            hourly = hourly[hourly.index <= now.tz_convert("UTC") - pd.Timedelta("1h")]
            cut = last.get((station, qty))
            if cut is not None:
                hourly = hourly[hourly.index > cut]
            if hourly.empty:
                continue
            rows.append(pd.DataFrame({"station": station, "qty": qty,
                                      "time": hourly.index, "value": hourly.values.round(3)}))
    if not rows:
        return 0
    df = pd.concat(rows).sort_values(["time", "station", "qty"])
    df["time"] = _iso(df["time"])
    return _append("observations", df, OBS_COLS, "time")


def _thin(fc: pd.Series, issued: pd.Timestamp) -> pd.DataFrame:
    s = fc.copy()
    s.index = s.index.tz_convert("UTC")
    s = s.resample("1h").first().dropna()
    s = s[s.index >= issued.floor("h")]
    lead = ((s.index - issued).total_seconds() / 3600).round().astype(int)
    keep = (lead <= 96) | (s.index.hour % 6 == 0)
    return pd.DataFrame({"time": s.index[keep], "lead_h": lead[keep], "value": s.values[keep].round(3)})


def archive_forecasts(data, now) -> int:
    stored = rd.read_forecasts()
    issued = now.tz_convert("UTC").floor("min")
    rows = []
    for station, d in data.items():
        for qty in ("H", "Q"):
            fc = d.get((qty, "fc"))
            if fc is None or fc.empty:
                continue
            new = _thin(fc, issued)
            if new.empty:
                continue
            # skip if identical to the previous stored run on overlapping times
            if not stored.empty:
                prev = stored[(stored.station == station) & (stored.qty == qty)]
                if not prev.empty:
                    prev = prev[prev.fetched_at == prev.fetched_at.max()]
                    m = new.merge(prev[["time", "value"]], on="time", suffixes=("", "_p"))
                    if len(m) >= 0.8 * len(new) and (m.value - m.value_p).abs().max() < 1e-6:
                        continue
            new.insert(0, "fetched_at", issued)
            new.insert(1, "station", station)
            new.insert(2, "qty", qty)
            rows.append(new)
    if not rows:
        return 0
    df = pd.concat(rows)
    df["fetched_at"] = _iso(df["fetched_at"])
    df["time"] = _iso(df["time"])
    return _append("forecasts", df, FC_COLS, "fetched_at")


def write_snapshot(data, log, now):
    BUILD.mkdir(exist_ok=True)
    cur = rd.to_long(data)
    cur["time"] = _iso(cur["time"])
    cur.to_csv(BUILD / "latest.csv", index=False)
    meta = {
        "generated": now.isoformat(),
        "koeln_state": rd.pegel_state(),
        "log": [{"station": a, "qty": b, "series": c, "source": d or "not in catalogue",
                 "values": 0 if s is None else int(len(s)), "error": e or ""}
                for a, b, c, d, s, e in log],
    }
    (BUILD / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))


def main():
    now = _now()
    try:
        locs = rd.catalogue()
        rd.DATA_DIR.mkdir(exist_ok=True)
        rd.catalogue_matches(locs).to_csv(rd.DATA_DIR / "catalogue_matches.csv", index=False)
    except Exception as e:
        print("  ! catalogue:", e)
        locs = None
    data, log = rd.load_all(DAYS_BACK, DAYS_AHEAD, locs=locs, now=now)
    n_obs = archive_observations(data, now)
    n_fc = archive_forecasts(data, now)
    write_snapshot(data, log, now)
    errors = [r for r in log if r[5]]
    print(f"{now:%Y-%m-%d %H:%M} — archived {n_obs} observation rows, {n_fc} forecast rows; "
          f"{len(errors)} fetch errors")
    for a, b, c, d, s, e in errors:
        print(f"  ! {a} {b} {c}: {e}")


if __name__ == "__main__":
    main()
