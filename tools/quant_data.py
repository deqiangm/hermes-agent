#!/usr/bin/env python3
"""
Quant Data Tool — Fetch market data across asset classes.

Supported asset classes:
  crypto (default) — via CCXT (OKX, Binance, etc.)
  stock            — via yfinance (AAPL, MSFT, TSLA, etc.)
  fx               — via yfinance (EURUSD=X, GBPUSD=X, etc.)

Actions:
 ticker — latest price for a symbol
 ohlcv — OHLCV klines (candlestick data)
 orderbook — order book depth (crypto only)
 markets — list available markets on the exchange (crypto only)
"""

import json
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_ccxt():
    """Return True if ccxt is importable."""
    try:
        import ccxt # noqa: F401
        return True
    except ImportError:
        return False


def _check_yfinance():
    """Return True if yfinance is importable."""
    try:
        import yfinance # noqa: F401
        return True
    except ImportError:
        return False


def _check_quant_data():
    """Return True if at least one data source (ccxt or yfinance) is available."""
    return _check_ccxt() or _check_yfinance()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# yfinance helpers (stock & fx)
# ---------------------------------------------------------------------------

def _yf_timeframe_map(timeframe: str) -> str:
    """Map our timeframe strings to yfinance interval strings."""
    mapping = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "1h": "1h",
        "4h": "1h",   # yfinance has no 4h; we resample later
        "1d": "1d",
        "1w": "1wk",
        "1M": "1mo",
    }
    return mapping.get(timeframe, "1h")


def _yf_symbol(symbol: str, asset_class: str) -> str:
    """Convert symbol to yfinance format based on asset_class.

    crypto: 'BTC/USDT' -> used as-is (not applicable for yfinance path)
    stock:  'AAPL'     -> 'AAPL'
    fx:     'EUR/USD'  -> 'EURUSD=X'  (yfinance FX convention)
    """
    if asset_class == "fx":
        # Accept both "EUR/USD" and "EURUSD=X" formats
        if "=X" in symbol:
            return symbol
        # Convert "EUR/USD" -> "EURUSD=X"
        return symbol.replace("/", "") + "=X"
    return symbol  # stock: use as-is


def _resample_4h(df):
    """Resample a 1h yfinance DataFrame to 4h candles."""
    import pandas as pd
    df_4h = df.resample("4h", closed="left", label="left").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna()
    return df_4h


def _handle_ticker_yfinance(symbol: str, asset_class: str) -> str:
    """Fetch ticker data via yfinance (stock or fx)."""
    import yfinance as yf

    yf_symbol = _yf_symbol(symbol, asset_class)
    try:
        tk = yf.Ticker(yf_symbol)
        info = tk.info
    except Exception as e:
        return json.dumps({
            "error": f"Failed to fetch ticker for '{symbol}' ({asset_class}): {e}",
            "status": "error",
        }, ensure_ascii=False)

    # yfinance info keys vary; extract what we can with fallbacks
    current_price = info.get("currentPrice") or info.get("regularMarketPrice")
    previous_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
    volume = info.get("volume") or info.get("regularMarketVolume")
    day_high = info.get("dayHigh") or info.get("regularMarketDayHigh")
    day_low = info.get("dayLow") or info.get("regularMarketDayLow")
    open_price = info.get("open") or info.get("regularMarketOpen")

    # Compute change from previous close if not directly available
    if current_price and previous_close:
        change = current_price - previous_close
        percentage = (change / previous_close) * 100.0 if previous_close else None
    else:
        change = None
        percentage = None

    return json.dumps({
        "status": "ok",
        "asset_class": asset_class,
        "symbol": symbol,
        "data": {
            "symbol": yf_symbol,
            "last": current_price,
            "bid": None,
            "ask": None,
            "high": day_high,
            "low": day_low,
            "open": open_price,
            "close": current_price,
            "change": round(change, 6) if change is not None else None,
            "percentage": round(percentage, 4) if percentage is not None else None,
            "baseVolume": volume,
            "quoteVolume": None,
            "timestamp": None,
            "datetime": None,
        },
    }, ensure_ascii=False)


