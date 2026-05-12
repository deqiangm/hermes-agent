#!/usr/bin/env python3
"""quant_situational_awareness — Enhanced situational awareness for the quant trading brain.

V2: 8 microstructure features + 3 macro/sentiment features with FRED daily cache.
Traditional quantitative methods (RS, breadth, concentration) supplemented by LLM reasoning.
All data from free APIs — FRED_API_KEY required for macro_regime.

Actions (V1 Microstructure):
 sector_rotation — Sector relative strength + rotation detection (P0)
 concentration — Asset class + sector concentration analysis (P0)
 breadth — Market breadth indicators (advance/decline, new H/L) (P0)
 iv_regime — IV regime dual logic: buy OR sell premium based on IV rank (P0)
 dynamic_limits — Dynamic position limits based on regime + conviction (P1)
 gap_analysis — Alpha vs portfolio sector gap analysis (P1)
 opportunity_cost — Track missed alpha vs actual portfolio returns (P1)
 cash_drag — Cash utilization analysis + deployment suggestions (P1)

Actions (V2 Macro/Sentiment):
 macro_regime — Monetary + credit + economic regime from FRED cache (daily refresh)
 social_sentiment — Reddit + Finnhub social sentiment for watchlist tickers
 news_sentiment — Aggregated news sentiment with NLP scoring for key tickers
"""

import glob
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.registry import registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

SECTOR_ETFS = {
    "XLK": "Technology",
    "XLV": "Healthcare",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLU": "Utilities",
    "XLI": "Industrials",
    "XLP": "Consumer Staples",
    "XLRE": "Real Estate",
    "XLB": "Materials",
    "XLC": "Communication Services",
    "XLY": "Consumer Discretionary",
}

RS_WINDOWS = [5, 10, 20, 60]
RS_WEIGHTS = {5: 0.15, 10: 0.25, 20: 0.30, 60: 0.30}

# Ticker → sector static mapping (extended)
TICKER_SECTOR_MAP: Dict[str, str] = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology",
    "GOOG": "Technology", "AMZN": "Technology", "META": "Technology",
    "NFLX": "Technology", "CRM": "Technology", "ORCL": "Technology",
    "ADBE": "Technology", "NOW": "Technology", "INTU": "Technology",
    "SNPS": "Technology", "CDNS": "Technology", "PANW": "Technology",
    "FTNT": "Technology", "SHOP": "Technology", "SQ": "Technology",
    # Semiconductors
    "NVDA": "Semiconductors", "AMD": "Semiconductors", "MU": "Semiconductors",
    "AVGO": "Semiconductors", "INTC": "Semiconductors", "QCOM": "Semiconductors",
    "MCHP": "Semiconductors", "TXN": "Semiconductors", "AMAT": "Semiconductors",
    "LRCX": "Semiconductors", "KLAC": "Semiconductors", "MRVL": "Semiconductors",
    "ON": "Semiconductors", "NXPI": "Semiconductors", "SWKS": "Semiconductors",
    "TSM": "Semiconductors", "ASML": "Semiconductors",
    # Consumer Discretionary
    "TSLA": "Consumer Discretionary", "HD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary", "MCD": "Consumer Discretionary",
    "SBUX": "Consumer Discretionary", "LOW": "Consumer Discretionary",
    "TGT": "Consumer Discretionary", "BKNG": "Consumer Discretionary",
    "MAR": "Consumer Discretionary", "GM": "Consumer Discretionary",
    "F": "Consumer Discretionary", "RIVN": "Consumer Discretionary",
    # Communication Services
    "DIS": "Communication Services", "WBD": "Communication Services",
    "CMCSA": "Communication Services", "T": "Communication Services",
    "VZ": "Communication Services", "TMUS": "Communication Services",
    # Financials
    "JPM": "Financials", "BAC": "Financials", "GS": "Financials",
    "MS": "Financials", "C": "Financials", "WFC": "Financials",
    "SCHW": "Financials", "BLK": "Financials", "SPGI": "Financials",
    "AXP": "Financials", "COF": "Financials", "USB": "Financials",
    # Healthcare
    "JNJ": "Healthcare", "PFE": "Healthcare", "UNH": "Healthcare",
    "MRK": "Healthcare", "ABBV": "Healthcare", "LLY": "Healthcare",
    "TMO": "Healthcare", "ABT": "Healthcare", "MDT": "Healthcare",
    "AMGN": "Healthcare", "GILD": "Healthcare", "CVS": "Healthcare",
    "ISRG": "Healthcare", "VRTX": "Healthcare", "BIIB": "Healthcare",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "SLB": "Energy", "EOG": "Energy", "OXY": "Energy",
    "VLO": "Energy", "MPC": "Energy", "PSX": "Energy",
    # Industrials
    "CAT": "Industrials", "BA": "Industrials", "GE": "Industrials",
    "MMM": "Industrials", "HON": "Industrials", "UPS": "Industrials",
    "RTX": "Industrials", "LMT": "Industrials", "DE": "Industrials",
    "UNP": "Industrials", "EMR": "Industrials", "ETN": "Industrials",
    # Utilities
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    "D": "Utilities", "AEP": "Utilities", "EXC": "Utilities",
    # Consumer Staples
    "PG": "Consumer Staples", "KO": "Consumer Staples",
    "PEP": "Consumer Staples", "COST": "Consumer Staples",
    "WMT": "Consumer Staples", "PM": "Consumer Staples",
    "MO": "Consumer Staples", "MDLZ": "Consumer Staples",
    # Real Estate
    "AMT": "Real Estate", "PLD": "Real Estate",
    "CCI": "Real Estate", "EQIX": "Real Estate",
    "SPG": "Real Estate", "O": "Real Estate",
    # Materials
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials",
    "ECL": "Materials", "DD": "Materials", "DOW": "Materials",
    "NEM": "Materials", "FCX": "Materials", "NUE": "Materials",
    # Broad Market / Index
    "SPY": "Broad Market", "QQQ": "Broad Market", "IWM": "Broad Market",
    "DIA": "Broad Market", "VTI": "Broad Market", "VOO": "Broad Market",
    # Crypto
    "BTC/USDT": "Crypto", "ETH/USDT": "Crypto", "SOL/USDT": "Crypto",
    "BNB/USDT": "Crypto", "XRP/USDT": "Crypto", "ADA/USDT": "Crypto",
    "DOGE/USDT": "Crypto", "AVAX/USDT": "Crypto", "DOT/USDT": "Crypto",
    "MATIC/USDT": "Crypto", "LINK/USDT": "Crypto",
    # Crypto without slash
    "BTCUSD": "Crypto", "ETHUSD": "Crypto",
}

SECTOR_IV_ETFS = ["XLK", "XLV", "XLF", "XLE", "XLU", "XLI", "XLP", "XLRE", "XLB", "XLC", "XLY"]

# ---------------------------------------------------------------------------
# V2: FRED Macro Data Cache
# ---------------------------------------------------------------------------

FRED_CACHE_DIR = os.path.expanduser("~/.hermes/quant_trading/fred_cache")
FRED_CACHE_HOURS = 12 # Refresh every 12 hours (not every call)

SA_CACHE_DIR = os.path.expanduser("~/.hermes/quant_trading/sa_cache")

# FRED series IDs for macro regime detection
FRED_SERIES = {
    # Monetary (L1)
    "DFF": {"name": "Fed Funds Rate", "dimension": "monetary", "unit": "%"},
    "T10Y2Y": {"name": "10Y-2Y Spread", "dimension": "monetary", "unit": "%"},
    "T10Y3M": {"name": "10Y-3M Spread", "dimension": "monetary", "unit": "%"},
    "WALCL": {"name": "Fed Total Assets", "dimension": "monetary", "unit": "B$"},
    "M2SL": {"name": "M2 Money Stock", "dimension": "monetary", "unit": "B$"},
    # Credit (L2)
    "BAA10Y": {"name": "BAA-10Y Spread (IG proxy)", "dimension": "credit", "unit": "%"},
    "FRED_HYOAS": {"name": "HY OAS (ICE BofA)", "dimension": "credit", "unit": "%"},
    "TOTLL": {"name": "Commercial Loans", "dimension": "credit", "unit": "B$"},
    "DRSFRACBS": {"name": "Delinquency Rate", "dimension": "credit", "unit": "%"},
    # Economic (L3)
    "ADS_INDEX": {"name": "ADS Real-time Index", "dimension": "economic", "unit": "index"},
    "CPIAUCSL": {"name": "CPI (All Urban)", "dimension": "economic", "unit": "index"},
    "UNRATE": {"name": "Unemployment Rate", "dimension": "economic", "unit": "%"},
    "ICSA": {"name": "Initial Jobless Claims", "dimension": "economic", "unit": "K"},
    "UMCSENT": {"name": "Consumer Sentiment", "dimension": "economic", "unit": "index"},
}

# FRED_HYOAS is actually "BAMLH0A0HYM2" in FRED
FRED_SERIES_ALIASES = {"FRED_HYOAS": "BAMLH0A0HYM2"}


def _get_fred_cache_path(series_id: str) -> str:
    return os.path.join(FRED_CACHE_DIR, f"{series_id}.json")


def _fred_cache_is_fresh(series_id: str) -> bool:
    """Check if cached FRED data is fresh enough (< FRED_CACHE_HOURS old)."""
    cache_path = _get_fred_cache_path(series_id)
    if not os.path.exists(cache_path):
        return False
    try:
        mtime = os.path.getmtime(cache_path)
        age_hours = (datetime.now().timestamp() - mtime) / 3600
        return age_hours < FRED_CACHE_HOURS
    except Exception:
        return False


def _fetch_fred_series(series_id: str) -> Optional[dict]:
    """Fetch a single FRED series. Uses cache if fresh, otherwise API call."""
    actual_id = FRED_SERIES_ALIASES.get(series_id, series_id)

    # Return cached data if fresh
    if _fred_cache_is_fresh(actual_id):
        cache_path = _get_fred_cache_path(actual_id)
        try:
            with open(cache_path, "r") as f:
                return json.load(f)
        except Exception:
            pass

    # Fetch from FRED API
    fred_key = os.getenv("FRED_API_KEY", "")
    if not fred_key:
        return {"error": "FRED_API_KEY not set", "series_id": actual_id}

    try:
        from fredapi import Fred
        fred = Fred(api_key=fred_key)
        series = fred.get_series(actual_id)
        if series is None or len(series) == 0:
            return {"error": "no data", "series_id": actual_id}

        latest_val = float(series.iloc[-1])
        prev_val = float(series.iloc[-2]) if len(series) > 1 else latest_val
        change = latest_val - prev_val

        # Get last 30 data points for trend analysis
        recent = series.iloc[-30:] if len(series) >= 30 else series
        values = [float(v) for v in recent.values]
        dates = [str(d.date()) for d in recent.index]

        result = {
            "series_id": actual_id,
            "name": FRED_SERIES.get(series_id, {}).get("name", actual_id),
            "dimension": FRED_SERIES.get(series_id, {}).get("dimension", "unknown"),
            "latest_value": round(latest_val, 4),
            "previous_value": round(prev_val, 4),
            "change": round(change, 4),
            "change_pct": round(change / abs(prev_val) * 100, 2) if prev_val != 0 else 0,
            "last_updated": str(series.index[-1].date()),
            "trend_30d": values,
            "trend_dates": dates,
        }

        # Save to cache
        os.makedirs(FRED_CACHE_DIR, exist_ok=True)
        cache_path = _get_fred_cache_path(actual_id)
        with open(cache_path, "w") as f:
            json.dump(result, f)

        return result

    except Exception as e:
        logger.warning("FRED fetch failed for %s: %s", actual_id, e)
        # Return stale cache if available
        cache_path = _get_fred_cache_path(actual_id)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    stale = json.load(f)
                    stale["stale"] = True
                    return stale
            except Exception:
                pass
        return {"error": str(e), "series_id": actual_id}


