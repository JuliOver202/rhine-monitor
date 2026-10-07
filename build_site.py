"""Render the static dashboard into site/ from build/ (snapshot) and data/ (archive).

Run after update.py:  python build_site.py
"""
from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.offline
from plotly.subplots import make_subplots

import rhine_data as rd
from rhine_data import STATION_ORDER, TZ, unit_of

ROOT = Path(__file__).parent
BUILD, SITE = ROOT / "build", ROOT / "site"
C_OBS, C_FC = "#1f4e79", "#c0392b"
C_STATION = {"Köln": "#6b7280", "Lobith": "#1f4e79", "Millingen": "#2e86c1", "Dodewaard": "#d68910"}
LEAD_BINS = [0, 24, 48, 72, 96, 168, 400]
LEAD_LABELS = ["0–24 h", "24–48 h", "48–72 h", "72–96 h", "4–7 d", "7–15 d"]
PLOT_CFG = {"displaylogo": False, "responsive": True,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"]}


# ---------------------------------------------------------------- helpers
def esc(x) -> str:
    return html.escape(str(x))


def fmt(v, unit, signed=False):
    if v is None or pd.isna(v):
        return "n/a"
    dec = 2 if unit == "m +NAP" else 0
    return f"{v:{'+' if signed else ''},.{dec}f}"


def value_at(s, t, tol="3h"):
    if s is None or s.empty:
        return None
    i = s.index.get_indexer([t], method="nearest")[0]
    return s.iloc[i] if abs(s.index[i] - t) <= pd.Timedelta(tol) else None


def trend_word(d):
    if d is None or pd.isna(d):
        return "trend n/a"
    return "rising" if d > 0 else "falling" if d < 0 else "steady"


def fig_html(fig, div_id):
    return fig.to_html(full_html=False, include_plotlyjs=False, config=PLOT_CFG, div_id=div_id)


def base_layout(fig, height):
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=36, b=8), template="plotly_white",
        legend=dict(orientation="h", y=1.1, x=1, xanchor="right"), hovermode="x unified",
        font=dict(family="Inter, system-ui, sans-serif", size=12),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    return fig


# ---------------------------------------------------------------- load
def load_snapshot():
    latest = pd.read_csv(BUILD / "latest.csv")
    latest["time"] = pd.to_datetime(latest["time"], utc=True).dt.tz_convert(TZ)
    meta = json.loads((BUILD / "meta.json").read_text())
    data = {s: {} for s in STATION_ORDER}
    for (st_, qty, kind), g in latest.groupby(["station", "qty", "kind"]):
        tag = "obs" if kind == "measured" else "fc"
        data.setdefault(st_, {})[(qty, tag)] = g.set_index("time")["value"].sort_index()
    return data, meta, latest


def series(data, station, qty, tag):
    return data.get(station, {}).get((qty, tag))


# ---------------------------------------------------------------- "Now" section
def build_state(data, now):
    rows = []
    for station in STATION_ORDER:
        for qty in ("H", "Q"):
            obs, fc = series(data, station, qty, "obs"), series(data, station, qty, "fc")
            last_t = obs.index[-1] if obs is not None and not obs.empty else None
            last = obs.iloc[-1] if last_t is not None else None
            prev = value_at(obs, last_t - pd.Timedelta("24h")) if last_t is not None else None
            fut = fc[fc.index > now] if fc is not None else None
            rows.append(dict(
                station=station, qty=qty, unit=unit_of(station, qty), latest=last,
                at=last_t.strftime("%d-%m %H:%M") if last_t is not None else "n/a",
                d24=None if last is None or prev is None else last - prev,
                f24=value_at(fc, now + pd.Timedelta("24h")),
                f48=value_at(fc, now + pd.Timedelta("48h")),
                f96=value_at(fc, now + pd.Timedelta("96h")),
                fmin=None if fut is None or fut.empty else fut.min()))
    return pd.DataFrame(rows)


