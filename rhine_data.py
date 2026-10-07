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


DESC_COLS = ["Parameter_Wat_Omschrijving", "WaardeBewerkingsMethode.Code", "Typering.Code",
             "Parameter.Code", "Groepering.Code", "Hoedanigheid.Code", "Eenheid.Code"]
MAX_CANDIDATES = 12


def candidates(locs: pd.DataFrame, key: str, qty: str, proces: str) -> pd.DataFrame:
    """All catalogue rows for a station/quantity/process (there can be several series)."""
    grootheid, hoed = QTY[qty]
    m = locs["_code"].str.contains(key) | locs["_name"].str.contains(key)
    m &= locs["Grootheid.Code"].eq(grootheid) & locs["ProcesType"].eq(proces)
    if "Groepering.Code" in locs:
        m &= locs["Groepering.Code"].fillna("").eq("")
    if "Compartiment.Code" in locs:
        m &= locs["Compartiment.Code"].eq("OW")
    if hoed:
        m &= locs["Hoedanigheid.Code"].eq(hoed)
    sel = locs[m].copy()
    if sel.empty:
        return sel
    cols = [c for c in DESC_COLS if c in sel]
    sel["_key"] = sel.index.astype(str) + "|" + sel[cols].astype(str).agg("|".join, axis=1)
    return sel[~sel["_key"].duplicated()].head(MAX_CANDIDATES)


def describe(row) -> str:
    """Short human-readable label of what distinguishes a catalogue row."""
    parts = [str(row.name)]
    for c in ("WaardeBewerkingsMethode.Code", "Typering.Code", "Parameter.Code"):
        v = row.get(c)
        if isinstance(v, str) and v and v != "NVT":
            parts.append(f"{c.split('.')[0]}={v}")
    return " ".join(parts)


def _fetch(row, qty, proces, start, end, now):
    df = ddlpy.measurements(row, start, end, freq=None)
    if df is None or df.empty:
        return None
    s = pd.to_numeric(df["Meetwaarde.Waarde_Numeriek"], errors="coerce")
    s = s[s.abs() < 1e5]  # drop fill values like 999999999
    s.index = pd.to_datetime(s.index).tz_convert(TZ)
    s = s[~s.index.duplicated(keep="last")].sort_index()  # several forecast runs -> latest
    if proces == "meting":
        s = s[s.index <= now]
    if qty == "H":
        s = s / 100.0  # cm -> m +NAP
    return s if not s.empty else None


def rws_series(locs, key, qty, proces, start, end, now=None):
    """Try every matching catalogue series and keep the best one.

    Best = most recent data for measurements, furthest horizon for forecasts.
    Returns (series|None, label|None, error|None, tried) where tried is a list of
    (label, series|None, error|None) for every candidate.
    """
    now = now or pd.Timestamp.now(tz=TZ)
    cands = candidates(locs, key, qty, proces)
    if cands.empty:
        return None, None, None, []
    tried = []
    for _, row in cands.iterrows():
        label = describe(row)
        try:
            tried.append((label, _fetch(row, qty, proces, start, end, now), None))
        except Exception as e:  # network / service errors, "no data" responses
            tried.append((label, None, str(e)))
    ok = [t for t in tried if t[1] is not None]
    if not ok:
        return None, tried[0][0], tried[0][2], tried
    best = max(ok, key=lambda t: (t[1].index[-1], len(t[1])))
    return best[1], best[0], None, tried


def catalogue_matches(locs) -> pd.DataFrame:
    """Every catalogue row for the configured stations, for inspection (data/catalogue_matches.csv)."""
    keys = list(RWS_STATIONS.values()) + [RWS_KOELN_KEY]
    m = pd.Series(False, index=locs.index)
    for k in keys:
        m |= locs["_code"].str.contains(k) | locs["_name"].str.contains(k)
    cols = [c for c in ["Naam", "ProcesType", "Grootheid.Code", "Compartiment.Code"] + DESC_COLS
            if c in locs]
    out = locs.loc[m, cols].copy()
    out.insert(0, "Code", out.index)
    out = out.reset_index(drop=True)
    return out.drop_duplicates().sort_values(["Code", "ProcesType", "Grootheid.Code"])


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
            return None, None, f"catalogue unavailable: {cat_err}", []
        try:
            win = obs_window if proces == "meting" else fc_window
            return rws_series(locs, key, qty, proces, *win, now=now)
        except Exception as e:
            return None, None, str(e), []

    def record(station, qty, label, d, tag, result):
        s, code, err, tried = result
        d[(qty, tag)] = s
        log.append((station, qty, label, code, s, err))
        for lab, alt, aerr in tried:
            if lab == code:
                continue
            log.append((station, qty, f"{label} (alternative)", lab, alt, aerr))
            if tag == "fc" and alt is not None:
                d[(qty, "alt:" + lab)] = alt   # shown as extra forecast lines

    k = {}
    for ts, qty in (("W", "H"), ("Q", "Q")):
        s, err = pegel_series(ts, days_back)
        k[(qty, "obs")] = s
        log.append(("Köln", qty, "measured", "PEGELONLINE", s, err))
    k[("H", "fc")] = None  # an RWS Keulen level forecast uses a different datum than the gauge
    record("Köln", "Q", "forecast", k, "fc", rws(RWS_KOELN_KEY, "Q", "verwachting"))
    data["Köln"] = k

    for st_name, key in RWS_STATIONS.items():
        d = {}
        for qty in ("H", "Q"):
            for proces, tag, label in (("meting", "obs", "measured"),
                                       ("verwachting", "fc", "forecast")):
                record(st_name, qty, label, d, tag, rws(key, qty, proces))
        data[st_name] = d
    return data, log


def to_long(data) -> pd.DataFrame:
    """Flatten load_all() output to rows: station, qty, kind, time (local), value, unit."""
    frames = []
    for st_name, d in data.items():
        for (qty, tag), s in d.items():
            if s is None or s.empty:
                continue
            kind = {"obs": "measured", "fc": "forecast"}.get(tag, tag)  # "alt:<label>"
            frames.append(pd.DataFrame({
                "station": st_name, "qty": qty, "kind": kind,
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
