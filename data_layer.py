"""
data_layer.py — Data fetching and indicator computation for the Indian stocks MCP.

Two data sources, tried in order:
  1. yfinance (Yahoo Finance)      -> primary; good for most NSE (.NS) and BSE (.BO) names
  2. NSE/BSE end-of-day bhavcopy   -> fallback for small/SME names yfinance misses

All indicators (SMA, RSI) are computed here from daily OHLC candles, so the
numbers are exact and reproducible rather than scraped from a third-party site.

Freshness note: this module is built for END-OF-DAY / daily-trend use. It does
NOT provide live or 15-min-delayed intraday data. The most recent candle is the
last completed trading session.
"""

from __future__ import annotations

import io
import zipfile
import datetime as dt
from typing import Optional

import pandas as pd
import requests
import yfinance as yf

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUEST_TIMEOUT = 20  # seconds

# NSE UDiFF end-of-day bhavcopy (format used from 2024 onwards).
# NOTE: NSE changes these paths occasionally. If the fallback stops working,
# verify the current URL from the NSE "All Reports" page and update this.
NSE_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)

# BSE end-of-day bhavcopy (UDiFF format).
BSE_BHAVCOPY_URL = (
    "https://www.bseindia.com/download/BhavCopy/Equity/"
    "BhavCopy_BSE_CM_0_0_0_{yyyymmdd}_F_0000.CSV"
)

# NSE blocks bare requests; it needs a browser-like session that has first
# visited the homepage to pick up cookies.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Indicator math (pure functions, no I/O)
# ---------------------------------------------------------------------------

def sma(series: pd.Series, window: int) -> Optional[float]:
    """Simple moving average of the last `window` closes. None if not enough data."""
    if len(series) < window:
        return None
    return round(float(series.tail(window).mean()), 2)


def rsi(series: pd.Series, period: int = 14) -> Optional[float]:
    """Wilder's RSI on closing prices. None if not enough data."""
    if len(series) < period + 1:
        return None
    delta = series.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi_series = 100 - (100 / (1 + rs))
    val = rsi_series.iloc[-1]
    return None if pd.isna(val) else round(float(val), 2)


def trend_read(close: float, s20: Optional[float], s50: Optional[float],
               s200: Optional[float], rsi_val: Optional[float]) -> str:
    """Plain-language, mechanical description of where price sits. NOT advice."""
    parts = []
    if s50 is not None:
        parts.append("above 50-DMA" if close > s50 else "below 50-DMA")
    if s200 is not None:
        parts.append("above 200-DMA (long-term uptrend)" if close > s200
                     else "below 200-DMA (long-term downtrend)")
    if rsi_val is not None:
        if rsi_val >= 70:
            parts.append(f"RSI {rsi_val} (overbought)")
        elif rsi_val <= 30:
            parts.append(f"RSI {rsi_val} (oversold)")
        else:
            parts.append(f"RSI {rsi_val} (neutral)")
    return "; ".join(parts) if parts else "insufficient history for indicators"


# ---------------------------------------------------------------------------
# Source 1: yfinance
# ---------------------------------------------------------------------------

