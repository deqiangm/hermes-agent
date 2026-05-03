#!/usr/bin/env python3
"""
Quant Data Tool — Fetch cryptocurrency market data via CCXT.

Supported exchanges: OKX (default), and any other CCXT-supported exchange.

Actions:
  ticker    — latest price for a symbol
  ohlcv     — OHLCV klines (candlestick data)
  orderbook — order book depth
  markets   — list available markets on the exchange
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
        import ccxt  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _handle_quant_data(args: dict, **kw) -> str:
    """Dispatch quant_data actions via CCXT."""
    import ccxt

    action   = args.get("action", "ticker")
    exchange = args.get("exchange", "okx")
    symbol   = args.get("symbol", "BTC/USDT")
    timeframe = args.get("timeframe", "1h")
    limit    = int(args.get("limit", 100))

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
                "exchange": exchange,
                "symbol": symbol,
                "data": {
                    "symbol":       ticker.get("symbol"),
                    "last":         ticker.get("last"),
                    "bid":          ticker.get("bid"),
                    "ask":          ticker.get("ask"),
                    "high":         ticker.get("high"),
                    "low":          ticker.get("low"),
                    "open":         ticker.get("open"),
                    "close":        ticker.get("close"),
                    "change":       ticker.get("change"),
                    "percentage":   ticker.get("percentage"),
                    "baseVolume":   ticker.get("baseVolume"),
                    "quoteVolume":  ticker.get("quoteVolume"),
                    "timestamp":    ticker.get("timestamp"),
                    "datetime":     ticker.get("datetime"),
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
                    "open":      c[1],
                    "high":      c[2],
                    "low":       c[3],
                    "close":     c[4],
                    "volume":    c[5],
                })
            return json.dumps({
                "status": "ok",
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
                "exchange": exchange,
                "symbol": symbol,
                "data": {
                    "bids":       orderbook.get("bids", [])[:20],
                    "asks":       orderbook.get("asks", [])[:20],
                    "timestamp":  orderbook.get("timestamp"),
                    "datetime":   orderbook.get("datetime"),
                    "nonce":      orderbook.get("nonce"),
                },
            }, ensure_ascii=False)

        # --- markets ---
        elif action == "markets":
            markets = ex.load_markets()
            # Return summary list (not the full dict — too large)
            market_list = sorted(markets.keys()) if isinstance(markets, dict) else []
            # Provide a compact summary per market
            summary = []
            for mkt_name in market_list[:200]:  # cap at 200
                mkt = markets[mkt_name]
                summary.append({
                    "symbol":  mkt.get("symbol"),
                    "base":    mkt.get("base"),
                    "quote":   mkt.get("quote"),
                    "type":    mkt.get("type"),
                    "active":  mkt.get("active"),
                })
            return json.dumps({
                "status": "ok",
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
        "Fetch cryptocurrency market data from exchanges via CCXT. "
        "Actions: 'ticker' (latest price), 'ohlcv' (OHLCV candlestick/kline data), "
        "'orderbook' (order book depth), 'markets' (list available markets). "
        "Default exchange is OKX (works from US). Default symbol is BTC/USDT."
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
                    "'orderbook' for order book depth, "
                    "'markets' to list available markets."
                ),
            },
            "exchange": {
                "type": "string",
                "description": "Exchange name (e.g. 'okx', 'binance'). Default: 'okx'.",
                "default": "okx",
            },
            "symbol": {
                "type": "string",
                "description": "Trading pair symbol (e.g. 'BTC/USDT', 'ETH/USDT'). Default: 'BTC/USDT'.",
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
    check_fn=_check_ccxt,
    requires_env=[],
    is_async=False,
    description="Fetch cryptocurrency market data (ticker, OHLCV, orderbook, markets) via CCXT.",
    emoji="📊",
)
