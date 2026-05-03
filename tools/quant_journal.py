#!/usr/bin/env python3
"""Quant Journal Tool - Decision logging for quantitative trading.

Records trade decisions, reasoning, and outcomes to a SQLite database
for post-trade review and reflection (the "Reflection Loop" from TradingAgents).
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tools.registry import registry

logger = logging.getLogger(__name__)


def _get_db_path() -> Path:
    """Get the journal database path, profile-aware."""
    try:
        from hermes_constants import get_hermes_home
        base = Path(get_hermes_home())
    except ImportError:
        base = Path.home() / ".hermes"
    db_dir = base / "quant_trading"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "journal.db"


def _get_conn(db_path: str = None) -> sqlite3.Connection:
    """Get a SQLite connection, creating tables if needed."""
    path = db_path or str(_get_db_path())
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection):
    """Create journal tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            entry_type TEXT NOT NULL,  -- 'decision', 'observation', 'review', 'signal'
            symbol TEXT DEFAULT 'BTC/USDT',
            side TEXT,                 -- 'buy', 'sell', 'hold'
            amount REAL,
            price REAL,
            reasoning TEXT,           -- LLM reasoning / signal analysis
            confidence REAL,          -- 0.0 - 1.0
            indicators TEXT,          -- JSON blob of indicator values
            market_context TEXT,      -- JSON blob of market conditions
            outcome TEXT,             -- 'profit', 'loss', 'neutral', NULL (pending)
            pnl REAL,                 -- Realized PnL for closed trades
            tags TEXT,                -- Comma-separated tags
            notes TEXT                -- Free-form notes
        );

        CREATE INDEX IF NOT EXISTS idx_journal_timestamp
            ON journal_entries(timestamp);
        CREATE INDEX IF NOT EXISTS idx_journal_symbol
            ON journal_entries(symbol);
        CREATE INDEX IF NOT EXISTS idx_journal_type
            ON journal_entries(entry_type);

        CREATE TABLE IF NOT EXISTS daily_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            total_decisions INTEGER DEFAULT 0,
            buy_count INTEGER DEFAULT 0,
            sell_count INTEGER DEFAULT 0,
            hold_count INTEGER DEFAULT 0,
            total_pnl REAL DEFAULT 0,
            win_rate REAL,
            avg_confidence REAL,
            notes TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_summary(date);
    """)
    conn.commit()


def _add_entry(args: dict, **kw) -> str:
    """Add a journal entry."""
    try:
        conn = _get_conn()
        conn.execute("""
            INSERT INTO journal_entries
                (entry_type, symbol, side, amount, price, reasoning,
                 confidence, indicators, market_context, outcome, pnl, tags, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            args.get("entry_type", "decision"),
            args.get("symbol", "BTC/USDT"),
            args.get("side"),
            args.get("amount"),
            args.get("price"),
            args.get("reasoning"),
            args.get("confidence"),
            args.get("indicators"),  # JSON string
            args.get("market_context"),  # JSON string
            args.get("outcome"),
            args.get("pnl"),
            args.get("tags"),
            args.get("notes"),
        ))
        conn.commit()
        entry_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return json.dumps({
            "status": "success",
            "entry_id": entry_id,
            "message": f"Journal entry #{entry_id} recorded"
        })
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _query_entries(args: dict, **kw) -> str:
    """Query journal entries."""
    try:
        conn = _get_conn()
        query = "SELECT * FROM journal_entries WHERE 1=1"
        params = []

        if args.get("entry_type"):
            query += " AND entry_type = ?"
            params.append(args["entry_type"])
        if args.get("symbol"):
            query += " AND symbol = ?"
            params.append(args["symbol"])
        if args.get("side"):
            query += " AND side = ?"
            params.append(args["side"])
        if args.get("since"):
            query += " AND timestamp >= ?"
            params.append(args["since"])
        if args.get("until"):
            query += " AND timestamp <= ?"
            params.append(args["until"])

        query += " ORDER BY timestamp DESC"

        limit = min(int(args.get("limit", 20)), 100)
        query += f" LIMIT {limit}"

        rows = conn.execute(query, params).fetchall()
        entries = [dict(row) for row in rows]
        conn.close()

        return json.dumps({
            "status": "success",
            "count": len(entries),
            "entries": entries
        }, default=str)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _review_day(args: dict, **kw) -> str:
    """Generate a daily review summary."""
    try:
        conn = _get_conn()
        date = args.get("date", datetime.now().strftime("%Y-%m-%d"))

        # Get all entries for the day
        rows = conn.execute(
            "SELECT * FROM journal_entries WHERE date(timestamp) = ? ORDER BY timestamp",
            (date,)
        ).fetchall()
        entries = [dict(row) for row in rows]

        if not entries:
            conn.close()
            return json.dumps({
                "status": "success",
                "date": date,
                "message": "No entries found for this date"
            })

        # Calculate stats
        decisions = [e for e in entries if e["entry_type"] == "decision"]
        buy_count = sum(1 for d in decisions if d["side"] == "buy")
        sell_count = sum(1 for d in decisions if d["side"] == "sell")
        hold_count = sum(1 for d in decisions if d["side"] == "hold")
        total_pnl = sum(d.get("pnl") or 0 for d in decisions)
        closed = [d for d in decisions if d.get("pnl") is not None]
        wins = sum(1 for d in closed if (d.get("pnl") or 0) > 0)
        win_rate = wins / len(closed) if closed else None
        avg_confidence = (
            sum(d["confidence"] for d in decisions if d.get("confidence"))
            / len([d for d in decisions if d.get("confidence")])
            if any(d.get("confidence") for d in decisions)
            else None
        )

        # Upsert daily summary
        conn.execute("""
            INSERT OR REPLACE INTO daily_summary
                (date, total_decisions, buy_count, sell_count, hold_count,
                 total_pnl, win_rate, avg_confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (date, len(decisions), buy_count, sell_count, hold_count,
              total_pnl, win_rate, avg_confidence))
        conn.commit()
        conn.close()

        return json.dumps({
            "status": "success",
            "date": date,
            "summary": {
                "total_decisions": len(decisions),
                "buy_count": buy_count,
                "sell_count": sell_count,
                "hold_count": hold_count,
                "total_pnl": round(total_pnl, 2),
                "win_rate": round(win_rate, 2) if win_rate is not None else None,
                "avg_confidence": round(avg_confidence, 2) if avg_confidence is not None else None,
            },
            "entries": entries[:10],  # Top 10 for context
        }, default=str)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _update_outcome(args: dict, **kw) -> str:
    """Update the outcome of a previous journal entry."""
    try:
        conn = _get_conn()
        entry_id = args.get("entry_id")
        if not entry_id:
            conn.close()
            return json.dumps({"status": "error", "error": "entry_id required"})

        updates = []
        params = []
        for field in ["outcome", "pnl", "notes"]:
            if args.get(field) is not None:
                updates.append(f"{field} = ?")
                params.append(args[field])

        if not updates:
            conn.close()
            return json.dumps({"status": "error", "error": "No fields to update"})

        params.append(entry_id)
        conn.execute(
            f"UPDATE journal_entries SET {', '.join(updates)} WHERE id = ?",
            params
        )
        conn.commit()
        conn.close()

        return json.dumps({
            "status": "success",
            "message": f"Entry #{entry_id} updated"
        })
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _handle_quant_journal(args: dict, **kw) -> str:
    """Route journal actions to sub-handlers."""
    action = args.get("action", "add")
    handlers = {
        "add": _add_entry,
        "query": _query_entries,
        "review": _review_day,
        "update": _update_outcome,
    }
    handler = handlers.get(action)
    if not handler:
        return json.dumps({
            "status": "error",
            "error": f"Unknown action: {action}. Use: add, query, review, update"
        })
    return handler(args, **kw)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _check_ccxt() -> bool:
    """Check if required dependencies are available."""
    try:
        import ccxt
        return True
    except ImportError:
        return False


registry.register(
    name="quant_journal",
    toolset="quant",
    schema={
        "name": "quant_journal",
        "description": (
            "Record and query trading decisions, observations, and reviews in a persistent journal. "
            "Supports: add (log a decision/observation/signal), query (search entries), "
            "review (daily summary with win rate and PnL), update (set outcome for closed trades). "
            "The journal enables post-trade reflection and strategy improvement."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "query", "review", "update"],
                    "description": "Journal action: add=new entry, query=search entries, review=daily summary, update=set outcome"
                },
                "entry_type": {
                    "type": "string",
                    "enum": ["decision", "observation", "review", "signal"],
                    "description": "Type of entry (for 'add' action): decision=trade decision, observation=market observation, signal=indicator signal, review=reflection"
                },
                "symbol": {
                    "type": "string",
                    "description": "Trading pair, e.g. BTC/USDT (default: BTC/USDT)"
                },
                "side": {
                    "type": "string",
                    "enum": ["buy", "sell", "hold"],
                    "description": "Trade direction (for decisions)"
                },
                "amount": {
                    "type": "number",
                    "description": "Trade amount in base currency"
                },
                "price": {
                    "type": "number",
                    "description": "Price at time of decision"
                },
                "reasoning": {
                    "type": "string",
                    "description": "Reasoning behind the decision (LLM analysis)"
                },
                "confidence": {
                    "type": "number",
                    "description": "Decision confidence 0.0-1.0"
                },
                "indicators": {
                    "type": "string",
                    "description": "JSON string of indicator values at decision time"
                },
                "market_context": {
                    "type": "string",
                    "description": "JSON string of market conditions (trend, volatility regime, etc.)"
                },
                "outcome": {
                    "type": "string",
                    "enum": ["profit", "loss", "neutral"],
                    "description": "Trade outcome (for update action)"
                },
                "pnl": {
                    "type": "number",
                    "description": "Realized profit/loss amount (for update action)"
                },
                "entry_id": {
                    "type": "integer",
                    "description": "Entry ID to update (for update action)"
                },
                "date": {
                    "type": "string",
                    "description": "Date for review (YYYY-MM-DD format)"
                },
                "since": {
                    "type": "string",
                    "description": "Query entries from this timestamp"
                },
                "until": {
                    "type": "string",
                    "description": "Query entries until this timestamp"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return (default 20, max 100)"
                },
                "tags": {
                    "type": "string",
                    "description": "Comma-separated tags for categorization"
                },
                "notes": {
                    "type": "string",
                    "description": "Free-form notes or reflection text"
                },
            },
            "required": ["action"],
        },
    },
    handler=_handle_quant_journal,
    check_fn=_check_ccxt,
    requires_env=[],
    is_async=False,
    description="Trading decision journal with reflection loop",
    emoji="📝",
)
