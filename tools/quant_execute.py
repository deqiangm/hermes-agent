"""Quantitative trading execution tool for Hermes Agent.

Supports crypto (CCXT) and stocks (yfinance) via quant_data asset_class parameter.

Handles paper trading (simulated) and live order placement with safety layers:
- Paper mode: simulate orders, track positions in local SQLite DB
- Live mode: requires QUANT_LIVE_TRADING_ENABLED env var; otherwise rejected
- Max position size: configurable via QUANT_MAX_POSITION_USD, default $5000
- Kill switch: if daily loss > 5%, reject all orders
- Safety Shell enhancements:
 - Stop-loss: auto-set at 1.5x ATR below entry on buy; auto-sell on breach
 - Consecutive loss cooldown: 4h cooldown after 3 consecutive losing trades
 - Max open positions: limit to 3 concurrent positions
 - stop_check action: scan all positions and auto-sell if stop-loss breached
- Supported actions: buy, sell, status, history, balance, stop_check
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

DEFAULT_INITIAL_BALANCE = 100_000.0
DEFAULT_FEE_RATE = 0.001  # 0.1%
DEFAULT_MAX_POSITION_USD = 5000.0
DAILY_LOSS_THRESHOLD = 0.05  # 5%
DEFAULT_EXCHANGE = "okx"
DEFAULT_SYMBOL = "BTC/USDT"
MAX_OPEN_POSITIONS = 3
COOLDOWN_HOURS = 4
COOLDOWN_LOSS_THRESHOLD = 3  # consecutive losses to trigger cooldown
ATR_STOP_MULTIPLIER = 1.5


def _get_db_path() -> Path:
    """Return the SQLite database path under HERMES_HOME."""
    db_dir = get_hermes_home() / "quant_trading"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "paper_trading.db"


def _get_max_position_usd() -> float:
    """Return max position size in USD from env or default."""
    try:
        return float(os.getenv("QUANT_MAX_POSITION_USD", str(DEFAULT_MAX_POSITION_USD)))
    except (ValueError, TypeError):
        return DEFAULT_MAX_POSITION_USD


def _is_live_trading_enabled() -> bool:
    """Check if live trading is explicitly enabled via env var."""
    return os.getenv("QUANT_LIVE_TRADING_ENABLED", "").lower() == "true"


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _get_db_connection() -> sqlite3.Connection:
    """Return a connection to the paper trading database, initializing if needed."""
    db_path = _get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_tables(conn)
    return conn


_SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS balance (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    usd REAL NOT NULL DEFAULT 0.0,
    initial_usd REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    amount REAL NOT NULL,
    entry_price REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL NOT NULL,
    price REAL NOT NULL,
    order_type TEXT NOT NULL DEFAULT 'market',
    mode TEXT NOT NULL DEFAULT 'paper',
    fee REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'filled',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    date TEXT PRIMARY KEY,
    realized_pnl REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS trade_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    realized_pnl REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cooldown_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cooldown_until TEXT,
    consecutive_losses INTEGER NOT NULL DEFAULT 0
);
"""


def _init_tables(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript(_SQL_SCHEMA)
    # Initialize balance row if missing
    row = conn.execute("SELECT usd FROM balance WHERE id = 1").fetchone()
    if row is None:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO balance (id, usd, initial_usd, updated_at) VALUES (1, ?, ?, ?)",
            (DEFAULT_INITIAL_BALANCE, DEFAULT_INITIAL_BALANCE, now),
        )
    # Initialize cooldown_state row if missing
    cd_row = conn.execute("SELECT id FROM cooldown_state WHERE id = 1").fetchone()
    if cd_row is None:
        conn.execute(
            "INSERT INTO cooldown_state (id, cooldown_until, consecutive_losses) VALUES (1, NULL, 0)"
        )
    # Migration: add stop_loss_price column if it doesn't exist
    try:
        conn.execute("SELECT stop_loss_price FROM positions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE positions ADD COLUMN stop_loss_price REAL DEFAULT NULL")
        conn.commit()
    # Migration: add asset_class column if it doesn't exist
    try:
        conn.execute("SELECT asset_class FROM positions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE positions ADD COLUMN asset_class TEXT DEFAULT 'crypto'")
        conn.commit()


