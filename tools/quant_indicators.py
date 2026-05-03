"""quant_indicators – Technical analysis indicators from OHLCV data.

Computes common trading indicators (SMA, EMA, RSI, MACD, Bollinger Bands, ATR,
ADX, market regime) using pure-Python math so there is no ta-lib dependency.
Data can be supplied directly as OHLCV arrays or fetched live from an exchange
via ccxt.
"""

import json
import logging
import math
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure-Python indicator implementations
# ---------------------------------------------------------------------------

def _sma(closes: List[float], period: int) -> List[Optional[float]]:
    """Simple Moving Average."""
    result: List[Optional[float]] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        result[i] = sum(window) / period
    return result


def _ema(values: List[float], period: int) -> List[Optional[float]]:
    """Exponential Moving Average."""
    result: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result
    k = 2.0 / (period + 1)
    # Seed with SMA of first *period* values
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def _rsi(closes: List[float], period: int) -> List[Optional[float]]:
    """Relative Strength Index."""
    result: List[Optional[float]] = [None] * len(closes)
    if len(closes) < period + 1:
        return result
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    # First average gain/loss (simple average of first *period* deltas)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    idx = period  # index into closes
    if avg_loss == 0:
        result[idx] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[idx] = 100.0 - (100.0 / (1.0 + rs))
    # Subsequent values use smoothed (Wilder) averages
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        idx = i + 1  # closes index is +1 because gains/losses start at close[1]
        if avg_loss == 0:
            result[idx] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[idx] = 100.0 - (100.0 / (1.0 + rs))
    return result