def _handle_ohlcv_yfinance(symbol: str, timeframe: str, limit: int, asset_class: str) -> str:
    """Fetch OHLCV data via yfinance (stock or fx)."""
    import yfinance as yf

    yf_symbol = _yf_symbol(symbol, asset_class)
    yf_interval = _yf_timeframe_map(timeframe)
    need_4h_resample = (timeframe == "4h")

    # yfinance period limits: 1m/5m/15m max 60d, 1h max 730d, 1d max unlimited
    # Convert limit (number of candles) to a period string
    interval_minutes = {
        "1m": 1, "5m": 5, "15m": 15, "1h": 60,
        "4h": 240, "1d": 1440, "1w": 10080, "1M": 43200,
    }
    minutes_per_candle = interval_minutes.get(timeframe, 60)
    days_needed = max(int(limit * minutes_per_candle / 1440) + 5, 1)

    # Cap period to yfinance limits
    if yf_interval in ("1m", "5m", "15m"):
        days_needed = min(days_needed, 60)
        period = f"{days_needed}d"
    elif yf_interval in ("1h",):
        days_needed = min(days_needed, 730)
        period = f"{days_needed}d"
    elif yf_interval in ("1d",):
        period = f"{min(days_needed, 3650)}d"
    elif yf_interval in ("1wk",):
        period = f"{max(days_needed // 7, 1)}wk"
    elif yf_interval in ("1mo",):
        period = f"{max(days_needed // 30, 1)}mo"
    else:
        period = f"{days_needed}d"

    try:
        tk = yf.Ticker(yf_symbol)
        hist = tk.history(period=period, interval=yf_interval)
    except Exception as e:
        return json.dumps({
            "error": f"Failed to fetch OHLCV for '{symbol}' ({asset_class}): {e}",
            "status": "error",
        }, ensure_ascii=False)

    if hist.empty:
        return json.dumps({
            "error": f"No OHLCV data returned for '{symbol}' ({asset_class}). Symbol may be invalid.",
            "status": "error",
        }, ensure_ascii=False)

    # Resample to 4h if requested
    if need_4h_resample:
        hist = _resample_4h(hist)

    # Convert yfinance DataFrame to CCXT-compatible format
    candles = []
    for idx, row in hist.iterrows():
        # yfinance timestamps are timezone-aware DatetimeIndex
        ts_ms = int(idx.timestamp() * 1000)
        candles.append({
            "timestamp": ts_ms,
            "open": round(float(row["Open"]), 6),
            "high": round(float(row["High"]), 6),
            "low": round(float(row["Low"]), 6),
            "close": round(float(row["Close"]), 6),
            "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
        })

    # Trim to requested limit
    candles = candles[-limit:]

    return json.dumps({
        "status": "ok",
        "asset_class": asset_class,
        "symbol": symbol,
        "timeframe": timeframe,
        "count": len(candles),
        "data": candles,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _handle_quant_data(args: dict, **kw) -> str:
    """Dispatch quant_data actions based on asset_class."""
    action = args.get("action", "ticker")
    asset_class = args.get("asset_class", "crypto")
    exchange = args.get("exchange", "okx")
    symbol = args.get("symbol", "BTC/USDT")
    timeframe = args.get("timeframe", "1h")
    limit = int(args.get("limit", 100))

    # ------------------------------------------------------------------
    # Stock / FX path  (yfinance)
    # ------------------------------------------------------------------
    if asset_class in ("stock", "fx"):
        import yfinance  # ensure available (checked at registration)

        if action == "ticker":
            return _handle_ticker_yfinance(symbol, asset_class)
        elif action == "ohlcv":
            return _handle_ohlcv_yfinance(symbol, timeframe, limit, asset_class)
        elif action in ("orderbook", "markets"):
            return json.dumps({
                "error": f"Action '{action}' is only supported for asset_class='crypto'. "
                         f"For {asset_class}, use 'ticker' or 'ohlcv'.",
                "status": "error",
            }, ensure_ascii=False)
        else:
            return json.dumps({
                "error": f"Unknown action '{action}'. Valid: ticker, ohlcv",
                "status": "error",
            }, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Crypto path  (CCXT — existing logic)
    # ------------------------------------------------------------------
    import ccxt

    # --- Create exchange instance ---
    exchange_lower = exchange.lower()
    if not hasattr(ccxt, exchange_lower):
        return json.dumps({
            "error": f"Unsupported exchange: {exchange}",
            "status": "error",
        }, ensure_ascii=False)

    try:
        ex = getattr(ccxt, exchange_lower)()
    except Exception as e:
        return json.dumps({
            "error": f"Failed to create exchange '{exchange}': {e}",
            "status": "error",
        }, ensure_ascii=False)

    try:
        # --- ticker ---
        if action == "ticker":
            ticker = ex.fetch_ticker(symbol)
            return json.dumps({
                "status": "ok",
                "asset_class": "crypto",
                "exchange": exchange,
                "symbol": symbol,
                "data": {
                    "symbol": ticker.get("symbol"),
                    "last": ticker.get("last"),
                    "bid": ticker.get("bid"),
                    "ask": ticker.get("ask"),
                    "high": ticker.get("high"),
                    "low": ticker.get("low"),
                    "open": ticker.get("open"),
                    "close": ticker.get("close"),
                    "change": ticker.get("change"),
                    "percentage": ticker.get("percentage"),
                    "baseVolume": ticker.get("baseVolume"),
                    "quoteVolume": ticker.get("quoteVolume"),
                    "timestamp": ticker.get("timestamp"),
                    "datetime": ticker.get("datetime"),
                },
            }, ensure_ascii=False)

        # --- ohlcv ---
        elif action == "ohlcv":
            valid_timeframes = ["1m", "5m", "15m", "1h", "4h", "1d", "1w", "1M"]
            if timeframe not in valid_timeframes:
                return json.dumps({
                    "error": f"Invalid timeframe '{timeframe}'. Valid: {valid_timeframes}",
                    "status": "error",
                }, ensure_ascii=False)

            ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            # Convert lists to labeled dicts for readability
            candles = []
            for c in ohlcv:
                candles.append({
                    "timestamp": c[0],
                    "open": c[1],
                    "high": c[2],
                    "low": c[3],
                    "close": c[4],
                    "volume": c[5],
                })
            return json.dumps({
                "status": "ok",
                "asset_class": "crypto",
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "count": len(candles),
                "data": candles,
            }, ensure_ascii=False)

        # --- orderbook ---
        elif action == "orderbook":
            orderbook = ex.fetch_order_book(symbol)
            return json.dumps({
                "status": "ok",
                "asset_class": "crypto",
                "exchange": exchange,
                "symbol": symbol,
                "data": {
                    "bids": orderbook.get("bids", [])[:20],
                    "asks": orderbook.get("asks", [])[:20],
                    "timestamp": orderbook.get("timestamp"),
                    "datetime": orderbook.get("datetime"),
                    "nonce": orderbook.get("nonce"),
                },
            }, ensure_ascii=False)

        # --- markets ---
        elif action == "markets":
            markets = ex.load_markets()
            # Return summary list (not the full dict — too large)
            market_list = sorted(markets.keys()) if isinstance(markets, dict) else []
            # Provide a compact summary per market
            summary = []
            for mkt_name in market_list[:200]: # cap at 200
                mkt = markets[mkt_name]
                summary.append({
                    "symbol": mkt.get("symbol"),
                    "base": mkt.get("base"),
                    "quote": mkt.get("quote"),
                    "type": mkt.get("type"),
                    "active": mkt.get("active"),
                })
            return json.dumps({
                "status": "ok",
                "asset_class": "crypto",
                "exchange": exchange,
                "total_markets": len(market_list),
                "showing": len(summary),
                "data": summary,
            }, ensure_ascii=False)

        else:
            return json.dumps({
                "error": f"Unknown action '{action}'. Valid: ticker, ohlcv, orderbook, markets",
                "status": "error",
            }, ensure_ascii=False)

    except ccxt.BadSymbol as e:
        return json.dumps({
            "error": f"Bad symbol '{symbol}' on {exchange}: {e}",
            "status": "error",
        }, ensure_ascii=False)
    except ccxt.NetworkError as e:
        return json.dumps({
            "error": f"Network error on {exchange}: {e}",
            "status": "error",
        }, ensure_ascii=False)
    except ccxt.ExchangeError as e:
        return json.dumps({
            "error": f"Exchange error on {exchange}: {e}",
            "status": "error",
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("quant_data unexpected error: %s", e)
        return json.dumps({
            "error": f"Unexpected error: {type(e).__name__}: {e}",
            "status": "error",
        }, ensure_ascii=False)
    finally:
        try:
            ex.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Schema (OpenAI function-calling format)
# ---------------------------------------------------------------------------

QUANT_DATA_SCHEMA = {
 "name": "quant_data",
 "description": (
    "Fetch market data across asset classes. "
    "Actions: 'ticker' (latest price), 'ohlcv' (OHLCV candlestick/kline data), "
    "'orderbook' (order book depth, crypto only), 'markets' (list available markets, crypto only). "
    "asset_class='crypto' (default) uses CCXT (exchange like OKX, Binance). "
    "asset_class='stock' uses yfinance (symbols: AAPL, MSFT, TSLA, etc.). "
    "asset_class='fx' uses yfinance with FX pairs (symbols: EUR/USD, GBP/USD -> EURUSD=X, GBPUSD=X). "
    "Default exchange is OKX (for crypto). Default symbol is BTC/USDT."
 ),
 "parameters": {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["ticker", "ohlcv", "orderbook", "markets"],
            "description": (
                "Action to perform: 'ticker' for latest price, "
                "'ohlcv' for candlestick/kline data, "
                "'orderbook' for order book depth (crypto only), "
                "'markets' to list available markets (crypto only)."
            ),
        },
        "asset_class": {
            "type": "string",
            "enum": ["crypto", "stock", "fx"],
            "description": (
                "Asset class: 'crypto' (CCXT exchanges, default), "
                "'stock' (yfinance: AAPL, MSFT, TSLA), "
                "'fx' (yfinance: EUR/USD, GBP/USD). "
                "Controls which data source is used."
            ),
            "default": "crypto",
        },
        "exchange": {
            "type": "string",
            "description": "Exchange name for crypto (e.g. 'okx', 'binance'). Default: 'okx'. Ignored for stock/fx.",
            "default": "okx",
        },
        "symbol": {
            "type": "string",
            "description": (
                "Trading symbol. Crypto: 'BTC/USDT'. Stock: 'AAPL'. "
                "FX: 'EUR/USD' or 'EURUSD=X'. Default: 'BTC/USDT'."
            ),
            "default": "BTC/USDT",
        },
        "timeframe": {
            "type": "string",
            "enum": ["1m", "5m", "15m", "1h", "4h", "1d", "1w", "1M"],
            "description": "Candle timeframe for ohlcv action. Default: '1h'.",
            "default": "1h",
        },
        "limit": {
            "type": "integer",
            "description": "Number of candles to fetch for ohlcv action. Default: 100.",
            "default": 100,
        },
    },
    "required": ["action"],
 },
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

from tools.registry import registry

registry.register(
 name="quant_data",
 toolset="quant",
 schema=QUANT_DATA_SCHEMA,
 handler=_handle_quant_data,
 check_fn=_check_quant_data,
 requires_env=[],
 is_async=False,
 description="Fetch market data (ticker, OHLCV, orderbook, markets) across crypto (CCXT), stocks (yfinance), and FX (yfinance).",
 emoji="📊",
)