def cards_html(state, kstate):
    out = []
    for station in STATION_ORDER:
        q = state[(state.station == station) & (state.qty == "Q")].iloc[0]
        h = state[(state.station == station) & (state.qty == "H")].iloc[0]
        d = q["d24"]
        cls = "" if d is None or pd.isna(d) else ("up" if d > 0 else "down" if d < 0 else "")
        arrow = {"up": "▲", "down": "▼"}.get(cls, "")
        delta = "" if not cls and (d is None or pd.isna(d)) else \
            f'<span class="delta {cls}">{arrow} {fmt(d, "m³/s", True)} / 24h</span>'
        h_unit = "cm" if station == "Köln" else "m +NAP"
        flag = (f' · <span class="flag">{esc(kstate["W"])}</span>'
                if station == "Köln" and kstate.get("W") else "")
        out.append(f"""
        <div class="card">
          <div class="card-label">{esc(station)}</div>
          <div class="card-value">{fmt(q['latest'], 'm³/s')}<span class="unit"> m³/s</span></div>
          {delta}
          <div class="card-sub">H {fmt(h['latest'], h['unit'])} {h_unit if not pd.isna(h['latest']) else ''}{flag}</div>
        </div>""")
    return "\n".join(out)


def summary_html(state):
    items = []
    for station in STATION_ORDER:
        parts = []
        for qty in ("H", "Q"):
            r = state[(state.station == station) & (state.qty == qty)].iloc[0]
            if pd.isna(r["latest"]):
                continue
            u = r["unit"]
            lab = "Q" if qty == "Q" else ("W" if station == "Köln" else "H")
            t = f"{lab} {fmt(r['latest'], u)} {u.replace(' (gauge)', '')} ({trend_word(r['d24'])}"
            t += f", {fmt(r['d24'], u, True)}/24h)" if not pd.isna(r["d24"]) else ")"
            if not pd.isna(r["f96"]):
                t += f" → {fmt(r['f96'], u)} in 4 days"
            parts.append(esc(t))
        items.append(f"<li><b>{esc(station)}</b>: {'; '.join(parts) or 'no data'}</li>")
    return "<ul class='summary'>" + "".join(items) + "</ul>"


def state_table_html(state):
    head = ("<tr><th>Station</th><th></th><th>Unit</th><th>Latest</th><th>At</th>"
            "<th>Δ 24h</th><th>+24h</th><th>+48h</th><th>+96h</th><th>Fc. min</th></tr>")
    body = []
    for _, r in state.iterrows():
        u = r["unit"]
        body.append(
            f"<tr><td>{esc(r['station'])}</td><td>{'Water level' if r['qty'] == 'H' else 'Discharge'}</td>"
            f"<td>{esc(u)}</td><td class='num'>{fmt(r['latest'], u)}</td><td>{esc(r['at'])}</td>"
            f"<td class='num'>{fmt(r['d24'], u, True)}</td><td class='num'>{fmt(r['f24'], u)}</td>"
            f"<td class='num'>{fmt(r['f48'], u)}</td><td class='num'>{fmt(r['f96'], u)}</td>"
            f"<td class='num'>{fmt(r['fmin'], u)}</td></tr>")
    return f"<div class='table-wrap'><table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table></div>"


def hydrograph(data, station, now):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.1,
                        subplot_titles=(f"Water level ({unit_of(station, 'H')})", "Discharge (m³/s)"))
    shown, any_data = set(), False
    for i, qty in enumerate(("H", "Q"), start=1):
        for tag, name, color, dash in (("obs", "measured", C_OBS, None),
                                       ("fc", "forecast", C_FC, "dash")):
            s = series(data, station, qty, tag)
            if s is None or s.empty:
                continue
            any_data = True
            fig.add_trace(go.Scatter(x=s.index, y=s.values, name=name, mode="lines",
                                     line=dict(color=color, width=2, dash=dash),
                                     legendgroup=tag, showlegend=tag not in shown,
                                     hovertemplate="%{y:,.2f}"), row=i, col=1)
            shown.add(tag)
    fig.add_vline(x=now, line=dict(color="#888", width=1, dash="dot"))
    base_layout(fig, 480)
    return fig if any_data else None


# ---------------------------------------------------------------- verification
def latest_hourly_obs(latest):
    """Hourly measured values from the snapshot (fills the gap before the next archive run)."""
    m = latest[latest.kind == "measured"].copy()
    if m.empty:
        return pd.DataFrame(columns=["station", "qty", "time", "value"])
    m["time"] = m["time"].dt.tz_convert("UTC")
    m = m[m["time"].dt.minute == 0]
    return m[["station", "qty", "time", "value"]]


