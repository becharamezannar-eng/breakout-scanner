# Rules extracted from the six books, and what the scanner does with each

Legend: **ADDED** = new code in this revision · **CHANGED** = existing rule altered ·
**KEPT** = already in the scanner, now cited · **TESTED** = put to the backtest before adoption ·
**NOT ADDED** = deliberately left out (reason given).

Books:
- [M] John J. Murphy, *Technical Analysis of the Financial Markets* (1999). Chapter numbers refer to this book.
- [H] Tom Hougaard, *Best Loser Wins*.
- [D] Mark Douglas, *Trading in the Zone*.
- [A] Daniel Allemann, *Secrets de trading d'un moine bouddhiste* (incl. W.D. Gann's 17 rules, Ichimoku).
- [K] Peter Krauth, *The Great Silver Bull*.
- [O] Svein Harald Øygard, *In the Combat Zone of Finance*.

## 1. Breakout definition and false-breakout filters
| Rule | Source | Status |
|---|---|---|
| Signals on closing prices only; intraday pokes don't count | M ch2 (Dow), ch4 | KEPT |
| Price filter: close beyond the level by 1% (short-term) to 3% (major) | M ch4, ch5 | ADDED as score points for ≥1% clearance (hard floor stays 0.2% + 0.1 ATR) |
| Time filter: two consecutive closes beyond the level (two-day rule) | M ch4, ch5 | KEPT (Watch → Buy only after the hold day) |
| Breakout must make a new 20-day (4-week) closing high — Donchian channel breakout, rated best system in Dunn & Hargitt / Lukac studies | M ch9 | ADDED (warning if not a 20-day closing high) |
| A decisive close back through the breakout level = failed pattern; exit | M ch5 (failed H&S), ch4 | ADDED + TESTED (failed-breakout exit) |
| A good trade works immediately; if not, close it (Trevor Neil anecdote) | H "Curse of Patterns" | ADDED + TESTED (same exit, first 10 sessions) |
| Bull trap = breakout on light volume, decline on heavy volume | M ch5 | KEPT (volume floor) + ADDED (hold day on heavier volume than breakout and closing lower → drop) |
| Breakaway gap out of a base on heavy volume is bullish and usually not filled; a close back below the gap is weakness | M ch4 | CHANGED (gap + ≥2× volume is now a plus, not a warning; gap on weak volume stays a warning; tracker warns on gap fill) |
| Gap-up days favour continuation; most gaps are not filled the same day | H "Curse of Patterns" | supports the change above |

## 2. Patterns
| Rule | Source | Status |
|---|---|---|
| Reversal patterns need a prior trend to reverse | M ch5 | KEPT (Bottoming needs ≥20% drawdown) |
| Bottoms: volume increase on breakout is "absolutely essential" | M ch5 | ADDED (Bottoming breakout under 2× volume gets a warning) |
| Double bottom: at least a month between the two lows | M ch5 | CHANGED (≥20 trading days, was 10) |
| Triple-top / ascending-triple-top breakouts are stronger than simple ones; wider base = more potential | M ch11 (P&F) | KEPT (resistance tests) + ADDED (base-length points) |
| Triangle: ≥4 reversal points; breakout should come 2/3–3/4 of the way to the apex, later loses potency | M ch6 | ADDED (late-triangle warning for ascending triangles) |
| Ascending triangle is bullish wherever it appears | M ch6 | KEPT |
| Flag/pennant: sharp pole on heavy volume, 1–3 week pause, volume dries up noticeably, flag slopes against trend | M ch6 | CHANGED (pole volume ≥ average, flag volume < 80% of pole volume, 4–15 bars) |
| Rising wedge is bearish; falling wedge bullish | M ch6 | ADDED (rising-wedge base = warning) |
| Broadening formation (expanding range, rising volume) is bearish | M ch6 | ADDED (broadening base = warning) |
| Volume should contract during consolidations and expand on the breakout | M ch6, ch7 | KEPT (dry-up + breakout volume) |
| Measured move = pattern height from breakout; adjust to nearby S/R | M ch5, ch6 | KEPT |
| Fibonacci retracements/ratios | A ch8 presents it as an unproven hypothesis; H shows S&P retracements don't cluster at 38.2/61.8 | NOT ADDED |
| Named candlestick patterns | M ch12 (describes them); H cites two academic studies finding no predictive power | NOT ADDED — kept only measurable candle quality (close position, upper wick, range) |