def _macd(
    closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict:
    """MACD indicator.  Returns dict with macd_line, signal_line, histogram lists."""
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    n = len(closes)
    macd_line: List[Optional[float]] = [None] * n
    for i in range(n):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            macd_line[i] = fast_ema[i] - slow_ema[i]
    # Build signal line as EMA of the MACD line (non-None values only)
    valid_macd = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    signal_line: List[Optional[float]] = [None] * n
    if len(valid_macd) >= signal:
        vals_for_signal = [v for _, v in valid_macd]
        sig_ema = _ema(vals_for_signal, signal)
        for j, (orig_idx, _) in enumerate(valid_macd):
            signal_line[orig_idx] = sig_ema[j]
    histogram: List[Optional[float]] = [None] * n
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram[i] = macd_line[i] - signal_line[i]
    return {
        "macd_line": macd_line,
        "signal_line": signal_line,
        "histogram": histogram,
    }


def _bollinger(
    closes: List[float], period: int = 20, std_dev: float = 2.0
) -> dict:
    """Bollinger Bands.  Returns dict with middle, upper, lower lists."""
    n = len(closes)
    middle: List[Optional[float]] = [None] * n
    upper: List[Optional[float]] = [None] * n
    lower: List[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        sd = math.sqrt(variance)
        middle[i] = mean
        upper[i] = mean + std_dev * sd
        lower[i] = mean - std_dev * sd
    return {"middle": middle, "upper": upper, "lower": lower}


def _atr(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> List[Optional[float]]:
    """Average True Range (exponential, Wilder-style)."""
    n = len(closes)
    result: List[Optional[float]] = [None] * n
    if n < 2:
        return result
    # True range series (first bar has no previous close, use high-low)
    tr: List[float] = []
    for i in range(n):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )
    # First ATR value: simple average of first *period* TRs
    if len(tr) < period:
        return result
    atr_val = sum(tr[:period]) / period
    result[period - 1] = atr_val
    for i in range(period, n):
        atr_val = (atr_val * (period - 1) + tr[i]) / period
        result[i] = atr_val
    return result


def _adx(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> List[Optional[float]]:
    """Average Directional Index (ADX) using +DI/-DI/DX calculation.

    Returns a list the same length as *closes* with None for the warm-up
    bars and the ADX value thereafter.
    """
    n = len(closes)
    result: List[Optional[float]] = [None] * n
    if n < 2:
        return result

    # True range and directional movement
    tr_list: List[float] = []
    plus_dm: List[float] = []
    minus_dm: List[float] = []

    for i in range(1, n):
        high_diff = highs[i] - highs[i - 1]
        low_diff = lows[i - 1] - lows[i]

        plus_dm.append(high_diff if high_diff > low_diff and high_diff > 0 else 0.0)
        minus_dm.append(low_diff if low_diff > high_diff and low_diff > 0 else 0.0)

        tr_list.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    if len(tr_list) < period:
        return result

    # Initial smoothed values (simple average of first *period* values)
    smooth_tr = sum(tr_list[:period])
    smooth_plus_dm = sum(plus_dm[:period])
    smooth_minus_dm = sum(minus_dm[:period])

    # +DI and -DI series (Wilder smoothing)
    dx_values: List[float] = []

    def _calc_dx(str_val, spdm, smdm):
        if str_val == 0:
            return 0.0
        plus_di = 100.0 * spdm / str_val
        minus_di = 100.0 * smdm / str_val
        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 0.0
        return 100.0 * abs(plus_di - minus_di) / di_sum

    dx_values.append(_calc_dx(smooth_tr, smooth_plus_dm, smooth_minus_dm))

    # Continue Wilder smoothing for the rest of the data
    for i in range(period, len(tr_list)):
        smooth_tr = smooth_tr - smooth_tr / period + tr_list[i]
        smooth_plus_dm = smooth_plus_dm - smooth_plus_dm / period + plus_dm[i]
        smooth_minus_dm = smooth_minus_dm - smooth_minus_dm / period + minus_dm[i]
        dx_values.append(_calc_dx(smooth_tr, smooth_plus_dm, smooth_minus_dm))

    if not dx_values:
        return result

    # ADX is the smoothed average of DX values (Wilder smoothing over *period*)
    # First ADX = simple average of first *period* DX values
    if len(dx_values) < period:
        return result

    adx_val = sum(dx_values[:period]) / period
    # The ADX first valid index in the closes array:
    # +DM/-DM start at index 1, so tr_list[0] corresponds to closes[1].
    # After *period* smoothed bars we get the first DX at closes[period].
    # After *period* DX values we get the first ADX at closes[2*period - 1].
    first_adx_idx = 2 * period - 1
    if first_adx_idx >= n:
        return result
    result[first_adx_idx] = adx_val

    for i in range(period, len(dx_values)):
        adx_val = (adx_val * (period - 1) + dx_values[i]) / period
        idx = first_adx_idx + (i - period) + 1
        if idx < n:
            result[idx] = adx_val

    return result


def _atr_percentile(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    atr_period: int = 14,
    lookback: int = 100,
) -> List[Optional[float]]:
    """Current ATR expressed as a percentile (0-100) of the last *lookback* ATR values.

    A high percentile means volatility is elevated relative to recent history.
    Returns a list aligned with *closes*.
    """
    atr_series = _atr(highs, lows, closes, atr_period)
    n = len(closes)
    result: List[Optional[float]] = [None] * n

    for i in range(n):
        if atr_series[i] is None:
            continue
        # Collect up to *lookback* previous non-None ATR values ending at i
        window_vals: List[float] = []
        for j in range(max(0, i - lookback + 1), i + 1):
            if atr_series[j] is not None:
                window_vals.append(atr_series[j])
        if len(window_vals) < 2:
            continue
        current_atr = atr_series[i]
        rank = sum(1 for v in window_vals if v < current_atr)
        result[i] = round(rank / (len(window_vals) - 1) * 100.0, 2)

    return result


# ---------------------------------------------------------------------------
# Data fetching via ccxt
# ---------------------------------------------------------------------------

def _fetch_ohlcv(
    exchange_id: str = "okx",
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    limit: int = 200,
) -> List[list]:
    """Fetch OHLCV data from an exchange using ccxt.

    Returns list of [timestamp, open, high, low, close, volume].
    """
    import ccxt

    exchange_class = getattr(ccxt, exchange_id, None)
    if exchange_class is None:
        raise ValueError(f"Unknown exchange: {exchange_id}")
    exchange = exchange_class()
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    # Some ccxt exchange objects have close(), others don't
    if hasattr(exchange, "close"):
        exchange.close()
    return ohlcv


# ---------------------------------------------------------------------------
# Core handler logic
# ---------------------------------------------------------------------------

def quant_indicators_handler(args: dict, **kw) -> str:
    """Compute technical indicators from OHLCV data.

    If ``ohlcv`` is not supplied the handler will fetch data from an exchange
    using ccxt.
    """
    try:
        action = args.get("action", "all")
        symbol = args.get("symbol", "BTC/USDT")
        timeframe = args.get("timeframe", "1h")
        limit = int(args.get("limit", 200))
        period = args.get("period")
        exchange_id = args.get("exchange", "okx")
        fast = int(args.get("fast", 12))
        slow = int(args.get("slow", 26))
        signal = int(args.get("signal", 9))
        std_dev = float(args.get("std_dev", 2.0))
        ohlcv_input = args.get("ohlcv")

        # Obtain OHLCV data
        if ohlcv_input:
            ohlcv = ohlcv_input
        else:
            ohlcv = _fetch_ohlcv(exchange_id, symbol, timeframe, limit)

        if not ohlcv or len(ohlcv) < 2:
            return json.dumps({"error": "Not enough OHLCV data"})

        # Extract columns
        timestamps = [row[0] for row in ohlcv]
        opens = [float(row[1]) for row in ohlcv]
        highs = [float(row[2]) for row in ohlcv]
        lows = [float(row[3]) for row in ohlcv]
        closes = [float(row[4]) for row in ohlcv]
        volumes = [float(row[5]) for row in ohlcv]

        # Helper: trim trailing None values and return last N non-None entries
        def _tail(values, n=20):
            trimmed = [v for v in values if v is not None]
            return trimmed[-n:] if n else trimmed

        result: dict = {
            "symbol": symbol,
            "timeframe": timeframe,
            "exchange": exchange_id,
            "data_points": len(ohlcv),
            "last_timestamp": timestamps[-1],
            "last_close": closes[-1],
        }

        # Determine default periods per action
        sma_period = int(period) if period else 20
        ema_period = int(period) if period else 20
        rsi_period = int(period) if period else 14
        atr_period = int(period) if period else 14

        valid_actions = ("sma", "ema", "rsi", "macd", "bollinger", "atr", "regime", "all")
        if action not in valid_actions:
            return json.dumps(
                {"error": f"Invalid action '{action}'. Must be one of {valid_actions}"}
            )

        if action in ("sma", "all"):
            result["sma"] = _tail(_sma(closes, sma_period))

        if action in ("ema", "all"):
            result["ema"] = _tail(_ema(closes, ema_period))

        if action in ("rsi", "all"):
            rsi_vals = _rsi(closes, rsi_period)
            result["rsi"] = _tail(rsi_vals)

        if action in ("macd", "all"):
            macd_res = _macd(closes, fast, slow, signal)
            result["macd"] = {
                "macd_line": _tail(macd_res["macd_line"]),
                "signal_line": _tail(macd_res["signal_line"]),
                "histogram": _tail(macd_res["histogram"]),
            }

        if action in ("bollinger", "all"):
            bb_res = _bollinger(closes, period=20, std_dev=std_dev)
            result["bollinger"] = {
                "middle": _tail(bb_res["middle"]),
                "upper": _tail(bb_res["upper"]),
                "lower": _tail(bb_res["lower"]),
            }

        if action in ("atr", "all"):
            result["atr"] = _tail(_atr(highs, lows, closes, atr_period))

        if action in ("regime", "all"):
            adx_period = int(period) if period else 14
            adx_series = _adx(highs, lows, closes, adx_period)
            atr_p_series = _atr_percentile(highs, lows, closes, atr_period, lookback=100)

            # Extract latest non-None values
            latest_adx = None
            for v in reversed(adx_series):
                if v is not None:
                    latest_adx = round(v, 2)
                    break

            latest_atr_pct = None
            for v in reversed(atr_p_series):
                if v is not None:
                    latest_atr_pct = v
                    break

            # Classify market regime
            if latest_adx is not None and latest_atr_pct is not None:
                if latest_adx > 25 and latest_atr_pct > 60:
                    regime = "trending"
                elif latest_adx < 20:
                    regime = "ranging"
                elif latest_atr_pct > 80:
                    regime = "volatile"
                else:
                    regime = "neutral"
            else:
                regime = "unknown"

            result["regime"] = {
                "regime": regime,
                "adx": latest_adx,
                "atr_percentile": latest_atr_pct,
                "adx_period": adx_period,
                "atr_period": atr_period,
                "thresholds": {
                    "trending": "ADX > 25 AND ATR_pct > 60%",
                    "ranging": "ADX < 20",
                    "volatile": "ATR_pct > 80%",
                    "neutral": "all other cases",
                },
            }

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.exception("quant_indicators error: %s", e)
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_quant_requirements() -> bool:
    """Return True if ccxt is importable."""
    try:
        import ccxt  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Schema & Registry
# ---------------------------------------------------------------------------

from tools.registry import registry, tool_error

QUANT_INDICATORS_SCHEMA = {
    "name": "quant_indicators",
    "description": (
        "Compute technical analysis indicators (SMA, EMA, RSI, MACD, Bollinger "
        "Bands, ATR, ADX, market regime) from OHLCV price data. Fetches data "
        "from an exchange via ccxt (default: OKX, BTC/USDT, 1h). Use "
        "action='all' to compute every supported indicator at once. Use "
        "action='regime' to detect market regime (trending/ranging/volatile/"
        "neutral) via ADX + ATR percentile."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["sma", "ema", "rsi", "macd", "bollinger", "atr", "regime", "all"],
                "description": (
                    "Indicator to compute. 'regime' detects market regime "
                    "(trending/ranging/volatile/neutral) using ADX + ATR "
                    "percentile. 'all' computes every supported indicator at once."
                ),
            },
            "symbol": {
                "type": "string",
                "description": "Trading pair symbol, e.g. 'BTC/USDT', 'ETH/USDT'.",
                "default": "BTC/USDT",
            },
            "timeframe": {
                "type": "string",
                "description": "Candle timeframe, e.g. '1m', '5m', '15m', '1h', '4h', '1d'.",
                "default": "1h",
            },
            "limit": {
                "type": "integer",
                "description": "Number of candles to fetch (need enough for indicator warm-up).",
                "default": 200,
            },
            "period": {
                "type": "integer",
                "description": (
                    "Look-back period for SMA / EMA / RSI / ATR. Ignored for "
                    "MACD and Bollinger (use fast/slow/signal or std_dev instead)."
                ),
            },
            "fast": {
                "type": "integer",
                "description": "Fast EMA period for MACD (default 12).",
                "default": 12,
            },
            "slow": {
                "type": "integer",
                "description": "Slow EMA period for MACD (default 26).",
                "default": 26,
            },
            "signal": {
                "type": "integer",
                "description": "Signal line period for MACD (default 9).",
                "default": 9,
            },
            "std_dev": {
                "type": "number",
                "description": "Standard-deviation multiplier for Bollinger Bands (default 2).",
                "default": 2.0,
            },
            "exchange": {
                "type": "string",
                "description": "ccxt exchange id to fetch data from (default 'okx').",
                "default": "okx",
            },
        },
        "required": ["action"],
    },
}

registry.register(
    name="quant_indicators",
    toolset="quant",
    schema=QUANT_INDICATORS_SCHEMA,
    handler=quant_indicators_handler,
    check_fn=_check_quant_requirements,
    requires_env=[],
    is_async=False,
    description="Technical analysis indicators from OHLCV data (SMA, EMA, RSI, MACD, Bollinger, ATR, ADX, regime).",
    emoji="📈",
)