def verification_pairs(fc_arch, obs_all):
    if fc_arch.empty or obs_all.empty:
        return pd.DataFrame()
    p = fc_arch.merge(obs_all, on=["station", "qty", "time"], suffixes=("_fc", "_obs"))
    p = p.rename(columns={"value_fc": "forecast", "value_obs": "observed"})
    p["error"] = p["forecast"] - p["observed"]
    p["lead_bucket"] = pd.cut(p["lead_h"], LEAD_BINS, labels=LEAD_LABELS, right=False)
    return p


def error_table(pairs):
    if pairs.empty:
        return pd.DataFrame()
    g = pairs.groupby(["station", "qty", "lead_bucket"], observed=True)["error"]
    t = pd.DataFrame({"n": g.size(), "bias": g.mean(), "mae": g.apply(lambda e: e.abs().mean())})
    return t.reset_index()


def runs_figure(station, qty, fc_arch, obs_all, now):
    unit = unit_of(station, qty)
    o = obs_all[(obs_all.station == station) & (obs_all.qty == qty)].sort_values("time")
    f = fc_arch[(fc_arch.station == station) & (fc_arch.qty == qty)]
    if f.empty:
        return None
    # one run per day (the last of each day), most recent 10 days
    runs = sorted(f.fetched_at.unique())
    daily = (pd.Series(runs, index=pd.DatetimeIndex(runs))
             .groupby(pd.DatetimeIndex(runs).tz_convert(TZ).date).last().tail(10).tolist())
    fig = go.Figure()
    n = len(daily)
    for i, run in enumerate(daily):
        r = f[f.fetched_at == run].sort_values("time")
        shade = 0.25 + 0.75 * (i + 1) / n
        fig.add_trace(go.Scatter(
            x=r.time.dt.tz_convert(TZ), y=r.value, mode="lines",
            name=pd.Timestamp(run).tz_convert(TZ).strftime("run %d %b %H:%M"),
            line=dict(color=f"rgba(192,57,43,{shade:.2f})", width=1.6 if i == n - 1 else 1.1, dash="dash"),
            hovertemplate="%{y:,.2f}"))
    if not o.empty:
        fig.add_trace(go.Scatter(x=o.time.dt.tz_convert(TZ), y=o.value, mode="lines", name="observed",
                                 line=dict(color=C_OBS, width=2.4), hovertemplate="%{y:,.2f}"))
    fig.add_vline(x=now, line=dict(color="#888", width=1, dash="dot"))
    fig.update_yaxes(title_text=unit)
    base_layout(fig, 420)
    fig.update_layout(legend=dict(orientation="v", y=1, x=1.01, xanchor="left", font=dict(size=10)),
                      margin=dict(r=110))
    return fig


def error_figure(err, qty):
    e = err[err.qty == qty]
    if e.empty:
        return None
    fig = go.Figure()
    for station in STATION_ORDER:
        s = e[e.station == station]
        if s.empty:
            continue
        fig.add_trace(go.Bar(x=s.lead_bucket.astype(str), y=s.mae, name=station,
                             marker_color=C_STATION[station],
                             customdata=s[["n", "bias"]].values,
                             hovertemplate="MAE %{y:,.2f}<br>bias %{customdata[1]:+,.2f}<br>n=%{customdata[0]}"))
    unit = "m" if qty == "H" else "m³/s"
    fig.update_yaxes(title_text=f"mean abs. error ({unit})")
    base_layout(fig, 320)
    fig.update_layout(barmode="group", hovermode="closest",
                      title=dict(text="Water level" if qty == "H" else "Discharge", x=0, font=dict(size=13)))
    return fig


def error_table_html(err):
    if err.empty:
        return ""
    rows = []
    order = {s: i for i, s in enumerate(STATION_ORDER)}
    err = err.assign(_o=err.station.map(order)).sort_values(["_o", "qty"])
    for (station, qty), g in err.groupby(["station", "qty"], sort=False):
        u = unit_of(station, qty)
        cells = {b: "" for b in LEAD_LABELS}
        for _, r in g.iterrows():
            cells[str(r.lead_bucket)] = (f"{fmt(r.mae, u)}<span class='bias'> ({fmt(r.bias, u, True)})</span>"
                                         f"<span class='n'>n={int(r.n)}</span>")
        rows.append(f"<tr><td>{esc(station)}</td><td>{'H' if qty == 'H' else 'Q'}</td><td>{esc(u)}</td>"
                    + "".join(f"<td class='num'>{cells[b] or '–'}</td>" for b in LEAD_LABELS) + "</tr>")
    head = "<tr><th>Station</th><th></th><th>Unit</th>" + "".join(f"<th>{b}</th>" for b in LEAD_LABELS) + "</tr>"
    return (f"<div class='table-wrap'><table class='err'><thead>{head}</thead><tbody>{''.join(rows)}"
            "</tbody></table></div><p class='note'>Mean absolute error with mean bias in brackets "
            "(forecast − observed; positive = forecast too high).</p>")


