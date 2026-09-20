"""
server.py — Indian Stocks MCP server (end-of-day / daily-trend focus).

Exposes tools for tracking a changeable watchlist of NSE/BSE stocks and getting
daily-trend readings computed from end-of-day candles. Data is FREE (yfinance +
NSE/BSE bhavcopy fallback) and END-OF-DAY, not live/intraday.

Tools:
  - indianstocks_resolve_symbol   : company name/partial -> correct .NS/.BO ticker
  - indianstocks_get_daily_trend  : price action + indicators for given symbols
  - indianstocks_get_history      : raw daily OHLC candles for one symbol
  - indianstocks_watchlist_add    : add symbols to the saved watchlist
  - indianstocks_watchlist_remove : remove symbols from the saved watchlist
  - indianstocks_watchlist_get    : list the saved watchlist (and, optionally,
                                     get the daily trend for every name in it)

Run locally:   python server.py
"""

from __future__ import annotations

import json
import pathlib
from enum import Enum
from typing import List, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, ConfigDict

import data_layer as dl

mcp = FastMCP(
    "indianstocks_mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

WATCHLIST_PATH = pathlib.Path(__file__).parent / "watchlist.json"

READONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,  # reaches out to Yahoo / NSE / BSE
}
MUTATING = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


# ---------------------------------------------------------------------------
# Watchlist persistence helpers (shared, not duplicated across tools)
# ---------------------------------------------------------------------------

def _load_watchlist() -> List[str]:
    if not WATCHLIST_PATH.exists():
        return []
    try:
        return json.loads(WATCHLIST_PATH.read_text()).get("symbols", [])
    except Exception:
        return []


def _save_watchlist(symbols: List[str]) -> None:
    # de-dup, preserve order, upper-case
    seen, ordered = set(), []
    for s in symbols:
        u = s.upper().strip()
        if u and u not in seen:
            seen.add(u)
            ordered.append(u)
    WATCHLIST_PATH.write_text(json.dumps({"symbols": ordered}, indent=2))


def _fmt_trend_md(rec: dict) -> str:
    if "error" in rec:
        return f"- **{rec['symbol']}** — {rec['error']}"
    chg = f"{rec['change_pct']:+.2f}%" if rec.get("change_pct") is not None else "n/a"
    vol_line = f"{rec['volume']:,}" if rec.get("volume") else "n/a"
    if rec.get("volume_vs_avg"):
        vol_line += f" ({rec['volume_vs_avg']}x 20-day avg)"
    return (
        f"**{rec['symbol']}** — ₹{rec['close']} ({chg})  _[{rec['source']}, "
        f"as of {rec['as_of']}]_\n"
        f"  - Volume: {vol_line}\n"
        f"  - SMA20/50/200: {rec.get('sma20')} / {rec.get('sma50')} / {rec.get('sma200')}\n"
        f"  - RSI(14): {rec.get('rsi14')}\n"
        f"  - Trend: {rec['trend']}"
    )


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class ResolveInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(..., description="Company name or partial ticker, e.g. 'Rajesh Power' or 'HALDYN'", min_length=1, max_length=100)


class TrendInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    symbols: List[str] = Field(..., description="Yahoo tickers, e.g. ['PRINCEPIPE.NS','IVP.BO']. Use resolve_symbol if unsure.", min_items=1, max_items=50)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' for reading, 'json' for machine use")


class HistoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    symbol: str = Field(..., description="Single Yahoo ticker, e.g. 'ATLANTAELE.NS'", min_length=1, max_length=30)
    days: int = Field(default=60, description="Number of recent trading days of daily candles", ge=2, le=400)


class WatchlistEditInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    symbols: List[str] = Field(..., description="Yahoo tickers to add/remove", min_items=1, max_items=50)


class WatchlistGetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    with_trend: bool = Field(default=False, description="If true, also fetch the daily trend for every saved symbol")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="'markdown' or 'json'")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(name="indianstocks_resolve_symbol", annotations={"title": "Resolve ticker", **READONLY})
async def resolve_symbol(params: ResolveInput) -> str:
    """Resolve a company name or partial ticker to the correct NSE (.NS) / BSE (.BO)
    Yahoo ticker. Use this first when unsure, to avoid matching an unrelated
    company's symbol. Returns a JSON list of {symbol, name, exchange} candidates."""
    matches = dl.resolve_symbol(params.query)
    if not matches:
        return json.dumps({"query": params.query, "matches": [],
                           "hint": "No Indian match. Try the company's exact name."}, indent=2)
    return json.dumps({"query": params.query, "matches": matches}, indent=2)