## 3. Volume
| Rule | Source | Status |
|---|---|---|
| Volume must expand in the direction of the breakout | M ch2, ch5–7 | KEPT |
| On-balance volume should confirm; OBV leading to a new high before price is bullish; OBV failing to confirm a new high is a divergence | M ch7 | ADDED (OBV at 50-day high = points; OBV divergence = warning) |
| Heavier volume on up days than down days inside the pattern | M ch6 | KEPT (up/down volume) |

## 4. Trend, momentum and volatility
| Rule | Source | Status |
|---|---|---|
| Weekly signals filter daily ones; trade only with the higher-timeframe trend | M ch10, ch8, ch19; D ch11; A (Gann rule 16) | ADDED (weekly trend: close > 10-wk and 10-wk > 30-wk average, approximated by 50/150-day; weekly MACD) |
| Oscillators are already overbought at a real breakout — ignore them early in a trend; what matters is bearish divergence in mature trends | M ch10; H ("no bargains", stopped using stochastics) | CHANGED (RSI > 80 warning removed; bearish RSI divergence warning added) |
| ADX rising from below 20 = new trend starting; ADX falling from above 40 = trend tiring; +DI above −DI | M ch15 | ADDED |
| Bollinger Bands contracting too far often precedes a new trend (squeeze) | M ch9 | ADDED (band width in lowest 20% of 6 months) |
| Price above STARC+ (6-day MA + 2 ATR) = high-risk time to buy, don't chase | M App. A | ADDED (warning) |
| A moving-average signal is stronger when the average itself turns in that direction | M ch9 | KEPT (rising 50-day) |
| Ichimoku Kinko Hyo: dynamic support/resistance; trends on several horizons at a glance | A ch12 | ADDED (above the cloud, Tenkan > Kijun, bullish cloud ahead) |

## 5. Relative strength and market health
| Rule | Source | Status |
|---|---|---|
| Buy the strongest stocks in the strongest groups; avoid falling RS lines; RS breakouts lead | M ch17 | KEPT + ADDED (RS line below its 50-day average = warning) |
| AD line, new highs − new lows (Elder: "best leading indicator"), up/down volume should confirm the indexes | M ch18 | ADDED (NH−NL 10-day, up-volume share 10-day) |
| Small caps (Russell 2000) and Nasdaq should trend with the S&P | M ch18 | ADDED to the market review |
| % of stocks above their averages: < 30% oversold, a move back above 30% is a buy signal | M ch10 | ADDED (breadth thrust bonus) |
| Distribution days | (IBD practice; consistent with M ch7 volume rules) | KEPT |
| Markets priced the Icelandic crisis (CDS, bank shares halving) before experts did; credit booms and bank stress precede crashes | O ch2, ch5 | ADDED (credit-stress gauge: high-yield vs Treasury ETF ratio, bank-stock relative strength) |
| When the crowd and press see only the worst, a turn up is near | A (Gann rule 15); M ch10 | used via breadth thrust; no sentiment data source |