# ---------------------------------------------------------------- downloads
def prepare_downloads(latest, pairs):
    dl = SITE / "downloads"
    dl.mkdir(parents=True, exist_ok=True)
    links = []
    l2 = latest.copy()
    l2["time"] = l2["time"].dt.tz_convert("UTC").dt.strftime("%Y-%m-%dT%H:%MZ")
    l2.to_csv(dl / "latest.csv", index=False)
    links.append(("latest.csv", "Current window: measured (10 days, full resolution) + latest forecast"))
    if not pairs.empty:
        p = pairs.copy()
        for c in ("fetched_at", "time"):
            p[c] = p[c].dt.strftime("%Y-%m-%dT%H:%MZ")
        p.to_csv(dl / "forecast_vs_observed.csv", index=False)
        links.append(("forecast_vs_observed.csv",
                      "Every archived forecast value matched to the observation at that time, with error and lead time"))
    for sub, desc in (("observations", "Observed, hourly"), ("forecasts", "Forecast runs")):
        files = sorted((rd.DATA_DIR / sub).glob("*.csv"))
        if files:
            (dl / sub).mkdir(exist_ok=True)
            for f in files:
                shutil.copy(f, dl / sub / f.name)
                links.append((f"{sub}/{f.name}", f"{desc}, {f.stem}"))
    items = "".join(f"<li><a href='downloads/{esc(p)}' download>{esc(p)}</a> — {esc(d)}</li>"
                    for p, d in links)
    return f"<ul class='downloads'>{items}</ul>"


# ---------------------------------------------------------------- page
CSS = """
:root{color-scheme:light;--bg:#f6f7f9;--panel:#fff;--ink:#1d2430;--muted:#667085;--line:#e4e7ec;
--accent:#1f4e79;--up:#0b7a43;--down:#b42318;--flag:#b54708}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{background:var(--accent);color:#fff;padding:20px 16px 0}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px}
header .wrap{padding:0}h1{margin:0;font-size:22px;font-weight:650;letter-spacing:-.01em}
.sub{opacity:.85;font-size:13px;margin:2px 0 14px}
nav{display:flex;gap:4px;overflow-x:auto}nav button{background:none;border:0;color:#fff;opacity:.75;
font:inherit;font-weight:550;padding:10px 14px;border-radius:8px 8px 0 0;cursor:pointer;white-space:nowrap}
nav button.active{background:var(--bg);color:var(--accent);opacity:1}
main.wrap{padding-top:22px;padding-bottom:40px}section{display:none}section.active{display:block}
h2{font-size:16px;margin:26px 0 10px;font-weight:650}h2:first-child{margin-top:0}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card-label{color:var(--muted);font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.card-value{font-size:28px;font-weight:650;letter-spacing:-.02em;margin:2px 0}
.card-value .unit{font-size:14px;font-weight:500;color:var(--muted)}
.delta{font-size:12px;font-weight:600;color:var(--muted)}.delta.up{color:var(--up)}.delta.down{color:var(--down)}
.card-sub{color:var(--muted);font-size:13px;margin-top:6px}.flag{color:var(--flag);font-weight:600}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.summary{margin:0;padding-left:18px}.summary li{margin:3px 0}
.table-wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:8px 12px;text-align:left;
border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-weight:600;font-size:12px}
tbody tr:last-child td{border-bottom:0}td.num{text-align:right;font-variant-numeric:tabular-nums}
.bias{color:var(--muted)}.n{display:block;color:var(--muted);font-size:11px}
.note{color:var(--muted);font-size:12px;margin:8px 2px 0}
.tabs2{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.tabs2 button,select{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--ink);
padding:6px 12px;border-radius:999px;cursor:pointer}.tabs2 button.active{background:var(--accent);
color:#fff;border-color:var(--accent)}select{border-radius:8px;padding:7px 10px}
.plot{display:none}.plot.active{display:block}.empty{color:var(--muted);padding:24px;text-align:center}
.downloads li{margin:6px 0}a{color:var(--accent)}
footer{color:var(--muted);font-size:12px;padding:20px 0 30px;border-top:1px solid var(--line)}
@media(max-width:760px){.cards{grid-template-columns:repeat(2,1fr)}.card-value{font-size:22px}}
"""