@mcp.tool(name="indianstocks_get_daily_trend", annotations={"title": "Daily trend", **READONLY})
async def get_daily_trend(params: TrendInput) -> str:
    """Get the end-of-day daily-trend reading for one or more symbols: last close,
    % change vs previous session, volume vs 20-day average, SMA20/50/200, RSI(14),
    and a plain-language trend description. END-OF-DAY data only (not live/intraday).
    Indicators are computed from daily candles. This is descriptive data, NOT
    investment advice or a buy/sell signal."""
    records = [dl.build_daily_trend(s.upper().strip()) for s in params.symbols]
    if params.response_format == ResponseFormat.JSON:
        return json.dumps({"count": len(records), "results": records}, indent=2)
    header = "## Daily trend (end-of-day data)\n"
    body = "\n\n".join(_fmt_trend_md(r) for r in records)
    footer = ("\n\n_Data is end-of-day, not live. Indicators are mechanical "
              "descriptions of price position, not buy/sell recommendations._")
    return header + body + footer


@mcp.tool(name="indianstocks_get_history", annotations={"title": "Daily candles", **READONLY})
async def get_history(params: HistoryInput) -> str:
    """Return raw daily OHLC+volume candles for one symbol over the last `days`
    trading days, as JSON. Useful for charting or custom indicator work.
    END-OF-DAY data only."""
    hist = dl.fetch_yfinance(params.symbol.upper().strip(), days=params.days)
    if hist is None:
        return json.dumps({"symbol": params.symbol,
                           "error": "No daily history (try resolve_symbol first)."}, indent=2)
    hist = hist.tail(params.days)
    candles = [{
        "date": idx.date().isoformat(),
        "open": round(float(row["Open"]), 2),
        "high": round(float(row["High"]), 2),
        "low": round(float(row["Low"]), 2),
        "close": round(float(row["Close"]), 2),
        "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else None,
    } for idx, row in hist.iterrows()]
    return json.dumps({"symbol": params.symbol, "count": len(candles), "candles": candles}, indent=2)


@mcp.tool(name="indianstocks_watchlist_add", annotations={"title": "Add to watchlist", **MUTATING})
async def watchlist_add(params: WatchlistEditInput) -> str:
    """Add one or more symbols to the persistent watchlist (stored on disk).
    Returns the full updated watchlist."""
    wl = _load_watchlist()
    wl.extend(s.upper().strip() for s in params.symbols)
    _save_watchlist(wl)
    return json.dumps({"watchlist": _load_watchlist()}, indent=2)


@mcp.tool(name="indianstocks_watchlist_remove", annotations={"title": "Remove from watchlist", **MUTATING})
async def watchlist_remove(params: WatchlistEditInput) -> str:
    """Remove one or more symbols from the persistent watchlist.
    Returns the full updated watchlist."""
    drop = {s.upper().strip() for s in params.symbols}
    _save_watchlist([s for s in _load_watchlist() if s not in drop])
    return json.dumps({"watchlist": _load_watchlist()}, indent=2)


@mcp.tool(name="indianstocks_watchlist_get", annotations={"title": "Get watchlist", **READONLY})
async def watchlist_get(params: WatchlistGetInput) -> str:
    """Return the saved watchlist. If with_trend=true, also fetch the daily-trend
    reading for every symbol in it — the one-call 'daily update' for your whole list."""
    wl = _load_watchlist()
    if not params.with_trend:
        return json.dumps({"watchlist": wl}, indent=2)
    records = [dl.build_daily_trend(s) for s in wl]
    if params.response_format == ResponseFormat.JSON:
        return json.dumps({"watchlist": wl, "results": records}, indent=2)
    if not wl:
        return "Your watchlist is empty. Add symbols with indianstocks_watchlist_add."
    header = "## Watchlist daily update (end-of-day)\n"
    body = "\n\n".join(_fmt_trend_md(r) for r in records)
    return header + body + "\n\n_End-of-day data. Descriptive only, not advice._"


if __name__ == "__main__":
    mcp.run()