## 6. Risk, exits and position management
| Rule | Source | Status |
|---|---|---|
| Stop below a valid support level, sized by volatility, not an arbitrary amount | M ch16; D ch11; H (stop = 2N) | KEPT (base low / 1 ATR) |
| Don't place stops on obvious round numbers; round numbers act as resistance | M ch4 | ADDED (stop nudged below round number; round-number overhead warning) |
| Never let a large profit turn into a loss / move stop to breakeven | A (Gann rule 3); D ch11 ("risk-free opportunity"); M ch16 | ADDED + TESTED (breakeven after +1R) |
| Reward:risk of at least 3:1 | M ch16; D ch11 | TESTED against 2:1 |
| Scale out in thirds: small first profit, second at target with stop to entry, trail the rest | D ch11 | TESTED |
| Don't cap winners with fixed targets | H | TESTED (pure trailing stop variant) |
| Pyramid only winners, smaller layers, every ½N, raise stops | M ch16; H | ADDED as optional add-on level on Hold cards (not automated) |
| Risk 1–2% per trade; size from stop distance | H (2% example); M ch16 (5% max, futures) | KEPT (1% default) |
| Judge an edge over samples of ≥20 trades; watch for decay | D ch11 | ADDED (live edge monitor: last 20 closed signals) |
| Don't raise size after a winning streak | M ch16; A (Gann rule 13) | ADDED (note on the report) |
| Only liquid, active markets | A (Gann rule 4); M ch16 | KEPT |
| Don't buy because the price looks low | A (Gann rule 10); H | KEPT (breakouts only) |
| No trade when the trend can't be determined | A (Gann rule 16) | TESTED (the "Other" category) |
| Insider buying with volume as a buy signal | A ch8 | NOT ADDED (no reliable free data) |

## 7. Precious metals and seasonality (TSX-heavy universe)
| Rule | Source | Status |
|---|---|---|
| Real interest rates falling/negative = metals thrive | K ch15 | ADDED to market review (TIP ETF trend as real-yield proxy) |
| Gold/silver ratio ≥ 80 and turning down = silver/metals outperformance phase | K ch32 | ADDED to market review; context flag for precious-metal miners |
| Silver outperforms gold in bull markets; spikes then long bases | K ch28, ch34 | context only |
| Seasonality: metals strongest Sep–Feb; summer doldrums Jun–Aug; December tax-loss selling in US/Canadian small caps | K ch35 | ADDED (seasonal warnings) |

## 8. What the backtest said (Aug 2022 - Oct 2026, S&P 500 + TSX 60 + Dow, 1,313 trades, 202 passing the Buy gate)
| Test | Result (Buy-gate trades, avg R per trade) | Decision |
|---|---|---|
| New scorecard vs previous | +0.27R, profit factor 1.59 (previous gate: about +0.19R) | adopted |
| Exit A - half at target, trail rest (before) | +0.27R, PF 1.59 | baseline |
| Exit E - A + failed-breakout exit [Murphy ch5; Hougaard] | +0.27R, PF 1.63 | **adopted** (same expectancy, better PF, losers cut early) |
| Exit F - A + breakeven at +1R [Gann rule 3; Douglas] | +0.24R | rejected (normal pullbacks stop out winners) |
| Exit C - Douglas thirds | +0.20R | rejected |
| Exit D - no target, trail all [Hougaard] | +0.24R | rejected |
| R:R 3:1 instead of 2:1 [Murphy ch16; Douglas] | +0.24R, fewer trades | rejected; 2:1 kept |
| ADX turning up from below 20 [Murphy ch15] | +0.06R with vs +0.29R without | removed from the score |
| Bollinger squeeze [Murphy ch9] | +0.21R with vs +0.24R without | reported, not scored |
| Bearish RSI divergence [Murphy ch10] | +0.18R with vs +0.26R without | warning kept |
| OBV divergence [Murphy ch7] | +0.16R with vs +0.25R without | warning kept |
| Score 75+ / 80+ | +0.12R / -0.05R | the score is a pass mark at 70, not a ranking |
| By year (gate) | 2022 -0.90R (9 trades), 2023 +0.72R, 2024 +0.40R, 2025 -0.04R, 2026 +0.19R | expect losing years |

Limits: today's index members only (survivorship bias), no historical sector/earnings data (scored neutral),
daily bars only. Groups under ~30 trades are noise.