JS = """
function resize(root){root.querySelectorAll('.js-plotly-plot').forEach(p=>{try{Plotly.Plots.resize(p)}catch(e){}})}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button,section').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');const s=document.getElementById(b.dataset.s);s.classList.add('active');resize(s);
  history.replaceState(null,'','#'+b.dataset.s)});
document.querySelectorAll('.tabs2').forEach(g=>g.querySelectorAll('button').forEach(b=>b.onclick=()=>{
  g.querySelectorAll('button').forEach(x=>x.classList.remove('active'));b.classList.add('active');
  const box=document.getElementById(g.dataset.box);box.querySelectorAll('.plot').forEach(p=>p.classList.toggle('active',p.dataset.k===b.dataset.k));
  resize(box)}));
const vs=document.getElementById('vsel');if(vs)vs.onchange=()=>{const box=document.getElementById('vbox');
  box.querySelectorAll('.plot').forEach(p=>p.classList.toggle('active',p.dataset.k===vs.value));resize(box)};
const h=location.hash.slice(1);if(h){const b=document.querySelector(`nav button[data-s="${h}"]`);if(b)b.click()}
"""


def main():
    data, meta, latest = load_snapshot()
    now = pd.Timestamp(meta["generated"]).tz_convert(TZ)
    state = build_state(data, now)
    kstate = meta.get("koeln_state") or {}

    # Now: hydrographs per station (tabs)
    hyd_btn, hyd_div = [], []
    for i, station in enumerate(STATION_ORDER):
        fig = hydrograph(data, station, now)
        a = " active" if i == 0 else ""
        hyd_btn.append(f"<button class='{a.strip()}' data-k='{i}'>{esc(station)}</button>")
        body = fig_html(fig, f"hyd{i}") if fig else f"<div class='empty'>No data for {esc(station)}</div>"
        hyd_div.append(f"<div class='plot{a}' data-k='{i}'>{body}</div>")

    # Verification
    fc_arch = rd.read_forecasts()
    obs_arch = rd.read_observations()
    obs_all = pd.concat([obs_arch, latest_hourly_obs(latest)], ignore_index=True)
    if not obs_all.empty:
        obs_all["time"] = pd.to_datetime(obs_all["time"], utc=True)
        obs_all = obs_all.drop_duplicates(["station", "qty", "time"], keep="first")
    pairs = verification_pairs(fc_arch, obs_all)
    err = error_table(pairs)

    opts, vdivs = [], []
    k = 0
    for station in STATION_ORDER:
        for qty in ("H", "Q"):
            fig = runs_figure(station, qty, fc_arch, obs_all, now) if not fc_arch.empty else None
            if fig is None:
                continue
            a = " active" if k == 0 else ""
            label = f"{station} — {'water level' if qty == 'H' else 'discharge'}"
            opts.append(f"<option value='{k}'>{esc(label)}</option>")
            vdivs.append(f"<div class='plot{a}' data-k='{k}'>{fig_html(fig, f'run{k}')}</div>")
            k += 1
    n_runs = 0 if fc_arch.empty else fc_arch.fetched_at.nunique()
    first_run = "" if fc_arch.empty else pd.Timestamp(fc_arch.fetched_at.min()).tz_convert(TZ).strftime("%d %b %Y")
    if vdivs:
        runs_block = (f"<select id='vsel'>{''.join(opts)}</select><div id='vbox' class='panel' "
                      f"style='margin-top:10px'>{''.join(vdivs)}</div>"
                      "<p class='note'>Dashed lines: archived forecast runs (one per day, darker = newer). "
                      "Solid: observed.</p>")
    else:
        runs_block = "<div class='panel empty'>No forecasts archived yet — the first one is stored at the next update.</div>"
    if not err.empty:
        figs = [error_figure(err, q) for q in ("H", "Q")]
        err_block = ("<div class='panel'>" + "".join(fig_html(f, f"err{q}") for f, q in zip(figs, "HQ") if f)
                     + "</div>" + error_table_html(err))
    else:
        err_block = ("<div class='panel empty'>Not enough history yet. Error statistics appear once archived "
                     "forecasts reach their target times (first values after ~1 day, 4-day leads after ~4 days).</div>")

    downloads = prepare_downloads(latest, pairs)
    log_rows = "".join(
        f"<tr><td>{esc(r['station'])}</td><td>{esc(r['qty'])}</td><td>{esc(r['series'])}</td>"
        f"<td>{esc(r['source'])}</td><td class='num'>{r['values']}</td><td>{esc(r['error'])}</td></tr>"
        for r in meta.get("log", []))

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rhine monitor</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🌊</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
<script src="plotly.min.js"></script>
</head><body>
<header><div class="wrap">
  <h1>Rhine monitor</h1>
  <div class="sub">Köln → Lobith → Millingen → Dodewaard · water level and discharge · updated {now:%a %d %b %Y, %H:%M} ({TZ.split('/')[1]})</div>
  <nav>
    <button class="active" data-s="now">Now</button>
    <button data-s="check">Forecast check</button>
    <button data-s="downloads">Downloads</button>
    <button data-s="about">About</button>
  </nav>
