# Daily Breakout Scanner

Every weekday after the close (6 pm ET summer / 5 pm ET winter), GitHub runs `breakout_scanner.py` across the
S&P/TSX Composite, S&P/TSX 60, S&P 500, DJIA, Nasdaq Composite and Russell 2000,
looking for volume-confirmed breakouts out of tight consolidations
(bottoming breakouts ranked first).

- **Latest report:** the GitHub Pages link for this repository (`https://<your-username>.github.io/breakout-scanner/`)
- **Past scans:** `.../archive.html`, or the `reports/` folder
- **Run it now:** the **Scan now** button at the top of the report (one-time token setup under the gear icon),
  or Actions tab → *Daily breakout scan* → *Run workflow*. During market hours a manual scan writes `live.html`
  (today's volume projected to a full day) and leaves the tracker untouched.
- **Ticker lookup:** type any ticker in the search box at the top of any page. GitHub builds `lookup/<TICKER>.html`
  in about 2-3 minutes: what the company does, earnings and financial health, valuation, analysts, headlines,
  the scanner's breakout rules and scorecard, and a long-term scorecard from the books (`lookup.py`).
- **Market review:** top of every report - indexes, VIX, 10-yr yield, USD/CAD, oil, gold, breadth, sectors and a market stance.
- **Canada first:** TSX names are downloaded, analysed and listed first in every section (with a CAD 3M/day
  liquidity floor instead of USD 5M), and the report keeps the top 40 new setups per market so US names still appear.
- **On the radar:** stocks closing within 3% under a flat resistance that haven't broken out yet, with the trigger
  price and the volume needed - set price alerts there. A breakout that was on the radar says since when.
- **Catch-up:** a breakout missed on its day (skipped scan, data gap) is still picked up for 3 sessions if it held every day.
- **Tracker:** `reports/tracker.csv` follows every setup day to day:
  Watch → Buy (or Don't chase) → Hold → Take profit → Hold-trailing → Sell, or Drop if it fails before entry.

- **Morning check:** around 10:15 and 11:15 am ET - intraday VWAP, time-of-day volume and buy-zone verdict for last night's names, shown at the top of the main report and in full on `morning.html`
- **Backtest:** `backtest.html`, every Saturday - 5 years of S&P 500 + TSX 60 + Dow, win rate and average R by score, pattern and volume

Every setup is scored out of 100 with the checklist weights (volume 20, price action 15, base 10, relative strength 15,
market 10, VWAP/structure 10, reward:risk 15, liquidity 5). Buy = 70+, R:R 2+, no earnings within 5 sessions, under 4 warnings (thresholds set from the backtest).

See `PROJECT.md` for the project brief and schedule.

Rules taken from six trading books (Murphy, Hougaard, Douglas, Allemann, Krauth, Øygard) are listed in
`BOOK_RULES.md` with the chapter each came from, whether it was added, changed, tested or left out, and why.
Every book-derived line in the code carries a `[Author ch.]` comment.

All thresholds live in the `CFG`, `TRADE` and `ACCOUNT` blocks at the top of `breakout_scanner.py`.

Technical screen only, not investment advice. Check every chart before trading.

Add-ons (ESG scanner, phone app, accounts): see `ARCHITECTURE.md`.
