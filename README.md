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
- **Market review:** top of every report - indexes, VIX, 10-yr yield, USD/CAD, oil, gold, breadth, sectors and a market stance.
- **Tracker:** `reports/tracker.csv` follows every setup day to day:
  Watch → Buy (or Don't chase) → Hold → Take profit → Hold-trailing → Sell, or Drop if it fails before entry.

- **Morning check:** around 10:15 and 11:15 am ET - intraday VWAP, time-of-day volume and buy-zone verdict for last night's names, shown at the top of the main report and in full on `morning.html`
- **Backtest:** `backtest.html`, every Saturday - 5 years of S&P 500 + TSX 60 + Dow, win rate and average R by score, pattern and volume

Every setup is scored out of 100 with the checklist weights (volume 20, price action 15, base 10, relative strength 15,
market 10, VWAP/structure 10, reward:risk 15, liquidity 5). Buy = 70+, R:R 2+, no earnings within 5 sessions, under 4 warnings (thresholds set from the backtest).

Rules taken from six trading books (Murphy, Hougaard, Douglas, Allemann, Krauth, Øygard) are listed in
`BOOK_RULES.md` with the chapter each came from, whether it was added, changed, tested or left out, and why.
Every book-derived line in the code carries a `[Author ch.]` comment.

All thresholds live in the `CFG`, `TRADE` and `ACCOUNT` blocks at the top of `breakout_scanner.py`.

Technical screen only, not investment advice. Check every chart before trading.