def _reset_balance_if_needed(conn: sqlite3.Connection) -> None:
    """Ensure balance row exists; reset to initial if something went wrong."""
    row = conn.execute("SELECT usd, initial_usd FROM balance WHERE id = 1").fetchone()
    now = datetime.now(timezone.utc).isoformat()
    if row is None:
        conn.execute(
            "INSERT INTO balance (id, usd, initial_usd, updated_at) VALUES (1, ?, ?, ?)",
            (DEFAULT_INITIAL_BALANCE, DEFAULT_INITIAL_BALANCE, now),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Price fetching (multi-asset: crypto via ccxt, stock/fx via yfinance)
# ---------------------------------------------------------------------------

def _yf_symbol(symbol: str, asset_class: str) -> str:
    """Convert symbol to yfinance format.

    stock: 'AAPL' -> 'AAPL'
    fx:    'EUR/USD' -> 'EURUSD=X'
    """
    if asset_class == "fx":
        if "=X" in symbol:
            return symbol
        return symbol.replace("/", "") + "=X"
    return symbol


def _fetch_current_price(
    symbol: str,
    exchange_id: str = DEFAULT_EXCHANGE,
    asset_class: str = "crypto",
) -> Optional[float]:
    """Fetch the current ticker price for a symbol.

    crypto -> CCXT (exchange_id)
    stock/fx -> yfinance
    """
    if asset_class in ("stock", "fx"):
        try:
            import yfinance as yf
            yf_sym = _yf_symbol(symbol, asset_class)
            tk = yf.Ticker(yf_sym)
            info = tk.info
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            if price is not None:
                return float(price)
            # Fallback: try fast_info
            try:
                return float(tk.fast_info.last_price)
            except Exception:
                pass
            logger.error("No price in yfinance for %s (%s)", symbol, asset_class)
            return None
        except Exception as e:
            logger.error("Failed to fetch yfinance price for %s (%s): %s", symbol, asset_class, e)
            return None

    # Default: crypto via CCXT
    try:
        import ccxt
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None:
            logger.error("Unknown exchange id: %s", exchange_id)
            return None
        exchange = exchange_class()
        ticker = exchange.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if price is not None:
            return float(price)
        logger.error("No price in ticker for %s on %s", symbol, exchange_id)
        return None
    except Exception as e:
        logger.error("Failed to fetch price for %s on %s: %s", symbol, exchange_id, e)
        return None


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------

def _check_daily_loss_kill_switch(conn: sqlite3.Connection) -> Optional[str]:
    """Return an error message if daily loss exceeds threshold, else None."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT realized_pnl FROM daily_pnl WHERE date = ?", (today,)
    ).fetchone()
    if row is None:
        return None

    realized_pnl = row["realized_pnl"]
    balance_row = conn.execute("SELECT initial_usd FROM balance WHERE id = 1").fetchone()
    if balance_row is None:
        return None

    initial_usd = balance_row["initial_usd"]
    if initial_usd <= 0:
        return None

    daily_loss_ratio = -realized_pnl / initial_usd
    if realized_pnl < 0 and daily_loss_ratio > DAILY_LOSS_THRESHOLD:
        return (
            f"Kill switch triggered: daily loss {daily_loss_ratio:.2%} exceeds "
            f"{DAILY_LOSS_THRESHOLD:.0%} threshold. All orders rejected for today."
        )
    return None


def _check_position_size(symbol: str, amount: float, price: float) -> Optional[str]:
    """Return an error message if position value exceeds max, else None."""
    max_usd = _get_max_position_usd()
    position_value = amount * price
    if position_value > max_usd:
        return (
            f"Position size ${position_value:,.2f} exceeds max "
            f"${max_usd:,.2f}. Reduce amount or adjust QUANT_MAX_POSITION_USD."
        )
    return None


def _check_max_open_positions(conn: sqlite3.Connection) -> Optional[str]:
    """Return an error message if max open positions reached, else None."""
    row = conn.execute("SELECT COUNT(*) AS cnt FROM positions").fetchone()
    count = row["cnt"] if row else 0
    if count >= MAX_OPEN_POSITIONS:
        return (
            f"Max open positions reached ({count}/{MAX_OPEN_POSITIONS}). "
            f"Close an existing position before opening a new one."
        )
    return None


def _check_cooldown(conn: sqlite3.Connection) -> Optional[str]:
    """Return an error message if in consecutive-loss cooldown, else None."""
    cd_row = conn.execute(
        "SELECT cooldown_until, consecutive_losses FROM cooldown_state WHERE id = 1"
    ).fetchone()
    if cd_row is None:
        return None
    cooldown_until = cd_row["cooldown_until"]
    consecutive_losses = cd_row["consecutive_losses"]
    if cooldown_until is not None:
        now = datetime.now(timezone.utc)
        try:
            cd_time = datetime.fromisoformat(cooldown_until)
            if now < cd_time:
                remaining = cd_time - now
                mins = int(remaining.total_seconds() / 60)
                return (
                    f"Cooldown active: {consecutive_losses} consecutive losses. "
                    f"Trading resumes in ~{mins} minutes (at {cd_time.strftime('%H:%M UTC')})."
                )
        except (ValueError, TypeError):
            pass  # invalid timestamp, clear it
        # Cooldown expired, clear it
        conn.execute(
            "UPDATE cooldown_state SET cooldown_until = NULL WHERE id = 1"
        )
        conn.commit()
    return None


def _record_trade_result(conn: sqlite3.Connection, realized_pnl: float) -> None:
    """Record trade result and update consecutive loss / cooldown state."""
    now = datetime.now(timezone.utc).isoformat()
    # Log the trade result
    conn.execute(
        "INSERT INTO trade_results (realized_pnl, created_at) VALUES (?, ?)",
        (realized_pnl, now),
    )
    # Get current cooldown state
    cd_row = conn.execute(
        "SELECT consecutive_losses FROM cooldown_state WHERE id = 1"
    ).fetchone()
    current_losses = cd_row["consecutive_losses"] if cd_row else 0

    if realized_pnl < 0:
        new_losses = current_losses + 1
        if new_losses >= COOLDOWN_LOSS_THRESHOLD:
            cooldown_until = (
                datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_HOURS)
            ).isoformat()
            conn.execute(
                "UPDATE cooldown_state SET consecutive_losses = ?, cooldown_until = ? WHERE id = 1",
                (new_losses, cooldown_until),
            )
        else:
            conn.execute(
                "UPDATE cooldown_state SET consecutive_losses = ? WHERE id = 1",
                (new_losses,),
            )
    else:
        # Winning trade resets consecutive losses
        conn.execute(
            "UPDATE cooldown_state SET consecutive_losses = 0, cooldown_until = NULL WHERE id = 1"
        )
    conn.commit()


def _check_stop_loss_hit(
    symbol: str, position: dict, exchange_id: str = DEFAULT_EXCHANGE
) -> Optional[str]:
    """Check if stop-loss is breached for a position; auto-sell if hit.
    Returns a description string if an auto-sell was triggered, else None."""
    stop_loss_price = position.get("stop_loss_price")
    if stop_loss_price is None:
        return None
    current_price = _fetch_current_price(symbol, exchange_id)
    if current_price is None:
        return None
    if current_price < stop_loss_price:
        # Auto-sell triggered
        logger.warning(
            "Stop-loss hit for %s: current %.4f < stop %.4f. Auto-selling.",
            symbol, current_price, stop_loss_price,
        )
        sell_args = {
            "symbol": symbol,
            "amount": position["amount"],
            "order_type": "market",
            "mode": "paper",
            "exchange": exchange_id,
            "_auto_stop_loss": True,
        }
        result = _handle_sell(sell_args)
        return (
            f"Stop-loss auto-sell triggered for {symbol} at {current_price:.4f} "
            f"(stop: {stop_loss_price:.4f}). Result: {result}"
        )
    return None


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _handle_buy(args: dict, **kw) -> str:
    """Execute a buy order (paper or live)."""
    symbol = args.get("symbol", DEFAULT_SYMBOL)
    amount = float(args.get("amount", 0))
    order_type = args.get("order_type", "market")
    price_arg = args.get("price")
    mode = args.get("mode", "paper")
    exchange_id = args.get("exchange", DEFAULT_EXCHANGE)
    atr = args.get("atr")
    asset_class = args.get("asset_class", "crypto")

    if amount <= 0:
        return tool_error("Amount must be positive for buy orders.")

    # Safety: live mode guard
    if mode == "live" and not _is_live_trading_enabled():
        return tool_error(
            "Live trading rejected: QUANT_LIVE_TRADING_ENABLED is not set to 'true'. "
            "Use paper mode for simulation."
        )

    conn = _get_db_connection()
    try:
        _reset_balance_if_needed(conn)

        # Kill switch check
        kill_msg = _check_daily_loss_kill_switch(conn)
        if kill_msg:
            return tool_error(kill_msg)

        # Cooldown check
        cooldown_msg = _check_cooldown(conn)
        if cooldown_msg:
            return tool_error(cooldown_msg)

        # Max open positions check (only for new positions, not averaging in)
        existing_pos = conn.execute(
            "SELECT id FROM positions WHERE symbol = ? AND side = 'buy'",
            (symbol,),
        ).fetchone()
        if existing_pos is None:
            max_msg = _check_max_open_positions(conn)
            if max_msg:
                return tool_error(max_msg)

        # Determine execution price
        if order_type == "limit":
            if price_arg is None:
                return tool_error("Price is required for limit orders.")
            exec_price = float(price_arg)
        else:
            exec_price = _fetch_current_price(symbol, exchange_id, asset_class)
            if exec_price is None:
                return tool_error(
                    f"Could not fetch current price for {symbol} on {exchange_id}. "
                    f"Try again or use a limit order with explicit price."
                )

        # Position size check
        size_msg = _check_position_size(symbol, amount, exec_price)
        if size_msg:
            return tool_error(size_msg)

        cost = amount * exec_price
        fee = cost * DEFAULT_FEE_RATE
        total_cost = cost + fee

        # Balance check
        row = conn.execute("SELECT usd FROM balance WHERE id = 1").fetchone()
        current_balance = row["usd"] if row else 0.0
        if current_balance < total_cost:
            return tool_error(
                f"Insufficient balance: ${current_balance:,.2f} USD available, "
                f"but order requires ${total_cost:,.2f} (cost ${cost:,.2f} + fee ${fee:,.2f})."
            )

        now = datetime.now(timezone.utc).isoformat()

        if mode == "live":
            # Execute real order via ccxt
            try:
                import ccxt
                exchange_class = getattr(ccxt, exchange_id, None)
                if exchange_class is None:
                    return tool_error(f"Unknown exchange: {exchange_id}")
                exchange = exchange_class()
                if order_type == "limit":
                    order = exchange.create_limit_buy_order(symbol, amount, exec_price)
                else:
                    order = exchange.create_market_buy_order(symbol, amount)
                # Record the live order
                conn.execute(
                    "INSERT INTO orders (symbol, side, amount, price, order_type, mode, fee, status, created_at) "
                    "VALUES (?, 'buy', ?, ?, ?, 'live', ?, 'filled', ?)",
                    (symbol, amount, exec_price, order_type, fee, now),
                )
            except Exception as e:
                return tool_error(f"Live order failed: {e}")
        else:
            # Paper mode: just record it
            conn.execute(
                "INSERT INTO orders (symbol, side, amount, price, order_type, mode, fee, status, created_at) "
                "VALUES (?, 'buy', ?, ?, ?, 'paper', ?, 'filled', ?)",
                (symbol, amount, exec_price, order_type, fee, now),
            )

        # Update balance
        conn.execute(
            "UPDATE balance SET usd = usd - ?, updated_at = ? WHERE id = 1",
            (total_cost, now),
        )

        # Track position
        existing = conn.execute(
            "SELECT id, amount, entry_price, stop_loss_price, asset_class FROM positions WHERE symbol = ? AND side = 'buy'",
            (symbol,),
        ).fetchone()

        # Calculate stop_loss_price for this buy
        stop_loss_price = None
        if atr is not None:
            try:
                atr_val = float(atr)
                stop_loss_price = exec_price - ATR_STOP_MULTIPLIER * atr_val
            except (ValueError, TypeError):
                logger.warning("Invalid ATR value '%s' provided; stop-loss not set.", atr)

        if existing:
            # Average in
            old_amount = existing["amount"]
            old_entry = existing["entry_price"]
            old_stop = existing["stop_loss_price"]
            new_amount = old_amount + amount
            new_entry = (old_amount * old_entry + amount * exec_price) / new_amount
            # Update stop_loss: use the lower of existing and new stop, or set new if none
            if stop_loss_price is not None:
                if old_stop is not None:
                    new_stop = min(old_stop, stop_loss_price)
                else:
                    new_stop = stop_loss_price
            else:
                new_stop = old_stop
            conn.execute(
                "UPDATE positions SET amount = ?, entry_price = ?, stop_loss_price = ?, updated_at = ? WHERE id = ?",
                (new_amount, new_entry, new_stop, now, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO positions (symbol, side, amount, entry_price, stop_loss_price, asset_class, created_at, updated_at) "
                "VALUES (?, 'buy', ?, ?, ?, ?, ?, ?)",
                (symbol, amount, exec_price, stop_loss_price, asset_class, now, now),
            )

        conn.commit()
        return tool_result(
            action="buy",
            symbol=symbol,
            side="buy",
            amount=amount,
            price=exec_price,
            order_type=order_type,
            mode=mode,
            fee=round(fee, 4),
            cost=round(cost, 4),
            total_cost=round(total_cost, 4),
            stop_loss_price=round(stop_loss_price, 4) if stop_loss_price is not None else None,
            status="filled",
        )
    except Exception as e:
        logger.error("Buy error: %s", e)
        return tool_error(f"Buy order failed: {e}")
    finally:
        conn.close()


def _handle_sell(args: dict, **kw) -> str:
    """Execute a sell order (paper or live)."""
    symbol = args.get("symbol", DEFAULT_SYMBOL)
    amount = float(args.get("amount", 0))
    order_type = args.get("order_type", "market")
    price_arg = args.get("price")
    mode = args.get("mode", "paper")
    exchange_id = args.get("exchange", DEFAULT_EXCHANGE)
    asset_class = args.get("asset_class", "crypto")

    if amount <= 0:
        return tool_error("Amount must be positive for sell orders.")

    if mode == "live" and not _is_live_trading_enabled():
        return tool_error(
            "Live trading rejected: QUANT_LIVE_TRADING_ENABLED is not set to 'true'. "
            "Use paper mode for simulation."
        )

    conn = _get_db_connection()
    try:
        _reset_balance_if_needed(conn)

        kill_msg = _check_daily_loss_kill_switch(conn)
        if kill_msg:
            return tool_error(kill_msg)

        # Check we have enough of the position
        position = conn.execute(
            "SELECT id, amount, entry_price, stop_loss_price, asset_class FROM positions WHERE symbol = ? AND side = 'buy'",
            (symbol,),
        ).fetchone()
        if position is None or position["amount"] < amount:
            held = position["amount"] if position else 0.0
            return tool_error(
                f"Insufficient position: holding {held} {symbol.split('/')[0]}, "
                f"requested to sell {amount}."
            )

        # Use position's asset_class if available, otherwise fall back to args
        pos_asset_class = position["asset_class"] if position and position["asset_class"] else asset_class

        # Determine execution price
        if order_type == "limit":
            if price_arg is None:
                return tool_error("Price is required for limit orders.")
            exec_price = float(price_arg)
        else:
            exec_price = _fetch_current_price(symbol, exchange_id, pos_asset_class)
            if exec_price is None:
                return tool_error(
                    f"Could not fetch current price for {symbol} ({pos_asset_class}) on {exchange_id}."
                )

        size_msg = _check_position_size(symbol, amount, exec_price)
        if size_msg:
            return tool_error(size_msg)

        proceeds = amount * exec_price
        fee = proceeds * DEFAULT_FEE_RATE
        net_proceeds = proceeds - fee

        # Realized PnL
        entry_price = position["entry_price"]
        realized_pnl = (exec_price - entry_price) * amount - fee

        now = datetime.now(timezone.utc).isoformat()

        if mode == "live":
            try:
                import ccxt
                exchange_class = getattr(ccxt, exchange_id, None)
                if exchange_class is None:
                    return tool_error(f"Unknown exchange: {exchange_id}")
                exchange = exchange_class()
                if order_type == "limit":
                    order = exchange.create_limit_sell_order(symbol, amount, exec_price)
                else:
                    order = exchange.create_market_sell_order(symbol, amount)
                conn.execute(
                    "INSERT INTO orders (symbol, side, amount, price, order_type, mode, fee, status, created_at) "
                    "VALUES (?, 'sell', ?, ?, ?, 'live', ?, 'filled', ?)",
                    (symbol, amount, exec_price, order_type, fee, now),
                )
            except Exception as e:
                return tool_error(f"Live order failed: {e}")
        else:
            conn.execute(
                "INSERT INTO orders (symbol, side, amount, price, order_type, mode, fee, status, created_at) "
                "VALUES (?, 'sell', ?, ?, ?, 'paper', ?, 'filled', ?)",
                (symbol, amount, exec_price, order_type, fee, now),
            )

        # Update balance
        conn.execute(
            "UPDATE balance SET usd = usd + ?, updated_at = ? WHERE id = 1",
            (net_proceeds, now),
        )

        # Update or remove position
        remaining = position["amount"] - amount
        if remaining > 1e-10:
            conn.execute(
                "UPDATE positions SET amount = ?, updated_at = ? WHERE id = ?",
                (remaining, now, position["id"]),
            )
        else:
            conn.execute("DELETE FROM positions WHERE id = ?", (position["id"],))

        # Track daily realized PnL
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        existing_pnl = conn.execute(
            "SELECT realized_pnl FROM daily_pnl WHERE date = ?", (today,)
        ).fetchone()
        if existing_pnl is not None:
            conn.execute(
                "UPDATE daily_pnl SET realized_pnl = realized_pnl + ? WHERE date = ?",
                (realized_pnl, today),
            )
        else:
            conn.execute(
                "INSERT INTO daily_pnl (date, realized_pnl) VALUES (?, ?)",
                (today, realized_pnl),
            )

        # Record trade result for cooldown tracking
        # (skip if auto stop-loss to avoid double-counting)
        if not args.get("_auto_stop_loss"):
            _record_trade_result(conn, realized_pnl)

        conn.commit()
        return tool_result(
            action="sell",
            symbol=symbol,
            side="sell",
            amount=amount,
            price=exec_price,
            order_type=order_type,
            mode=mode,
            fee=round(fee, 4),
            proceeds=round(proceeds, 4),
            net_proceeds=round(net_proceeds, 4),
            realized_pnl=round(realized_pnl, 4),
            entry_price=entry_price,
            stop_loss_price=position["stop_loss_price"],
            status="filled",
            stop_loss_triggered=args.get("_auto_stop_loss", False),
        )
    except Exception as e:
        logger.error("Sell error: %s", e)
        return tool_error(f"Sell order failed: {e}")
    finally:
        conn.close()


def _handle_status(args: dict, **kw) -> str:
    """Return current positions and unrealized PnL."""
    symbol = args.get("symbol", DEFAULT_SYMBOL)
    exchange_id = args.get("exchange", DEFAULT_EXCHANGE)
    default_asset_class = args.get("asset_class", "crypto")

    conn = _get_db_connection()
    try:
        _reset_balance_if_needed(conn)

        positions = conn.execute(
            "SELECT id, symbol, side, amount, entry_price, stop_loss_price, asset_class, created_at FROM positions"
        ).fetchall()

        current_price = _fetch_current_price(symbol, exchange_id, default_asset_class)

        position_list = []
        total_unrealized_pnl = 0.0
        for pos in positions:
            pos_symbol = pos["symbol"]
            pos_ac = pos["asset_class"] if pos["asset_class"] else default_asset_class
            # Try to get price for each position's symbol
            if pos_symbol == symbol and current_price is not None:
                price = current_price
            else:
                price = _fetch_current_price(pos_symbol, exchange_id, pos_ac)

            entry = pos["entry_price"]
            amt = pos["amount"]
            unrealized = (price - entry) * amt if price else 0.0
            total_unrealized_pnl += unrealized

            position_list.append({
                "id": pos["id"],
                "symbol": pos_symbol,
                "side": pos["side"],
                "amount": amt,
                "entry_price": entry,
                "current_price": price,
                "unrealized_pnl": round(unrealized, 4),
                "value_usd": round(amt * price, 4) if price else None,
                "stop_loss_price": pos["stop_loss_price"],
                "stop_loss_distance_pct": (
                    round((entry - pos["stop_loss_price"]) / entry * 100, 4)
                    if pos["stop_loss_price"] and entry
                    else None
                ),
                "created_at": pos["created_at"],
            })

        # Daily PnL
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl_row = conn.execute(
            "SELECT realized_pnl FROM daily_pnl WHERE date = ?", (today,)
        ).fetchone()
        daily_realized = pnl_row["realized_pnl"] if pnl_row else 0.0

        balance_row = conn.execute("SELECT usd, initial_usd FROM balance WHERE id = 1").fetchone()
        current_balance = balance_row["usd"] if balance_row else 0.0
        initial_balance = balance_row["initial_usd"] if balance_row else DEFAULT_INITIAL_BALANCE

        return tool_result(
            action="status",
            balance_usd=round(current_balance, 2),
            initial_balance_usd=round(initial_balance, 2),
            positions=position_list,
            position_count=len(position_list),
            unrealized_pnl=round(total_unrealized_pnl, 4),
            daily_realized_pnl=round(daily_realized, 4),
            total_pnl=round(total_unrealized_pnl + daily_realized, 4),
            kill_switch_active=_check_daily_loss_kill_switch(conn) is not None,
            cooldown_active=_check_cooldown(conn) is not None,
        )
    except Exception as e:
        logger.error("Status error: %s", e)
        return tool_error(f"Status check failed: {e}")
    finally:
        conn.close()


def _handle_history(args: dict, **kw) -> str:
    """Return recent order history."""
    limit = int(args.get("limit", 20))

    conn = _get_db_connection()
    try:
        orders = conn.execute(
            "SELECT id, symbol, side, amount, price, order_type, mode, fee, status, created_at "
            "FROM orders ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

        order_list = [
            {
                "id": o["id"],
                "symbol": o["symbol"],
                "side": o["side"],
                "amount": o["amount"],
                "price": o["price"],
                "order_type": o["order_type"],
                "mode": o["mode"],
                "fee": o["fee"],
                "status": o["status"],
                "created_at": o["created_at"],
            }
            for o in orders
        ]

        return tool_result(
            action="history",
            orders=order_list,
            count=len(order_list),
        )
    except Exception as e:
        logger.error("History error: %s", e)
        return tool_error(f"History query failed: {e}")
    finally:
        conn.close()


def _handle_balance(args: dict, **kw) -> str:
    """Return current balance info."""
    conn = _get_db_connection()
    try:
        _reset_balance_if_needed(conn)
        row = conn.execute("SELECT usd, initial_usd, updated_at FROM balance WHERE id = 1").fetchone()
        if row is None:
            return tool_error("Balance record not found.")

        current = row["usd"]
        initial = row["initial_usd"]
        total_pnl = current - initial

        return tool_result(
            action="balance",
            usd=round(current, 2),
            initial_usd=round(initial, 2),
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl / initial * 100, 4) if initial > 0 else 0.0,
            updated_at=row["updated_at"],
        )
    except Exception as e:
        logger.error("Balance error: %s", e)
        return tool_error(f"Balance query failed: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stop-check action
# ---------------------------------------------------------------------------

def _handle_stop_check(args: dict, **kw) -> str:
    """Scan all positions with stop-loss prices and auto-sell if breached."""
    exchange_id = args.get("exchange", DEFAULT_EXCHANGE)
    default_asset_class = args.get("asset_class", "crypto")

    conn = _get_db_connection()
    try:
        _reset_balance_if_needed(conn)

        positions = conn.execute(
            "SELECT id, symbol, side, amount, entry_price, stop_loss_price, asset_class, created_at "
            "FROM positions WHERE stop_loss_price IS NOT NULL"
        ).fetchall()

        if not positions:
            return tool_result(
                action="stop_check",
                positions_scanned=0,
                stop_losses_triggered=0,
                message="No positions with stop-loss prices set.",
            )

        triggered = []
        safe = []
        for pos in positions:
            pos_symbol = pos["symbol"]
            stop_price = pos["stop_loss_price"]
            pos_ac = pos["asset_class"] if pos["asset_class"] else default_asset_class
            current_price = _fetch_current_price(pos_symbol, exchange_id, pos_ac)
            if current_price is None:
                safe.append({
                    "id": pos["id"],
                    "symbol": pos_symbol,
                    "stop_loss_price": stop_price,
                    "current_price": None,
                    "status": "price_unavailable",
                })
                continue
            if current_price < stop_price:
                # Stop-loss breached — auto-sell
                logger.warning(
                    "Stop-check: %s current %.4f < stop %.4f. Auto-selling.",
                    pos_symbol, current_price, stop_price,
                )
                sell_args = {
                    "symbol": pos_symbol,
                    "amount": pos["amount"],
                    "order_type": "market",
                    "mode": "paper",
                    "exchange": exchange_id,
                    "_auto_stop_loss": True,
                }
                sell_result = _handle_sell(sell_args)
                triggered.append({
                    "id": pos["id"],
                    "symbol": pos_symbol,
                    "stop_loss_price": stop_price,
                    "current_price": current_price,
                    "amount_sold": pos["amount"],
                    "entry_price": pos["entry_price"],
                    "status": "auto_sold",
                    "sell_result": sell_result,
                })
            else:
                safe.append({
                    "id": pos["id"],
                    "symbol": pos_symbol,
                    "stop_loss_price": stop_price,
                    "current_price": current_price,
                    "distance_to_stop_pct": round(
                        (current_price - stop_price) / current_price * 100, 4
                    ),
                    "status": "safe",
                })

        # Also check cooldown state
        cd_row = conn.execute(
            "SELECT consecutive_losses, cooldown_until FROM cooldown_state WHERE id = 1"
        ).fetchone()
        cooldown_info = {
            "consecutive_losses": cd_row["consecutive_losses"] if cd_row else 0,
            "cooldown_until": cd_row["cooldown_until"] if cd_row else None,
            "cooldown_active": _check_cooldown(conn) is not None,
        }

        return tool_result(
            action="stop_check",
            positions_scanned=len(positions),
            stop_losses_triggered=len(triggered),
            triggered=triggered,
            safe=safe,
            cooldown=cooldown_info,
        )
    except Exception as e:
        logger.error("Stop-check error: %s", e)
        return tool_error(f"Stop-check failed: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unified handler dispatched by registry
# ---------------------------------------------------------------------------

def _handle_quant_execute(args: dict, **kw) -> str:
    """Route quant_execute actions to the appropriate handler."""
    action = args.get("action", "").lower()

    handlers = {
        "buy": _handle_buy,
        "sell": _handle_sell,
        "status": _handle_status,
        "positions": _handle_status,  # alias: positions == status
        "history": _handle_history,
        "balance": _handle_balance,
        "stop_check": _handle_stop_check,
    }

    handler = handlers.get(action)
    if handler is None:
        return tool_error(
            f"Unknown action '{action}'. Valid actions: {', '.join(sorted(handlers.keys()))}"
        )
    return handler(args, **kw)


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_quant_available() -> bool:
    """Quant tool requires ccxt to be importable."""
    try:
        import ccxt  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

QUANT_EXECUTE_SCHEMA = {
    "name": "quant_execute",
    "description": (
        "Execute quantitative trading operations with built-in safety layers. "
        "Supports paper trading (simulated, default) and live order placement. "
        "Actions: 'buy' (open long), 'sell' (close long), 'status' (view positions & PnL), "
        "'history' (recent orders), 'balance' (account balance), "
        "'stop_check' (scan positions for stop-loss breaches). "
        "Safety: max position size enforced, daily 5% loss kill switch, "
        "stop-loss auto-set at 1.5x ATR below entry, max 3 open positions, "
        "4h cooldown after 3 consecutive losing trades. "
        "Live mode requires QUANT_LIVE_TRADING_ENABLED=true. "
        "Paper mode starts with $100,000 simulated USD."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["buy", "sell", "status", "positions", "history", "balance", "stop_check"],
                "description": (
                    "Trading action to perform. "
                    "'buy': open/increase a long position. "
                    "'sell': close/reduce a long position. "
                    "'status': view open positions, unrealized PnL, and kill switch state. "
                    "'history': view recent order history. "
                    "'balance': view current USD balance and total PnL. "
                    "'stop_check': scan all positions with stop-loss prices and auto-sell if breached."
                ),
            },
            "symbol": {
                "type": "string",
                "description": "Trading pair symbol (e.g. 'BTC/USDT', 'ETH/USDT').",
                "default": "BTC/USDT",
            },
            "side": {
                "type": "string",
                "enum": ["buy", "sell"],
                "description": "Order side: 'buy' or 'sell'. Used with buy/sell actions.",
            },
            "amount": {
                "type": "number",
                "description": "Quantity to trade in base currency (e.g. 0.1 for 0.1 BTC).",
            },
            "order_type": {
                "type": "string",
                "enum": ["market", "limit"],
                "description": "Order type: 'market' (execute at current price) or 'limit' (at specified price).",
                "default": "market",
            },
            "price": {
                "type": "number",
                "description": "Limit price. Required when order_type is 'limit'.",
            },
            "mode": {
                "type": "string",
                "enum": ["paper", "live"],
                "description": (
                    "Trading mode: 'paper' (simulated, no real orders, default) or "
                    "'live' (real orders, requires QUANT_LIVE_TRADING_ENABLED=true)."
                ),
                "default": "paper",
            },
            "exchange": {
                "type": "string",
                "description": "Exchange identifier for ccxt (e.g. 'okx', 'binance', 'coinbase').",
                "default": "okx",
            },
            "atr": {
                "type": "number",
                "description": (
                    "Average True Range value for the symbol. When provided with a 'buy' action, "
                    "a stop-loss price is automatically set at entry_price - 1.5 * ATR."
                ),
            },
            "asset_class": {
                "type": "string",
                "enum": ["crypto", "stock", "fx"],
                "description": (
                    "Asset class for price fetching. 'crypto' uses CCXT (default), "
                    "'stock' and 'fx' use yfinance. Stored per-position so sell/status "
                    "use the correct data source automatically."
                ),
                "default": "crypto",
            },
        },
        "required": ["action"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="quant_execute",
    toolset="quant",
    schema=QUANT_EXECUTE_SCHEMA,
    handler=_handle_quant_execute,
    check_fn=_check_quant_available,
    requires_env=[],
    is_async=False,
    description="Execute quantitative trading operations (paper/live) with safety layers.",
    emoji="📈",
)