def _prefetch_all_fred_series() -> dict:
    """Prefetch all FRED series — called by daily cron job or on first access."""
    results = {}
    for sid in FRED_SERIES:
        results[sid] = _fetch_fred_series(sid)
    return {
        "status": "ok",
        "series_fetched": len([r for r in results.values() if "error" not in r]),
        "series_errors": len([r for r in results.values() if "error" in r]),
        "cache_dir": FRED_CACHE_DIR,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# SA Snapshot Cache Layer
# ---------------------------------------------------------------------------

# Max cache age (hours) per action type
_V1_MICROSTRUCTURE_ACTIONS = {
    "sector_rotation", "concentration", "breadth", "iv_regime",
    "dynamic_limits", "gap_analysis", "opportunity_cost", "cash_drag",
}
_MACRO_ACTIONS = {"macro_regime"}
_SOCIAL_SENTIMENT_ACTIONS = {"social_sentiment"}
_NEWS_SENTIMENT_ACTIONS = {"news_sentiment"}


def _get_cache_max_age(action: str) -> int:
    """Return max cache age in hours for a given action."""
    if action in _V1_MICROSTRUCTURE_ACTIONS:
        return 1
    if action in _MACRO_ACTIONS:
        return 12
    if action in _SOCIAL_SENTIMENT_ACTIONS:
        return 2
    if action in _NEWS_SENTIMENT_ACTIONS:
        return 1
    return 12  # default


def _write_sa_cache(action: str, data: dict) -> None:
    """Save an SA action result to the local JSON cache."""
    os.makedirs(SA_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(SA_CACHE_DIR, f"{action}.json")
    cache_entry = {
        "action": action,
        "data": data,
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(cache_path, "w") as f:
            json.dump(cache_entry, f, indent=2)
    except Exception as e:
        logger.warning("SA cache write failed for %s: %s", action, e)


def _read_sa_cache(action: str, max_age_hours: int = None) -> Optional[dict]:
    """Read cached SA action result if fresh enough.

    Returns the cache entry dict (with 'data', 'cached_at' keys) or None.
    """
    if max_age_hours is None:
        max_age_hours = _get_cache_max_age(action)
    cache_path = os.path.join(SA_CACHE_DIR, f"{action}.json")
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r") as f:
            entry = json.load(f)
        cached_at_str = entry.get("cached_at", "")
        if cached_at_str:
            cached_at = datetime.fromisoformat(cached_at_str)
            age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
            if age_hours > max_age_hours:
                entry["stale"] = True
        return entry
    except Exception as e:
        logger.warning("SA cache read failed for %s: %s", action, e)
        return None


def _action_sa_snapshot(args: dict) -> str:
    """Read ALL cached SA action results in one shot — zero API calls.

    Brain Trader can call this to get the full SA picture without
    triggering any external data fetches.
    """
    actions_data = {}
    cache_age_summary = {}
    stale_actions = []

    if not os.path.isdir(SA_CACHE_DIR):
        return json.dumps({
            "status": "no_cache",
            "cache_age_summary": {},
            "actions": {},
            "stale_actions": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    for filename in sorted(os.listdir(SA_CACHE_DIR)):
        if not filename.endswith(".json"):
            continue
        action_name = filename[:-5]  # strip .json
        max_age = _get_cache_max_age(action_name)
        entry = _read_sa_cache(action_name, max_age_hours=max_age)
        if entry is None:
            continue

        cached_at_str = entry.get("cached_at", "unknown")
        is_stale = entry.get("stale", False)
        if is_stale:
            stale_actions.append(action_name)

        # Compute age in hours for summary
        age_hours = None
        if cached_at_str and cached_at_str != "unknown":
            try:
                cached_at = datetime.fromisoformat(cached_at_str)
                age_hours = round(
                    (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600, 2
                )
            except Exception:
                pass

        cache_age_summary[action_name] = {
            "age_hours": age_hours,
            "max_age_hours": max_age,
            "stale": is_stale,
        }
        actions_data[action_name] = entry.get("data", {})

    return json.dumps({
        "status": "ok",
        "cache_age_summary": cache_age_summary,
        "actions": actions_data,
        "stale_actions": stale_actions,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# V2: Reddit/Finnhub Sentiment Helpers
# ---------------------------------------------------------------------------

# Default watchlist tickers for sentiment scanning
SENTIMENT_WATCHLIST = [
    "SPY", "QQQ", "AAPL", "NVDA", "TSLA", "META", "MSFT", "AMZN",
    "AMD", "GOOGL", "NFLX", "PLTR", "SOXX",
]

REDDIT_SUBREDDITS = ["wallstreetbets", "stocks", "options", "investing"]
FINNHUB_SENTIMENT_URL = "https://finnhub.io/api/v1/stock/social-sentiment"

# Alpha Scanner V4 DB & report paths
ALPHA_SCANNER_DB = os.path.expanduser("~/.hermes/cron/alpha-stock-finder/social_data/social_sentiment.db")
ALPHA_SCANNER_REPORTS_DIR = os.path.expanduser("~/.hermes/cron/alpha-stock-finder/reports")


def _fetch_reddit_from_alpha_scanner(tickers: List[str]) -> dict:
    """Read Reddit sentiment from Alpha Scanner V4's SQLite DB + report JSON.

    Reuses the Reddit data that Alpha Scanner already collects hourly via
    its SocialSentimentEngine, avoiding the need for praw API credentials.

    Priority:
      1. Alpha Scanner SQLite DB (today's post_tickers + reddit_posts)
      2. Latest Alpha Scanner report JSON (social_signal, spike_ratio, conviction)
      3. Falls back to praw-based _fetch_reddit_sentiment if no data

    Returns per-ticker dict with: mentions, bullish_pct, bearish_pct,
    neutral_pct, net_sentiment, mention_spike_ratio, call_count, put_count,
    social_conviction, sample_titles, source.
    """
    import time as _time

    results = {}
    source = "alpha_scanner_db"
    db_has_data = False

    # --- 1. Read from Alpha Scanner SQLite DB ---
    try:
        if os.path.exists(ALPHA_SCANNER_DB):
            db = sqlite3.connect(ALPHA_SCANNER_DB, timeout=10)
            db.row_factory = sqlite3.Row
            cur = db.cursor()

            # Use last 24 hours as the time window (more robust than calendar day
            # boundary, handles cases where the scanner hasn't run since midnight UTC)
            cutoff_ts = _time.time() - 86400

            # Aggregate today's mentions per ticker (from requested list)
            # Use ? placeholders for the ticker list
            placeholders = ",".join("?" for _ in tickers)
            cur.execute(f"""
                SELECT pt.ticker,
                       COUNT(*) as mentions,
                       AVG(pt.wsb_score) as avg_wsb_score,
                       SUM(CASE WHEN pt.wsb_score > 0 THEN 1 ELSE 0 END) as bullish_count,
                       SUM(CASE WHEN pt.wsb_score < 0 THEN 1 ELSE 0 END) as bearish_count,
                       SUM(CASE WHEN pt.wsb_score = 0 THEN 1 ELSE 0 END) as neutral_count,
                       SUM(pt.has_calls) as call_count,
                       SUM(pt.has_puts) as put_count
                FROM post_tickers pt
                JOIN reddit_posts rp ON pt.post_id = rp.id
                WHERE rp.created_utc >= ?
                  AND pt.ticker IN ({placeholders})
                GROUP BY pt.ticker
            """, [cutoff_ts] + list(tickers))

            for row in cur.fetchall():
                db_has_data = True
                t = row["ticker"]
                mentions = row["mentions"] or 0
                bull = row["bullish_count"] or 0
                bear = row["bearish_count"] or 0
                neut = row["neutral_count"] or 0
                avg_score = row["avg_wsb_score"] or 0.0

                # Convert wsb_score to percentage scale
                # wsb_score ranges roughly -1 to +1
                bullish_pct = round(bull / mentions * 100, 1) if mentions else 0.0
                bearish_pct = round(bear / mentions * 100, 1) if mentions else 0.0
                neutral_pct = round(neut / mentions * 100, 1) if mentions else 0.0
                # net_sentiment: scaled to -100..+100 range
                net_sentiment = round(avg_score * 100, 1)

                results[t] = {
                    "mentions": mentions,
                    "bullish_pct": bullish_pct,
                    "bearish_pct": bearish_pct,
                    "neutral_pct": neutral_pct,
                    "net_sentiment": net_sentiment,
                    "mention_spike_ratio": 0.0,  # filled from report below
                    "call_count": row["call_count"] or 0,
                    "put_count": row["put_count"] or 0,
                    "social_conviction": "",
                    "sample_titles": [],  # filled below
                    "source": source,
                }

            # Fetch sample titles for tickers found in DB
            found_tickers = list(results.keys())
            if found_tickers:
                tp = ",".join("?" for _ in found_tickers)
                cur.execute(f"""
                    SELECT pt.ticker, rp.title, rp.subreddit, pt.wsb_score, rp.score
                    FROM post_tickers pt
                    JOIN reddit_posts rp ON pt.post_id = rp.id
                    WHERE rp.created_utc >= ?
                      AND pt.ticker IN ({tp})
                      AND rp.title != ''
                    GROUP BY pt.post_id, pt.ticker
                    ORDER BY rp.score DESC
                """, [cutoff_ts] + found_tickers)

                title_map = {}  # ticker -> list of sample titles
                for row in cur.fetchall():
                    t = row["ticker"]
                    if t not in title_map:
                        title_map[t] = []
                    if len(title_map[t]) < 3:
                        title_map[t].append({
                            "title": row["title"][:80],
                            "sentiment": round(row["wsb_score"], 3),
                            "subreddit": row["subreddit"],
                            "score": row["score"],
                        })
                for t, titles in title_map.items():
                    if t in results:
                        results[t]["sample_titles"] = titles

            db.close()

    except Exception as e:
        logger.warning("Alpha Scanner DB read failed: %s", e)

    # --- 2. Enrich from latest Alpha Scanner report JSON ---
    try:
        if os.path.isdir(ALPHA_SCANNER_REPORTS_DIR):
            report_files = sorted(
                glob.glob(os.path.join(ALPHA_SCANNER_REPORTS_DIR, "alpha_scan_v4_*.json")),
                reverse=True,
            )
            if report_files:
                latest_report = report_files[0]
                with open(latest_report, "r") as f:
                    report_data = json.load(f)

                # Build a lookup: ticker -> social fields
                all_cands = report_data.get("all_candidates", [])
                if not all_cands:
                    # Fallback to top_picks
                    all_cands = report_data.get("top_picks", [])

                for cand in all_cands:
                    if not isinstance(cand, dict):
                        continue
                    t = cand.get("ticker", "")
                    if not t:
                        continue

                    if t in results:
                        # Enrich existing DB entry
                        results[t]["mention_spike_ratio"] = cand.get("mention_spike_ratio", 0.0)
                        results[t]["social_conviction"] = cand.get("social_conviction", "")
                        if results[t]["source"] == "alpha_scanner_db":
                            results[t]["source"] = "alpha_scanner_db+report"
                    elif t in [tk.upper() for tk in tickers]:
                        # Ticker not in DB but in report — add from report only
                        results[t] = {
                            "mentions": cand.get("wsb_mentions", 0),
                            "bullish_pct": 0.0,
                            "bearish_pct": 0.0,
                            "neutral_pct": 0.0,
                            "net_sentiment": round(cand.get("wsb_sentiment", 0) * 100, 1),
                            "mention_spike_ratio": cand.get("mention_spike_ratio", 0.0),
                            "call_count": 1 if cand.get("cp_signal") == "heavy_calls" else 0,
                            "put_count": 1 if cand.get("cp_signal") == "heavy_puts" else 0,
                            "social_conviction": cand.get("social_conviction", ""),
                            "sample_titles": [],
                            "source": "alpha_scanner_report",
                        }
                        db_has_data = True  # report data counts as having data

    except Exception as e:
        logger.warning("Alpha Scanner report read failed: %s", e)

    # --- 3. Fallback to praw if no data from Alpha Scanner ---
    if not db_has_data:
        logger.info("No Alpha Scanner data found for today, falling back to praw")
        praw_results = _fetch_reddit_sentiment(tickers)
        if "error" not in praw_results:
            for t, data in praw_results.items():
                if isinstance(data, dict):
                    data["source"] = "praw_fallback"
                    # Add missing fields with defaults
                    data.setdefault("mention_spike_ratio", 0.0)
                    data.setdefault("call_count", 0)
                    data.setdefault("put_count", 0)
                    data.setdefault("social_conviction", "")
                    results[t] = data
        return praw_results if not results else results

    return results


def _fetch_reddit_sentiment(tickers: List[str]) -> dict:
    """Fetch Reddit sentiment for given tickers using praw.
    Returns mention counts + sentiment from post titles."""
    reddit_id = os.getenv("REDDIT_CLIENT_ID", "")
    reddit_secret = os.getenv("REDDIT_CLIENT_SECRET", "")
    reddit_ua = os.getenv("REDDIT_USER_AGENT", "hermes_quant_sa/1.0")

    if not reddit_id or not reddit_secret:
        return {"error": "REDDIT_CLIENT_ID/SECRET not set", "source": "reddit"}

    try:
        import praw
        reddit = praw.Reddit(
            client_id=reddit_id,
            client_secret=reddit_secret,
            user_agent=reddit_ua,
        )

        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        analyzer = SentimentIntensityAnalyzer()

        results = {}
        for ticker in tickers[:6]:  # Limit to 6 tickers to avoid rate limits
            mentions = 0
            pos_count = 0
            neg_count = 0
            neu_count = 0
            sample_titles = []

            for sub_name in REDDIT_SUBREDDITS:
                try:
                    subreddit = reddit.subreddit(sub_name)
                    # Search hot + new posts
                    for post in subreddit.search(ticker, sort="hot", time_filter="day", limit=10):
                        title = post.title
                        score = analyzer.polarity_scores(title)
                        mentions += 1
                        if score["compound"] >= 0.05:
                            pos_count += 1
                        elif score["compound"] <= -0.05:
                            neg_count += 1
                        else:
                            neu_count += 1
                        if len(sample_titles) < 3:
                            sample_titles.append({
                                "title": title[:80],
                                "sentiment": round(score["compound"], 3),
                                "subreddit": sub_name,
                                "score": post.score,
                            })
                except Exception as e:
                    logger.warning("Reddit search failed for %s in %s: %s", ticker, sub_name, e)

            if mentions > 0:
                results[ticker] = {
                    "mentions": mentions,
                    "bullish_pct": round(pos_count / mentions * 100, 1),
                    "bearish_pct": round(neg_count / mentions * 100, 1),
                    "neutral_pct": round(neu_count / mentions * 100, 1),
                    "net_sentiment": round((pos_count - neg_count) / mentions * 100, 1),
                    "sample_titles": sample_titles,
                    "source": "reddit",
                }

        return results

    except Exception as e:
        logger.warning("Reddit sentiment fetch failed: %s", e)
        return {"error": str(e), "source": "reddit"}


def _fetch_finnhub_sentiment(tickers: List[str]) -> dict:
    """Fetch social sentiment from Finnhub API (free tier)."""
    finnhub_key = os.getenv("FINNHUB_API_KEY", "")
    if not finnhub_key:
        return {"error": "FINNHUB_API_KEY not set", "source": "finnhub"}

    import urllib.request
    import urllib.parse

    results = {}
    for ticker in tickers[:6]:
        try:
            url = f"{FINNHUB_SENTIMENT_URL}?symbol={ticker}&token={finnhub_key}"
            req = urllib.request.Request(url, headers={"User-Agent": "hermes/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            reddit_data = data.get("reddit", [])[:7]  # Last 7 days
            twitter_data = data.get("twitter", [])[:7]

            reddit_score = sum(d.get("score", 0) for d in reddit_data) / len(reddit_data) if reddit_data else 0
            reddit_mentions = sum(d.get("mention", 0) for d in reddit_data)
            twitter_score = sum(d.get("score", 0) for d in twitter_data) / len(twitter_data) if twitter_data else 0
            twitter_mentions = sum(d.get("mention", 0) for d in twitter_data)

            results[ticker] = {
                "reddit_sentiment": round(reddit_score, 3),
                "reddit_mentions_7d": reddit_mentions,
                "twitter_sentiment": round(twitter_score, 3),
                "twitter_mentions_7d": twitter_mentions,
                "combined_sentiment": round((reddit_score + twitter_score) / 2, 3) if (reddit_score or twitter_score) else 0,
                "source": "finnhub",
            }
        except Exception as e:
            logger.warning("Finnhub sentiment failed for %s: %s", ticker, e)

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db_path() -> str:
    return os.path.expanduser("~/.hermes/quant_trading/paper_trading.db")


def _get_report_dir() -> str:
    return os.path.expanduser("~/.hermes/cron/alpha-stock-finder/reports")


def _get_db_conn() -> Optional[sqlite3.Connection]:
    db_path = _get_db_path()
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except Exception as e:
        logger.warning("DB connection failed: %s", e)
        return None


def _safe_close(conn):
    try:
        if conn:
            conn.close()
    except Exception:
        pass


def _read_latest_alpha_report() -> Optional[dict]:
    report_dir = _get_report_dir()
    pattern = os.path.join(report_dir, "alpha_scan_v4_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    try:
        with open(files[-1], "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to read alpha report: %s", e)
        return None


def _get_sector_for_ticker(ticker: str) -> str:
    t = ticker.upper().strip()
    if t in TICKER_SECTOR_MAP:
        return TICKER_SECTOR_MAP[t]
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        sector = info.get("sector", "")
        if sector:
            return sector
    except Exception:
        pass
    return "Unknown"


def _classify_asset(symbol: str, asset_class_col: str = "") -> str:
    s = symbol.upper()
    if asset_class_col and asset_class_col.lower() == "crypto":
        return "crypto"
    if asset_class_col and asset_class_col.lower() in ("option", "options"):
        return "option"
    if "/USDT" in s or "/USD" in s or s in ("BTCUSD", "ETHUSD"):
        return "crypto"
    return "stock"


def _get_column_from_multi(data, ticker: str, col: str):
    """Extract a column from a yfinance multi-ticker DataFrame.

    yfinance download with multiple tickers returns a DataFrame with
    MultiIndex columns (ticker, field) when group_by='ticker' (default),
    or a Panel-style DataFrame.  This helper handles both layouts.
    """
    import pandas as pd
    if isinstance(data.columns, pd.MultiIndex):
        try:
            return data[ticker][col]
        except (KeyError, TypeError):
            # Sometimes yfinance returns (field, ticker) instead
            try:
                return data[col][ticker]
            except (KeyError, TypeError):
                return None
    else:
        # Single-ticker or flat layout
        try:
            return data[col]
        except KeyError:
            return None


# ---------------------------------------------------------------------------
# Action 1: sector_rotation
# ---------------------------------------------------------------------------

def _action_sector_rotation(args: dict) -> str:
    """Sector relative strength + rotation detection using 11 SPDR sector ETFs."""
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return json.dumps({"status": "error", "error": "yfinance / pandas not available"})

    all_tickers = list(SECTOR_ETFS.keys()) + ["SPY"]

    try:
        data = yf.download(
            tickers=all_tickers,
            period="70d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        return json.dumps({"status": "error", "error": f"yfinance download failed: {e}"})

    if data.empty:
        return json.dumps({"status": "error", "error": "No data returned from yfinance"})

    # Compute SPY returns for each window
    spy_close = _get_column_from_multi(data, "SPY", "Close")
    if spy_close is None:
        return json.dumps({"status": "error", "error": "Could not extract SPY Close data"})

    spy_close = spy_close.dropna()
    spy_returns = {}
    for w in RS_WINDOWS:
        if len(spy_close) > w:
            spy_returns[w] = (spy_close.iloc[-1] / spy_close.iloc[-w - 1]) - 1
        else:
            spy_returns[w] = 0.0

    sector_scores = {}
    rotation_signals = []
    money_flow_proxies = {}

    for etf, sector_name in SECTOR_ETFS.items():
        try:
            close = _get_column_from_multi(data, etf, "Close")
            vol = _get_column_from_multi(data, etf, "Volume")
            if close is None or vol is None:
                continue
            close = close.dropna()
            vol = vol.dropna()

            if len(close) < 2:
                continue

            rs_values = {}
            sector_returns = {}
            for w in RS_WINDOWS:
                if len(close) > w:
                    ret = (close.iloc[-1] / close.iloc[-w - 1]) - 1
                else:
                    ret = 0.0
                sector_returns[w] = ret
                spy_ret = spy_returns.get(w, 0)
                if abs(spy_ret) > 1e-8:
                    rs_values[w] = ret / spy_ret
                else:
                    rs_values[w] = 0.0

            # Weighted momentum score
            score = sum(rs_values.get(w, 0) * RS_WEIGHTS[w] for w in RS_WINDOWS)
            # Normalize to 0-100 (RS values typically -2 to +2)
            normalized_score = min(max(50 + score * 25, 0), 100)

            sector_scores[sector_name] = {
                "etf": etf,
                "rs_5d": round(rs_values.get(5, 0), 4),
                "rs_10d": round(rs_values.get(10, 0), 4),
                "rs_20d": round(rs_values.get(20, 0), 4),
                "rs_60d": round(rs_values.get(60, 0), 4),
                "return_5d": round(sector_returns.get(5, 0) * 100, 2),
                "return_20d": round(sector_returns.get(20, 0) * 100, 2),
                "momentum_score": round(normalized_score, 1),
            }

            # Money flow proxy = volume * price_change (OBV concept)
            if len(close) >= 2 and len(vol) >= 1:
                price_chg = close.iloc[-1] - close.iloc[-2]
                mfp = float(vol.iloc[-1]) * float(price_chg)
                money_flow_proxies[sector_name] = round(mfp, 0)

            # Rotation detection: 5d RS vs 60d RS
            rs_5 = rs_values.get(5, 0)
            rs_60 = rs_values.get(60, 0)
            if rs_5 > rs_60 + 0.3 and rs_5 > 1.0:
                rotation_signals.append({
                    "sector": sector_name,
                    "etf": etf,
                    "signal": "ROTATING_INTO",
                    "rs_5d": round(rs_5, 4),
                    "rs_60d": round(rs_60, 4),
                    "delta": round(rs_5 - rs_60, 4),
                })
            elif rs_5 < rs_60 - 0.3 and rs_60 > 1.0:
                rotation_signals.append({
                    "sector": sector_name,
                    "etf": etf,
                    "signal": "ROTATING_OUT",
                    "rs_5d": round(rs_5, 4),
                    "rs_60d": round(rs_60, 4),
                    "delta": round(rs_5 - rs_60, 4),
                })

        except Exception as e:
            logger.debug("Error processing %s: %s", etf, e)
            continue

    # Sort sectors by momentum score
    sorted_sectors = sorted(
        sector_scores.items(),
        key=lambda x: x[1]["momentum_score"],
        reverse=True,
    )
    sorted_scores = [{k: v} for k, v in sorted_sectors]

    # Flat format for frontend compatibility — each entry is a flat dict
    sorted_scores_flat = []
    for sector_name, sdata in sorted_sectors:
        ms = sdata["momentum_score"]
        if ms > 60:
            signal = "BULLISH"
        elif ms < 40:
            signal = "BEARISH"
        else:
            signal = "NEUTRAL"
        sorted_scores_flat.append({
            "sector": sector_name,
            "momentum_score": ms,
            "signal": signal,
            "etf": sdata["etf"],
            "rs_5d": sdata["rs_5d"],
            "rs_10d": sdata["rs_10d"],
            "rs_20d": sdata["rs_20d"],
            "rs_60d": sdata["rs_60d"],
            "return_5d": sdata["return_5d"],
            "return_20d": sdata["return_20d"],
        })

    leaders = [name for name, sdata in sorted_sectors if sdata["momentum_score"] > 60]
    laggards = [name for name, sdata in sorted_sectors if sdata["momentum_score"] < 40]

    # Alpha V4 report sector composition
    alpha_sector_composition = {}
    report = _read_latest_alpha_report()
    if report:
        top_picks = report.get("top_picks", [])
        sector_counts: Dict[str, int] = {}
        for pick in top_picks:
            ticker = pick.get("ticker", "")
            sector = pick.get("sector") or _get_sector_for_ticker(ticker)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
        alpha_sector_composition = dict(sorted(
            sector_counts.items(),
            key=lambda x: x[1],
            reverse=True,
        ))

    return json.dumps({
        "status": "ok",
        "action": "sector_rotation",
        "sector_scores": sorted_scores_flat,
        "sector_scores_nested": sorted_scores,
        "leaders": leaders,
        "laggards": laggards,
        "rotation_signals": rotation_signals,
        "money_flow_proxies": money_flow_proxies,
        "alpha_sector_composition": alpha_sector_composition,
    })


# ---------------------------------------------------------------------------
# Price fetching helpers (with simple in-memory cache)
# ---------------------------------------------------------------------------

_PRICE_CACHE: Dict[str, tuple] = {}  # symbol -> (price, timestamp)
_PRICE_CACHE_TTL = 300  # 5 minutes


def _fetch_price_cached(symbol: str) -> Optional[float]:
    """Fetch current price with 5-min in-memory cache to reduce yfinance calls."""
    import time
    now = time.time()
    cached = _PRICE_CACHE.get(symbol)
    if cached and (now - cached[1]) < _PRICE_CACHE_TTL:
        return cached[0]
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        price = tk.fast_info.get("lastPrice") or 0
        if price > 0:
            _PRICE_CACHE[symbol] = (float(price), now)
            return float(price)
    except Exception:
        pass
    return None


_OPTION_PREM_CACHE: Dict[str, tuple] = {}  # key -> (premium, timestamp)


def _fetch_option_premium_cached(symbol: str, strike: float, expiry: str, option_type: str) -> Optional[float]:
    """Fetch current option premium with 5-min cache."""
    import time
    now = time.time()
    cache_key = f"{symbol}_{option_type}_{strike}_{expiry}"
    cached = _OPTION_PREM_CACHE.get(cache_key)
    if cached and (now - cached[1]) < _PRICE_CACHE_TTL:
        return cached[0]
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        chain = tk.option_chain(expiry)
        if option_type == "call":
            df = chain.calls
        else:
            df = chain.puts
        if "impliedVolatility" not in df.columns:
            return None
        # Find closest strike
        closest = df.iloc[(df["strike"] - strike).abs().argsort().iloc[0]]
        premium = float(closest.get("lastPrice", 0))
        if premium > 0:
            _OPTION_PREM_CACHE[cache_key] = (premium, now)
            return premium
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Action 2: concentration
# ---------------------------------------------------------------------------

def _action_concentration(args: dict) -> str:
    """Asset class + sector concentration analysis from portfolio DB."""
    conn = _get_db_conn()
    if conn is None:
        return json.dumps({
            "status": "error",
            "error": "paper_trading.db not found or empty. No portfolio to analyze.",
        })

    try:
        bal_row = conn.execute(
            "SELECT usd, initial_usd FROM balance WHERE id = 1"
        ).fetchone()
        cash = bal_row["usd"] if bal_row else 0.0

        rows = conn.execute(
            "SELECT symbol, side, amount, entry_price, stop_loss_price, "
            "COALESCE(asset_class, '') as asset_class, "
            "COALESCE(instrument_type, 'stock_crypto') as instrument_type, "
            "COALESCE(option_type, '') as option_type, "
            "COALESCE(premium_paid, NULL) as premium_paid "
            "FROM positions"
        ).fetchall()

        positions = []
        for r in rows:
            positions.append({
                "symbol": r["symbol"],
                "side": r["side"],
                "amount": r["amount"],
                "entry_price": r["entry_price"],
                "stop_loss_price": r["stop_loss_price"],
                "asset_class": r["asset_class"],
                "instrument_type": r["instrument_type"],
                "option_type": r["option_type"],
                "premium_paid": r["premium_paid"],
            })
    finally:
        _safe_close(conn)

    class_invested = {"crypto": 0.0, "stock": 0.0, "option": 0.0}
    sector_invested: Dict[str, float] = {}

    for pos in positions:
        sym = pos["symbol"]
        amount = pos["amount"]
        entry = pos["entry_price"]

        inst = pos.get("instrument_type", "stock_crypto")
        if inst == "option":
            ac = "option"
            # Use current premium for mark-to-market value
            current_prem = _fetch_option_premium_cached(
                sym, pos.get("strike", 0), pos.get("expiry", ""), pos.get("option_type", "call")
            )
            if current_prem and current_prem > 0:
                value = current_prem * 100 * amount
            else:
                # Fallback to entry premium if current quote unavailable
                premium = pos.get("premium_paid") or entry
                value = premium * 100 * amount
        else:
            ac = _classify_asset(sym, pos.get("asset_class", ""))
            # Use current market price for mark-to-market value
            current_price = _fetch_price_cached(sym)
            if current_price and current_price > 0:
                value = amount * current_price
            else:
                # Fallback to entry price if current quote unavailable
                value = amount * entry

        class_invested[ac] = class_invested.get(ac, 0.0) + value

        sector = _get_sector_for_ticker(sym)
        sector_invested[sector] = sector_invested.get(sector, 0.0) + value

    total_invested = sum(class_invested.values())
    total_value = cash + total_invested

    class_pct = {}
    if total_invested > 0:
        for ac, val in class_invested.items():
            class_pct[ac] = round(val / total_invested * 100, 1)
    else:
        class_pct = {k: 0.0 for k in class_invested}

    sector_pct = {}
    if total_invested > 0:
        for sec, val in sector_invested.items():
            sector_pct[sec] = round(val / total_invested * 100, 1)

    sector_allocation = dict(sorted(sector_pct.items(), key=lambda x: x[1], reverse=True))

    warnings = []
    risk_level = "LOW"

    for ac, pct in class_pct.items():
        if pct > 70:
            warnings.append(f"CRITICAL: {ac} is {pct}% of invested capital (>70%)")
            risk_level = "CRITICAL"
        elif pct > 50:
            warnings.append(f"WARNING: {ac} is {pct}% of invested capital (>50%)")
            if risk_level != "CRITICAL":
                risk_level = "HIGH"

    for sec, pct in sector_pct.items():
        if pct > 70:
            warnings.append(f"CRITICAL: {sec} sector is {pct}% of invested capital (>70%)")
            risk_level = "CRITICAL"
        elif pct > 50:
            warnings.append(f"WARNING: {sec} sector is {pct}% of invested capital (>50%)")
            if risk_level not in ("CRITICAL", "HIGH"):
                risk_level = "HIGH"

    if not warnings:
        risk_level = "LOW"

    # --- New computed fields ---
    # HHI: Herfindahl-Hirschman Index from sector percentages (range 0-1)
    hhi = round(sum((pct / 100) ** 2 for pct in sector_pct.values()), 4) if sector_pct else 0.0

    # Sector concentration: percentage in single largest sector
    sector_concentration = round(max(sector_pct.values()), 1) if sector_pct else 0.0

    # Stock concentration: percentage of invested capital in single largest stock position
    stock_position_values = {}
    for pos in positions:
        sym = pos["symbol"]
        amount = pos["amount"]
        entry = pos["entry_price"]
        inst = pos.get("instrument_type", "stock_crypto")
        if inst == "option":
            premium = pos.get("premium_paid") or entry
            value = premium * 100 * amount
        else:
            value = amount * entry
        stock_position_values[sym] = stock_position_values.get(sym, 0.0) + value

    stock_concentration = round(
        max(stock_position_values.values()) / total_invested * 100, 1
    ) if total_invested > 0 and stock_position_values else 0.0

    return json.dumps({
        "status": "ok",
        "action": "concentration",
        "total_value": round(total_value, 2),
        "cash": round(cash, 2),
        "invested_capital": round(total_invested, 2),
        "hhi": hhi,
        "stock_concentration": stock_concentration,
        "sector_concentration": sector_concentration,
        "allocation_by_class": {
            "crypto": {"invested": round(class_invested["crypto"], 2), "pct": class_pct.get("crypto", 0)},
            "stock": {"invested": round(class_invested["stock"], 2), "pct": class_pct.get("stock", 0)},
            "option": {"invested": round(class_invested["option"], 2), "pct": class_pct.get("option", 0)},
        },
        "allocation_by_sector": sector_allocation,
        "sector_invested": {k: round(v, 2) for k, v in sorted(
            sector_invested.items(), key=lambda x: x[1], reverse=True
        )},
        "warnings": warnings,
        "concentration_risk_level": risk_level,
    })


# ---------------------------------------------------------------------------
# Action 3: breadth
# ---------------------------------------------------------------------------

def _action_breadth(args: dict) -> str:
    """Market breadth indicators from 11 sector ETFs."""
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return json.dumps({"status": "error", "error": "yfinance / pandas not available"})

    etf_list = list(SECTOR_ETFS.keys())

    try:
        data = yf.download(
            tickers=etf_list,
            period="60d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        return json.dumps({"status": "error", "error": f"yfinance download failed: {e}"})

    if data.empty:
        return json.dumps({"status": "error", "error": "No data returned"})

    advancing = 0
    declining = 0
    new_highs = 0
    new_lows = 0
    leadership_sectors = []
    count_above_sma50 = 0
    total_etfs_processed = 0

    # Collect per-ETF closes for McClellan
    closes_by_etf: Dict[str, "pd.Series"] = {}

    for etf, sector_name in SECTOR_ETFS.items():
        try:
            close = _get_column_from_multi(data, etf, "Close")
            if close is None:
                continue
            close = close.dropna()
            closes_by_etf[etf] = close

            if len(close) < 2:
                continue

            today_chg = close.iloc[-1] - close.iloc[-2]
            if today_chg > 0:
                advancing += 1
            elif today_chg < 0:
                declining += 1

            if len(close) >= 20:
                high_20d = close.iloc[-20:].max()
                low_20d = close.iloc[-20:].min()
                if close.iloc[-1] >= high_20d:
                    new_highs += 1
                    leadership_sectors.append(sector_name)
                if close.iloc[-1] <= low_20d:
                    new_lows += 1

            # Check if current close > 50-day SMA
            total_etfs_processed += 1
            if len(close) >= 50:
                sma_50 = close.iloc[-50:].mean()
                if close.iloc[-1] > sma_50:
                    count_above_sma50 += 1

        except Exception as e:
            logger.debug("Breadth error for %s: %s", etf, e)
            continue

    total = advancing + declining
    ad_ratio = round(advancing / declining, 2) if declining > 0 else float(advancing)

    # McClellan Oscillator proxy
    mcclellan_proxy = _compute_mcclellan_proxy(closes_by_etf, etf_list)

    # Breadth assessment
    breadth_pct = (advancing / total * 100) if total > 0 else 0
    if breadth_pct >= 80:
        breadth_assessment = "STRONG_BULLISH"
        breadth_note = "Breadth thrust: >80% sectors advancing"
    elif breadth_pct >= 60:
        breadth_assessment = "BULLISH"
        breadth_note = "Healthy breadth: majority advancing"
    elif breadth_pct >= 40:
        breadth_assessment = "NEUTRAL"
        breadth_note = "Mixed breadth"
    elif breadth_pct >= 20:
        breadth_assessment = "BEARISH"
        breadth_note = "Weak breadth: majority declining"
    else:
        breadth_assessment = "STRONG_BEARISH"
        breadth_note = "Breadth collapse: <20% advancing"

    # Compute pct_above_sma50
    pct_above_sma50 = round(count_above_sma50 / total_etfs_processed * 100, 1) if total_etfs_processed > 0 else 0.0

    return json.dumps({
        "status": "ok",
        "action": "breadth",
        "advance_decline_ratio": ad_ratio,
        "advancing": advancing,
        "declining": declining,
        "new_highs_count": new_highs,
        "new_lows_count": new_lows,
        "mcclellan_proxy": mcclellan_proxy,
        "mcclellan": mcclellan_proxy,
        "leadership_sectors": leadership_sectors,
        "breadth_pct": round(breadth_pct, 1),
        "breadth_assessment": breadth_assessment,
        "breadth_status": breadth_assessment,
        "breadth_note": breadth_note,
        "pct_above_sma50": pct_above_sma50,
    })


def _compute_mcclellan_proxy(closes_by_etf: dict, etf_list: list) -> float:
    """Compute a simplified McClellan Oscillator proxy.

    McClellan = EMA_19(net advances) - EMA_39(net advances).
    With only ~11 sectors, this is a rough proxy.
    """
    if not closes_by_etf:
        return 0.0

    # Find the max length of available data
    max_len = 0
    for etf in etf_list:
        c = closes_by_etf.get(etf)
        if c is not None and len(c) > max_len:
            max_len = len(c)

    if max_len < 2:
        return 0.0

    # Compute daily net advances (across all ETFs, aligned by position from end)
    daily_net_advances = []
    for i in range(max_len):
        day_adv = 0
        day_dec = 0
        for etf in etf_list:
            c = closes_by_etf.get(etf)
            if c is not None and len(c) > i + 1:
                # Compare day[-(i+2)] vs day[-(i+1)]
                idx_current = -(i + 1)
                idx_prev = -(i + 2)
                if abs(idx_prev) <= len(c) and abs(idx_current) <= len(c):
                    chg = c.iloc[idx_current] - c.iloc[idx_prev]
                    if chg > 0:
                        day_adv += 1
                    elif chg < 0:
                        day_dec += 1
        daily_net_advances.append(day_adv - day_dec)

    # Reverse to chronological order (oldest first)
    daily_net_rev = list(reversed(daily_net_advances))

    if not daily_net_rev:
        return 0.0

    # EMA helper
    def _ema(values: list, span: int) -> float:
        if not values:
            return 0.0
        k = 2.0 / (span + 1)
        ema = float(values[0])
        for v in values[1:]:
            ema = float(v) * k + ema * (1 - k)
        return ema

    ema_19 = _ema(daily_net_rev, 19)
    ema_39 = _ema(daily_net_rev, 39)
    return round(ema_19 - ema_39, 2)


# ---------------------------------------------------------------------------
# Action 4: iv_regime
# ---------------------------------------------------------------------------

def _action_iv_regime(args: dict) -> str:
    """IV regime dual logic: premium selling vs buying based on IV rank."""
    try:
        import yfinance as yf
    except ImportError:
        return json.dumps({"status": "error", "error": "yfinance not available"})

    symbol = args.get("symbol", "SPY")

    spy_iv_data = _fetch_iv_for_symbol("SPY")
    iv_rank = spy_iv_data.get("iv_rank", 50)
    current_iv = spy_iv_data.get("current_iv", 0)
    iv_1y_high = spy_iv_data.get("iv_1y_high", 0)
    iv_1y_low = spy_iv_data.get("iv_1y_low", 0)

    # Regime classification with dual logic
    if iv_rank > 50:
        regime_label = "PREMIUM_SELLING_FAVORABLE"
        premium_selling_favorable = True
        premium_buying_favorable = False
        recommended_strategies = [
            {"strategy": "Covered Call", "description": "Sell OTM calls against long stock"},
            {"strategy": "Iron Condor", "description": "Sell OTM call + put spread for credit"},
            {"strategy": "Credit Spread", "description": "Sell higher IV, buy lower IV wing"},
        ]
    elif iv_rank < 30:
        regime_label = "PREMIUM_BUYING_FAVORABLE"
        premium_selling_favorable = False
        premium_buying_favorable = True
        recommended_strategies = [
            {"strategy": "Bull Call Debit Spread", "description": "Buy ATM call, sell OTM call — cheap directional bet"},
            {"strategy": "Long Calls/Puts", "description": "Buy options when they are cheap"},
            {"strategy": "Calendar Spread", "description": "Cheap entry — sell near-term, buy far-term"},
        ]
    else:
        regime_label = "NEUTRAL"
        premium_selling_favorable = False
        premium_buying_favorable = False
        recommended_strategies = [
            {"strategy": "Small Iron Condor", "description": "Reduced size credit spread in neutral IV"},
            {"strategy": "Calendar Spread", "description": "Sell near-term, buy far-term at similar IV"},
            {"strategy": "Vertical Spreads", "description": "Directional spreads with moderate IV"},
        ]

    # Derive premium_action / recommendation / reasoning
    if regime_label == "PREMIUM_SELLING_FAVORABLE":
        premium_action = "sell_premium"
        reasoning = (
            f"IV rank at {iv_rank:.0f}% — options are expensive, "
            f"favorable for selling premium to capture elevated implied volatility"
        )
    elif regime_label == "PREMIUM_BUYING_FAVORABLE":
        premium_action = "buy_premium"
        reasoning = (
            f"IV rank at {iv_rank:.0f}% — options are cheap, "
            f"favorable for buying premium to capture directional moves at low cost"
        )
    else:
        premium_action = "neutral"
        reasoning = (
            f"IV rank at {iv_rank:.0f}% — IV is in the neutral zone, "
            f"no strong premium selling or buying advantage"
        )
    recommendation = premium_action

    # Sector IV opportunities
    sector_iv_opportunities = []
    for etf in SECTOR_IV_ETFS:
        iv_data = _fetch_iv_for_symbol(etf)
        etf_iv = iv_data.get("current_iv", 0)
        etf_rank = iv_data.get("iv_rank")
        if etf_iv > 0 and etf_rank is not None:
            sector_name = SECTOR_ETFS.get(etf, etf)
            if etf_rank > 60:
                sector_iv_opportunities.append({
                    "etf": etf,
                    "sector": sector_name,
                    "iv_rank": round(etf_rank, 1),
                    "opportunity": "PREMIUM_SELLING",
                    "note": f"{sector_name} IV rank {etf_rank:.0f}% — favorable for selling premium",
                })
            elif etf_rank < 25:
                sector_iv_opportunities.append({
                    "etf": etf,
                    "sector": sector_name,
                    "iv_rank": round(etf_rank, 1),
                    "opportunity": "PREMIUM_BUYING",
                    "note": f"{sector_name} IV rank {etf_rank:.0f}% — options are cheap, good for buying",
                })

    return json.dumps({
        "status": "ok",
        "action": "iv_regime",
        "symbol": symbol,
        "current_iv": round(current_iv, 4),
        "iv_1y_high": round(iv_1y_high, 4),
        "iv_1y_low": round(iv_1y_low, 4),
        "iv_rank": round(iv_rank, 1),
        "iv_regime_label": regime_label,
        "premium_selling_favorable": premium_selling_favorable,
        "premium_buying_favorable": premium_buying_favorable,
        "premium_action": premium_action,
        "recommendation": recommendation,
        "reasoning": reasoning,
        "recommended_strategies": recommended_strategies,
        "sector_iv_opportunities": sector_iv_opportunities,
    })


def _fetch_iv_for_symbol(symbol: str) -> dict:
    """Fetch current IV and compute IV rank using multi-expiry historical IV.

    True IV rank compares current ATM IV to the range of ATM IVs across
    available expiries, rather than using realized vol as a proxy (which
    systematically underestimates implied vol and mis-ranks IV).
    """
    try:
        import yfinance as yf
        import pandas as pd

        tk = yf.Ticker(symbol)

        # Get options chain
        expiries = tk.options or []
        if not expiries:
            return {"current_iv": 0, "iv_rank": 50, "iv_1y_high": 0, "iv_1y_low": 0}

        # Pick nearest expiry a few days out
        target_expiry = None
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for exp in expiries:
            if exp > today_str:
                target_expiry = exp
                break
        if not target_expiry:
            target_expiry = expiries[0]

        chain = tk.option_chain(target_expiry)
        calls = chain.calls

        # Current price
        current_price = 0
        try:
            current_price = tk.fast_info.get("lastPrice") or 0
        except Exception:
            pass
        if current_price <= 0:
            try:
                current_price = tk.info.get("regularMarketPrice", 0)
            except Exception:
                pass

        if current_price <= 0:
            return {"current_iv": 0, "iv_rank": 50, "iv_1y_high": 0, "iv_1y_low": 0}

        # Find ATM call IV
        if "impliedVolatility" not in calls.columns:
            return {"current_iv": 0, "iv_rank": 50, "iv_1y_high": 0, "iv_1y_low": 0}

        atm_calls = calls[
            (calls["strike"] >= current_price * 0.95) &
            (calls["strike"] <= current_price * 1.05)
        ]
        if atm_calls.empty:
            calls_copy = calls.copy()
            calls_copy["strike_diff"] = abs(calls_copy["strike"] - current_price)
            atm_calls = calls_copy.nsmallest(1, "strike_diff")

        if atm_calls.empty:
            return {"current_iv": 0, "iv_rank": 50, "iv_1y_high": 0, "iv_1y_low": 0}

        current_iv = float(atm_calls["impliedVolatility"].iloc[0])

        # --- IV Rank: build from multi-expiry ATM IV series ---
        # Each expiry's ATM call IV represents market's implied vol expectation
        # for that period. This gives a true IV rank rather than realized-vol proxy.
        historical_ivs = [current_iv]

        for exp in expiries[:8]:
            if exp == target_expiry:
                continue
            try:
                exp_chain = tk.option_chain(exp)
                exp_calls = exp_chain.calls
                if "impliedVolatility" not in exp_calls.columns:
                    continue
                exp_atm = exp_calls[
                    (exp_calls["strike"] >= current_price * 0.95) &
                    (exp_calls["strike"] <= current_price * 1.05)
                ]
                if exp_atm.empty:
                    exp_c = exp_calls.copy()
                    exp_c["strike_diff"] = abs(exp_c["strike"] - current_price)
                    exp_atm = exp_c.nsmallest(1, "strike_diff")
                if not exp_atm.empty:
                    exp_iv = float(exp_atm["impliedVolatility"].iloc[0])
                    if exp_iv > 0.01:
                        historical_ivs.append(exp_iv)
            except Exception:
                continue

        # If we have enough expiry IVs, use them directly for IV rank
        if len(historical_ivs) >= 3:
            iv_1y_high = max(historical_ivs)
            iv_1y_low = min(historical_ivs)
            iv_method = "multi_expiry_iv"
        else:
            # Fallback: use 1-year realized vol range as proxy (less accurate)
            hist = tk.history(period="1y")
            if hist.empty or len(hist) < 20:
                return {
                    "current_iv": current_iv,
                    "iv_rank": 50,
                    "iv_1y_high": current_iv * 1.5,
                    "iv_1y_low": current_iv * 0.5,
                    "iv_method": "fallback_no_data",
                }

            returns = hist["Close"].pct_change().dropna()
            rolling_vol = returns.rolling(20).std() * (252 ** 0.5)

            iv_1y_high = float(rolling_vol.max()) if not rolling_vol.empty else current_iv * 1.5
            iv_1y_low = float(rolling_vol.min()) if not rolling_vol.empty else current_iv * 0.5
            iv_method = "realized_vol_proxy"

        # Ensure current_iv fits in range
        iv_1y_high = max(iv_1y_high, current_iv)
        iv_1y_low = min(iv_1y_low, current_iv)

        iv_range = iv_1y_high - iv_1y_low
        if iv_range > 1e-6:
            iv_rank = (current_iv - iv_1y_low) / iv_range * 100
        else:
            iv_rank = 50.0

        iv_rank = max(0, min(100, iv_rank))

        return {
            "current_iv": current_iv,
            "iv_rank": iv_rank,
            "iv_1y_high": iv_1y_high,
            "iv_1y_low": iv_1y_low,
            "iv_method": iv_method,
            "historical_iv_points": len(historical_ivs),
        }

    except Exception as e:
        logger.debug("IV fetch failed for %s: %s", symbol, e)
        return {"current_iv": 0, "iv_rank": 50, "iv_1y_high": 0, "iv_1y_low": 0}


# ---------------------------------------------------------------------------
# Action 5: dynamic_limits
# ---------------------------------------------------------------------------

def _action_dynamic_limits(args: dict) -> str:
    """Compute dynamic position limits based on market regime + confidence."""
    # Determine current regime via quant_market_intel (single source of truth)
    regime, confidence = _compute_current_regime()

    # Base limits by regime
    regime_base_limits = {
        "trending": 6,
        "trending_high_confidence": 8,
        "ranging": 5,
        "neutral": 5,
        "volatile_trend": 5,
        "stressed": 3,
        "crisis": 2,
        "complacent": 6,
        "contrarian_extreme_fear": 5,
        "contrarian_extreme_greed": 3,
    }

    base = regime_base_limits.get(regime, 3)

    # Confidence modifier
    if confidence >= 0.8:
        conf_mod = 1
    elif confidence >= 0.5:
        conf_mod = 0
    else:
        conf_mod = -1

    max_positions = max(1, base + conf_mod)

    # Per-asset dollar limits
    per_asset_limits = {
        "stock_max": 15000,
        "option_max": 5000,
        "crypto_max": None,
        "sector_basket_max": 15000,
    }

    # Read current positions from DB
    conn = _get_db_conn()
    current_positions = []
    cash = 0.0
    total_value = 0.0

    if conn is not None:
        try:
            bal_row = conn.execute(
                "SELECT usd, initial_usd FROM balance WHERE id = 1"
            ).fetchone()
            cash = bal_row["usd"] if bal_row else 0.0
            total_value = cash

            rows = conn.execute(
                "SELECT symbol, side, amount, entry_price, stop_loss_price, "
                "COALESCE(asset_class, '') as asset_class, "
                "COALESCE(instrument_type, 'stock_crypto') as instrument_type, "
                "COALESCE(option_type, '') as option_type, "
                "COALESCE(strike, 0) as strike, "
                "COALESCE(expiry, '') as expiry "
                "FROM positions"
            ).fetchall()

            for r in rows:
                inst = r["instrument_type"]
                sym = r["symbol"]
                if inst == "option":
                    ac = "option"
                    # Use current option premium for mark-to-market
                    cur_prem = _fetch_option_premium_cached(
                        sym, r["strike"], r["expiry"], r["option_type"]
                    )
                    if cur_prem and cur_prem > 0:
                        value = r["amount"] * cur_prem * 100
                    else:
                        value = r["amount"] * r["entry_price"]
                else:
                    ac = _classify_asset(sym, r["asset_class"])
                    # Use current market price for mark-to-market
                    cur_price = _fetch_price_cached(sym)
                    if cur_price and cur_price > 0:
                        value = r["amount"] * cur_price
                    else:
                        value = r["amount"] * r["entry_price"]

                current_positions.append({
                    "symbol": sym,
                    "side": r["side"],
                    "amount": r["amount"],
                    "entry_price": r["entry_price"],
                    "stop_loss_price": r["stop_loss_price"],
                    "value": round(value, 2),
                    "asset_class": ac,
                })
                total_value += value
        finally:
            _safe_close(conn)

    current_count = len(current_positions)
    available_slots = max(0, max_positions - current_count)

    # Position sizing: risk_per_trade = 5% of portfolio / (entry - stop)
    risk_budget_pct = 0.05
    risk_budget_dollars = round(total_value * risk_budget_pct, 2) if total_value > 0 else 0

    if available_slots == 0:
        sizing_recommendation = "No available slots — reduce existing positions before adding new ones"
    elif risk_budget_dollars <= 0:
        sizing_recommendation = "Portfolio value too low for meaningful position sizing"
    else:
        sizing_recommendation = (
            f"Risk ${risk_budget_dollars:.2f} per trade (5% of ${total_value:,.2f}). "
            f"Position size = risk_budget / (entry - stop). "
            f"Available slots: {available_slots}."
        )

    # Flat limits for frontend — same values as per_asset_limits but as top-level keys
    flat_limits = dict(per_asset_limits)

    return json.dumps({
        "status": "ok",
        "action": "dynamic_limits",
        "regime": regime,
        "confidence": round(confidence, 2),
        "max_positions": max_positions,
        "current_positions": current_count,
        "available_slots": available_slots,
        "per_asset_limits": per_asset_limits,
        "flat_limits": flat_limits,
"stock_max": per_asset_limits.get("stock_max", 15000),
"option_max": per_asset_limits.get("option_max", 5000),
"crypto_max": per_asset_limits.get("crypto_max", None),
"sector_basket_max": per_asset_limits.get("sector_basket_max", 15000),
        "risk_budget_pct": risk_budget_pct,
        "risk_budget_dollars": risk_budget_dollars,
        "portfolio_value": round(total_value, 2),
        "cash": round(cash, 2),
        "sizing_recommendation": sizing_recommendation,
        "current_position_details": current_positions,
    })


def _compute_current_regime() -> Tuple[str, float]:
    """Compute current market regime by delegating to quant_market_intel._action_regime.

    Returns (regime_string, confidence_multiplier).
    Falls back to a simplified inline calculation if market_intel is unavailable.
    """
    try:
        from tools.quant_market_intel import quant_market_intel_handler
        result_str = quant_market_intel_handler({
            "action": "regime",
            "symbol": "SPY",
            "asset_class": "stock",
        })
        result = json.loads(result_str) if isinstance(result_str, str) else result_str
        regime = result.get("composite_regime", "neutral")
        confidence = result.get("confidence_multiplier", 0.7)
        return regime, confidence
    except Exception as e:
        logger.debug("Regime delegation to market_intel failed: %s, using fallback", e)

    # Fallback: simplified inline regime (only used if market_intel is broken)
    try:
        import yfinance as yf
        tk = yf.Ticker("SPY")
        hist = tk.history(period="60d")
        if len(hist) > 20:
            closes = hist["Close"].tolist()
            deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
            up_days = sum(1 for d in deltas[-14:] if d > 0)
            down_days = sum(1 for d in deltas[-14:] if d < 0)
            direction = abs(up_days - down_days) / 14
            if direction > 0.5:
                tech_regime = "trending"
            elif direction < 0.2:
                tech_regime = "ranging"
            else:
                tech_regime = "neutral"
        else:
            tech_regime = "neutral"
    except Exception:
        tech_regime = "neutral"

    vix = 15.0
    try:
        import yfinance as yf
        vix_tk = yf.Ticker("^VIX")
        vix_hist = vix_tk.history(period="5d")
        if not vix_hist.empty:
            vix = float(vix_hist["Close"].iloc[-1])
    except Exception:
        pass

    if vix > 30:
        return "crisis", 0.2
    elif vix > 20:
        return "stressed", 0.4

    if tech_regime == "trending":
        return "trending", 0.9
    elif tech_regime == "ranging":
        return "ranging", 0.5
    else:
        return "neutral", 0.7


# ---------------------------------------------------------------------------
# Action 6: gap_analysis
# ---------------------------------------------------------------------------

def _action_gap_analysis(args: dict) -> str:
    """Alpha vs portfolio sector gap analysis."""
    report = _read_latest_alpha_report()
    if report is None:
        return json.dumps({
            "status": "error",
            "error": "No alpha V4 report found. Run alpha_scanner_v4.py first.",
        })

    top_picks = report.get("top_picks", [])
    all_candidates = report.get("all_candidates", [])

    # Extract sector composition from alpha picks
    # Prefer 'sector' field from alpha report (available after scanner update), fallback to TICKER_SECTOR_MAP
    alpha_sectors: Dict[str, List[str]] = {}
    for pick in (top_picks + all_candidates):
        ticker = pick.get("ticker", "")
        if not ticker:
            continue
        # Use real sector from alpha report if available, otherwise map
        sector = pick.get("sector") or _get_sector_for_ticker(ticker)
        if sector not in alpha_sectors:
            alpha_sectors[sector] = []
        if ticker not in alpha_sectors[sector]:
            alpha_sectors[sector].append(ticker)

    # Read portfolio positions
    conn = _get_db_conn()
    portfolio_sectors: Dict[str, List[str]] = {}
    portfolio_sectors_invested: Dict[str, float] = {}

    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT symbol, side, amount, entry_price FROM positions"
            ).fetchall()
            for r in rows:
                sym = r["symbol"]
                sector = _get_sector_for_ticker(sym)
                if sector not in portfolio_sectors:
                    portfolio_sectors[sector] = []
                if sym not in portfolio_sectors[sector]:
                    portfolio_sectors[sector].append(sym)
                value = r["amount"] * r["entry_price"]
                portfolio_sectors_invested[sector] = portfolio_sectors_invested.get(sector, 0) + value
        finally:
            _safe_close(conn)

    # Gaps: in alpha but NOT in portfolio
    alpha_sector_names = set(alpha_sectors.keys())
    portfolio_sector_names = set(portfolio_sectors.keys())

    gaps = []
    for sec in (alpha_sector_names - portfolio_sector_names):
        tickers = alpha_sectors.get(sec, [])
        gaps.append({
            "sector": sec,
            "alpha_tickers": tickers[:5],
            "note": f"No {sec} exposure in portfolio — alpha identifies {len(tickers)} candidate(s)",
        })

    # Overlaps: in both
    overlaps = []
    for sec in (alpha_sector_names & portfolio_sector_names):
        overlaps.append({
            "sector": sec,
            "portfolio_tickers": portfolio_sectors.get(sec, []),
            "alpha_tickers": alpha_sectors.get(sec, [])[:5],
        })

    # Redundancy warnings: >50% in one sector
    redundancy_warnings = []
    total_invested = sum(portfolio_sectors_invested.values())
    for sec, invested in portfolio_sectors_invested.items():
        if total_invested > 0:
            pct = invested / total_invested * 100
            if pct > 50:
                redundancy_warnings.append({
                    "sector": sec,
                    "pct_of_portfolio": round(pct, 1),
                    "invested": round(invested, 2),
                    "warning": f">50% concentrated in {sec}",
                })

    # Suggested actions
    suggested_actions = []
    for gap in gaps:
        sec = gap["sector"]
        tickers = gap["alpha_tickers"][:3]
        suggested_actions.append(
            f"Add {sec} exposure via {', '.join(tickers)} from alpha picks"
        )

    return json.dumps({
        "status": "ok",
        "action": "gap_analysis",
        "portfolio_sectors": list(portfolio_sector_names),
        "alpha_leading_sectors": list(alpha_sector_names),
        "gaps": gaps,
        "overlaps": overlaps,
        "gap_sectors": [g["sector"] for g in gaps],
        "overlap_sectors": [o["sector"] for o in overlaps],
        "redundancy_warnings": redundancy_warnings,
        "suggested_actions": suggested_actions,
    })


# ---------------------------------------------------------------------------
# Action 7: opportunity_cost
# ---------------------------------------------------------------------------

def _action_opportunity_cost(args: dict) -> str:
    """Track what top alpha picks earned vs what portfolio actually earned."""
    report = _read_latest_alpha_report()
    if report is None:
        return json.dumps({
            "status": "error",
            "error": "No alpha V4 report found. Run alpha_scanner_v4.py first.",
        })

    report_timestamp = report.get("timestamp", "unknown")
    top_picks = report.get("top_picks", [])[:12]

    if not top_picks:
        return json.dumps({"status": "error", "error": "No top picks in alpha report"})

    try:
        import yfinance as yf
    except ImportError:
        return json.dumps({"status": "error", "error": "yfinance not available"})

    hypothetical_returns = []
    top_missed = []

    for pick in top_picks[:3]:
        ticker = pick.get("ticker", "")
        if not ticker:
            continue

        # Get current price
        current_price = 0.0
        try:
            tk = yf.Ticker(ticker)
            current_price = tk.fast_info.get("lastPrice") or 0
            if current_price <= 0:
                hist = tk.history(period="5d")
                if not hist.empty:
                    current_price = float(hist["Close"].iloc[-1])
        except Exception:
            pass

        # Get entry price — try report data, then recent history
        entry_price = pick.get("price") or pick.get("entry_price") or 0
        if entry_price <= 0:
            try:
                tk2 = yf.Ticker(ticker)
                h = tk2.history(period="5d")
                if len(h) > 1:
                    entry_price = float(h["Close"].iloc[0])
            except Exception:
                entry_price = current_price  # fallback

        ret_pct = 0.0
        if entry_price > 0:
            ret_pct = (current_price - entry_price) / entry_price * 100

        hypothetical_returns.append({
            "ticker": ticker,
            "entry_price": round(entry_price, 2),
            "current_price": round(current_price, 2),
            "return_pct": round(ret_pct, 2),
        })

        if ret_pct > 2:
            top_missed.append({
                "ticker": ticker,
                "return_pct": round(ret_pct, 2),
                "note": f"Missed {ret_pct:.1f}% gain on {ticker}",
            })

    alpha_hypothetical_return = 0.0
    if hypothetical_returns:
        alpha_hypothetical_return = sum(
            h["return_pct"] for h in hypothetical_returns
        ) / len(hypothetical_returns)

    # Read portfolio trade results
    conn = _get_db_conn()
    portfolio_actual_return = 0.0
    total_pnl = 0.0
    trade_count = 0

    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT realized_pnl, created_at FROM trade_results "
                "ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
            for r in rows:
                pnl = r["realized_pnl"] if r["realized_pnl"] is not None else 0
                total_pnl += pnl
                trade_count += 1

            bal_row = conn.execute(
                "SELECT initial_usd FROM balance WHERE id = 1"
            ).fetchone()
            initial = bal_row["initial_usd"] if bal_row else 100000
            if initial > 0:
                portfolio_actual_return = total_pnl / initial * 100
        finally:
            _safe_close(conn)

    opportunity_cost_pct = round(alpha_hypothetical_return - portfolio_actual_return, 2)

    # --- New computed fields ---
    # missed_trades: alias for top_missed_opportunities with additional frontend-expected fields
    missed_trades = []
    for tm in top_missed:
        missed_trades.append({
            "symbol": tm.get("ticker", ""),
            "date": report_timestamp,
            "missed_return": tm.get("return_pct", 0),
            "ticker": tm.get("ticker", ""),
            "return_pct": tm.get("return_pct", 0),
            "note": tm.get("note", ""),
        })

    # cumulative_missed_return and total_missed: alias for opportunity_cost_pct
    cumulative_missed_return = opportunity_cost_pct
    total_missed = cumulative_missed_return

    # Store tracking data
    _store_opportunity_tracking(hypothetical_returns)

    return json.dumps({
        "status": "ok",
        "action": "opportunity_cost",
        "report_timestamp": report_timestamp,
        "alpha_picks_hypothetical_return": round(alpha_hypothetical_return, 2),
        "portfolio_actual_return": round(portfolio_actual_return, 2),
        "opportunity_cost_pct": opportunity_cost_pct,
        "cumulative_missed_return": cumulative_missed_return,
        "total_missed": total_missed,
        "total_trades": trade_count,
        "total_pnl": round(total_pnl, 2),
        "top_missed_opportunities": sorted(
            top_missed, key=lambda x: x["return_pct"], reverse=True
        )[:5],
        "missed_trades": sorted(
            missed_trades, key=lambda x: x.get("missed_return", 0), reverse=True
        )[:5],
        "hypothetical_details": hypothetical_returns,
    })


def _store_opportunity_tracking(hypothetical_returns: list) -> None:
    """Store opportunity tracking data in a simple SQLite table."""
    db_path = _get_db_path()
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS opportunity_tracking (
                date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL NOT NULL,
                return_pct REAL NOT NULL
            )
        """)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for h in hypothetical_returns:
            conn.execute(
                "INSERT INTO opportunity_tracking "
                "(date, ticker, entry_price, current_price, return_pct) "
                "VALUES (?, ?, ?, ?, ?)",
                (today, h["ticker"], h["entry_price"], h["current_price"], h["return_pct"]),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("Opportunity tracking store failed: %s", e)


# ---------------------------------------------------------------------------
# Action 8: cash_drag
# ---------------------------------------------------------------------------

def _action_cash_drag(args: dict) -> str:
    """Cash utilization analysis + deployment suggestions."""
    conn = _get_db_conn()
    if conn is None:
        return json.dumps({
            "status": "error",
            "error": "paper_trading.db not found or empty.",
        })

    try:
        bal_row = conn.execute(
            "SELECT usd, initial_usd FROM balance WHERE id = 1"
        ).fetchone()
        cash = bal_row["usd"] if bal_row else 0.0

        rows = conn.execute(
            "SELECT symbol, side, amount, entry_price FROM positions"
        ).fetchall()

        invested = sum(r["amount"] * r["entry_price"] for r in rows)
    finally:
        _safe_close(conn)

    total_value = cash + invested
    cash_pct = (cash / total_value * 100) if total_value > 0 else 100.0

    # Drag level
    if cash_pct > 85:
        drag_level = "CRITICAL"
        drag_note = "Major cash drag — portfolio almost entirely in cash"
    elif cash_pct > 70:
        drag_level = "WARNING"
        drag_note = "Significant cash drag — large unallocated capital"
    elif cash_pct > 50:
        drag_level = "INFO"
        drag_note = "Moderate cash drag — consider deploying more capital"
    else:
        drag_level = "OK"
        drag_note = "Cash allocation is reasonable"

    # Estimated annual drag: cash * SPY_avg_daily_return * 252
    # SPY avg daily ≈ 0.04% (long-term ~10% annual / 252)
    spy_avg_daily = 0.0004
    estimated_annual_drag = round(cash * spy_avg_daily * 252, 2)

    # Check for actionable signals
    actionable_signals_exist = False
    deployment_suggestions = []  # object format for frontend
    deployment_suggestions_text = []  # string format for backward compat

    # Sector rotation leaders
    try:
        rot_result = json.loads(_action_sector_rotation({}))
        leaders = rot_result.get("leaders", [])
        rotation_signals = rot_result.get("rotation_signals", [])
        if leaders:
            actionable_signals_exist = True
            for sec in leaders[:3]:
                rot_in = [
                    r for r in rotation_signals
                    if r["signal"] == "ROTATING_INTO" and r["sector"] == sec
                ]
                note = " (rotation INTO detected)" if rot_in else ""
                deploy_amt = min(cash * 0.2, 5000)
                est_ret = round(estimated_annual_drag * (deploy_amt / cash), 2) if cash > 0 else 0
                deployment_suggestions.append({
                    "sector": sec,
                    "strategy": f"Deploy ${deploy_amt:,.0f} via sector ETF or leading stocks{note}",
                    "est_return": est_ret,
                })
                deployment_suggestions_text.append(
                    f"Deploy ${deploy_amt:,.0f} into {sec} via sector ETF or leading stocks{note}"
                )
    except Exception:
        pass

    # IV regime buying opportunities
    try:
        iv_result = json.loads(_action_iv_regime({"symbol": "SPY"}))
        iv_rank = iv_result.get("iv_rank", 50)
        if iv_rank < 30:
            actionable_signals_exist = True
            deploy_amt = min(cash * 0.1, 2000)
            est_ret = round(estimated_annual_drag * (deploy_amt / cash), 2) if cash > 0 else 0
            deployment_suggestions.append({
                "sector": "Options",
                "strategy": f"Deploy ${deploy_amt:,.0f} into debit spreads or long calls",
                "est_return": est_ret,
            })
            deployment_suggestions_text.append(
                f"IV rank is {iv_rank:.0f}% — options are cheap. "
                f"Deploy ${deploy_amt:,.0f} into debit spreads or long calls"
            )
        sector_opps = iv_result.get("sector_iv_opportunities", [])
        buying_opps = [o for o in sector_opps if o.get("opportunity") == "PREMIUM_BUYING"]
        if buying_opps:
            actionable_signals_exist = True
            for opp in buying_opps[:2]:
                deploy_amt = min(cash * 0.1, 2000)
                est_ret = round(estimated_annual_drag * (deploy_amt / cash), 2) if cash > 0 else 0
                deployment_suggestions.append({
                    "sector": opp["sector"],
                    "strategy": f"Buy options exposure via {opp['etf']}",
                    "est_return": est_ret,
                })
                deployment_suggestions_text.append(
                    f"Low IV in {opp['sector']} ({opp['etf']}) — buy options exposure"
                )
    except Exception:
        pass

    # General suggestion if high cash
    if cash_pct > 50 and not deployment_suggestions:
        deploy_amt = min(cash * 0.3, 10000)
        est_ret = round(estimated_annual_drag * (deploy_amt / cash), 2) if cash > 0 else 0
        deployment_suggestions.append({
            "sector": "Broad Market",
            "strategy": f"Deploy ${deploy_amt:,.0f} into broad market (SPY) to reduce cash drag",
            "est_return": est_ret,
        })
        deployment_suggestions_text.append(
            f"Consider deploying ${deploy_amt:,.0f} into broad market (SPY) "
            f"to reduce cash drag"
        )

    return json.dumps({
        "status": "ok",
        "action": "cash_drag",
        "cash_amount": round(cash, 2),
        "total_value": round(total_value, 2),
        "invested_capital": round(invested, 2),
        "cash_pct": round(cash_pct, 1),
        "drag_level": drag_level,
        "drag_note": drag_note,
        "estimated_annual_drag": estimated_annual_drag,
        "actionable_signals_exist": actionable_signals_exist,
        "deployment_suggestions": deployment_suggestions,
        "deployment_suggestions_text": deployment_suggestions_text,
    })


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# V2 Action 9: macro_regime
# ---------------------------------------------------------------------------

def _action_macro_regime(args: dict) -> str:
    """Macro regime detection from FRED cached data.
    Reads 12h-cached FRED series, classifies monetary/credit/economic regime,
    and produces a fused regime signal with confidence."""
    tickers_str = args.get("tickers", "")
    # Fetch all FRED series from cache (no API calls if cache fresh)
    series_data = {}
    for sid in FRED_SERIES:
        series_data[sid] = _fetch_fred_series(sid)

    # --- Monetary Regime (L1) ---
    monetary = {}
    for sid in ["DFF", "T10Y2Y", "T10Y3M", "WALCL", "M2SL"]:
        d = series_data.get(sid, {})
        if "error" not in d:
            monetary[sid] = d

    fed_funds = monetary.get("DFF", {}).get("latest_value", 0)
    spread_10y2y = monetary.get("T10Y2Y", {}).get("latest_value", 0)
    spread_10y3m = monetary.get("T10Y3M", {}).get("latest_value", 0)

    # Inverted yield curve = monetary tightening signal
    monetary_signal = "neutral"
    monetary_confidence = 50
    if spread_10y2y < -0.5 or spread_10y3m < -0.5:
        monetary_signal = "tight"
        monetary_confidence = 80
    elif spread_10y2y < 0 or spread_10y3m < 0:
        monetary_signal = "tight"
        monetary_confidence = 65
    elif fed_funds < 2.0:
        monetary_signal = "accommodative"
        monetary_confidence = 70
    elif fed_funds < 4.0:
        monetary_signal = "neutral"
        monetary_confidence = 55

    # --- Credit Regime (L2) ---
    credit = {}
    for sid in ["BAA10Y", "FRED_HYOAS", "TOTLL", "DRSFRACBS"]:
        d = series_data.get(sid, {})
        if "error" not in d:
            credit[sid] = d

    baa_spread = credit.get("BAA10Y", {}).get("latest_value", 0)
    delinquency = credit.get("DRSFRACBS", {}).get("latest_value", 0)

    credit_signal = "normal"
    credit_confidence = 50
    if baa_spread > 3.5:
        credit_signal = "stress"
        credit_confidence = 80
    elif baa_spread > 2.5:
        credit_signal = "widening"
        credit_confidence = 65
    elif baa_spread < 1.5:
        credit_signal = "easy"
        credit_confidence = 60

    # --- Economic Regime (L3) ---
    economic = {}
    for sid in ["ADS_INDEX", "CPIAUCSL", "UNRATE", "ICSA", "UMCSENT"]:
        d = series_data.get(sid, {})
        if "error" not in d:
            economic[sid] = d

    ads_index = economic.get("ADS_INDEX", {}).get("latest_value", 0)
    unrate = economic.get("UNRATE", {}).get("latest_value", 0)
    umcsent = economic.get("UMCSENT", {}).get("latest_value", 0)

    economic_signal = "neutral"
    economic_confidence = 50
    if ads_index < -0.5 or unrate > 6.0:
        economic_signal = "recession"
        economic_confidence = 75
    elif ads_index < 0 or unrate > 5.0:
        economic_signal = "slowing"
        economic_confidence = 60
    elif ads_index > 0.5 and unrate < 4.0:
        economic_signal = "expansion"
        economic_confidence = 70

    # --- Fused Regime (L0) ---
    # Weight: monetary 30%, credit 30%, economic 40%
    regime_scores = {
        "expansion": 100, "easy": 85, "normal": 60,
        "neutral": 50, "widening": 40, "tight": 30,
        "slowing": 25, "stress": 15, "recession": 5,
    }
    m_score = regime_scores.get(monetary_signal, 50)
    c_score = regime_scores.get(credit_signal, 50)
    e_score = regime_scores.get(economic_signal, 50)
    fused_score = m_score * 0.3 + c_score * 0.3 + e_score * 0.4

    if fused_score >= 70:
        regime = "expansion"
    elif fused_score >= 50:
        regime = "normal"
    elif fused_score >= 30:
        regime = "stress"
    else:
        regime = "crisis"

    fused_confidence = int((monetary_confidence + credit_confidence + economic_confidence) / 3)

    # Regime-appropriate strategy hints
    strategy_map = {
        "expansion": "Risk-on: favor growth, leverage, long duration. Increase position sizes.",
        "normal": "Balanced: barbell strategy, maintain hedges. Standard position sizes.",
        "stress": "Risk-off: reduce leverage, raise cash, favor defensive sectors. Tighten stops.",
        "crisis": "Capital preservation: maximum cash, short vol if IV>80, wait for Fed pivot signal.",
    }

    return json.dumps({
        "status": "ok",
        "action": "macro_regime",
        "regime": regime,
        "regime_score": round(fused_score, 1),
        "confidence": fused_confidence,
        "strategy_hint": strategy_map.get(regime, ""),
        "dimensions": {
            "monetary": {"signal": monetary_signal, "confidence": monetary_confidence,
                         "fed_funds": fed_funds, "spread_10y2y": spread_10y2y, "spread_10y3m": spread_10y3m},
            "credit": {"signal": credit_signal, "confidence": credit_confidence,
                       "baa_spread": baa_spread, "delinquency_rate": delinquency},
            "economic": {"signal": economic_signal, "confidence": economic_confidence,
                         "ads_index": ads_index, "unemployment": unrate, "consumer_sentiment": umcsent},
        },
        "data_freshness": {
            sid: series_data[sid].get("last_updated", "unknown")
            for sid in FRED_SERIES if "error" not in series_data.get(sid, {})
        },
        "errors": {sid: series_data[sid].get("error") for sid in FRED_SERIES
                   if "error" in series_data.get(sid, {})},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# V2 Action 10: social_sentiment
# ---------------------------------------------------------------------------

def _action_social_sentiment(args: dict) -> str:
    """Social sentiment aggregation from Reddit + Finnhub.
    Real-time every call — captures latest community mood."""
    tickers_str = args.get("tickers", "")
    if tickers_str:
        tickers = [t.strip().upper() for t in tickers_str.split(",") if t.strip()]
    else:
        tickers = SENTIMENT_WATCHLIST

    # Fetch from both sources in parallel-ish (sequential but fast)
    reddit_data = _fetch_reddit_from_alpha_scanner(tickers)
    finnhub_data = _fetch_finnhub_sentiment(tickers)

    # Merge results per ticker
    merged = {}
    all_tickers = set(list(reddit_data.keys()) + list(finnhub_data.keys()))
    for t in all_tickers:
        # Skip error keys
        if t in ("error", "source"):
            continue
        entry = {"ticker": t}
        if t in reddit_data and isinstance(reddit_data[t], dict):
            entry["reddit"] = reddit_data[t]
        if t in finnhub_data and isinstance(finnhub_data[t], dict):
            entry["finnhub"] = finnhub_data[t]

        # Compute composite sentiment (-100 to +100)
        scores = []
        if "reddit" in entry:
            scores.append(entry["reddit"].get("net_sentiment", 0))
        if "finnhub" in entry:
            scores.append(entry["finnhub"].get("combined_sentiment", 0) * 100)
        entry["composite_sentiment"] = round(sum(scores) / len(scores), 1) if scores else 0

        # Sentiment classification
        cs = entry["composite_sentiment"]
        if cs > 30:
            entry["sentiment_label"] = "bullish"
        elif cs > 10:
            entry["sentiment_label"] = "slightly_bullish"
        elif cs < -30:
            entry["sentiment_label"] = "bearish"
        elif cs < -10:
            entry["sentiment_label"] = "slightly_bearish"
        else:
            entry["sentiment_label"] = "neutral"

        merged[t] = entry

    # Overall market sentiment
    if merged:
        avg_sentiment = round(sum(v["composite_sentiment"] for v in merged.values()) / len(merged), 1)
        bull_count = sum(1 for v in merged.values() if "bull" in v.get("sentiment_label", ""))
        bear_count = sum(1 for v in merged.values() if "bear" in v.get("sentiment_label", ""))
    else:
        avg_sentiment = 0
        bull_count = bear_count = 0

    # Contrarian signal
    contrarian = ""
    if avg_sentiment > 50:
        contrarian = "Euphoria zone — contrarian sell signal (historical win rate ~65%)"
    elif avg_sentiment > 30:
        contrarian = "Optimism elevated — consider tightening stops"
    elif avg_sentiment < -50:
        contrarian = "Panic zone — contrarian buy signal (historical win rate ~70%)"
    elif avg_sentiment < -30:
        contrarian = "Pessimism elevated — watch for reversal signals"

    return json.dumps({
        "status": "ok",
        "action": "social_sentiment",
        "overall_sentiment": avg_sentiment,
        "sentiment_label": "bullish" if avg_sentiment > 10 else "bearish" if avg_sentiment < -10 else "neutral",
        "bullish_tickers": bull_count,
        "bearish_tickers": bear_count,
        "contrarian_signal": contrarian,
        "tickers": merged,
        "data_sources": {
            "reddit": "ok" if "error" not in reddit_data else reddit_data.get("error", "error"),
            "finnhub": "ok" if "error" not in finnhub_data else finnhub_data.get("error", "error"),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# V2 Action 11: news_sentiment
# ---------------------------------------------------------------------------

def _action_news_sentiment(args: dict) -> str:
    """Aggregated news sentiment with NLP scoring.
    Uses Finnhub news API + VADER sentiment analysis."""
    tickers_str = args.get("tickers", "")
    if tickers_str:
        tickers = [t.strip().upper() for t in tickers_str.split(",") if t.strip()]
    else:
        tickers = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "META", "MSFT", "AMZN"]

    finnhub_key = os.getenv("FINNHUB_API_KEY", "")
    if not finnhub_key:
        return json.dumps({"status": "error", "action": "news_sentiment",
                           "error": "FINNHUB_API_KEY not set"})

    import urllib.request
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    analyzer = SentimentIntensityAnalyzer()

    results = {}
    for ticker in tickers[:6]:  # Limit to 6 tickers
        try:
            url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&token={finnhub_key}"
            req = urllib.request.Request(url, headers={"User-Agent": "hermes/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                articles = json.loads(resp.read().decode())

            # Analyze last 20 articles
            recent = articles[:20] if isinstance(articles, list) else []
            pos_count = 0
            neg_count = 0
            neu_count = 0
            total_score = 0
            headline_samples = []

            for art in recent:
                headline = art.get("headline", "")
                if not headline:
                    continue
                score = analyzer.polarity_scores(headline)
                compound = score["compound"]
                total_score += compound

                if compound >= 0.05:
                    pos_count += 1
                elif compound <= -0.05:
                    neg_count += 1
                else:
                    neu_count += 1

                if len(headline_samples) < 3:
                    headline_samples.append({
                        "headline": headline[:100],
                        "sentiment": round(compound, 3),
                        "source": art.get("source", ""),
                        "time": art.get("datetime", 0),
                    })

            n = max(pos_count + neg_count + neu_count, 1)
            results[ticker] = {
                "articles_analyzed": len(recent),
                "bullish_pct": round(pos_count / n * 100, 1),
                "bearish_pct": round(neg_count / n * 100, 1),
                "neutral_pct": round(neu_count / n * 100, 1),
                "avg_sentiment": round(total_score / n, 3),
                "net_bias": "bullish" if pos_count > neg_count else "bearish" if neg_count > pos_count else "neutral",
                "sample_headlines": headline_samples,
            }

        except Exception as e:
            logger.warning("News sentiment failed for %s: %s", ticker, e)
            results[ticker] = {"error": str(e)}

    # Aggregate market-level news sentiment
    valid = [v for v in results.values() if "avg_sentiment" in v]
    if valid:
        market_avg = round(sum(v["avg_sentiment"] for v in valid) / len(valid), 3)
        market_bull = sum(v["bullish_pct"] for v in valid) / len(valid)
        market_bear = sum(v["bearish_pct"] for v in valid) / len(valid)
    else:
        market_avg = 0
        market_bull = market_bear = 0

    # Signal velocity: compare bull/bear ratio
    if market_bear > 0 and market_bull / market_bear > 2.0:
        velocity_signal = "strong_bullish_flow"
    elif market_bull > 0 and market_bear / market_bull > 2.0:
        velocity_signal = "strong_bearish_flow"
    else:
        velocity_signal = "mixed_flow"

    return json.dumps({
        "status": "ok",
        "action": "news_sentiment",
        "market_avg_sentiment": market_avg,
        "market_bullish_pct": round(market_bull, 1),
        "market_bearish_pct": round(market_bear, 1),
        "signal_velocity": velocity_signal,
        "tickers": results,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


_ACTION_HANDLERS = {
    "sector_rotation": _action_sector_rotation,
    "concentration": _action_concentration,
    "breadth": _action_breadth,
    "iv_regime": _action_iv_regime,
    "dynamic_limits": _action_dynamic_limits,
    "gap_analysis": _action_gap_analysis,
    "opportunity_cost": _action_opportunity_cost,
    "cash_drag": _action_cash_drag,
    "macro_regime": _action_macro_regime,
    "social_sentiment": _action_social_sentiment,
    "news_sentiment": _action_news_sentiment,
    "sa_snapshot": _action_sa_snapshot,
}


def _handle_quant_situational_awareness(args: dict, **kw) -> str:
    """Main entry point for quant_situational_awareness tool."""
    action = args.get("action", "sector_rotation")
    handler = _ACTION_HANDLERS.get(action)
    if not handler:
        return json.dumps({
            "status": "error",
            "error": f"Unknown action: {action}. Available: {list(_ACTION_HANDLERS.keys())}",
        })
    try:
        result_str = handler(args)
        # Cache the result (except sa_snapshot itself to avoid recursive cache entries)
        if action != "sa_snapshot":
            try:
                result_dict = json.loads(result_str)
                _write_sa_cache(action, result_dict)
            except Exception as cache_err:
                logger.warning("SA cache write error for %s: %s", action, cache_err)
        return result_str
    except Exception as e:
        logger.error(
            "quant_situational_awareness error (action=%s): %s",
            action, e, exc_info=True,
        )
        return json.dumps({"status": "error", "action": action, "error": str(e)})


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_yfinance():
    """Return True if yfinance is importable."""
    try:
        import yfinance  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

registry.register(
 name="quant_situational_awareness",
 toolset="quant",
 schema={
 "name": "quant_situational_awareness",
"description": (
    "Situational awareness for the quant trading brain — 12 features (8 V1 microstructure + 3 V2 macro/sentiment + 1 snapshot cache). "
    "Traditional quantitative methods (RS, breadth, concentration) supplemented by LLM reasoning. "
    "V1 Actions: 'sector_rotation' (11 SPDR sector RS + rotation detection), "
    "'concentration' (asset class + sector concentration warnings), "
    "'breadth' (advance/decline, new H/L, McClellan proxy), "
    "'iv_regime' (IV rank dual logic: premium selling vs buying), "
    "'dynamic_limits' (position limits based on regime + confidence), "
    "'gap_analysis' (alpha vs portfolio sector gaps), "
    "'opportunity_cost' (missed alpha vs actual returns tracking), "
    "'cash_drag' (cash utilization + deployment suggestions). "
    "V2 Actions: 'macro_regime' (monetary/credit/economic regime from FRED cache), "
    "'social_sentiment' (Reddit + Finnhub social sentiment aggregation), "
    "'news_sentiment' (news headline NLP sentiment with VADER scoring). "
    "Cache Action: 'sa_snapshot' (one-shot read of all cached SA data, zero API calls). "
    "Macro data uses 12h FRED cache — no API overhead on hourly calls. "
    "Social + news are real-time every call."
),
 "parameters": {
 "type": "object",
 "properties": {
 "action": {
 "type": "string",
"enum": [
    "sector_rotation",
    "concentration",
    "breadth",
    "iv_regime",
    "dynamic_limits",
    "gap_analysis",
    "opportunity_cost",
    "cash_drag",
    "macro_regime",
    "social_sentiment",
    "news_sentiment",
    "sa_snapshot",
],
 "description": (
 "'sector_rotation': Sector relative strength + rotation detection (P0). "
 "'concentration': Asset class + sector concentration analysis with warnings (P0). "
 "'breadth': Market breadth — advance/decline, new highs/lows, McClellan proxy (P0). "
 "'iv_regime': IV rank dual logic — premium selling vs buying regime (P0). "
 "'dynamic_limits': Dynamic position limits based on regime + conviction (P1). "
 "'gap_analysis': Alpha watchlist vs portfolio sector gap analysis (P1). "
 "'opportunity_cost': Track missed alpha returns vs actual portfolio returns (P1). "
 "'cash_drag': Cash utilization analysis + deployment suggestions (P1). "
 "'macro_regime': Macro regime from FRED cache — monetary/credit/economic fusion (V2). "
 "'social_sentiment': Reddit + Finnhub social sentiment aggregation (V2). "
 "'news_sentiment': News headline NLP sentiment with VADER scoring (V2). "
    "'sa_snapshot': One-shot read of all cached SA action results, zero API calls — for Brain Trader (Cache)."
 ),
 "default": "sector_rotation",
 },
 "symbol": {
 "type": "string",
 "description": "Ticker symbol for IV analysis (used by iv_regime action). Default: SPY.",
 "default": "SPY",
 },
 "tickers": {
 "type": "string",
 "description": "Comma-separated ticker list for sentiment actions. Default: major watchlist.",
 "default": "",
 },
 },
 "required": ["action"],
 },
 },
 handler=_handle_quant_situational_awareness,
 check_fn=_check_yfinance,
 requires_env=[],
 is_async=False,
description=(
    "12 situational awareness features: 8 microstructure (sector rotation, concentration, breadth, "
    "IV regime, dynamic limits, gap analysis, opportunity cost, cash drag) + 3 V2 macro/sentiment "
    "(macro regime from FRED, social sentiment from Reddit/Finnhub, news sentiment with NLP) + "
    "1 cache snapshot (sa_snapshot for zero-API-cost Brain Trader reads). "
    "All action results auto-cached to sa_cache dir. Macro data cached 12h — zero overhead on hourly calls."
),
 emoji="🧭",
)
