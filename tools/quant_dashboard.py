#!/usr/bin/env python3
"""Quant Dashboard - Self-contained Web UI for the quantitative trading system.

Single-file aiohttp web dashboard with all HTML/CSS/JS embedded.
Three pages: Portfolio Dashboard, Equity Curve, Trade Journal.
Dark theme, auto-refresh every 10s, REST API endpoints for all data.

Registers as ``quant_dashboard`` tool with action='serve' (starts on port 8899).
"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web

from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DASHBOARD_PORT = 8899
DEFAULT_EXCHANGE = "okx"
DEFAULT_SYMBOL = "BTC/USDT"


def _get_trading_db_path() -> Path:
    base = get_hermes_home() / "quant_trading"
    base.mkdir(parents=True, exist_ok=True)
    return base / "paper_trading.db"


def _get_journal_db_path() -> Path:
    base = get_hermes_home() / "quant_trading"
    base.mkdir(parents=True, exist_ok=True)
    return base / "journal.db"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_trading_conn() -> sqlite3.Connection:
    path = _get_trading_db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _get_journal_conn() -> sqlite3.Connection:
    path = _get_journal_db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# Data queries
# ---------------------------------------------------------------------------

def _fetch_portfolio() -> dict:
    """Fetch portfolio summary: balance, daily PnL, kill switch, cooldown."""
    try:
        conn = _get_trading_conn()
        try:
            balance_row = conn.execute(
                "SELECT usd, initial_usd, updated_at FROM balance WHERE id = 1"
            ).fetchone()

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            pnl_row = conn.execute(
                "SELECT realized_pnl FROM daily_pnl WHERE date = ?", (today,)
            ).fetchone()

            cd_row = conn.execute(
                "SELECT cooldown_until, consecutive_losses FROM cooldown_state WHERE id = 1"
            ).fetchone()

            # Kill switch check
            kill_active = False
            if balance_row and pnl_row:
                initial = balance_row["initial_usd"]
                realized = pnl_row["realized_pnl"]
                if initial > 0 and realized < 0 and (-realized / initial) > 0.05:
                    kill_active = True

            # Cooldown check
            cooldown_active = False
            cooldown_remaining_min = 0
            cooldown_until = None
            consecutive_losses = 0
            if cd_row:
                consecutive_losses = cd_row["consecutive_losses"] or 0
                cooldown_until = cd_row["cooldown_until"]
                if cooldown_until:
                    try:
                        cd_time = datetime.fromisoformat(cooldown_until)
                        now = datetime.now(timezone.utc)
                        if now < cd_time:
                            cooldown_active = True
                            cooldown_remaining_min = int(
                                (cd_time - now).total_seconds() / 60
                            )
                    except (ValueError, TypeError):
                        pass

            return {
                "balance_usd": round(balance_row["usd"], 2) if balance_row else 0.0,
                "initial_usd": round(balance_row["initial_usd"], 2) if balance_row else 100000.0,
                "updated_at": balance_row["updated_at"] if balance_row else None,
                "daily_realized_pnl": round(pnl_row["realized_pnl"], 4) if pnl_row else 0.0,
                "kill_switch_active": kill_active,
                "cooldown_active": cooldown_active,
                "cooldown_remaining_min": cooldown_remaining_min,
                "cooldown_until": cooldown_until,
                "consecutive_losses": consecutive_losses,
            }
        finally:
            conn.close()
    except Exception as e:
        logger.error("Portfolio fetch error: %s", e)
        return {"error": str(e)}


def _fetch_positions() -> list:
    """Fetch all open positions with unrealized PnL."""
    try:
        conn = _get_trading_conn()
        try:
            rows = conn.execute(
                "SELECT id, symbol, side, amount, entry_price, stop_loss_price, "
                "created_at, updated_at FROM positions ORDER BY id"
            ).fetchall()
            positions = []
            for r in rows:
                positions.append({
                    "id": r["id"],
                    "symbol": r["symbol"],
                    "side": r["side"],
                    "amount": r["amount"],
                    "entry_price": r["entry_price"],
                    "stop_loss_price": r["stop_loss_price"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                })
            return positions
        finally:
            conn.close()
    except Exception as e:
        logger.error("Positions fetch error: %s", e)
        return []


def _fetch_history(limit: int = 50) -> list:
    """Fetch recent order history."""
    try:
        conn = _get_trading_conn()
        try:
            rows = conn.execute(
                "SELECT id, symbol, side, amount, price, order_type, mode, fee, "
                "status, created_at FROM orders ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.error("History fetch error: %s", e)
        return []


def _fetch_equity_curve() -> list:
    """Fetch balance-over-time data for equity curve chart.

    Builds from order history: starts at initial_usd, applies each
    order's net cash flow to reconstruct running balance.
    """
    try:
        conn = _get_trading_conn()
        try:
            balance_row = conn.execute(
                "SELECT usd, initial_usd FROM balance WHERE id = 1"
            ).fetchone()
            initial = balance_row["initial_usd"] if balance_row else 100000.0

            rows = conn.execute(
                "SELECT id, side, amount, price, fee, created_at FROM orders "
                "ORDER BY id ASC"
            ).fetchall()

            points = []
            running = initial
            # Pre-seed with initial balance
            if rows:
                points.append({
                    "time": rows[0]["created_at"],
                    "balance": round(running, 2),
                })
            for r in rows:
                if r["side"] == "buy":
                    running -= (r["amount"] * r["price"] + (r["fee"] or 0))
                else:
                    running += (r["amount"] * r["price"] - (r["fee"] or 0))
                points.append({
                    "time": r["created_at"],
                    "balance": round(running, 2),
                })

            # Add current balance as last point
            if balance_row:
                points.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "balance": round(balance_row["usd"], 2),
                })
            return points
        finally:
            conn.close()
    except Exception as e:
        logger.error("Equity curve fetch error: %s", e)
        return []


def _fetch_trade_results() -> list:
    """Fetch trade results for PnL over time."""
    try:
        conn = _get_trading_conn()
        try:
            rows = conn.execute(
                "SELECT id, realized_pnl, created_at FROM trade_results "
                "ORDER BY id ASC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.error("Trade results fetch error: %s", e)
        return []


def _fetch_journal(limit: int = 30) -> list:
    """Fetch recent journal entries."""
    try:
        conn = _get_journal_conn()
        try:
            rows = conn.execute(
                "SELECT id, timestamp, entry_type, symbol, side, amount, price, "
                "reasoning, confidence, indicators, market_context, outcome, pnl, "
                "tags, notes FROM journal_entries ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.error("Journal fetch error: %s", e)
        return []


def _fetch_regime() -> dict:
    """Fetch the latest market regime / daily summary from journal."""
    try:
        conn = _get_journal_conn()
        try:
            row = conn.execute(
                "SELECT * FROM daily_summary ORDER BY date DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()
    except Exception as e:
        logger.error("Regime fetch error: %s", e)
        return {}


# ---------------------------------------------------------------------------
# REST API handlers
# ---------------------------------------------------------------------------

async def api_portfolio(request: web.Request) -> web.Response:
    data = _fetch_portfolio()
    return web.json_response(data)


async def api_positions(request: web.Request) -> web.Response:
    positions = _fetch_positions()
    # Try to enrich with current prices
    try:
        import ccxt
        exchange = ccxt.okx()
        for pos in positions:
            try:
                ticker = exchange.fetch_ticker(pos["symbol"])
                cur_price = ticker.get("last") or ticker.get("close")
                if cur_price:
                    pos["current_price"] = float(cur_price)
                    pos["unrealized_pnl"] = round(
                        (float(cur_price) - pos["entry_price"]) * pos["amount"], 4
                    )
                    pos["value_usd"] = round(pos["amount"] * float(cur_price), 4)
                else:
                    pos["current_price"] = None
                    pos["unrealized_pnl"] = 0.0
                    pos["value_usd"] = None
            except Exception:
                pos["current_price"] = None
                pos["unrealized_pnl"] = 0.0
                pos["value_usd"] = None
    except ImportError:
        for pos in positions:
            pos["current_price"] = None
            pos["unrealized_pnl"] = 0.0
            pos["value_usd"] = None
    return web.json_response({"positions": positions})


async def api_history(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", "50"))
    data = _fetch_history(min(limit, 200))
    return web.json_response({"orders": data, "count": len(data)})


async def api_equity(request: web.Request) -> web.Response:
    data = _fetch_equity_curve()
    return web.json_response({"points": data, "count": len(data)})


async def api_journal(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", "30"))
    data = _fetch_journal(min(limit, 100))
    return web.json_response({"entries": data, "count": len(data)})


async def api_regime(request: web.Request) -> web.Response:
    data = _fetch_regime()
    return web.json_response(data)


async def api_trade_results(request: web.Request) -> web.Response:
    data = _fetch_trade_results()
    return web.json_response({"results": data, "count": len(data)})


# ---------------------------------------------------------------------------
# Embedded HTML/CSS/JS
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Quant Dashboard</title>
<style>
:root {
  --bg-primary: #0d1117;
  --bg-secondary: #161b22;
  --bg-tertiary: #21262d;
  --border: #30363d;
  --text-primary: #e6edf3;
  --text-secondary: #8b949e;
  --accent-green: #3fb950;
  --accent-red: #f85149;
  --accent-yellow: #d29922;
  --accent-blue: #58a6f;
  --accent-purple: #bc8cff;
  --accent-orange: #d18616;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  background: var(--bg-primary);
  color: var(--text-primary);
  min-height: 100vh;
}

/* Nav */
.nav {
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
  padding: 0 24px;
  display: flex;
  align-items: center;
  height: 56px;
  position: sticky;
  top: 0;
  z-index: 100;
}
.nav-brand {
  font-size: 18px;
  font-weight: 700;
  color: var(--accent-blue);
  margin-right: 32px;
  letter-spacing: -0.5px;
}
.nav a {
  color: var(--text-secondary);
  text-decoration: none;
  padding: 8px 16px;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.15s;
}
.nav a:hover, .nav a.active {
  color: var(--text-primary);
  background: var(--bg-tertiary);
}
.nav a.active {
  color: var(--accent-blue);
}
.nav-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 12px;
}
.nav-right .refresh-timer {
  font-size: 11px;
  color: var(--text-secondary);
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
  margin-right: 6px;
}
.status-dot.green { background: var(--accent-green); }
.status-dot.red { background: var(--accent-red); }
.status-dot.yellow { background: var(--accent-yellow); }

/* Container */
.container { max-width: 1280px; margin: 0 auto; padding: 24px; }

/* Cards */
.card {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 16px;
}
.card-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 12px;
}

/* Stats grid */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.stat-card {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px 20px;
}
.stat-label {
  font-size: 12px;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}
.stat-value {
  font-size: 28px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.stat-value.positive { color: var(--accent-green); }
.stat-value.negative { color: var(--accent-red); }
.stat-value.neutral { color: var(--text-primary); }
.stat-value.warning { color: var(--accent-yellow); }

/* Tables */
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
thead th {
  text-align: left;
  padding: 10px 12px;
  color: var(--text-secondary);
  font-weight: 600;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  border-bottom: 1px solid var(--border);
}
tbody td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
tbody tr:hover { background: var(--bg-tertiary); }

.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 600;
}
.badge-buy { background: rgba(63,185,80,0.15); color: var(--accent-green); }
.badge-sell { background: rgba(248,81,73,0.15); color: var(--accent-red); }
.badge-hold { background: rgba(210,153,34,0.15); color: var(--accent-yellow); }
.badge-active { background: rgba(248,81,73,0.2); color: var(--accent-red); }
.badge-ok { background: rgba(63,185,80,0.15); color: var(--accent-green); }
.badge-cooldown { background: rgba(210,153,34,0.2); color: var(--accent-yellow); }
.badge-paper { background: rgba(88,166,255,0.15); color: var(--accent-blue); }
.badge-live { background: rgba(248,81,73,0.15); color: var(--accent-red); }
.badge-decision { background: rgba(88,166,255,0.15); color: var(--accent-blue); }
.badge-observation { background: rgba(210,153,34,0.15); color: var(--accent-yellow); }
.badge-signal { background: rgba(188,140,255,0.15); color: var(--accent-purple); }
.badge-review { background: rgba(63,185,80,0.15); color: var(--accent-green); }
.badge-profit { background: rgba(63,185,80,0.15); color: var(--accent-green); }
.badge-loss { background: rgba(248,81,73,0.15); color: var(--accent-red); }
.badge-neutral { background: rgba(139,148,158,0.15); color: var(--text-secondary); }

/* Chart */
.chart-container {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 16px;
  overflow-x: auto;
}

/* Journal entry */
.journal-entry {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px 20px;
  margin-bottom: 12px;
}
.journal-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.journal-time {
  font-size: 12px;
  color: var(--text-secondary);
}
.journal-symbol {
  font-weight: 700;
  font-size: 14px;
}
.journal-reasoning {
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.5;
  margin-top: 6px;
}
.journal-meta {
  display: flex;
  gap: 16px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-secondary);
  flex-wrap: wrap;
}
.confidence-bar {
  width: 60px;
  height: 6px;
  background: var(--bg-tertiary);
  border-radius: 3px;
  overflow: hidden;
  display: inline-block;
  vertical-align: middle;
  margin-left: 6px;
}
.confidence-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s;
}

/* Pages */
.page { display: none; }
.page.active { display: block; }

/* Footer */
.footer {
  text-align: center;
  padding: 24px;
  color: var(--text-secondary);
  font-size: 12px;
}

/* Responsive */
@media (max-width: 768px) {
  .stats-grid { grid-template-columns: repeat(2, 1fr); }
  .container { padding: 16px; }
  .nav { padding: 0 12px; }
  table { font-size: 12px; }
  .stat-value { font-size: 22px; }
}

@media (max-width: 480px) {
  .stats-grid { grid-template-columns: 1fr; }
}

/* Skeleton loading */
.skeleton {
  background: linear-gradient(90deg, var(--bg-tertiary) 25%, var(--bg-secondary) 50%, var(--bg-tertiary) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 4px;
  height: 20px;
}
@keyframes shimmer {
  0% { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}
</style>
</head>
<body>

<!-- Navigation -->
<nav class="nav">
  <div class="nav-brand">Quant Dashboard</div>
  <a href="#" class="active" data-page="dashboard" onclick="showPage('dashboard')">Portfolio</a>
  <a href="#" data-page="equity" onclick="showPage('equity')">Equity Curve</a>
  <a href="#" data-page="journal" onclick="showPage('journal')">Journal</a>
  <div class="nav-right">
    <span id="connection-status"><span class="status-dot green"></span>Live</span>
    <span class="refresh-timer" id="refresh-timer">10s</span>
  </div>
</nav>

<!-- Dashboard Page -->
<div class="page active" id="page-dashboard">
  <div class="container">
    <!-- Stats -->
    <div class="stats-grid" id="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Balance</div>
        <div class="stat-value neutral" id="stat-balance">--</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Daily PnL</div>
        <div class="stat-value neutral" id="stat-daily-pnl">--</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Total PnL</div>
        <div class="stat-value neutral" id="stat-total-pnl">--</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Kill Switch</div>
        <div class="stat-value" id="stat-kill-switch">--</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Cooldown</div>
        <div class="stat-value" id="stat-cooldown">--</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Open Positions</div>
        <div class="stat-value neutral" id="stat-position-count">--</div>
      </div>
    </div>

    <!-- Positions -->
    <div class="card">
      <div class="card-title">Open Positions</div>
      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Amount</th>
              <th>Entry Price</th>
              <th>Current Price</th>
              <th>Value (USD)</th>
              <th>Unrealized PnL</th>
              <th>Stop Loss</th>
              <th>Opened</th>
            </tr>
          </thead>
          <tbody id="positions-tbody">
            <tr><td colspan="9" style="text-align:center;color:var(--text-secondary)">Loading...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Recent Orders -->
    <div class="card">
      <div class="card-title">Recent Orders</div>
      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Symbol</th>
              <th>Side</th>
              <th>Amount</th>
              <th>Price</th>
              <th>Fee</th>
              <th>Mode</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody id="orders-tbody">
            <tr><td colspan="8" style="text-align:center;color:var(--text-secondary)">Loading...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- Equity Curve Page -->
<div class="page" id="page-equity">
  <div class="container">
    <div class="card">
      <div class="card-title">Equity Curve</div>
      <div class="chart-container" id="equity-chart" style="height:420px;position:relative;">
        <svg id="equity-svg" width="100%" height="100%" style="min-height:380px;"></svg>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Trade Results (Cumulative PnL)</div>
      <div class="chart-container" id="pnl-chart" style="height:300px;position:relative;">
        <svg id="pnl-svg" width="100%" height="100%" style="min-height:260px;"></svg>
      </div>
    </div>
  </div>
</div>

<!-- Journal Page -->
<div class="page" id="page-journal">
  <div class="container">
    <div class="card">
      <div class="card-title">Trade Journal - Recent Decisions</div>
      <div id="journal-entries">
        <p style="color:var(--text-secondary);text-align:center;">Loading...</p>
      </div>
    </div>
  </div>
</div>

<div class="footer">
  Quant Dashboard &mdash; Hermes Agent &mdash; Auto-refreshes every 10s
</div>

<script>
// ----- Page navigation -----
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav a[data-page]').forEach(a => a.classList.remove('active'));
  const page = document.getElementById('page-' + name);
  if (page) page.classList.add('active');
  const link = document.querySelector('.nav a[data-page="' + name + '"]');
  if (link) link.classList.add('active');
  // Refresh on switch
  if (name === 'dashboard') refreshDashboard();
  if (name === 'equity') refreshEquity();
  if (name === 'journal') refreshJournal();
}

// ----- Utility -----
function fmtUSD(v) {
  if (v == null) return '--';
  return '$' + Number(v).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}
function fmtPnl(v) {
  if (v == null) return '--';
  const n = Number(v);
  const sign = n >= 0 ? '+' : '';
  return sign + '$' + n.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}
function fmtPct(v) {
  if (v == null) return '--';
  const n = Number(v);
  const sign = n >= 0 ? '+' : '';
  return sign + n.toFixed(2) + '%';
}
function pnlClass(v) {
  if (v == null) return 'neutral';
  return Number(v) > 0 ? 'positive' : Number(v) < 0 ? 'negative' : 'neutral';
}
function fmtTime(iso) {
  if (!iso) return '--';
  try {
    const d = new Date(iso);
    return d.toLocaleString('en-US', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
  } catch { return iso; }
}
function escHtml(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ----- Dashboard refresh -----
async function refreshDashboard() {
  try {
    const [portfolioRes, positionsRes, historyRes] = await Promise.all([
      fetch('/api/portfolio'), fetch('/api/positions'), fetch('/api/history?limit=25')
    ]);
    const portfolio = await portfolioRes.json();
    const positionsData = await positionsRes.json();
    const historyData = await historyRes.json();

    // Stats
    const bal = portfolio.balance_usd || 0;
    const init = portfolio.initial_usd || 100000;
    const dailyPnl = portfolio.daily_realized_pnl || 0;
    const totalPnl = bal - init;

    document.getElementById('stat-balance').textContent = fmtUSD(bal);
    document.getElementById('stat-daily-pnl').textContent = fmtPnl(dailyPnl);
    document.getElementById('stat-daily-pnl').className = 'stat-value ' + pnlClass(dailyPnl);
    document.getElementById('stat-total-pnl').textContent = fmtPnl(totalPnl);
    document.getElementById('stat-total-pnl').className = 'stat-value ' + pnlClass(totalPnl);

    // Kill switch
    const killEl = document.getElementById('stat-kill-switch');
    if (portfolio.kill_switch_active) {
      killEl.innerHTML = '<span class="badge badge-active">ACTIVE</span>';
      killEl.className = 'stat-value negative';
    } else {
      killEl.innerHTML = '<span class="badge badge-ok">CLEAR</span>';
      killEl.className = 'stat-value positive';
    }

    // Cooldown
    const cdEl = document.getElementById('stat-cooldown');
    if (portfolio.cooldown_active) {
      cdEl.innerHTML = '<span class="badge badge-cooldown">ACTIVE (' + portfolio.cooldown_remaining_min + 'm)</span>';
      cdEl.className = 'stat-value warning';
    } else {
      cdEl.innerHTML = '<span class="badge badge-ok">NONE</span>';
      cdEl.className = 'stat-value positive';
    }

    // Position count
    const positions = positionsData.positions || [];
    document.getElementById('stat-position-count').textContent = positions.length;

    // Positions table
    const ptbody = document.getElementById('positions-tbody');
    if (positions.length === 0) {
      ptbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--text-secondary)">No open positions</td></tr>';
    } else {
      ptbody.innerHTML = positions.map(p => {
        const upnl = p.unrealized_pnl || 0;
        return '<tr>' +
          '<td>' + escHtml(p.symbol) + '</td>' +
          '<td><span class="badge badge-' + (p.side||'') + '">' + escHtml(p.side) + '</span></td>' +
          '<td>' + (p.amount||0).toFixed(6) + '</td>' +
          '<td>' + fmtUSD(p.entry_price) + '</td>' +
          '<td>' + (p.current_price ? fmtUSD(p.current_price) : '--') + '</td>' +
          '<td>' + (p.value_usd ? fmtUSD(p.value_usd) : '--') + '</td>' +
          '<td class="' + pnlClass(upnl) + '" style="font-weight:600">' + fmtPnl(upnl) + '</td>' +
          '<td>' + (p.stop_loss_price ? fmtUSD(p.stop_loss_price) : '--') + '</td>' +
          '<td>' + fmtTime(p.created_at) + '</td>' +
          '</tr>';
      }).join('');
    }

    // Orders table
    const orders = historyData.orders || [];
    const otbody = document.getElementById('orders-tbody');
    if (orders.length === 0) {
      otbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-secondary)">No orders yet</td></tr>';
    } else {
      otbody.innerHTML = orders.map(o => {
        return '<tr>' +
          '<td>' + fmtTime(o.created_at) + '</td>' +
          '<td>' + escHtml(o.symbol) + '</td>' +
          '<td><span class="badge badge-' + (o.side||'') + '">' + escHtml(o.side) + '</span></td>' +
          '<td>' + (o.amount||0).toFixed(6) + '</td>' +
          '<td>' + fmtUSD(o.price) + '</td>' +
          '<td>' + fmtUSD(o.fee) + '</td>' +
          '<td><span class="badge badge-' + (o.mode||'paper') + '">' + escHtml(o.mode) + '</span></td>' +
          '<td>' + escHtml(o.status) + '</td>' +
          '</tr>';
      }).join('');
    }

    document.getElementById('connection-status').innerHTML = '<span class="status-dot green"></span>Live';
  } catch (e) {
    console.error('Dashboard refresh error:', e);
    document.getElementById('connection-status').innerHTML = '<span class="status-dot red"></span>Offline';
  }
}

// ----- Equity curve refresh -----
async function refreshEquity() {
  try {
    const [equityRes, resultsRes] = await Promise.all([
      fetch('/api/equity'), fetch('/api/trade_results')
    ]);
    const equityData = await equityRes.json();
    const resultsData = await resultsRes.json();

    drawEquityCurve(equityData.points || []);
    drawPnlChart(resultsData.results || []);
  } catch (e) {
    console.error('Equity refresh error:', e);
  }
}

function drawEquityCurve(points) {
  const svg = document.getElementById('equity-svg');
  if (!points.length) {
    svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="#8b949e" font-size="14">No equity data yet</text>';
    return;
  }

  const rect = svg.getBoundingClientRect();
  const W = Math.max(rect.width || 800, 400);
  const H = Math.max(rect.height || 380, 200);
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);

  const pad = {top:20, right:20, bottom:40, left:70};
  const cw = W - pad.left - pad.right;
  const ch = H - pad.top - pad.bottom;

  const balances = points.map(p => p.balance);
  const minB = Math.min(...balances);
  const maxB = Math.max(...balances);
  const rangeB = maxB - minB || 1;
  const yPad = rangeB * 0.05;
  const yMin = minB - yPad;
  const yMax = maxB + yPad;
  const yRange = yMax - yMin;

  function x(i) { return pad.left + (i / (points.length - 1 || 1)) * cw; }
  function y(v) { return pad.top + ch - ((v - yMin) / yRange) * ch; }

  const initBalance = balances[0];
  const linePoints = points.map((p, i) => x(i) + ',' + y(p.balance)).join(' ');
  const areaPoints = x(0) + ',' + y(yMin) + ' ' + linePoints + ' ' + x(points.length-1) + ',' + y(yMin);

  // Y-axis ticks
  const nTicks = 5;
  let ticksHtml = '';
  for (let i = 0; i <= nTicks; i++) {
    const val = yMin + (yRange * i / nTicks);
    const ty = y(val);
    ticksHtml += '<line x1="' + pad.left + '" y1="' + ty + '" x2="' + (W - pad.right) + '" y2="' + ty + '" stroke="#30363d" stroke-width="0.5"/>';
    ticksHtml += '<text x="' + (pad.left - 8) + '" y="' + ty + '" text-anchor="end" fill="#8b949e" font-size="11" dominant-baseline="middle">' + fmtUSD(val) + '</text>';
  }

  // X-axis labels
  let xLabelsHtml = '';
  const step = Math.max(1, Math.floor(points.length / 6));
  for (let i = 0; i < points.length; i += step) {
    const tx = x(i);
    const label = fmtTime(points[i].time);
    xLabelsHtml += '<text x="' + tx + '" y="' + (H - pad.bottom + 20) + '" text-anchor="middle" fill="#8b949e" font-size="10">' + escHtml(label) + '</text>';
  }

  // Reference line at initial balance
  const refY = y(initBalance);
  const refColor = balances[balances.length-1] >= initBalance ? 'var(--accent-green)' : 'var(--accent-red)';

  svg.innerHTML =
    ticksHtml + xLabelsHtml +
    '<line x1="' + pad.left + '" y1="' + refY + '" x2="' + (W - pad.right) + '" y2="' + refY + '" stroke="#30363d" stroke-width="1" stroke-dasharray="4,4"/>' +
    '<polygon points="' + areaPoints + '" fill="url(#areaGrad)" opacity="0.3"/>' +
    '<defs><linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="' + refColor + '"/>' +
      '<stop offset="100%" stop-color="transparent"/>' +
    '</linearGradient></defs>' +
    '<polyline points="' + linePoints + '" fill="none" stroke="' + refColor + '" stroke-width="2" stroke-linejoin="round"/>' +
    '<circle cx="' + x(points.length-1) + '" cy="' + y(balances[balances.length-1]) + '" r="4" fill="' + refColor + '"/>' +
    '<text x="' + (x(points.length-1) + 8) + '" y="' + y(balances[balances.length-1]) + '" fill="' + refColor + '" font-size="12" font-weight="700" dominant-baseline="middle">' + fmtUSD(balances[balances.length-1]) + '</text>';
}

function drawPnlChart(results) {
  const svg = document.getElementById('pnl-svg');
  if (!results.length) {
    svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="#8b949e" font-size="14">No trade results yet</text>';
    return;
  }

  const rect = svg.getBoundingClientRect();
  const W = Math.max(rect.width || 800, 400);
  const H = Math.max(rect.height || 260, 160);
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);

  const pad = {top:16, right:20, bottom:36, left:70};
  const cw = W - pad.left - pad.right;
  const ch = H - pad.top - pad.bottom;

  // Build cumulative PnL
  let cum = 0;
  const cumData = results.map(r => { cum += r.realized_pnl || 0; return {time: r.created_at, pnl: cum}; });
  const vals = cumData.map(d => d.pnl);
  const minV = Math.min(0, Math.min(...vals));
  const maxV = Math.max(0, Math.max(...vals));
  const rangeV = maxV - minV || 1;
  const yPad2 = rangeV * 0.1;
  const yMin2 = minV - yPad2;
  const yMax2 = maxV + yPad2;
  const yRange2 = yMax2 - yMin2;

  function x(i) { return pad.left + (i / (cumData.length - 1 || 1)) * cw; }
  function y(v) { return pad.top + ch - ((v - yMin2) / yRange2) * ch; }

  const lastPnl = vals[vals.length - 1];
  const lineColor = lastPnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
  const linePoints = cumData.map((d, i) => x(i) + ',' + y(d.pnl)).join(' ');
  const zeroY = y(0);

  // Y-axis ticks
  const nTicks = 4;
  let ticksHtml = '';
  for (let i = 0; i <= nTicks; i++) {
    const val = yMin2 + (yRange2 * i / nTicks);
    const ty = y(val);
    ticksHtml += '<line x1="' + pad.left + '" y1="' + ty + '" x2="' + (W - pad.right) + '" y2="' + ty + '" stroke="#30363d" stroke-width="0.5"/>';
    ticksHtml += '<text x="' + (pad.left - 8) + '" y="' + ty + '" text-anchor="end" fill="#8b949e" font-size="11" dominant-baseline="middle">' + fmtPnl(val) + '</text>';
  }

  svg.innerHTML =
    ticksHtml +
    '<line x1="' + pad.left + '" y1="' + zeroY + '" x2="' + (W - pad.right) + '" y2="' + zeroY + '" stroke="#8b949e" stroke-width="1" stroke-dasharray="3,3"/>' +
    '<polyline points="' + linePoints + '" fill="none" stroke="' + lineColor + '" stroke-width="2" stroke-linejoin="round"/>' +
    '<circle cx="' + x(cumData.length-1) + '" cy="' + y(lastPnl) + '" r="4" fill="' + lineColor + '"/>' +
    '<text x="' + (x(cumData.length-1) + 8) + '" y="' + y(lastPnl) + '" fill="' + lineColor + '" font-size="12" font-weight="700" dominant-baseline="middle">' + fmtPnl(lastPnl) + '</text>';
}

// ----- Journal refresh -----
async function refreshJournal() {
  try {
    const res = await fetch('/api/journal?limit=30');
    const data = await res.json();
    const entries = data.entries || [];
    const container = document.getElementById('journal-entries');

    if (!entries.length) {
      container.innerHTML = '<p style="color:var(--text-secondary);text-align:center;">No journal entries yet</p>';
      return;
    }

    container.innerHTML = entries.map(e => {
      const confidence = e.confidence != null ? Number(e.confidence) : null;
      const confPct = confidence != null ? Math.round(confidence * 100) : 0;
      const confColor = confidence >= 0.7 ? 'var(--accent-green)' : confidence >= 0.4 ? 'var(--accent-yellow)' : 'var(--accent-red)';
      const pnlVal = e.pnl;
      const outcomeBadge = e.outcome ? '<span class="badge badge-' + escHtml(e.outcome) + '">' + escHtml(e.outcome).toUpperCase() + '</span>' : '';

      return '<div class="journal-entry">' +
        '<div class="journal-header">' +
          '<span class="badge badge-' + escHtml(e.entry_type||'decision') + '">' + escHtml(e.entry_type||'decision').toUpperCase() + '</span>' +
          (e.side ? '<span class="badge badge-' + escHtml(e.side) + '">' + escHtml(e.side).toUpperCase() + '</span>' : '') +
          outcomeBadge +
          '<span class="journal-symbol">' + escHtml(e.symbol) + '</span>' +
          (e.amount ? '<span style="font-size:13px;color:var(--text-secondary)">' + Number(e.amount).toFixed(6) + ' @ ' + fmtUSD(e.price) + '</span>' : '') +
        '</div>' +
        (e.reasoning ? '<div class="journal-reasoning">' + escHtml(e.reasoning) + '</div>' : '') +
        '<div class="journal-meta">' +
          '<span>' + fmtTime(e.timestamp) + '</span>' +
          (confidence != null ?
            '<span>Confidence: ' + confPct + '% <span class="confidence-bar"><span class="confidence-fill" style="width:' + confPct + '%;background:' + confColor + '"></span></span></span>' : '') +
          (pnlVal != null ? '<span class="' + pnlClass(pnlVal) + '">PnL: ' + fmtPnl(pnlVal) + '</span>' : '') +
          (e.tags ? '<span>Tags: ' + escHtml(e.tags) + '</span>' : '') +
        '</div>' +
        (e.notes ? '<div class="journal-reasoning" style="margin-top:6px;font-style:italic">Note: ' + escHtml(e.notes) + '</div>' : '') +
      '</div>';
    }).join('');
  } catch (e) {
    console.error('Journal refresh error:', e);
  }
}

// ----- Auto-refresh -----
let refreshInterval = null;
let countdown = 10;

function startAutoRefresh() {
  countdown = 10;
  if (refreshInterval) clearInterval(refreshInterval);
  refreshInterval = setInterval(() => {
    countdown--;
    if (countdown <= 0) {
      countdown = 10;
      const activePage = document.querySelector('.page.active');
      if (activePage) {
        const id = activePage.id.replace('page-', '');
        if (id === 'dashboard') refreshDashboard();
        else if (id === 'equity') refreshEquity();
        else if (id === 'journal') refreshJournal();
      }
    }
    document.getElementById('refresh-timer').textContent = countdown + 's';
  }, 1000);
}

// Initial load
refreshDashboard();
startAutoRefresh();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Web app factory
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    """Create the aiohttp web application with all routes."""
    app = web.Application()

    # API routes
    app.router.add_get("/api/portfolio", api_portfolio)
    app.router.add_get("/api/positions", api_positions)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/api/equity", api_equity)
    app.router.add_get("/api/journal", api_journal)
    app.router.add_get("/api/regime", api_regime)
    app.router.add_get("/api/trade_results", api_trade_results)

    # Serve the single-page HTML
    async def index(request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    app.router.add_get("/", index)
    # All sub-paths serve the same SPA
    app.router.add_get("/{path:.*}", index)

    return app


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

_runner_task: Optional[asyncio.Task] = None


def _handle_quant_dashboard(args: dict, **kw) -> str:
    """Handle quant_dashboard tool actions.

    action='serve': Start the web dashboard server on port 8899.
    """
    action = args.get("action", "serve")

    if action == "serve":
        return _start_server()
    elif action == "status":
        if _runner_task and not _runner_task.done():
            return tool_result(status="running", port=DASHBOARD_PORT, url=f"http://localhost:{DASHBOARD_PORT}")
        return tool_result(status="stopped", port=DASHBOARD_PORT)
    else:
        return tool_error(f"Unknown action '{action}'. Use: serve, status")


def _start_server() -> str:
    """Start the dashboard server (blocking or in background)."""
    global _runner_task

    if _runner_task and not _runner_task.done():
        return tool_result(
            status="already_running",
            port=DASHBOARD_PORT,
            url=f"http://localhost:{DASHBOARD_PORT}",
            message=f"Dashboard already running at http://localhost:{DASHBOARD_PORT}",
        )

    app = create_app()

    try:
        # Try to get a running event loop
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an async context — start in background
        async def _run():
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", DASHBOARD_PORT)
            await site.start()
            logger.info("Quant dashboard started on http://localhost:%d", DASHBOARD_PORT)
            # Keep running
            try:
                await asyncio.Future()  # run forever
            except asyncio.CancelledError:
                await runner.cleanup()

        _runner_task = asyncio.ensure_future(_run())

        return tool_result(
            status="started",
            port=DASHBOARD_PORT,
            url=f"http://localhost:{DASHBOARD_PORT}",
            message=f"Dashboard started at http://localhost:{DASHBOARD_PORT}",
        )
    else:
        # No running loop — start one
        web.run_app(app, host="0.0.0.0", port=DASHBOARD_PORT, print=None)
        return tool_result(
            status="started",
            port=DASHBOARD_PORT,
            url=f"http://localhost:{DASHBOARD_PORT}",
        )


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_aiohttp_available() -> bool:
    """Dashboard requires aiohttp."""
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="quant_dashboard",
    toolset="quant",
    schema={
        "name": "quant_dashboard",
        "description": (
            "Web dashboard for the quantitative trading system. "
            "Provides a real-time dark-themed UI at http://localhost:8899 with: "
            "(1) Portfolio Dashboard showing balance, positions with unrealized PnL, "
            "daily PnL, kill switch and cooldown status, recent orders. "
            "(2) Equity Curve page with SVG charts of balance over time and cumulative PnL. "
            "(3) Trade Journal page with recent decisions, confidence scores, and reasoning. "
            "Auto-refreshes every 10 seconds. Also exposes REST API endpoints: "
            "/api/portfolio, /api/positions, /api/history, /api/equity, /api/journal, /api/regime. "
            "Action 'serve' starts the web server on port 8899."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["serve", "status"],
                    "description": (
                        "'serve': start the web dashboard server on port 8899. "
                        "'status': check if the dashboard server is running."
                    ),
                },
            },
            "required": ["action"],
        },
    },
    handler=_handle_quant_dashboard,
    check_fn=_check_aiohttp_available,
    requires_env=[],
    is_async=False,
    description="Real-time web dashboard for quant trading portfolio, equity curve, and journal",
    emoji="📊",
)
