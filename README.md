# Indian Stocks MCP

A free, **end-of-day** market-data MCP server for NSE/BSE stocks. Built for a
"daily trend update" workflow — not live/intraday trading. It gives you last
close, % change vs the previous session, volume vs 20-day average, moving
averages (SMA 20/50/200) and RSI(14), all computed from daily candles.

**Data sources (both free, no account, no API key):**
- **yfinance** (Yahoo Finance) — primary; covers most NSE (`.NS`) and BSE (`.BO`) names, including many small-caps.
- **NSE/BSE bhavcopy** — fallback for names yfinance misses (e.g. some SME listings). Latest session only, no indicators.

> This tool returns **descriptive market data, not investment advice.** Trend
> readings are mechanical descriptions of where price sits relative to its
> moving averages and RSI — not buy/sell signals.

---

## Tools

| Tool | What it does |
|------|--------------|
| `indianstocks_resolve_symbol` | Company name / partial → correct `.NS`/`.BO` ticker. **Use first when unsure** — avoids matching an unrelated company. |
| `indianstocks_get_daily_trend` | Daily-trend reading for a list of symbols (close, % change, volume vs avg, SMA20/50/200, RSI14, trend text). |
| `indianstocks_get_history` | Raw daily OHLC+volume candles for one symbol. |
| `indianstocks_watchlist_add` | Add symbols to the saved watchlist (`watchlist.json`). |
| `indianstocks_watchlist_remove` | Remove symbols from the watchlist. |
| `indianstocks_watchlist_get` | Show the watchlist; with `with_trend=true` it fetches the daily update for every name in **one call**. |

### The daily-update workflow
1. `indianstocks_resolve_symbol` for any name you're unsure of.
2. `indianstocks_watchlist_add` the tickers (your list can change any time).
3. Each morning: `indianstocks_watchlist_get` with `with_trend=true` → your whole watchlist's end-of-day trend in one shot.

---

## Setup

```bash
cd indianstocks_mcp
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Test it runs:
```bash
python server.py                   # starts on stdio; Ctrl-C to stop
```
Or inspect with the official tool:
```bash
npx @modelcontextprotocol/inspector python server.py
```

---

## Connecting it to Claude

The server speaks MCP over **stdio** by default (good for local desktop
clients). How you attach it depends on where you use Claude:

- **Claude Desktop / a local MCP client:** point the client's MCP config at
  `python /full/path/to/indianstocks_mcp/server.py` (using the venv's Python).
  A typical config entry:
  ```json
  {
    "mcpServers": {
      "indianstocks": {
        "command": "/full/path/to/indianstocks_mcp/.venv/bin/python",
        "args": ["/full/path/to/indianstocks_mcp/server.py"]
      }
    }
  }
  ```

- **The web/mobile Claude app (custom connector):** a remote connector needs
  **HTTP transport and a public URL**, not stdio. Switch the last line of
  `server.py` to:
  ```python
  mcp.run(transport="streamable_http", port=8000)
  ```
  then host it somewhere reachable (a small cloud VM, or a tunnel like
  `cloudflared`/`ngrok` for testing) and add that URL as a custom connector.
  **This hosting step is the one part that lives on your side** — the server is
  ready for it, but it has to run somewhere the app can reach.

---

## Honest limitations

- **End-of-day only.** No live or 15-min data. The newest candle is the last
  completed session. That's by design (free sources), and matches a daily-trend
  workflow — it is *not* suitable for intraday trading decisions.
- **BSE SME coverage is the weak spot.** yfinance may lack some SME names; the
  bhavcopy fallback helps but returns latest-close only (no indicators).
- **Bhavcopy URLs drift.** NSE/BSE occasionally change their report URLs/format.
  If the fallback stops working, update `NSE_BHAVCOPY_URL` / `BSE_BHAVCOPY_URL`
  in `data_layer.py` from the exchange's current "All Reports" page.
- **Not advice.** Indicators describe price position; they are not
  recommendations, and past patterns don't predict future moves.

---

## Files
- `server.py` — MCP server (tools, watchlist, formatting).
- `data_layer.py` — data fetching (yfinance + bhavcopy) and indicator math.
- `requirements.txt` — dependencies.
- `watchlist.json` — created on first `watchlist_add`.
