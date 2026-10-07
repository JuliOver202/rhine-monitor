"""Shared data access for the Rhine monitor (app, archiver, verification page).

Sources
- Rijkswaterstaat DDL (via rws-ddlpy): Lobith, Millingen, Dodewaard (+ Keulen discharge forecast if published)
- PEGELONLINE (WSV): Köln measured water level and discharge
"""
from __future__ import annotations

import logging
import warnings
from pathlib import Path

import pandas as pd
import requests

import ddlpy

warnings.filterwarnings("ignore")
logging.getLogger("ddlpy").setLevel(logging.WARNING)

TZ = "Europe/Amsterdam"
PEGEL_BASE = "https://www.pegelonline.wsv.de/webservices/rest-api/v2/stations"
PEGEL_KOELN = "a6ee8177-107b-47dd-bcfd-30960ccc6e9c"  # PEGELONLINE uuid for KÖLN

# RWS stations, matched case-insensitively against catalogue code/name
RWS_STATIONS = {"Lobith": "lobith", "Millingen": "millingen", "Dodewaard": "dodewaard"}
RWS_KOELN_KEY = "keulen"  # only used for an RWS discharge forecast, if published
STATION_ORDER = ["Köln", "Lobith", "Millingen", "Dodewaard"]
QTY = {"H": ("WATHTE", "NAP"), "Q": ("Q", None)}

DATA_DIR = Path(__file__).parent / "data"


def unit_of(station: str, qty: str) -> str:
    if qty == "Q":
        return "m³/s"
    return "cm (gauge)" if station == "Köln" else "m +NAP"


# ---------------------------------------------------------------- live fetch
def catalogue() -> pd.DataFrame:
    locs = ddlpy.locations().copy()
    locs["_code"] = locs.index.str.lower()
    locs["_name"] = locs["Naam"].astype(str).str.lower()
    return locs


def pick(locs: pd.DataFrame, key: str, qty: str, proces: str):
    grootheid, hoed = QTY[qty]
    m = locs["_code"].str.contains(key) | locs["_name"].str.contains(key)
    m &= locs["Grootheid.Code"].eq(grootheid) & locs["ProcesType"].eq(proces)
    if "Groepering.Code" in locs:
        m &= locs["Groepering.Code"].fillna("").eq("")
    if "Compartiment.Code" in locs:
        m &= locs["Compartiment.Code"].eq("OW")
    if hoed:
        m &= locs["Hoedanigheid.Code"].eq(hoed)
    sel = locs[m]
    return None if sel.empty else sel.iloc[0]


def rws_series(locs, key, qty, proces, start, end, now=None):
    """Returns (series or None, catalogue code or None, error or None). H in m +NAP."""
    now = now or pd.Timestamp.now(tz=TZ)
    row = pick(locs, key, qty, proces)
    if row is None:
        return None, None, None
    try:
        df = ddlpy.measurements(row, start, end, freq=None)
    except Exception as e:  # network / service errors
        return None, row.name, str(e)
    if df is None or df.empty:
        return None, row.name, None
    s = pd.to_numeric(df["Meetwaarde.Waarde_Numeriek"], errors="coerce")
    s = s[s.abs() < 1e5]  # drop fill values like 999999999
    s.index = pd.to_datetime(s.index).tz_convert(TZ)
    s = s[~s.index.duplicated(keep="last")].sort_index()  # several forecast runs -> latest
    if proces == "meting":
        s = s[s.index <= now]
    if qty == "H":
        s = s / 100.0  # cm -> m +NAP
    return (s if not s.empty else None), row.name, None


def pegel_series(ts: str, days_back: int):
    """Köln measured series from PEGELONLINE ('W' cm at gauge, 'Q' m³/s). Returns (series, error)."""
    try:
        r = requests.get(f"{PEGEL_BASE}/{PEGEL_KOELN}/{ts}/measurements.json",
                         params={"start": f"P{days_back}D"}, timeout=30)
        r.raise_for_status()
        df = pd.DataFrame(r.json())
    except Exception as e:
        return None, str(e)
    if df.empty:
        return None, None
    idx = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(TZ)
    return pd.Series(df["value"].values, index=idx).sort_index(), None


def pegel_state() -> dict:
    try:
        meta = requests.get(f"{PEGEL_BASE}/{PEGEL_KOELN}.json",
                            params={"includeTimeseries": "true",
                                    "includeCurrentMeasurement": "true"},
                            timeout=30).json()
        return {t["shortname"]: t.get("currentMeasurement", {}).get("stateMnwMhw")
                for t in meta.get("timeseries", [])}
    except Exception:
        return {}


def load_all(days_back: int, days_ahead: int, locs=None, now=None):
    """All stations. Returns (data[station][(qty,'obs'|'fc')] -> Series|None, log rows)."""
    now = now or pd.Timestamp.now(tz=TZ)
    data, log = {}, []
    if locs is None:
        try:
            locs = catalogue()
        except Exception as e:
            locs, cat_err = None, str(e)
    obs_window = (now - pd.Timedelta(days=days_back), now + pd.Timedelta(hours=1))
    fc_window = (now - pd.Timedelta(days=2), now + pd.Timedelta(days=days_ahead))

    def rws(key, qty, proces):
        if locs is None:
            return None, None, f"catalogue unavailable: {cat_err}"
        try:
            win = obs_window if proces == "meting" else fc_window
            return rws_series(locs, key, qty, proces, *win, now=now)
        except Exception as e:
            return None, None, str(e)

    k = {}
    for ts, qty in (("W", "H"), ("Q", "Q")):
        s, err = pegel_series(ts, days_back)
        k[(qty, "obs")] = s
        log.append(("Köln", qty, "measured", "PEGELONLINE", s, err))
    k[("H", "fc")] = None  # an RWS Keulen level forecast uses a different datum than the gauge
    s, code, err = rws(RWS_KOELN_KEY, "Q", "verwachting")
    k[("Q", "fc")] = s
    log.append(("Köln", "Q", "forecast", code, s, err))
    data["Köln"] = k

    for st_name, key in RWS_STATIONS.items():
        d = {}
        for qty in ("H", "Q"):
            for proces, tag, label in (("meting", "obs", "measured"),
                                       ("verwachting", "fc", "forecast")):
                s, code, err = rws(key, qty, proces)
                d[(qty, tag)] = s
                log.append((st_name, qty, label, code, s, err))
        data[st_name] = d
    return data, log


def to_long(data) -> pd.DataFrame:
    """Flatten load_all() output to rows: station, qty, kind, time (local), value, unit."""
    frames = []
    for st_name, d in data.items():
        for (qty, tag), s in d.items():
            if s is None or s.empty:
                continue
            frames.append(pd.DataFrame({
                "station": st_name, "qty": qty,
                "kind": "measured" if tag == "obs" else "forecast",
                "time": s.index, "value": s.values, "unit": unit_of(st_name, qty)}))
    if not frames:
        return pd.DataFrame(columns=["station", "qty", "kind", "time", "value", "unit"])
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------- archive (read side)
def _read_dir(sub: str) -> pd.DataFrame:
    files = sorted((DATA_DIR / sub).glob("*.csv"))
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    for c in ("time", "fetched_at"):
        if c in df:
            df[c] = pd.to_datetime(df[c], utc=True)
    return df


def read_observations() -> pd.DataFrame:
    """Archived hourly observations: station, qty, time (UTC), value."""
    return _read_dir("observations")


def read_forecasts() -> pd.DataFrame:
    """Archived forecast runs: fetched_at (UTC), station, qty, time (UTC), lead_h, value."""
    return _read_dir("forecasts")
