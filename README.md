# Daily Breakout Scanner

Every weekday after the close (6 pm ET summer / 5 pm ET winter), GitHub runs `breakout_scanner.py` across the
S&P/TSX Composite, S&P/TSX 60, S&P 500, DJIA, Nasdaq Composite and Russell 2000,
looking for volume-confirmed breakouts out of tight consolidations
(bottoming breakouts ranked first).

- **Latest report:** the GitHub Pages link for this repository (`https://<your-username>.github.io/breakout-scanner/`)
- **Past scans:** `.../archive.html`, or the `reports/` folder
- **Run it now:** Actions tab → *Daily breakout scan* → *Run workflow*
- **Tracker:** `reports/tracker.csv` follows every setup day to day:
  Watch → Buy (or Don't chase) → Hold → Take profit → Hold-trailing → Sell, or Drop if it fails before entry.

- **Morning check:** `morning.html`, around 10:15 am ET - intraday VWAP, time-of-day volume and buy-zone verdict for last night's names
- **Backtest:** `backtest.html`, every Saturday - 5 years of S&P 500 + TSX 60 + Dow, win rate and average R by score, pattern and volume

Every setup is scored out of 100 with the checklist weights (volume 20, price action 15, base 10, relative strength 15,
market 10, VWAP/structure 10, reward:risk 15, liquidity 5). Buy = 70+, R:R 2+, no earnings within 5 sessions, under 4 warnings (thresholds set from the backtest).

All thresholds live in the `CFG`, `TRADE` and `ACCOUNT` blocks at the top of `breakout_scanner.py`.

Technical screen only, not investment advice. Check every chart before trading.
