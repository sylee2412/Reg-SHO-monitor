# RegSHO Monitor

NASDAQ Reg SHO Threshold List tracker. Tracks how many consecutive settlement days each security has been on the list and flags tickers approaching the 13-day mandatory close-out deadline.

Built with Python + Flask.

<img width="1914" height="666" alt="image" src="https://github.com/user-attachments/assets/08d19404-023f-4891-a73f-5dc6bc572bb3" />


## Why

SEC Rule 203(b)(3) — if a security stays on the threshold list for 13 consecutive settlement days, broker-dealers must close out all open fail-to-deliver positions. Missing that deadline is not optional.

This tool pulls data from `nasdaqtrader.com` daily, calculates streaks, and shows you which tickers are in danger territory so you don't have to check manually.

## What It Does

- Fetches NASDAQ threshold data automatically (07:00 & 22:30 ET via APScheduler)
- Calculates consecutive settlement day streaks (up to 60 days of history)
- Color-coded risk levels: red (11-13d), yellow (8-10d), green (1-7d)
- Tracks daily additions & removals from the list
- Per-ticker 30-day history calendar view
- Browser-based watchlist
- Rule 3210 obligation flag
- CSV export
- Runs 24/7

## Usage

```
pip install -r requirements.txt
python app.py
```

Opens at `http://localhost:5000`. First run downloads ~60 trading days of historical data (takes 1-2 min), then uses local cache.

## Filtering Unwanted Tickers (ETFs, etc.)

If you want to exclude specific types of securities (like certain ETFs or funds) from the analysis, simply add keywords or issuer names to the `EXCLUDE_SUBSTRINGS` list in `app.py`. Any ticker whose name contains these substrings will be completely omitted from the dashboard.

```python
# app.py
EXCLUDE_SUBSTRINGS = [
    "TIDAL", "DEFIANCE", "YIELDMAX",
    # Add your exclusions here!
    # "NEW_FUND_NAME", "ETC"
]
```

## Project Structure

```
├── app.py              # Flask server + scheduler
├── requirements.txt
├── templates/
│   └── index.html      # Dashboard UI
└── data/               # auto-generated, git-ignored
    ├── cache.json
    └── history.json
```

## API

```
GET  /                    Dashboard
GET  /api/data            Full analysis JSON
POST /api/refresh         Manual refresh
GET  /api/history/{sym}   30-day history for a ticker
GET  /api/export/csv      CSV download
```

## Running 24/7

**Windows** — Task Scheduler, run `pythonw app.py` on startup.

**Linux** — systemd:

```ini
[Unit]
Description=RegSHO Monitor
After=network.target

[Service]
WorkingDirectory=/path/to/regsho-monitor
ExecStart=/usr/bin/python3 app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## References

- [NASDAQ Reg SHO Threshold List](https://www.nasdaqtrader.com/trader.aspx?id=regshothreshold)
- [SEC Rule 203(b)(3)](https://www.sec.gov/rules/final/34-50103.htm)

## License

MIT — not financial or legal advice.