def fetch_yfinance(symbol: str, days: int = 400) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLC candles from yfinance.

    `symbol` must be a Yahoo-style ticker: e.g. 'PRINCEPIPE.NS' (NSE) or
    'IVP.BO' (BSE). Returns a DataFrame indexed by date with columns
    Open/High/Low/Close/Volume, or None if no data.
    """
    period = f"{max(days, 5)}d"
    try:
        hist = yf.Ticker(symbol).history(period=period, auto_adjust=False)
    except Exception:
        return None
    if hist is None or hist.empty:
        return None
    hist = hist.rename(columns=str.title)  # normalise column names
    keep = ["Open", "High", "Low", "Close", "Volume"]
    hist = hist[[c for c in keep if c in hist.columns]].dropna(subset=["Close"])
    return hist if not hist.empty else None


def resolve_symbol(query: str, max_results: int = 8) -> list[dict]:
    """
    Resolve a company name or partial ticker to candidate Yahoo tickers,
    restricted to Indian exchanges (.NS / .BO). This exists to avoid the
    wrong-ticker trap (e.g. a name matching an unrelated company's symbol).

    Returns a list of {symbol, name, exchange} dicts.
    """
    out: list[dict] = []
    try:
        res = yf.Search(query, max_results=max_results).quotes or []
    except Exception:
        res = []
    for q in res:
        sym = q.get("symbol", "")
        if sym.endswith(".NS") or sym.endswith(".BO"):
            out.append({
                "symbol": sym,
                "name": q.get("shortname") or q.get("longname") or "",
                "exchange": q.get("exchange", ""),
            })
    return out


# ---------------------------------------------------------------------------
# Source 2: NSE / BSE bhavcopy (fallback, end-of-day only)
# ---------------------------------------------------------------------------

def _last_trading_day(ref: Optional[dt.date] = None) -> dt.date:
    """Most recent weekday on/before `ref` (ignores exchange holidays)."""
    d = ref or dt.date.today()
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= dt.timedelta(days=1)
    return d


def fetch_bhavcopy_row(symbol_or_name: str, exchange: str = "NSE",
                       ref_date: Optional[dt.date] = None) -> Optional[dict]:
    """
    Fetch a single stock's end-of-day row from the exchange bhavcopy.

    This is the fallback for SME / thinly-covered names yfinance lacks.
    `symbol_or_name` is matched against the bhavcopy's trading-symbol column.
    Only returns the latest completed session (no history / no indicators).
    Returns {close, open, high, low, volume, date, source} or None.
    """
    day = _last_trading_day(ref_date)
    stamp = day.strftime("%Y%m%d")
    sess = requests.Session()
    sess.headers.update(BROWSER_HEADERS)

    try:
        if exchange.upper() == "NSE":
            sess.get("https://www.nseindia.com", timeout=REQUEST_TIMEOUT)  # cookies
            url = NSE_BHAVCOPY_URL.format(yyyymmdd=stamp)
            r = sess.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                with zf.open(zf.namelist()[0]) as f:
                    df = pd.read_csv(f)
            sym_col = "TckrSymb"
            close_col, open_col = "ClsPric", "OpnPric"
            high_col, low_col, vol_col = "HghPric", "LwPric", "TtlTradgVol"
        else:  # BSE
            url = BSE_BHAVCOPY_URL.format(yyyymmdd=stamp)
            r = sess.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            df = pd.read_csv(io.BytesIO(r.content))
            sym_col = "TckrSymb"
            close_col, open_col = "ClsPric", "OpnPric"
            high_col, low_col, vol_col = "HghPric", "LwPric", "TtlTradgVol"
    except Exception:
        return None

    key = symbol_or_name.upper().strip()
    if sym_col not in df.columns:
        return None
    match = df[df[sym_col].astype(str).str.upper() == key]
    if match.empty:
        return None
    row = match.iloc[0]

    def g(col):
        try:
            return round(float(row[col]), 2)
        except Exception:
            return None

    return {
        "close": g(close_col),
        "open": g(open_col),
        "high": g(high_col),
        "low": g(low_col),
        "volume": int(row[vol_col]) if vol_col in df.columns and pd.notna(row[vol_col]) else None,
        "date": day.isoformat(),
        "source": f"{exchange.upper()} bhavcopy",
    }


# ---------------------------------------------------------------------------
# Unified daily-trend builder
# ---------------------------------------------------------------------------

def build_daily_trend(symbol: str) -> dict:
    """
    Build a full daily-trend record for one symbol, trying yfinance first and
    falling back to bhavcopy. Returns a dict with price action + indicators,
    or {'symbol', 'error'} if nothing could be fetched.
    """
    hist = fetch_yfinance(symbol)
    if hist is not None and len(hist) >= 2:
        closes = hist["Close"]
        last, prev = hist.iloc[-1], hist.iloc[-2]
        close = round(float(last["Close"]), 2)
        prev_close = round(float(prev["Close"]), 2)
        chg_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else None
        vol = int(last["Volume"]) if pd.notna(last["Volume"]) else None
        avg_vol_20 = int(hist["Volume"].tail(20).mean()) if len(hist) >= 20 else None
        s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
        rsi_val = rsi(closes, 14)
        return {
            "symbol": symbol,
            "source": "yfinance",
            "as_of": hist.index[-1].date().isoformat(),
            "close": close,
            "prev_close": prev_close,
            "change_pct": chg_pct,
            "day_high": round(float(last["High"]), 2),
            "day_low": round(float(last["Low"]), 2),
            "volume": vol,
            "avg_volume_20d": avg_vol_20,
            "volume_vs_avg": (round(vol / avg_vol_20, 2) if vol and avg_vol_20 else None),
            "sma20": s20, "sma50": s50, "sma200": s200,
            "rsi14": rsi_val,
            "trend": trend_read(close, s20, s50, s200, rsi_val),
            "history_days": len(hist),
        }

    # Fallback: bhavcopy (latest session only, no indicators)
    for exch in ("NSE", "BSE"):
        row = fetch_bhavcopy_row(symbol.split(".")[0], exchange=exch)
        if row and row.get("close") is not None:
            return {
                "symbol": symbol,
                "source": row["source"],
                "as_of": row["date"],
                "close": row["close"],
                "prev_close": None,
                "change_pct": None,
                "day_high": row["high"],
                "day_low": row["low"],
                "volume": row["volume"],
                "avg_volume_20d": None,
                "volume_vs_avg": None,
                "sma20": None, "sma50": None, "sma200": None,
                "rsi14": None,
                "trend": "latest close only (bhavcopy fallback; no history for indicators)",
                "history_days": 1,
            }

    return {
        "symbol": symbol,
        "error": ("No data from yfinance or bhavcopy. Check the ticker with "
                  "resolve_symbol (Indian tickers end in .NS or .BO)."),
    }