</div></header>
<main class="wrap">
<section id="now" class="active">
  <div class="cards">{cards_html(state, kstate)}</div>
  <h2>Summary</h2><div class="panel">{summary_html(state)}</div>
  <h2>State and forecast</h2>{state_table_html(state)}
  <p class="note">+24h / +48h / +96h: forecast value at that time from now. Forecasts are Rijkswaterstaat model
  output, not the validated WMCN forecast (see <a href="https://waterinfo.rws.nl">waterinfo.rws.nl</a>).</p>
  <h2>Hydrographs</h2>
  <div class="tabs2" data-box="hydbox">{''.join(hyd_btn)}</div>
  <div id="hydbox" class="panel">{''.join(hyd_div)}</div>
</section>
<section id="check">
  <h2>Past forecasts vs observed</h2>
  <p class="note" style="margin-top:-4px">{n_runs} forecast runs archived{f' since {first_run}' if first_run else ''}.</p>
  {runs_block}
  <h2>Forecast error by lead time</h2>
  {err_block}
</section>
<section id="downloads">
  <h2>Downloads (CSV)</h2>
  <div class="panel">{downloads}
  <p class="note">Times are UTC. Water level: m +NAP for Dutch stations, cm at the gauge for Köln. Discharge: m³/s.
  Forecast files: <code>fetched_at</code> is when the run was stored, <code>lead_h</code> hours ahead of that.</p></div>
</section>
<section id="about">
  <h2>About</h2>
  <div class="panel"><p>Updated automatically every 3 hours by a scheduled GitHub workflow, which also archives each new
  forecast run and the hourly observations in this repository.</p>
  <p>Sources: Rijkswaterstaat open water data (Lobith, Millingen, Dodewaard; via
  <a href="https://pypi.org/project/rws-ddlpy">rws-ddlpy</a>) and
  <a href="https://www.pegelonline.wsv.de">PEGELONLINE</a> (Köln). Köln has no public forecast here unless
  Rijkswaterstaat publishes a discharge forecast for Keulen.</p></div>
  <h2>Last fetch</h2>
  <div class="table-wrap"><table><thead><tr><th>Station</th><th>Qty</th><th>Series</th><th>Source code</th>
  <th>Values</th><th>Error</th></tr></thead><tbody>{log_rows}</tbody></table></div>
</section>
</main>
<footer class="wrap">Data: Rijkswaterstaat, WSV/PEGELONLINE. Generated {now:%Y-%m-%d %H:%M}.</footer>
<script>{JS}</script>
</body></html>"""

    SITE.mkdir(exist_ok=True)
    (SITE / "plotly.min.js").write_text(plotly.offline.get_plotlyjs(), encoding="utf-8")
    (SITE / "index.html").write_text(page, encoding="utf-8")
    (SITE / ".nojekyll").write_text("")
    print(f"site built: {len(pairs)} verification pairs, {n_runs} archived runs")


if __name__ == "__main__":
    main()
