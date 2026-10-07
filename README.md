# Rhine monitor

A free, self-updating dashboard for the Rhine at **Köln, Lobith, Millingen aan de Rijn and Dodewaard**: water level and discharge, measured and forecast, with an archive of past forecasts to check them against what actually happened.

It runs entirely on GitHub: a scheduled workflow fetches the data every 3 hours, stores new forecasts and observations in this repository, and publishes the dashboard on GitHub Pages.

**Dashboard tabs**
- **Now**: discharge per station with 24 h change, summary, state-and-forecast table (+24 h / +48 h / +96 h), hydrographs.
- **Forecast check**: past forecast runs (one per day) against the observed line, and forecast error by lead time (mean absolute error and bias) for every station.
- **Downloads**: CSV files with the observation archive, all archived forecast runs, every forecast matched to its observation, and the current data window.

## Setup (about 10 minutes, once)

1. **Create a repository.** Go to <https://github.com/new>, name it e.g. `rhine-monitor`, choose **Public** (GitHub Pages is free for public repositories), and click **Create repository**.
2. **Upload the files.** On the new repository page click **uploading an existing file**, then drag in everything from the unzipped `rhine-monitor` folder: `update.py`, `build_site.py`, `rhine_data.py`, `requirements.txt`, `README.md`, `.gitignore` and the `.github` folder. Click **Commit changes**.
   - The `.github` folder is hidden on Mac and sometimes on Windows. If it didn't come along, click **Add file → Create new file**, type `.github/workflows/update.yml` as the name, paste in the contents of that file, and commit.
3. **Turn on Pages.** Go to **Settings → Pages** and under *Build and deployment → Source* choose **GitHub Actions**.
4. **Run the first update.** Go to the **Actions** tab, click **Update Rhine monitor** on the left, then **Run workflow**. It takes 2–3 minutes.
5. **Open your dashboard** at `https://<your-github-username>.github.io/rhine-monitor/` (the link is also shown in the finished workflow run). Bookmark it or add it to your phone's home screen.

From then on it updates itself every 3 hours.

## Good to know

- **The forecast check needs time.** The archive starts with the first run. Errors for short lead times appear after about a day, 4-day lead times after about 4 days, and the statistics get more reliable over weeks.
- **What is archived.** Observations: hourly values. Forecasts: every run that differs from the previous one, hourly up to 96 hours ahead and 6-hourly beyond. This grows by roughly 10 MB a month, comfortably within GitHub's limits. The files are in the `data/` folder of the repository.
- **Forecasts** are Rijkswaterstaat model output, not the validated WMCN forecast; for that see <https://waterinfo.rws.nl>. Köln has measurements only, unless Rijkswaterstaat publishes a discharge forecast for Keulen.
- **Updates on demand:** **Actions → Update Rhine monitor → Run workflow**. GitHub sometimes starts scheduled runs a little late.

## Troubleshooting

- **A value shows "n/a":** open the **About** tab of the dashboard. *Last fetch* lists each station and series with the Rijkswaterstaat code used and any error. Not every station publishes every series. Station names are matched using `RWS_STATIONS` near the top of `rhine_data.py`.
- **The workflow fails at "Commit archive":** go to **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, save, and run it again.
- **The workflow fails at the deploy step:** check that **Settings → Pages → Source** is set to **GitHub Actions**.
- **Updates stopped:** GitHub can pause scheduled workflows in repositories without activity for a long time. The **Actions** tab then shows a button to enable it again.

## Running it on your own computer

```bash
pip install -r requirements.txt
python update.py       # fetch + archive into data/
python build_site.py   # writes site/index.html
```
Then open `site/index.html` in a browser.
