# Bechara's Market Scanner & Trading Assistant

**Mission:** daily market intelligence, breakout detection, portfolio monitoring and trading insights,
using the uploaded trading books and live financial data.

## Data sources
- The six trading books (rules mapped in `BOOK_RULES.md`): Murphy, *Technical Analysis of the Financial Markets*;
  Hougaard, *Best Loser Wins*; Douglas, *Trading in the Zone*; Allemann, *Secrets de trading d'un moine bouddhiste*;
  Krauth, *The Great Silver Bull*; Øygard, *In the Combat Zone of Finance*.
- Live market data (Yahoo Finance via yfinance; Nasdaq and Wikipedia index lists) and financial news sites.
- Portfolio: `reports/holdings.csv` (positions) and `reports/tracker.csv` (breakout trades, including manual buys).
- Ticker search box on every report page (`lookup.py`).

## Functions and where they live
| Function | Where |
|---|---|
| Daily market summary | Market review at the top of each evening report |
| Breakout scanning | `breakout_scanner.py` evening scan; radar of near-breakouts; catch-up of missed breakouts |
| Portfolio monitoring | "Your portfolio" section; open positions in the tracker |
| Ticker analysis on demand | Search box → `lookup/<TICKER>.html` |
| Code updates | This repository; every book-derived rule is commented `[Author ch.]` |
| Backtesting | `--backtest`, results in `backtest.html` |

## Schedule (Toronto time, GitHub Actions `.github/workflows/scan.yml`)
| Run | When | Cron (UTC) |
|---|---|---|
| Evening scan | Mon-Fri 6:00 pm (5:00 pm after Nov 1) | `0 22 * * 1-5` |
| Morning check | Mon-Fri ~10:15 am, plus ~11:15 am until Nov 1 | `15 14 * * 1-5`, `15 15 * * 1-5` |
| Backtest | Saturdays 8:00 am (7:00 am after Nov 1) | `0 12 * * 6` |
GitHub often starts scheduled runs 15-45 minutes late. "Scan now" and the search box run on demand.
The schedule stays as is unless Bechara changes it.

## Rules for the assistant
- Always reference both the books and live financial data before answering.
- Maintain and update the scanner code when requested; test (`--selftest`) before pushing.
- Keep the schedule active unless told otherwise.
- Only market-scanning and trading work belongs in this project.
- Not investment advice: present the evidence and the rules; decisions are Bechara's.
