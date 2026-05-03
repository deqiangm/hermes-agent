#!/usr/bin/env python3
"""Quant Backtest Tool — Backtest trading strategies against historical data.

Evaluates how a strategy would have performed historically by simulating
trades on OHLCV data fetched via CCXT, computing key performance metrics
(Sharpe ratio, Sortino ratio, max drawdown, win rate, profit factor),
and storing results in SQLite for later review and comparison.

Actions:
 backtest    — Run a strategy backtest on historical data
 results     — Query past backtest results from DB
 compare     — Run two strategies on same data and compare
 walk_forward  — Rolling in-sample / out-of-sample validation
 stress_test   — Test strategy under extreme market scenarios
 report        — Comprehensive strategy comparison report
"""

import json
import logging
import math
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure-Python indicator implementations (inlined to avoid circular imports
# with quant_indicators.py)
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
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def _rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
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
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    idx = period
    if avg_loss == 0:
        result[idx] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[idx] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        idx = i + 1
        if avg_loss == 0:
            result[idx] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[idx] = 100.0 - (100.0 / (1.0 + rs))
    return result


def _macd(
    closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict:
    """MACD indicator. Returns dict with macd_line, signal_line, histogram lists."""
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    n = len(closes)
    macd_line: List[Optional[float]] = [None] * n
    for i in range(n):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            macd_line[i] = fast_ema[i] - slow_ema[i]
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
    """Bollinger Bands. Returns dict with middle, upper, lower lists."""
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
    if len(tr) < period:
        return result
    atr_val = sum(tr[:period]) / period
    result[period - 1] = atr_val
    for i in range(period, n):
        atr_val = (atr_val * (period - 1) + tr[i]) / period
        result[i] = atr_val
    return result


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def _fetch_ohlcv(
    exchange_id: str = "okx",
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    days: int = 30,
    asset_class: str = "crypto",
) -> list:
    """Fetch historical OHLCV data for *days* lookback.

    Returns list of [timestamp, open, high, low, close, volume].
    Supports crypto (CCXT), stock and fx (yfinance).
    """
    if asset_class in ("stock", "fx"):
        return _fetch_ohlcv_yfinance(symbol, timeframe, days, asset_class)

    import ccxt

    # Approximate number of candles needed based on timeframe
    tf_to_minutes = {
        "1m": 1, "5m": 5, "15m": 15, "1h": 60,
        "4h": 240, "1d": 1440, "1w": 10080, "1M": 43200,
    }
    minutes_per_candle = tf_to_minutes.get(timeframe, 60)
    candles_needed = min(int(days * 1440 / minutes_per_candle), 1500)

    exchange_class = getattr(ccxt, exchange_id, None)
    if exchange_class is None:
        raise ValueError(f"Unknown exchange: {exchange_id}")

    ex = exchange_class()
    try:
        ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=candles_needed)
    finally:
        try:
            ex.close()
        except Exception:
            pass

    return ohlcv


def _fetch_ohlcv_yfinance(
    symbol: str, timeframe: str, days: int, asset_class: str
) -> list:
    """Fetch OHLCV via yfinance for stock/fx. Returns CCXT-compatible format."""
    import yfinance as yf
    import pandas as pd

    # Convert symbol for yfinance FX convention
    yf_symbol = symbol
    if asset_class == "fx":
        # EUR/USD → EURUSD=X
        yf_symbol = symbol.replace("/", "") + "=X"

    tf_map = {
        "1m": ("1m", 7), "5m": ("5m", 60), "15m": ("15m", 60),
        "1h": ("1h", 730), "4h": ("1h", 730), "1d": ("1d", None),
    }
    yf_interval, max_period = tf_map.get(timeframe, ("1h", 730))
    period_days = min(days, max_period) if max_period else days
    yf_period = f"{period_days}d"

    tk = yf.Ticker(yf_symbol)
    hist = tk.history(period=yf_period, interval=yf_interval)

    if hist.empty:
        raise ValueError(f"No yfinance data for {yf_symbol} period={yf_period} interval={yf_interval}")

    # Resample to 4h if needed
    if timeframe == "4h" and yf_interval == "1h":
        hist = hist.resample("4h").agg({
            "Open": "first", "High": "max", "Low": "min",
            "Close": "last", "Volume": "sum",
        }).dropna()

    # Convert to CCXT format: [timestamp_ms, open, high, low, close, volume]
    result = []
    for idx, row in hist.iterrows():
        ts = int(idx.timestamp() * 1000) if hasattr(idx, "timestamp") else int(idx.value / 1_000_000)
        result.append([ts, row["Open"], row["High"], row["Low"], row["Close"], row["Volume"]])

    return result


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------

def _generate_signals(
    closes: List[float],
    highs: List[float],
    lows: List[float],
    strategy: str,
) -> List[Optional[str]]:
    """Generate BUY / SELL / None signals for each candle.

    Strategies:
      momentum — SMA20>SMA50 + EMA12>EMA26 + RSI 40-70 + MACD hist>0 + price>BB_mid -> BUY
                 Opposite conditions -> SELL
      mean_reversion — RSI<30 + price<BB_lower -> BUY; RSI>70 + price>BB_upper -> SELL
    """
    n = len(closes)
    signals: List[Optional[str]] = [None] * n

    # Compute all indicators once
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    rsi = _rsi(closes, 14)
    macd_res = _macd(closes, 12, 26, 9)
    bb = _bollinger(closes, 20, 2.0)

    macd_hist = macd_res["histogram"]
    bb_mid = bb["middle"]
    bb_upper = bb["upper"]
    bb_lower = bb["lower"]

    for i in range(n):
        if strategy == "momentum":
            # All indicators must be valid
            if any(v is None for v in [
                sma20[i], sma50[i], ema12[i], ema26[i],
                rsi[i], macd_hist[i], bb_mid[i]
            ]):
                continue

            buy_score = 0
            sell_score = 0

            # SMA crossover
            if sma20[i] > sma50[i]:
                buy_score += 1
            elif sma20[i] < sma50[i]:
                sell_score += 1

            # EMA crossover
            if ema12[i] > ema26[i]:
                buy_score += 1
            elif ema12[i] < ema26[i]:
                sell_score += 1

            # RSI range
            if 40 <= rsi[i] <= 70:
                buy_score += 1
            if rsi[i] >= 60 or rsi[i] <= 30:
                sell_score += 1

            # MACD histogram
            if macd_hist[i] > 0:
                buy_score += 1
            elif macd_hist[i] < 0:
                sell_score += 1

            # Price vs Bollinger middle
            if closes[i] > bb_mid[i]:
                buy_score += 1
            elif closes[i] < bb_mid[i]:
                sell_score += 1

            # Need at least 4 of 5 conditions aligned
            if buy_score >= 4:
                signals[i] = "BUY"
            elif sell_score >= 4:
                signals[i] = "SELL"

        elif strategy == "mean_reversion":
            if rsi[i] is None or bb_lower[i] is None or bb_upper[i] is None:
                continue

            # BUY: RSI < 30 AND price < BB lower band
            if rsi[i] < 30 and closes[i] < bb_lower[i]:
                signals[i] = "BUY"
            # SELL: RSI > 70 AND price > BB upper band
            elif rsi[i] > 70 and closes[i] > bb_upper[i]:
                signals[i] = "SELL"

    return signals


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def _simulate_trades(
    timestamps: list,
    closes: List[float],
    signals: List[Optional[str]],
    initial_balance: float = 10000.0,
    fee_pct: float = 0.1,
) -> dict:
    """Simulate trades based on signals and compute metrics.

    Uses a simple model: go all-in on BUY, close on SELL.
    Tracks individual trades with entry/exit prices and PnL.

    Returns dict with: trades, equity_curve, metrics.
    """
    balance = initial_balance
    position = None  # None or {"entry_price": float, "entry_idx": int, "amount": float}
    trades = []
    equity_curve = []

    fee_mult = 1.0 - fee_pct / 100.0  # e.g. 0.999 for 0.1% fee

    for i in range(len(closes)):
        signal = signals[i]
        price = closes[i]

        if signal == "BUY" and position is None:
            # Enter long position — invest full balance
            cost_fee = balance * (1 - fee_mult)
            invest = balance - cost_fee
            amount = invest / price
            position = {
                "entry_price": price,
                "entry_idx": i,
                "entry_time": timestamps[i],
                "amount": amount,
                "fee_paid": cost_fee,
            }
            balance = 0.0  # all capital in position

        elif signal == "SELL" and position is not None:
            # Close position
            gross = position["amount"] * price
            fee = gross * (fee_pct / 100.0)
            net = gross - fee
            pnl = net - (position["amount"] * position["entry_price"])
            pnl_pct = (pnl / (position["amount"] * position["entry_price"])) * 100.0

            trade = {
                "entry_idx": position["entry_idx"],
                "exit_idx": i,
                "entry_time": position["entry_time"],
                "exit_time": timestamps[i],
                "entry_price": round(position["entry_price"], 2),
                "exit_price": round(price, 2),
                "amount": round(position["amount"], 6),
                "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 2),
                "entry_fee": round(position.get("fee_paid", 0), 4),
                "exit_fee": round(fee, 4),
                "side": "long",
            }
            trades.append(trade)
            balance = net
            position = None

        # Track equity (balance + unrealized position value)
        if position is not None:
            unrealized = position["amount"] * price
            equity = balance + unrealized
        else:
            equity = balance
        equity_curve.append(round(equity, 2))

    # If still in position at end, close it at last price for metrics
    if position is not None:
        price = closes[-1]
        gross = position["amount"] * price
        fee = gross * (fee_pct / 100.0)
        net = gross - fee
        pnl = net - (position["amount"] * position["entry_price"])
        pnl_pct = (pnl / (position["amount"] * position["entry_price"])) * 100.0
        trades.append({
            "entry_idx": position["entry_idx"],
            "exit_idx": len(closes) - 1,
            "entry_time": position["entry_time"],
            "exit_time": timestamps[-1],
            "entry_price": round(position["entry_price"], 2),
            "exit_price": round(price, 2),
            "amount": round(position["amount"], 6),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
            "entry_fee": round(position.get("fee_paid", 0), 4),
            "exit_fee": round(fee, 4),
            "side": "long",
            "forced_close": True,
        })
        balance = net
        equity_curve[-1] = round(balance, 2)

    # --- Compute metrics ---
    metrics = _compute_metrics(equity_curve, trades, initial_balance)

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# Performance metrics
# ---------------------------------------------------------------------------

def _compute_metrics(
    equity_curve: List[float],
    trades: list,
    initial_balance: float,
) -> dict:
    """Compute backtest performance metrics."""
    if not equity_curve or len(equity_curve) < 2:
        return {
            "total_return_pct": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_trades": 0,
            "avg_trade_pnl": 0.0,
        }

    # --- Returns per bar ---
    returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        if prev > 0:
            returns.append((equity_curve[i] - prev) / prev)
        else:
            returns.append(0.0)

    # Total return
    final = equity_curve[-1]
    total_return_pct = ((final - initial_balance) / initial_balance) * 100.0

    # Sharpe ratio (annualized, risk-free = 0)
    # Assumes returns are per-bar; annualize based on ~8760 hours/year for 1h
    if returns:
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        std_ret = math.sqrt(variance) if variance > 0 else 0.0
        # Annualize: sqrt(bars_per_year) * mean / std
        bars_per_year = 8760  # ~1h bars
        sharpe = (math.sqrt(bars_per_year) * mean_ret / std_ret) if std_ret > 0 else 0.0
    else:
        sharpe = 0.0

    # Sortino ratio (only downside deviation)
    if returns:
        mean_ret = sum(returns) / len(returns)
        downside = [r for r in returns if r < 0]
        if downside:
            down_var = sum(r ** 2 for r in downside) / len(downside)
            down_std = math.sqrt(down_var)
            sortino = (math.sqrt(bars_per_year) * mean_ret / down_std) if down_std > 0 else 0.0
        else:
            sortino = 999.99 if mean_ret > 0 else 0.0  # No downside = perfect
    else:
        sortino = 0.0

    # Max drawdown
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        if peak > 0:
            dd = (peak - val) / peak
            if dd > max_dd:
                max_dd = dd
    max_drawdown_pct = max_dd * 100.0

    # Win rate and profit factor
    if trades:
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0]
        win_rate = (len(wins) / len(trades)) * 100 if trades else 0.0
        gross_profit = sum(t.get("pnl", 0) for t in wins)
        gross_loss = abs(sum(t.get("pnl", 0) for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.99 if gross_profit > 0 else 0.0)
        avg_pnl = sum(t.get("pnl", 0) for t in trades) / len(trades)
    else:
        win_rate = 0.0
        profit_factor = 0.0
        avg_pnl = 0.0

    return {
        "total_return_pct": round(total_return_pct, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "total_trades": len(trades),
        "avg_trade_pnl": round(avg_pnl, 4),
    }


# ---------------------------------------------------------------------------
# Backtest-on-slice helper (reused by walk_forward / stress_test / report)
# ---------------------------------------------------------------------------

def _run_backtest_on_slice(
    ohlcv: list,
    start_idx: int,
    end_idx: int,
    strategy: str,
    initial_balance: float = 10000.0,
    fee_pct: float = 0.1,
) -> dict:
    """Run a backtest on a sub-slice of OHLCV data.

    Returns the same dict as _simulate_trades: {trades, equity_curve, metrics}.
    """
    slice_data = ohlcv[start_idx:end_idx]
    if len(slice_data) < 10:
        return {
            "trades": [],
            "equity_curve": [initial_balance],
            "metrics": {
                "total_return_pct": 0.0,
                "sharpe_ratio": 0.0,
                "sortino_ratio": 0.0,
                "max_drawdown_pct": 0.0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "total_trades": 0,
                "avg_trade_pnl": 0.0,
            },
        }
    timestamps = [row[0] for row in slice_data]
    highs = [float(row[2]) for row in slice_data]
    lows = [float(row[3]) for row in slice_data]
    closes = [float(row[4]) for row in slice_data]

    signals = _generate_signals(closes, highs, lows, strategy)
    return _simulate_trades(timestamps, closes, signals,
                            initial_balance=initial_balance, fee_pct=fee_pct)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_db_path() -> Path:
    """Get the backtest database path, profile-aware."""
    try:
        from hermes_constants import get_hermes_home
        base = Path(get_hermes_home())
    except ImportError:
        base = Path.home() / ".hermes"
    db_dir = base / "quant_trading"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "paper_trading.db"


def _get_conn() -> sqlite3.Connection:
    """Get a SQLite connection, creating backtest_results table if needed."""
    path = str(_get_db_path())
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _init_backtest_table(conn)
    return conn


def _init_backtest_table(conn: sqlite3.Connection):
    """Create backtest_results table if it doesn't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS backtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            strategy TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            period_days INTEGER NOT NULL,
            initial_balance REAL NOT NULL,
            fee_pct REAL NOT NULL,
            total_return_pct REAL,
            sharpe_ratio REAL,
            sortino_ratio REAL,
            max_drawdown_pct REAL,
            win_rate REAL,
            profit_factor REAL,
            total_trades INTEGER,
            avg_trade_pnl REAL,
            trades_json TEXT,
            equity_start REAL,
            equity_end REAL
        );

        CREATE INDEX IF NOT EXISTS idx_backtest_timestamp
            ON backtest_results(timestamp);
        CREATE INDEX IF NOT EXISTS idx_backtest_strategy
            ON backtest_results(strategy);
        CREATE INDEX IF NOT EXISTS idx_backtest_symbol
            ON backtest_results(symbol);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _action_backtest(args: dict, **kw) -> str:
    """Run a strategy backtest on historical data."""
    try:
        symbol = args.get("symbol", "BTC/USDT")
        strategy = args.get("strategy", "momentum")
        timeframe = args.get("timeframe", "1h")
        period = int(args.get("period", 30))
        initial_balance = float(args.get("initial_balance", 10000))
        fee_pct = float(args.get("fee_pct", 0.1))
        exchange = args.get("exchange", "okx")

        if strategy not in ("momentum", "mean_reversion"):
            return json.dumps({
                "status": "error",
                "error": f"Unknown strategy '{strategy}'. Use: momentum, mean_reversion",
            })

        # Fetch historical data
        asset_class = args.get("asset_class", "crypto")
        ohlcv = _fetch_ohlcv(exchange, symbol, timeframe, period, asset_class)
        if not ohlcv or len(ohlcv) < 20:
            return json.dumps({
                "status": "error",
                "error": f"Not enough OHLCV data (got {len(ohlcv) if ohlcv else 0} candles, need 20+)",
            })

        # Extract columns
        timestamps = [row[0] for row in ohlcv]
        opens = [float(row[1]) for row in ohlcv]
        highs = [float(row[2]) for row in ohlcv]
        lows = [float(row[3]) for row in ohlcv]
        closes = [float(row[4]) for row in ohlcv]
        volumes = [float(row[5]) for row in ohlcv]

        # Generate signals
        signals = _generate_signals(closes, highs, lows, strategy)

        # Simulate trades
        sim = _simulate_trades(
            timestamps, closes, signals,
            initial_balance=initial_balance,
            fee_pct=fee_pct,
        )

        metrics = sim["metrics"]
        trades = sim["trades"]

        # Store results in DB
        try:
            conn = _get_conn()
            conn.execute("""
                INSERT INTO backtest_results
                (strategy, symbol, timeframe, period_days, initial_balance, fee_pct,
                 total_return_pct, sharpe_ratio, sortino_ratio, max_drawdown_pct,
                 win_rate, profit_factor, total_trades, avg_trade_pnl,
                 trades_json, equity_start, equity_end)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                strategy,
                symbol,
                timeframe,
                period,
                initial_balance,
                fee_pct,
                metrics["total_return_pct"],
                metrics["sharpe_ratio"],
                metrics["sortino_ratio"],
                metrics["max_drawdown_pct"],
                metrics["win_rate"],
                metrics["profit_factor"],
                metrics["total_trades"],
                metrics["avg_trade_pnl"],
                json.dumps(trades[:50]),  # Store first 50 trades
                initial_balance,
                sim["equity_curve"][-1] if sim["equity_curve"] else initial_balance,
            ))
            conn.commit()
            result_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.close()
        except Exception as db_err:
            logger.warning("Failed to store backtest results in DB: %s", db_err)
            result_id = None

        return json.dumps({
            "status": "ok",
            "result_id": result_id,
            "strategy": strategy,
            "symbol": symbol,
            "timeframe": timeframe,
            "period_days": period,
            "initial_balance": initial_balance,
            "fee_pct": fee_pct,
            "candles_analyzed": len(closes),
            "metrics": metrics,
            "trades": trades[:10],  # First 10 trades for context
            "total_trades": len(trades),
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("quant_backtest backtest error: %s", e)
        return json.dumps({
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }, ensure_ascii=False)


def _action_results(args: dict, **kw) -> str:
    """Query past backtest results from DB."""
    try:
        limit = min(int(args.get("limit", 10)), 50)
        strategy_filter = args.get("strategy")

        conn = _get_conn()

        query = "SELECT id, timestamp, strategy, symbol, timeframe, period_days, "
        query += "initial_balance, fee_pct, total_return_pct, sharpe_ratio, "
        query += "sortino_ratio, max_drawdown_pct, win_rate, profit_factor, "
        query += "total_trades, avg_trade_pnl, equity_start, equity_end "
        query += "FROM backtest_results WHERE 1=1"
        params = []

        if strategy_filter:
            query += " AND strategy = ?"
            params.append(strategy_filter)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        results = []
        for r in rows:
            results.append({
                "id": r["id"],
                "timestamp": r["timestamp"],
                "strategy": r["strategy"],
                "symbol": r["symbol"],
                "timeframe": r["timeframe"],
                "period_days": r["period_days"],
                "initial_balance": r["initial_balance"],
                "fee_pct": r["fee_pct"],
                "total_return_pct": r["total_return_pct"],
                "sharpe_ratio": r["sharpe_ratio"],
                "sortino_ratio": r["sortino_ratio"],
                "max_drawdown_pct": r["max_drawdown_pct"],
                "win_rate": r["win_rate"],
                "profit_factor": r["profit_factor"],
                "total_trades": r["total_trades"],
                "avg_trade_pnl": r["avg_trade_pnl"],
                "equity_start": r["equity_start"],
                "equity_end": r["equity_end"],
            })
        conn.close()

        return json.dumps({
            "status": "ok",
            "results": results,
            "count": len(results),
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("quant_backtest results error: %s", e)
        return json.dumps({
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }, ensure_ascii=False)


def _action_compare(args: dict, **kw) -> str:
    """Run two strategies on same data and compare."""
    try:
        symbol = args.get("symbol", "BTC/USDT")
        timeframe = args.get("timeframe", "1h")
        period = int(args.get("period", 30))
        initial_balance = float(args.get("initial_balance", 10000))
        fee_pct = float(args.get("fee_pct", 0.1))
        exchange = args.get("exchange", "okx")
        strategies = args.get("strategies", ["momentum", "mean_reversion"])

        if len(strategies) != 2:
            return json.dumps({
                "status": "error",
                "error": "Exactly 2 strategies required for comparison",
            })

        for s in strategies:
            if s not in ("momentum", "mean_reversion"):
                return json.dumps({
                    "status": "error",
                    "error": f"Unknown strategy '{s}'. Use: momentum, mean_reversion",
                })

        # Fetch historical data once
        asset_class = args.get("asset_class", "crypto")
        ohlcv = _fetch_ohlcv(exchange, symbol, timeframe, period, asset_class)
        if not ohlcv or len(ohlcv) < 20:
            return json.dumps({
                "status": "error",
                "error": f"Not enough OHLCV data (got {len(ohlcv) if ohlcv else 0} candles, need 20+)",
            })

        timestamps = [row[0] for row in ohlcv]
        highs = [float(row[2]) for row in ohlcv]
        lows = [float(row[3]) for row in ohlcv]
        closes = [float(row[4]) for row in ohlcv]

        comparison = {}
        best_return = -float("inf")
        winner = None

        for strat in strategies:
            signals = _generate_signals(closes, highs, lows, strat)
            sim = _simulate_trades(
                timestamps, closes, signals,
                initial_balance=initial_balance,
                fee_pct=fee_pct,
            )
            metrics = sim["metrics"]
            comparison[strat] = metrics

            # Store each strategy result
            try:
                conn = _get_conn()
                conn.execute("""
                    INSERT INTO backtest_results
                    (strategy, symbol, timeframe, period_days, initial_balance, fee_pct,
                     total_return_pct, sharpe_ratio, sortino_ratio, max_drawdown_pct,
                     win_rate, profit_factor, total_trades, avg_trade_pnl,
                     trades_json, equity_start, equity_end)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    strat, symbol, timeframe, period, initial_balance, fee_pct,
                    metrics["total_return_pct"], metrics["sharpe_ratio"],
                    metrics["sortino_ratio"], metrics["max_drawdown_pct"],
                    metrics["win_rate"], metrics["profit_factor"],
                    metrics["total_trades"], metrics["avg_trade_pnl"],
                    json.dumps(sim["trades"][:50]),
                    initial_balance,
                    sim["equity_curve"][-1] if sim["equity_curve"] else initial_balance,
                ))
                conn.commit()
                conn.close()
            except Exception as db_err:
                logger.warning("Failed to store comparison result for %s: %s", strat, db_err)

            # Track winner by total return
            if metrics["total_return_pct"] > best_return:
                best_return = metrics["total_return_pct"]
                winner = strat

        return json.dumps({
            "status": "ok",
            "symbol": symbol,
            "timeframe": timeframe,
            "period_days": period,
            "initial_balance": initial_balance,
            "fee_pct": fee_pct,
            "candles_analyzed": len(closes),
            "comparison": comparison,
            "winner": winner,
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("quant_backtest compare error: %s", e)
        return json.dumps({
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Stress-test data modifier
# ---------------------------------------------------------------------------

def _modify_ohlcv_for_scenario(ohlcv: list, scenario: str) -> list:
    """Return a *copy* of ohlcv with data modified to simulate *scenario*.

    Scenarios:
      flash_crash      — Drop price 20% in 1 hour, then recover 10%
      liquidity_crisis — Add 1% extra slippage per trade (via price adjustment)
      black_swan       — Sustained 30% decline over 3 days
      volatility_spike — ATR triples for 48 hours
    """
    modified = [list(row) for row in ohlcv]  # deep-enough copy
    n = len(modified)
    if n < 10:
        return modified

    if scenario == "flash_crash":
        # Pick a random point in the middle of the data for the crash
        crash_idx = random.randint(n // 4, 3 * n // 4)
        base_close = float(modified[crash_idx][4])
        # Drop 20% at crash candle
        crash_price = base_close * 0.80
        modified[crash_idx][2] = str(float(modified[crash_idx][2]) * 0.82)   # high
        modified[crash_idx][3] = str(crash_price * 0.98)                     # low
        modified[crash_idx][4] = str(crash_price)                            # close
        # Recovery 10% over next few candles (if they exist)
        recovery_price = crash_price * 1.10
        for j in range(1, min(6, n - crash_idx)):
            idx = crash_idx + j
            factor = 1.0 + (0.10 * j / 5)
            new_close = crash_price * factor
            modified[idx][4] = str(new_close)
            modified[idx][2] = str(new_close * 1.005)
            modified[idx][3] = str(new_close * 0.995)

    elif scenario == "liquidity_crisis":
        # Simulate wide spread by shifting closes by 0.5% per candle
        # (equivalent to ~1% round-trip extra slippage)
        for i in range(n):
            shift = float(modified[i][4]) * 0.005
            modified[i][4] = str(float(modified[i][4]) + shift)
            modified[i][2] = str(float(modified[i][2]) + shift)
            modified[i][3] = str(float(modified[i][3]) + shift)

    elif scenario == "black_swan":
        # Sustained 30% decline spread over 72 candles (≈ 3 days at 1h)
        decline_candles = min(72, n // 2)
        start = n // 4
        per_candle_factor = (1.0 - 0.30) ** (1.0 / decline_candles)
        for j in range(decline_candles):
            idx = start + j
            if idx >= n:
                break
            for col in [1, 2, 3, 4]:  # O, H, L, C
                modified[idx][col] = str(float(modified[idx][col]) * per_candle_factor)

    elif scenario == "volatility_spike":
        # Triple the range (H-L) for 48 candles (≈ 2 days at 1h)
        spike_candles = min(48, n // 3)
        start = n // 4
        for j in range(spike_candles):
            idx = start + j
            if idx >= n:
                break
            mid = (float(modified[idx][2]) + float(modified[idx][3])) / 2.0
            half_range = (float(modified[idx][2]) - float(modified[idx][3])) / 2.0
            new_half = half_range * 3.0
            modified[idx][2] = str(mid + new_half)
            modified[idx][3] = str(mid - new_half)

    return modified


# ---------------------------------------------------------------------------
# Walk-forward action
# ---------------------------------------------------------------------------

def _action_walk_forward(args: dict, **kw) -> str:
    """Walk-forward optimisation: rolling in-sample / out-of-sample windows."""
    try:
        symbol = args.get("symbol", "BTC/USDT")
        strategy = args.get("strategy", "momentum")
        timeframe = args.get("timeframe", "1h")
        period = int(args.get("period", 30))
        train_days = int(args.get("train_days", 7))
        test_days = int(args.get("test_days", 3))
        exchange = args.get("exchange", "okx")
        initial_balance = float(args.get("initial_balance", 10000))
        fee_pct = float(args.get("fee_pct", 0.1))

        if strategy not in ("momentum", "mean_reversion"):
            return json.dumps({
                "status": "error",
                "error": f"Unknown strategy '{strategy}'. Use: momentum, mean_reversion",
            })

        asset_class = args.get("asset_class", "crypto")
        ohlcv = _fetch_ohlcv(exchange, symbol, timeframe, period, asset_class)
        if not ohlcv or len(ohlcv) < 20:
            return json.dumps({
                "status": "error",
                "error": f"Not enough OHLCV data (got {len(ohlcv) if ohlcv else 0} candles, need 20+)",
            })

        # Determine candles per day based on timeframe
        tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60,
                       "4h": 240, "1d": 1440, "1w": 10080, "1M": 43200}
        minutes_per_candle = tf_minutes.get(timeframe, 60)
        candles_per_day = max(1, int(1440 / minutes_per_candle))

        train_candles = train_days * candles_per_day
        test_candles = test_days * candles_per_day
        window_size = train_candles + test_candles

        windows = []
        window_num = 0
        start = 0

        while start + window_size <= len(ohlcv):
            window_num += 1
            train_end = start + train_candles
            test_end = start + window_size

            # In-sample backtest
            is_sim = _run_backtest_on_slice(
                ohlcv, start, train_end, strategy,
                initial_balance=initial_balance, fee_pct=fee_pct,
            )
            # Out-of-sample backtest
            oos_sim = _run_backtest_on_slice(
                ohlcv, train_end, test_end, strategy,
                initial_balance=initial_balance, fee_pct=fee_pct,
            )

            is_ret = is_sim["metrics"]["total_return_pct"]
            oos_ret = oos_sim["metrics"]["total_return_pct"]
            is_sharpe = is_sim["metrics"]["sharpe_ratio"]
            oos_sharpe = oos_sim["metrics"]["sharpe_ratio"]

            windows.append({
                "window": window_num,
                "train_candles": train_candles,
                "test_candles": test_candles,
                "is_return": round(is_ret, 2),
                "oos_return": round(oos_ret, 2),
                "is_sharpe": round(is_sharpe, 2),
                "oos_sharpe": round(oos_sharpe, 2),
            })

            start += test_candles  # slide by test window

        if not windows:
            return json.dumps({
                "status": "error",
                "error": "Data too short for even one walk-forward window. "
                         "Reduce train_days/test_days or increase period.",
            })

        # Aggregate
        avg_is = sum(w["is_return"] for w in windows) / len(windows)
        avg_oos = sum(w["oos_return"] for w in windows) / len(windows)

        if abs(avg_is) > 0.01:
            degradation = abs(avg_is - avg_oos) / abs(avg_is) * 100.0
        else:
            degradation = 0.0

        # Classify overfitting risk
        if degradation < 25:
            risk = "low"
        elif degradation < 50:
            risk = "medium"
        else:
            risk = "high"

        return json.dumps({
            "status": "ok",
            "symbol": symbol,
            "strategy": strategy,
            "timeframe": timeframe,
            "period_days": period,
            "train_days": train_days,
            "test_days": test_days,
            "total_windows": len(windows),
            "windows": windows,
            "avg_is_return": round(avg_is, 2),
            "avg_oos_return": round(avg_oos, 2),
            "degradation_pct": round(degradation, 2),
            "overfitting_risk": risk,
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("quant_backtest walk_forward error: %s", e)
        return json.dumps({
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Stress-test action
# ---------------------------------------------------------------------------

def _action_stress_test(args: dict, **kw) -> str:
    """Stress-test a strategy under extreme market scenarios."""
    try:
        symbol = args.get("symbol", "BTC/USDT")
        strategy = args.get("strategy", "momentum")
        timeframe = args.get("timeframe", "1h")
        period = int(args.get("period", 30))
        scenario = args.get("scenario", "all")
        exchange = args.get("exchange", "okx")
        initial_balance = float(args.get("initial_balance", 10000))
        fee_pct = float(args.get("fee_pct", 0.1))

        if strategy not in ("momentum", "mean_reversion"):
            return json.dumps({
                "status": "error",
                "error": f"Unknown strategy '{strategy}'. Use: momentum, mean_reversion",
            })

        all_scenarios = ["flash_crash", "liquidity_crisis", "black_swan", "volatility_spike"]
        if scenario == "all":
            scenarios_to_run = all_scenarios
        elif scenario in all_scenarios:
            scenarios_to_run = [scenario]
        else:
            return json.dumps({
                "status": "error",
                "error": f"Unknown scenario '{scenario}'. Use: {', '.join(all_scenarios)}, all",
            })

        asset_class = args.get("asset_class", "crypto")
        ohlcv = _fetch_ohlcv(exchange, symbol, timeframe, period, asset_class)
        if not ohlcv or len(ohlcv) < 20:
            return json.dumps({
                "status": "error",
                "error": f"Not enough OHLCV data (got {len(ohlcv) if ohlcv else 0} candles, need 20+)",
            })

        # Baseline backtest
        baseline_sim = _run_backtest_on_slice(
            ohlcv, 0, len(ohlcv), strategy,
            initial_balance=initial_balance, fee_pct=fee_pct,
        )
        baseline_ret = baseline_sim["metrics"]["total_return_pct"]
        baseline_dd = baseline_sim["metrics"]["max_drawdown_pct"]

        results = []
        for sc in scenarios_to_run:
            modified = _modify_ohlcv_for_scenario(ohlcv, sc)
            stressed_sim = _run_backtest_on_slice(
                modified, 0, len(modified), strategy,
                initial_balance=initial_balance, fee_pct=fee_pct,
            )
            stressed_ret = stressed_sim["metrics"]["total_return_pct"]
            stressed_dd = stressed_sim["metrics"]["max_drawdown_pct"]

            # resilience_score = 1.0 - abs(stressed - baseline) / max(abs(baseline), 1)
            denominator = max(abs(baseline_ret), 1.0)
            resilience = 1.0 - abs(stressed_ret - baseline_ret) / denominator
            resilience = max(0.0, min(1.0, resilience))

            results.append({
                "name": sc,
                "baseline_return": round(baseline_ret, 2),
                "stressed_return": round(stressed_ret, 2),
                "baseline_dd": round(baseline_dd, 2),
                "stressed_dd": round(stressed_dd, 2),
                "resilience_score": round(resilience, 4),
            })

        avg_resilience = sum(r["resilience_score"] for r in results) / len(results) if results else 0.0

        return json.dumps({
            "status": "ok",
            "symbol": symbol,
            "strategy": strategy,
            "timeframe": timeframe,
            "period_days": period,
            "scenario_requested": scenario,
            "scenarios": results,
            "avg_resilience": round(avg_resilience, 4),
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("quant_backtest stress_test error: %s", e)
        return json.dumps({
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Comprehensive report action
# ---------------------------------------------------------------------------

def _action_report(args: dict, **kw) -> str:
    """Generate a comprehensive strategy comparison report."""
    try:
        symbol = args.get("symbol", "BTC/USDT")
        timeframe = args.get("timeframe", "1h")
        period = int(args.get("period", 30))
        exchange = args.get("exchange", "okx")
        initial_balance = float(args.get("initial_balance", 10000))
        fee_pct = float(args.get("fee_pct", 0.1))

        strategies = ["momentum", "mean_reversion"]

        asset_class = args.get("asset_class", "crypto")
        ohlcv = _fetch_ohlcv(exchange, symbol, timeframe, period, asset_class)
        if not ohlcv or len(ohlcv) < 20:
            return json.dumps({
                "status": "error",
                "error": f"Not enough OHLCV data (got {len(ohlcv) if ohlcv else 0} candles, need 20+)",
            })

        report_data = {
            "symbol": symbol,
            "timeframe": timeframe,
            "period_days": period,
            "candles_analyzed": len(ohlcv),
            "strategies": {},
        }

        # Buy-and-hold baseline
        first_close = float(ohlcv[0][4])
        last_close = float(ohlcv[-1][4])
        bah_return = ((last_close - first_close) / first_close) * 100.0
        report_data["buy_and_hold"] = {"return_pct": round(bah_return, 2)}

        best_score = -float("inf")
        recommendation = None

        for strat in strategies:
            # --- Full backtest ---
            bt_sim = _run_backtest_on_slice(
                ohlcv, 0, len(ohlcv), strat,
                initial_balance=initial_balance, fee_pct=fee_pct,
            )
            bt_metrics = bt_sim["metrics"]

            # --- Walk-forward ---
            tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60,
                           "4h": 240, "1d": 1440, "1w": 10080, "1M": 43200}
            minutes_per_candle = tf_minutes.get(timeframe, 60)
            candles_per_day = max(1, int(1440 / minutes_per_candle))
            train_candles = 7 * candles_per_day
            test_candles = 3 * candles_per_day
            window_size = train_candles + test_candles

            wf_windows = []
            start = 0
            while start + window_size <= len(ohlcv):
                is_sim = _run_backtest_on_slice(
                    ohlcv, start, start + train_candles, strat,
                    initial_balance=initial_balance, fee_pct=fee_pct,
                )
                oos_sim = _run_backtest_on_slice(
                    ohlcv, start + train_candles, start + window_size, strat,
                    initial_balance=initial_balance, fee_pct=fee_pct,
                )
                wf_windows.append({
                    "is_return": is_sim["metrics"]["total_return_pct"],
                    "oos_return": oos_sim["metrics"]["total_return_pct"],
                })
                start += test_candles

            avg_is = sum(w["is_return"] for w in wf_windows) / len(wf_windows) if wf_windows else 0.0
            avg_oos = sum(w["oos_return"] for w in wf_windows) / len(wf_windows) if wf_windows else 0.0
            degradation = (abs(avg_is - avg_oos) / abs(avg_is) * 100.0) if abs(avg_is) > 0.01 else 0.0
            if degradation < 25:
                risk = "low"
            elif degradation < 50:
                risk = "medium"
            else:
                risk = "high"

            wf_summary = {
                "windows": len(wf_windows),
                "avg_is_return": round(avg_is, 2),
                "avg_oos_return": round(avg_oos, 2),
                "degradation_pct": round(degradation, 2),
                "overfitting_risk": risk,
            }

            # --- Stress-test ---
            all_scenarios = ["flash_crash", "liquidity_crisis", "black_swan", "volatility_spike"]
            baseline_ret = bt_metrics["total_return_pct"]
            baseline_dd = bt_metrics["max_drawdown_pct"]
            stress_results = []
            for sc in all_scenarios:
                modified = _modify_ohlcv_for_scenario(ohlcv, sc)
                stressed_sim = _run_backtest_on_slice(
                    modified, 0, len(modified), strat,
                    initial_balance=initial_balance, fee_pct=fee_pct,
                )
                s_ret = stressed_sim["metrics"]["total_return_pct"]
                s_dd = stressed_sim["metrics"]["max_drawdown_pct"]
                denominator = max(abs(baseline_ret), 1.0)
                resilience = 1.0 - abs(s_ret - baseline_ret) / denominator
                resilience = max(0.0, min(1.0, resilience))
                stress_results.append({
                    "name": sc,
                    "stressed_return": round(s_ret, 2),
                    "stressed_dd": round(s_dd, 2),
                    "resilience_score": round(resilience, 4),
                })

            avg_resilience = sum(r["resilience_score"] for r in stress_results) / len(stress_results) if stress_results else 0.0
            st_summary = {
                "scenarios": stress_results,
                "avg_resilience": round(avg_resilience, 4),
            }

            # Composite score: return * (1 - degradation/100) * avg_resilience
            # Higher is better. Penalise overfitting and fragility.
            score = bt_metrics["total_return_pct"] * (1.0 - degradation / 100.0) * avg_resilience
            # Ensure score isn't ruined by negative returns being "amplified"
            # Keep it simple: we just compare scores.
            if score > best_score:
                best_score = score
                recommendation = strat

            report_data["strategies"][strat] = {
                "backtest_metrics": bt_metrics,
                "walk_forward_summary": wf_summary,
                "stress_test_summary": st_summary,
                "composite_score": round(score, 4),
            }

        report_data["recommendation"] = recommendation

        # Store the report in the DB
        try:
            conn = _get_conn()
            conn.execute("""
                INSERT INTO backtest_results
                (strategy, symbol, timeframe, period_days, initial_balance, fee_pct,
                 total_return_pct, sharpe_ratio, sortino_ratio, max_drawdown_pct,
                 win_rate, profit_factor, total_trades, avg_trade_pnl,
                 trades_json, equity_start, equity_end)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"report_{recommendation}",
                symbol, timeframe, period, initial_balance, fee_pct,
                report_data["strategies"][recommendation]["backtest_metrics"]["total_return_pct"],
                report_data["strategies"][recommendation]["backtest_metrics"]["sharpe_ratio"],
                report_data["strategies"][recommendation]["backtest_metrics"]["sortino_ratio"],
                report_data["strategies"][recommendation]["backtest_metrics"]["max_drawdown_pct"],
                report_data["strategies"][recommendation]["backtest_metrics"]["win_rate"],
                report_data["strategies"][recommendation]["backtest_metrics"]["profit_factor"],
                report_data["strategies"][recommendation]["backtest_metrics"]["total_trades"],
                report_data["strategies"][recommendation]["backtest_metrics"]["avg_trade_pnl"],
                json.dumps(report_data),
                initial_balance,
                initial_balance * (1 + report_data["strategies"][recommendation]["backtest_metrics"]["total_return_pct"] / 100),
            ))
            conn.commit()
            conn.close()
        except Exception as db_err:
            logger.warning("Failed to store report in DB: %s", db_err)

        return json.dumps({
            "status": "ok",
            "report": report_data,
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("quant_backtest report error: %s", e)
        return json.dumps({
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def _handle_quant_backtest(args: dict, **kw) -> str:
    """Dispatch quant_backtest actions."""
    action = args.get("action", "backtest")
    handlers = {
        "backtest": _action_backtest,
        "results": _action_results,
        "compare": _action_compare,
        "walk_forward": _action_walk_forward,
        "stress_test": _action_stress_test,
        "report": _action_report,
    }
    handler = handlers.get(action)
    if not handler:
        return json.dumps({
            "status": "error",
            "error": f"Unknown action '{action}'. Valid: backtest, results, compare, walk_forward, stress_test, report",
        })
    return handler(args, **kw)


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_ccxt() -> bool:
    """Return True if ccxt is importable."""
    try:
        import ccxt  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Schema (OpenAI function-calling format)
# ---------------------------------------------------------------------------

QUANT_BACKTEST_SCHEMA = {
    "name": "quant_backtest",
    "description": (
        "Backtest trading strategies against historical data. "
        "Actions: 'backtest' (run a strategy simulation computing Sharpe ratio, "
        "Sortino ratio, max drawdown, win rate, profit factor), "
        "'results' (query past backtest results from DB), "
        "'compare' (run two strategies on same data and compare), "
        "'walk_forward' (rolling in-sample / out-of-sample validation to detect overfitting), "
        "'stress_test' (test strategy robustness under extreme market scenarios), "
        "'report' (comprehensive strategy comparison with walk-forward, stress-test, "
        "and buy-and-hold baseline). "
        "Strategies: 'momentum' (trend-following via SMA/EMA/RSI/MACD/BB), "
        "'mean_reversion' (oversold/overbought via RSI + Bollinger Bands). "
        "Uses pure-Python indicator math — no external backtest libraries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["backtest", "results", "compare", "walk_forward", "stress_test", "report"],
                "description": (
                    "Action: 'backtest' to run a strategy simulation, "
                    "'results' to query past backtest results, "
                    "'compare' to run two strategies and compare, "
                    "'walk_forward' for rolling out-of-sample validation, "
                    "'stress_test' to test under extreme market scenarios, "
                    "'report' for a comprehensive strategy comparison report."
                ),
            },
            "symbol": {
                "type": "string",
                "description": "Trading pair symbol (e.g. 'BTC/USDT'). Default: 'BTC/USDT'.",
                "default": "BTC/USDT",
            },
            "strategy": {
                "type": "string",
                "enum": ["momentum", "mean_reversion"],
                "description": (
                    "Strategy type: 'momentum' (trend-following: SMA crossover + "
                    "EMA crossover + RSI 40-70 + MACD histogram > 0 + price > BB mid), "
                    "'mean_reversion' (RSI < 30 + price < BB lower -> buy; "
                    "RSI > 70 + price > BB upper -> sell). Default: 'momentum'."
                ),
                "default": "momentum",
            },
            "timeframe": {
                "type": "string",
                "description": "Candle timeframe (e.g. '1m', '5m', '15m', '1h', '4h', '1d'). Default: '1h'.",
                "default": "1h",
            },
            "period": {
                "type": "integer",
                "description": "Days of historical data to backtest over. Default: 30.",
                "default": 30,
            },
            "initial_balance": {
                "type": "number",
                "description": "Starting balance in USD for the simulation. Default: 10000.",
                "default": 10000,
            },
            "fee_pct": {
                "type": "number",
                "description": "Trading fee as percentage per trade (e.g. 0.1 for 0.1%). Default: 0.1.",
                "default": 0.1,
            },
            "limit": {
                "type": "integer",
                "description": "Max number of past results to return (for 'results' action). Default: 10.",
                "default": 10,
            },
            "strategies": {
                "type": "array",
                "items": {
                    "type": "string",
                },
                "description": (
                    "Two strategy names to compare (for 'compare' action). "
                    "Default: ['momentum', 'mean_reversion']."
                ),
            },
            "train_days": {
                "type": "integer",
                "description": "Walk-forward in-sample (train) window in days. Default: 7.",
                "default": 7,
            },
            "test_days": {
                "type": "integer",
                "description": "Walk-forward out-of-sample (test) window in days. Default: 3.",
                "default": 3,
            },
            "scenario": {
                "type": "string",
                "enum": ["flash_crash", "liquidity_crisis", "black_swan", "volatility_spike", "all"],
                "description": (
                    "Stress-test scenario to run (for 'stress_test' action). "
                    "'flash_crash': 20% drop then 10% recovery, "
                    "'liquidity_crisis': 5x spread widening (1% slippage), "
                    "'black_swan': sustained 30% decline over 3 days, "
                    "'volatility_spike': ATR triples for 48 hours, "
                    "'all': run all scenarios. Default: 'all'."
                ),
                "default": "all",
            },
            "asset_class": {
                "type": "string",
                "enum": ["crypto", "stock", "fx"],
                "description": (
                    "Asset class for data fetching. 'crypto' uses CCXT (default), "
                    "'stock' and 'fx' use yfinance. Affects symbol format and data source."
                ),
                "default": "crypto",
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
    name="quant_backtest",
    toolset="quant",
    schema=QUANT_BACKTEST_SCHEMA,
    handler=_handle_quant_backtest,
    check_fn=_check_ccxt,
    requires_env=[],
    is_async=False,
    description="Backtest trading strategies against historical data with performance metrics.",
    emoji="📈",
)
