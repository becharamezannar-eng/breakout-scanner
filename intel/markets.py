"""Market snapshot from Yahoo Finance (yfinance), so the brief's numbers come from data, not from the AI."""
import logging

log = logging.getLogger("intel.markets")

SNAPSHOT = [
    ("^GSPC", "S&P 500", "index"), ("^IXIC", "Nasdaq", "index"), ("^DJI", "Dow Jones", "index"),
    ("^GSPTSE", "S&P/TSX", "index"), ("ES=F", "S&P 500 futures", "futures"), ("NQ=F", "Nasdaq futures", "futures"),
    ("^STOXX50E", "Euro Stoxx 50", "index"), ("^FTSE", "FTSE 100", "index"), ("^N225", "Nikkei 225", "index"),
    ("^HSI", "Hang Seng", "index"), ("^VIX", "VIX (volatility)", "level"), ("^TNX", "US 10-year yield", "yield"),
    ("CAD=X", "USD/CAD", "fx"), ("EURUSD=X", "EUR/USD", "fx"), ("DX-Y.NYB", "US dollar index", "index"),
    ("CL=F", "WTI crude", "commodity"), ("BZ=F", "Brent crude", "commodity"), ("GC=F", "Gold", "commodity"),
    ("SI=F", "Silver", "commodity"), ("HG=F", "Copper", "commodity"), ("BTC-USD", "Bitcoin", "crypto"),
]


def snapshot():
    """[{ticker, name, kind, last, chg_pct, date}] - last value vs the previous session's close."""
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed: no market snapshot")
        return []
    tick = [t for t, _, _ in SNAPSHOT]
    try:
        df = yf.download(tick, period="7d", interval="1d", group_by="ticker", progress=False, threads=True,
                         auto_adjust=False)
    except Exception as e:
        log.warning("market snapshot failed: %s", e)
        return []
    out = []
    for t, name, kind in SNAPSHOT:
        try:
            c = df[t]["Close"].dropna()
            if len(c) < 2:
                continue
            last, prev = float(c.iloc[-1]), float(c.iloc[-2])
            chg = (last - prev) if kind == "yield" else (last / prev - 1) * 100
            # ^TNX is quoted x10 on older feeds (e.g. 45.1 = 4.51%)
            if kind == "yield" and last > 20:
                last, chg = last / 10, chg / 10
            out.append(dict(ticker=t, name=name, kind=kind, last=round(last, 4 if kind == "fx" else 2),
                            chg=round(chg, 2), date=str(c.index[-1].date())))
        except Exception:
            continue
    return out


def as_text(snap):
    """Compact lines for the AI prompt."""
    lines = []
    for s in snap:
        unit = " pts" if s["kind"] == "yield" else "%"
        lines.append(f"{s['name']}: {s['last']} ({s['chg']:+.2f}{unit} vs previous close, data date {s['date']})")
    return "\n".join(lines)
