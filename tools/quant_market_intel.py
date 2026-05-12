"""
quant_market_intel — Market intelligence tool for LLM-driven trading decisions.

Provides news, cross-asset correlation, fear/greed index, macro indicators,
earnings calendar, and sentiment analysis. This is the "eyes" of the LLM brain.

All data from free APIs (no API keys required):
- yfinance: stock news, cross-asset data, earnings, options IV
- Alternative.me: Crypto Fear & Greed Index
- VaderSentiment: News headline sentiment scoring

Actions:
  news         — Fetch recent news for a symbol (yfinance + sentiment scoring)
  macro        — Cross-asset snapshot (DXY, Gold, VIX, SPX, BTC, yields)
  fear_greed   — Crypto Fear & Greed Index
  earnings     — Upcoming earnings for a symbol
  sentiment    — Aggregate sentiment for a symbol (news + VADER)
  volatility   — Implied volatility surface for a symbol's options
  regime       — Market regime assessment (trending/ranging/volatile + cross-asset context)
"""

import json
import math
import os
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tools.registry import registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data fetching helpers
# ---------------------------------------------------------------------------

def _fetch_news(symbol, limit=10):
    """Fetch news for a symbol via yfinance, with sentiment scoring."""
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        raw_news = tk.news or []
    except Exception as e:
        logger.warning("yfinance news failed for %s: %s", symbol, e)
        return []

    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        analyzer = SentimentIntensityAnalyzer()
        has_vader = True
    except ImportError:
        has_vader = False

    articles = []
    for item in raw_news[:limit]:
        content = item.get("content", {})
        title = content.get("title", "")
        summary = content.get("summary", "") or content.get("description", "")
        pub_date = content.get("pubDate", "")
        source = content.get("provider", {})
        source_name = source.get("displayName", "") if isinstance(source, dict) else ""

        # VADER sentiment on title + summary
        text = f"{title}. {summary}".strip(". ")
        if has_vader and text:
            scores = analyzer.polarity_scores(text)
            compound = scores["compound"]
            label = "positive" if compound >= 0.05 else "negative" if compound <= -0.05 else "neutral"
        else:
            compound = 0.0
            label = "neutral"

        articles.append({
            "title": title[:200],
            "summary": (summary or "")[:300],
            "date": pub_date[:10] if pub_date else "",
            "source": source_name,
            "sentiment_score": round(compound, 4),
            "sentiment_label": label,
        })

    return articles


def _fetch_macro_snapshot():
    """Cross-asset macro snapshot: DXY, Gold, VIX, S&P500, BTC, 10Y yield."""
    try:
        import yfinance as yf
    except ImportError:
        return {"error": "yfinance not available"}

    # Symbol -> (display_name, category)
    macro_symbols = {
        "DX-Y.NYB": ("DXY", "usd"),
        "GC=F": ("Gold", "commodity"),
        "^VIX": ("VIX", "volatility"),
        "^GSPC": ("S&P 500", "equity"),
        "BTC-USD": ("BTC", "crypto"),
        "^TNX": ("10Y Yield", "rates"),
        "TLT": ("TLT (20Y)", "bonds"),
        "HYG": ("HYG (High Yield)", "credit"),
    }

    snapshot = {}
    for sym, (name, category) in macro_symbols.items():
        try:
            tk = yf.Ticker(sym)
            hist = tk.history(period="5d")
            if hist.empty:
                continue
            last = hist["Close"].iloc[-1]
            prev = hist["Close"].iloc[-2] if len(hist) > 1 else last
            chg_pct = (last - prev) / prev * 100 if prev else 0

            # 5-day trend
            if len(hist) >= 3:
                first = hist["Close"].iloc[0]
                trend_5d = (last - first) / first * 100
            else:
                trend_5d = chg_pct

            snapshot[name] = {
                "symbol": sym,
                "category": category,
                "price": round(last, 4),
                "daily_chg_pct": round(chg_pct, 2),
                "trend_5d_pct": round(trend_5d, 2),
            }
        except Exception:
            continue

    # Derive cross-asset signals
    signals = []
    dxy = snapshot.get("DXY", {}).get("daily_chg_pct", 0)
    vix = snapshot.get("VIX", {}).get("price", 0)
    gold = snapshot.get("Gold", {}).get("daily_chg_pct", 0)
    spx = snapshot.get("S&P 500", {}).get("daily_chg_pct", 0)
    btc = snapshot.get("BTC", {}).get("daily_chg_pct", 0)
    yield_10y = snapshot.get("10Y Yield", {}).get("price", 0)

    if dxy > 0.5:
        signals.append("USD_strength: DXY rising → pressure on commodities/EM/BTC")
    elif dxy < -0.5:
        signals.append("USD_weakness: DXY falling → tailwind for risk assets")

    if vix > 25:
        signals.append("FEAR: VIX>25 → elevated fear, potential buying opportunity")
    elif vix < 14:
        signals.append("COMPLACENCY: VIX<14 → low fear, potential correction risk")

    if gold > 1 and spx < -0.5:
        signals.append("RISK_OFF: Gold up + SPX down → risk-off rotation")

    if yield_10y > 4.5:
        signals.append("HIGH_YIELDS: 10Y>4.5% → pressure on growth/tech valuations")

    if abs(spx - btc) > 3 and abs(btc) > 2:
        if btc < spx - 3:
            signals.append("BTC_UNDERPERFORM: BTC significantly lagging SPX")
        elif btc > spx + 3:
            signals.append("BTC_OUTPERFORM: BTC significantly leading SPX")

    return {
        "assets": snapshot,
        "cross_asset_signals": signals,
        "timestamp": datetime.now(timezone.utc).isoformat()[:19],
    }


def _fetch_fear_greed():
    """Crypto Fear & Greed Index from Alternative.me (free, no key)."""
    try:
        import requests
        r = requests.get("https://api.alternative.me/fng/?limit=7", timeout=10)
        data = r.json().get("data", [])
        if not data:
            return {"error": "No data returned"}

        current = data[0]
        history = []
        for d in data[1:]:
            history.append({
                "date": d.get("timestamp", "")[:10],
                "value": int(d["value"]),
                "label": d.get("value_classification", ""),
            })

        # Trend analysis
        values = [int(d["value"]) for d in data]
        trend = "rising" if values[0] > values[-1] else "falling" if values[0] < values[-1] else "stable"
        extreme = values[0] > 75 or values[0] < 25

        return {
            "current_value": int(current["value"]),
            "current_label": current.get("value_classification", ""),
            "trend_7d": trend,
            "extreme_zone": extreme,
            "interpretation": (
                "Contrarian BUY signal — extreme fear" if values[0] < 25
                else "Contrarian SELL signal — extreme greed" if values[0] > 75
                else "Neutral zone — no contrarian signal"
            ),
            "history_7d": history,
        }
    except Exception as e:
        return {"error": f"Fear & Greed fetch failed: {e}"}


def _fetch_earnings(symbol):
    """Upcoming earnings for a symbol."""
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        cal = tk.calendar
        if cal is None or cal.empty:
            return {"symbol": symbol, "earnings": "no upcoming earnings data"}
        
        # Convert to dict
        result = {"symbol": symbol}
        if hasattr(cal, 'to_dict'):
            result["calendar"] = cal.to_dict()
        else:
            result["calendar"] = str(cal)
        return result
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


def _fetch_vol_surface(symbol):
    """Implied volatility overview for a symbol's options."""
    try:
        import yfinance as yf
        tk = yf.Ticker(symbol)
        expiries = tk.options
        if not expiries:
            return {"symbol": symbol, "error": "No options available"}
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}

    # Sample nearest 3 expiries
    surface = []
    for exp in expiries[:3]:
        try:
            chain = tk.option_chain(exp)
            calls = chain.calls
            puts = chain.puts

            # ATM IV (closest to current price)
            spot = tk.fast_info.get("lastPrice", 0) if hasattr(tk, 'fast_info') else 0
            if spot == 0:
                # Fallback: use midpoint of strikes
                spot = (calls["strike"].min() + calls["strike"].max()) / 2

            atm_calls = calls.iloc[(calls["strike"] - spot).abs().argsort()[:1]]
            atm_puts = puts.iloc[(puts["strike"] - spot).abs().argsort()[:1]]

            atm_iv_call = float(atm_calls["impliedVolatility"].iloc[0]) if not atm_calls.empty else 0
            atm_iv_put = float(atm_puts["impliedVolatility"].iloc[0]) if not atm_puts.empty else 0
            avg_iv = (atm_iv_call + atm_iv_put) / 2

            # IV rank proxy: compare current IV to high/low of this chain
            all_ivs = list(calls["impliedVolatility"]) + list(puts["impliedVolatility"])
            iv_min = min(all_ivs) if all_ivs else 0
            iv_max = max(all_ivs) if all_ivs else 0
            iv_rank = (avg_iv - iv_min) / max(iv_max - iv_min, 0.001) * 100 if all_ivs else 50

            # Total volume and OI
            total_vol = int(calls["volume"].sum()) + int(puts["volume"].sum())
            total_oi = int(calls["openInterest"].sum()) + int(puts["openInterest"].sum())
            put_call_ratio = puts["openInterest"].sum() / max(calls["openInterest"].sum(), 1)

            # Skew: 25-delta IV vs ATM IV (approximate with strike distance)
            otm_puts = puts[puts["strike"] < spot * 0.95]
            otm_calls = calls[calls["strike"] > spot * 1.05]
            avg_otm_put_iv = float(otm_puts["impliedVolatility"].mean()) if not otm_puts.empty else avg_iv
            avg_otm_call_iv = float(otm_calls["impliedVolatility"].mean()) if not otm_calls.empty else avg_iv
            skew = avg_otm_put_iv - avg_otm_call_iv  # positive = put skew (fear)

            surface.append({
                "expiry": exp,
                "atm_iv": round(avg_iv, 4),
                "iv_rank_proxy": round(iv_rank, 1),
                "total_volume": total_vol,
                "total_oi": total_oi,
                "put_call_ratio": round(put_call_ratio, 2),
                "skew": round(skew, 4),
                "skew_interpretation": (
                    "protective_bid (fear)" if skew > 0.05
                    else "call demand (greed)" if skew < -0.05
                    else "balanced"
                ),
            })
        except Exception as e:
            surface.append({"expiry": exp, "error": str(e)})

    # Overall IV assessment
    if surface:
        current_iv = surface[0].get("atm_iv", 0)
        iv_level = (
            "VERY_HIGH" if current_iv > 0.8
            else "HIGH" if current_iv > 0.5
            else "ELEVATED" if current_iv > 0.3
            else "NORMAL" if current_iv > 0.15
            else "LOW"
        )
        strategy_hint = (
            "SELL premium (covered calls, credit spreads)" if current_iv > 0.4
            else "BUY premium (directional plays, protective puts)" if current_iv < 0.15
            else "Neutral — either buy or sell premium"
        )
    else:
        current_iv = 0
        iv_level = "UNKNOWN"
        strategy_hint = "Insufficient data"

    return {
        "symbol": symbol,
        "current_atm_iv": round(current_iv, 4),
        "iv_level": iv_level,
        "strategy_hint": strategy_hint,
        "expiries_analyzed": len(surface),
        "surface": surface,
        "total_expiries_available": len(expiries),
    }


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _action_news(args):
    """Fetch news with sentiment analysis for a symbol."""
    symbol = args.get("symbol", "AAPL")
    limit = int(args.get("limit", 10))
    articles = _fetch_news(symbol, limit)

    if not articles:
        return json.dumps({"status": "ok", "symbol": symbol, "articles": [], "summary": "No news found"})

    # Aggregate sentiment
    scores = [a["sentiment_score"] for a in articles]
    avg_score = sum(scores) / len(scores)
    positive = sum(1 for s in scores if s > 0.05)
    negative = sum(1 for s in scores if s < -0.05)
    neutral = len(scores) - positive - negative

    return json.dumps({
        "status": "ok",
        "action": "news",
        "symbol": symbol,
        "article_count": len(articles),
        "articles": articles,
        "sentiment_summary": {
            "average_score": round(avg_score, 4),
            "label": "positive" if avg_score > 0.05 else "negative" if avg_score < -0.05 else "neutral",
            "positive_count": positive,
            "negative_count": negative,
            "neutral_count": neutral,
        },
    })


def _action_macro(args):
    """Cross-asset macro snapshot with signal interpretation."""
    snapshot = _fetch_macro_snapshot()
    return json.dumps({"status": "ok", "action": "macro", **snapshot})


def _action_fear_greed(args):
    """Crypto Fear & Greed Index with interpretation."""
    result = _fetch_fear_greed()
    return json.dumps({"status": "ok", "action": "fear_greed", **result})


def _action_earnings(args):
    """Upcoming earnings calendar for a symbol."""
    symbol = args.get("symbol", "AAPL")
    result = _fetch_earnings(symbol)
    return json.dumps({"status": "ok", "action": "earnings", **result})


def _action_sentiment(args):
    """Aggregate sentiment analysis combining news + VADER + market signals."""
    symbol = args.get("symbol", "AAPL")

    # News sentiment
    articles = _fetch_news(symbol, 10)
    news_scores = [a["sentiment_score"] for a in articles]
    avg_news = sum(news_scores) / len(news_scores) if news_scores else 0

    # Market signals from macro
    macro = _fetch_macro_snapshot()
    cross_signals = macro.get("cross_asset_signals", [])

    # Fear & Greed
    fg = _fetch_fear_greed()
    fg_value = fg.get("current_value", 50)
    fg_label = fg.get("current_label", "Neutral")

    # Combine signals
    # News sentiment: -1 to 1 → weight 0.4
    # Fear & Greed: 0-100, centered → weight 0.3
    # Cross-asset signals: count directional signals → weight 0.3
    fg_normalized = (fg_value - 50) / 50  # -1 to 1
    bullish_cross = sum(1 for s in cross_signals if any(w in s for w in ["tailwind", "BUY", "OUTPERFORM"]))
    bearish_cross = sum(1 for s in cross_signals if any(w in s for w in ["pressure", "FEAR", "RISK_OFF", "UNDERPERFORM", "COMPLACENCY", "HIGH_YIELDS"]))
    cross_score = (bullish_cross - bearish_cross) / max(bullish_cross + bearish_cross, 1)

    composite = avg_news * 0.4 + fg_normalized * 0.3 + cross_score * 0.3
    composite_label = "bullish" if composite > 0.15 else "bearish" if composite < -0.15 else "neutral"

    return json.dumps({
        "status": "ok",
        "action": "sentiment",
        "symbol": symbol,
        "composite_score": round(composite, 4),
        "composite_label": composite_label,
        "components": {
            "news_sentiment": {"score": round(avg_news, 4), "article_count": len(articles)},
            "fear_greed": {"value": fg_value, "label": fg_label, "normalized": round(fg_normalized, 4)},
            "cross_asset": {"bullish_signals": bullish_cross, "bearish_signals": bearish_cross, "score": round(cross_score, 4)},
        },
        "key_signals": cross_signals[:5],
        "interpretation": (
            f"Overall {composite_label} outlook for {symbol}. "
            f"News sentiment is {('positive' if avg_news > 0.05 else 'negative' if avg_news < -0.05 else 'neutral')}. "
            f"Market fear/greed is {fg_label.lower()}. "
            f"Cross-asset signals: {bullish_cross} bullish, {bearish_cross} bearish."
        ),
    })


def _action_volatility(args):
    """Implied volatility surface overview for a symbol."""
    symbol = args.get("symbol", "SPY")
    result = _fetch_vol_surface(symbol)
    return json.dumps({"status": "ok", "action": "volatility", **result})


def _action_regime(args):
    """Comprehensive market regime assessment combining technical + macro + sentiment."""
    symbol = args.get("symbol", "BTC/USDT")
    asset_class = args.get("asset_class", "crypto")

    # 1. Technical regime — use yfinance for stocks/fx, quant_indicators for crypto
    tech_regime = "neutral"
    tech_adx = 0
    tech_atr_pct = 0
    if asset_class in ("stock", "fx"):
        # Direct yfinance approach — don't go through quant_indicators (which uses CCXT)
        try:
            import yfinance as yf
            tk = yf.Ticker(symbol)
            hist = tk.history(period="60d")
            if len(hist) > 20:
                closes = hist["Close"].tolist()
                deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
                up_days = sum(1 for d in deltas[-14:] if d > 0)
                down_days = sum(1 for d in deltas[-14:] if d < 0)
                direction = abs(up_days - down_days) / 14
                recent = closes[-20:]
                daily_ranges = [abs(recent[i] - recent[i-1]) / recent[i-1] for i in range(1, len(recent))]
                avg_range = sum(daily_ranges) / len(daily_ranges) if daily_ranges else 0
                tech_adx = round(direction * 100, 2)
                tech_atr_pct = round(avg_range * 100 * 252, 2)
                if direction > 0.5:
                    tech_regime = "trending"
                elif direction < 0.2:
                    tech_regime = "ranging"
                else:
                    tech_regime = "neutral"
        except Exception:
            pass
    else:
        # Crypto: use quant_indicators with CCXT
        try:
            from tools.quant_indicators import quant_indicators_handler
            result = quant_indicators_handler({"action": "regime", "symbol": symbol, "asset_class": asset_class})
            if isinstance(result, str):
                result = json.loads(result)
            regime_data = result.get("regime", {})
            tech_regime = regime_data.get("regime", "neutral")
            tech_adx = regime_data.get("adx", 0)
            tech_atr_pct = regime_data.get("atr_percentile", 0)
        except Exception:
            pass

    # 2. Macro regime
    macro = _fetch_macro_snapshot()
    cross_signals = macro.get("cross_asset_signals", [])
    vix = macro.get("assets", {}).get("VIX", {}).get("price", 0)
    dxy = macro.get("assets", {}).get("DXY", {}).get("price", 0)

    # Macro regime classification
    if vix > 30:
        macro_regime = "crisis"
    elif vix > 20:
        macro_regime = "stressed"
    elif vix < 12:
        macro_regime = "complacent"
    else:
        macro_regime = "normal"

    # 3. Sentiment regime
    fg = _fetch_fear_greed()
    fg_value = fg.get("current_value", 50)
    if fg_value < 20:
        sentiment_regime = "extreme_fear"
    elif fg_value < 35:
        sentiment_regime = "fear"
    elif fg_value > 80:
        sentiment_regime = "extreme_greed"
    elif fg_value > 65:
        sentiment_regime = "greed"
    else:
        sentiment_regime = "neutral"

    # 4. Composite regime — this is what the LLM "brain" will use
    # Priority: crisis > stressed > technical > sentiment > normal
    if macro_regime == "crisis":
        composite = "crisis"
        confidence_mult = 0.2
        risk_level = "EXTREME"
    elif macro_regime == "stressed":
        composite = "stressed"
        confidence_mult = 0.4
        risk_level = "HIGH"
    elif tech_regime == "trending" and tech_atr_pct > 80:
        composite = "volatile_trend"
        confidence_mult = 0.6
        risk_level = "ELEVATED"
    elif tech_regime == "trending":
        composite = "trending"
        confidence_mult = 0.9
        risk_level = "MODERATE"
    elif tech_regime == "ranging":
        composite = "ranging"
        confidence_mult = 0.5
        risk_level = "LOW"
    elif sentiment_regime in ("extreme_fear", "extreme_greed"):
        composite = f"contrarian_{sentiment_regime}"
        confidence_mult = 0.7
        risk_level = "MODERATE"
    else:
        composite = "neutral"
        confidence_mult = 0.7
        risk_level = "MODERATE"

    # 5. Strategy recommendations based on composite regime
    strategy_map = {
        "crisis": "Risk-off: protective puts, reduce exposure, raise cash. No new speculative positions.",
        "stressed": "Defensive: tighten stops, hedge with puts, small position sizes only.",
        "volatile_trend": "Cautious trend-following: half positions, use options for leverage instead of margin.",
        "trending": "Active trend-following: full positions, momentum strategies, ride the trend.",
        "ranging": "Mean-reversion: sell rips/buy dips, covered calls for income, iron condors.",
        "contrarian_extreme_fear": "Contrarian BUY: extreme fear often marks bottoms. Scale in slowly.",
        "contrarian_extreme_greed": "Contrarian SELL: extreme greed often marks tops. Take profits, hedge.",
        "neutral": "Balanced: moderate positions, mixed strategies, no strong directional bias.",
    }

    return json.dumps({
        "status": "ok",
        "action": "regime",
        "composite_regime": composite,
        "confidence_multiplier": confidence_mult,
        "risk_level": risk_level,
        "strategy_recommendation": strategy_map.get(composite, "Observe and wait."),
        "components": {
            "technical": {"regime": tech_regime, "adx": round(tech_adx, 2), "atr_pct": round(tech_atr_pct, 2)},
            "macro": {"regime": macro_regime, "vix": vix, "dxy": dxy},
            "sentiment": {"regime": sentiment_regime, "fear_greed_value": fg_value},
        },
        "cross_asset_signals": cross_signals,
        "timestamp": datetime.now(timezone.utc).isoformat()[:19],
    })


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _action_alpha_watchlist(args: dict) -> str:
    """Read latest Alpha Stock Finder V4 report and extract trade candidates.
    
    This bridges the Alpha scanner (hourly cron) with the Strategy Brain,
    providing a rotating watchlist based on multi-dimensional fusion scores.
    """
    import glob
    from pathlib import Path
    
    report_dir = Path(os.path.expanduser("~/.hermes/cron/alpha-stock-finder/reports"))
    pattern = str(report_dir / "alpha_scan_v4_*.json")
    report_files = sorted(glob.glob(pattern))
    
    if not report_files:
        return json.dumps({
            "status": "error",
            "error": "No Alpha V4 reports found. Run alpha_scanner_v4.py first.",
            "watchlist": [],
        })
    
    latest_report = report_files[-1]
    
    try:
        with open(latest_report, "r") as f:
            data = json.load(f)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": f"Failed to parse report {latest_report}: {e}",
            "watchlist": [],
        })
    
    # Extract metadata
    timestamp = data.get("timestamp", "unknown")
    market_state = data.get("market_state", "unknown")
    market_state_cn = data.get("market_state_cn", "")
    crash_warning = data.get("crash_warning", {})
    crash_score = crash_warning.get("composite_score", 0)
    crash_level = crash_warning.get("warning_level", "NORMAL")
    
    # Extract top picks — these are the trade candidates
    top_picks = data.get("top_picks", [])
    all_candidates = data.get("all_candidates", [])
    
    # Categorize picks by trading strategy suitability
    premium_selling = []   # High IV rank → sell premium
    premium_buying = []    # Low IV rank → buy premium  
    momentum_long = []     # Strong tech + social alignment → momentum buy
    momentum_short = []    # Weak tech, high social hype → potential short/put
    divergence_plays = []  # High tech-social divergence → contrarian
    
    for c in all_candidates:
        ticker = c.get("ticker", "")
        fused = c.get("fused_score", 0) or 0
        tech = c.get("technical_score", 0) or 0
        social = c.get("social_signal", 0) or 0
        tv_score = c.get("tv_score", 0) or 0
        tv_consensus = c.get("tv_consensus", "neutral")
        insider_signal = c.get("insider_signal", "unavailable")
        divergence = c.get("divergence", 0) or 0
        wsb_mentions = c.get("wsb_mentions", 0) or 0
        mention_spike = c.get("mention_spike_ratio", 0) or 0
        sentiment = c.get("wsb_sentiment", 0) or 0
        cp_signal = c.get("cp_signal", "neutral")
        social_conviction = c.get("social_conviction", "")
        
        entry = {
            "ticker": ticker,
            "fused_score": round(fused, 1),
            "technical_score": round(tech, 1),
            "social_signal": round(social, 1),
            "tv_score": round(tv_score, 1),
            "tv_consensus": tv_consensus,
            "insider_signal": insider_signal,
            "divergence": round(divergence, 1),
            "wsb_mentions": wsb_mentions,
            "mention_spike_ratio": round(mention_spike, 1),
            "wsb_sentiment": round(sentiment, 2),
            "cp_signal": cp_signal,
            "social_conviction": social_conviction,
        }
        
        # Classify
        if divergence >= 35 and social > tech:
            # Social hype >> tech reality → potential overvalued
            momentum_short.append(entry)
        elif tech >= 60 and social >= 40 and divergence < 25:
            # Aligned bullish signal
            momentum_long.append(entry)
        elif tech >= 50 and social < 30:
            # Low social attention but strong tech → buy premium (cheap options)
            premium_buying.append(entry)
        elif social >= 50 and tv_consensus in ("buy", "strong_buy"):
            # High attention + fundamental backing → sell premium (expensive options)
            premium_selling.append(entry)
        elif divergence >= 25:
            divergence_plays.append(entry)
    
    # Sort each category
    momentum_long.sort(key=lambda x: x["fused_score"], reverse=True)
    momentum_short.sort(key=lambda x: x["divergence"], reverse=True)
    premium_selling.sort(key=lambda x: x["social_signal"], reverse=True)
    premium_buying.sort(key=lambda x: x["technical_score"], reverse=True)
    divergence_plays.sort(key=lambda x: x["divergence"], reverse=True)
    
    # Top picks summary (for quick reference)
    top_picks_summary = []
    for p in top_picks[:12]:
        top_picks_summary.append({
            "ticker": p.get("ticker", ""),
            "fused_score": round(p.get("fused_score", 0) or 0, 1),
            "technical_score": round(p.get("technical_score", 0) or 0, 1),
            "social_signal": round(p.get("social_signal", 0) or 0, 1),
            "tv_score": round(p.get("tv_score", 0) or 0, 1),
            "tv_consensus": p.get("tv_consensus", "neutral"),
            "insider_signal": p.get("insider_signal", "unavailable"),
            "divergence": round(p.get("divergence", 0) or 0, 1),
            "wsb_mentions": p.get("wsb_mentions", 0) or 0,
            "mention_spike_ratio": round(p.get("mention_spike_ratio", 0) or 0, 1),
        })
    
    return json.dumps({
        "status": "ok",
        "report_file": os.path.basename(latest_report),
        "report_timestamp": timestamp,
        "market_state": market_state,
        "market_state_cn": market_state_cn,
        "crash_warning_score": crash_score,
        "crash_warning_level": crash_level,
        "top_picks": top_picks_summary[:12],
        "trade_candidates": {
            "momentum_long": momentum_long[:5],
            "momentum_short": momentum_short[:5],
            "premium_selling": premium_selling[:5],
            "premium_buying": premium_buying[:5],
            "divergence_plays": divergence_plays[:5],
        },
        "strategy_hints": {
            "momentum_long": "Buy stocks or ATM calls — aligned tech+social bullish",
            "momentum_short": "Buy puts or bear spreads — social hype >> tech reality",
            "premium_selling": "Sell covered calls / credit spreads — high IV from attention",
            "premium_buying": "Buy debit spreads — low IV, strong tech, under-hyped",
            "divergence_plays": "Contrarian entry — tech/social disagreement = opportunity",
        },
    })


def _action_options_top12(args: dict) -> str:
    """Options trading opportunity scanner — top 12 picks with strategy recommendations.

    Reads Alpha Scanner V4 report + SA cache to generate options-specific
    trade ideas with strategy, DTE guidance, and risk levels.
    """
    import glob

    # --- 1. Read latest Alpha Scanner V4 report ---
    report_dir = Path(os.path.expanduser("~/.hermes/cron/alpha-stock-finder/reports"))
    pattern = str(report_dir / "alpha_scan_v4_*.json")
    report_files = sorted(glob.glob(pattern))

    if not report_files:
        return json.dumps({
            "status": "error",
            "error": "No Alpha V4 reports found. Run alpha_scanner_v4.py first.",
            "opportunities": [],
        })

    latest_report = report_files[-1]
    try:
        with open(latest_report, "r") as f:
            data = json.load(f)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": f"Failed to parse report {latest_report}: {e}",
            "opportunities": [],
        })

    all_candidates = data.get("all_candidates", [])
    report_timestamp = data.get("timestamp", "unknown")
    market_state = data.get("market_state", "unknown")

    # --- 2. Read SA cache for regime context ---
    sa_cache_dir = os.path.expanduser("~/.hermes/quant_trading/sa_cache")

    def _read_sa_action(action_name: str) -> dict:
        """Read a single SA cache action file."""
        cache_path = os.path.join(sa_cache_dir, f"{action_name}.json")
        if not os.path.exists(cache_path):
            return {}
        try:
            with open(cache_path, "r") as f:
                entry = json.load(f)
            return entry.get("data", {})
        except Exception:
            return {}

    macro_regime_data = _read_sa_action("macro_regime")
    iv_regime_data = _read_sa_action("iv_regime")
    social_sentiment_data = _read_sa_action("social_sentiment")

    # Extract regime summaries for context
    macro_regime_label = "unknown"
    if macro_regime_data:
        # Try common field names from quant_situational_awareness macro_regime output
        macro_regime_label = (
            macro_regime_data.get("regime_label")
            or macro_regime_data.get("composite_regime")
            or macro_regime_data.get("regime", "unknown")
        )

    iv_regime_label = "unknown"
    iv_rank_hint = "neutral"
    if iv_regime_data:
        iv_regime_label = (
            iv_regime_data.get("regime_label")
            or iv_regime_data.get("iv_regime")
            or iv_regime_data.get("regime", "unknown")
        )
        # Derive a broad hint for premium buy/sell
        iv_rank_hint = (
            "sell_premium" if "high" in str(iv_regime_label).lower()
            else "buy_premium" if "low" in str(iv_regime_label).lower()
            else "neutral"
        )

    social_regime_label = "unknown"
    if social_sentiment_data:
        social_regime_label = (
            social_sentiment_data.get("regime_label")
            or social_sentiment_data.get("sentiment_regime")
            or social_sentiment_data.get("regime", "unknown")
        )

    regime_context = f"Macro: {macro_regime_label}, IV: {iv_regime_label}, Social: {social_regime_label}"

    # --- 3. Score each candidate for options suitability ---
    scored = []

    for c in all_candidates:
        ticker = c.get("ticker", "")
        price = c.get("price", 0) or 0
        volume = c.get("volume", 0) or 0
        market_cap = c.get("market_cap", 0) or 0
        volatility = c.get("volatility", 0) or 0  # IV proxy
        technical_score = c.get("technical_score", 0) or 0
        social_signal = c.get("social_signal", 0) or 0
        social_conviction = c.get("social_conviction", "neutral") or ""
        mention_spike_ratio = c.get("mention_spike_ratio", 0) or 0
        rsi = c.get("rsi", 50) or 50
        bb_position = c.get("bb_position", 0.5) or 0
        price_52w_position = c.get("price_52w_position", 50) or 50
        sector = c.get("sector", "Unknown")
        fused_score = c.get("fused_score", 0) or 0
        divergence = c.get("divergence", 0) or 0
        signals = c.get("signals", [])

        # --- Liquidity filter ---
        if volume < 1_000_000 or market_cap < 5_000_000_000:
            continue

        # --- 3a. IV rank potential scoring ---
        # volatility field is annualized % (e.g., 41.4 = 41.4%)
        iv_score = 0
        if 40 <= volatility <= 70:
            # Sweet spot for premium selling
            iv_score = 30
        elif volatility < 30:
            # Low IV — good for premium buying (straddles, debit)
            iv_score = 20
        elif 30 <= volatility < 40:
            iv_score = 15
        elif 70 < volatility <= 80:
            iv_score = 10
        elif volatility > 80:
            # Very high IV — premium buying for potential IV crush or straddles
            iv_score = 15

        # --- 3b. Technical signal quality ---
        tech_score_pts = 0
        if technical_score > 75:
            tech_score_pts = 25  # Strong directional bias for debit spreads
        elif technical_score > 60:
            tech_score_pts = 18
        elif 40 <= technical_score <= 60:
            tech_score_pts = 12  # Neutral — good for iron condors
        elif technical_score < 40:
            tech_score_pts = 8  # Bearish directional for bear put spreads

        # --- 3c. Social conviction / volatility expansion ---
        social_score_pts = 0
        conviction_lower = social_conviction.lower() if social_conviction else ""
        if conviction_lower in ("strong_bullish", "strong_bearish"):
            social_score_pts = 20
            if mention_spike_ratio > 2.0:
                social_score_pts += 5  # Volatility expansion ahead
        elif conviction_lower in ("bullish", "bearish"):
            social_score_pts = 12
            if mention_spike_ratio > 2.0:
                social_score_pts += 3
        else:
            social_score_pts = 5

        # --- 3d. Regime alignment bonus ---
        regime_pts = 0
        if iv_rank_hint == "sell_premium" and 40 <= volatility <= 70:
            regime_pts += 5  # High IV regime aligns with premium selling
        elif iv_rank_hint == "buy_premium" and volatility < 40:
            regime_pts += 5  # Low IV regime aligns with premium buying
        elif iv_rank_hint == "neutral":
            regime_pts += 2
        if "risk_off" in str(macro_regime_label).lower() or "crisis" in str(macro_regime_label).lower():
            # In risk-off, favor defensive strategies
            if technical_score < 50:
                regime_pts += 3
        elif "risk_on" in str(macro_regime_label).lower() or "trending" in str(macro_regime_label).lower():
            if technical_score > 65:
                regime_pts += 3

        # --- 3e. Fused score quality bonus ---
        fused_bonus = min(fused_score / 10, 10)  # 0-10 pts

        # --- Total options suitability score ---
        options_score = min(iv_score + tech_score_pts + social_score_pts + regime_pts + fused_bonus, 100)

        # --- 4. Classify into options strategy ---
        recommended_strategy = None
        strategy_rationale = ""
        suggested_dte = 30
        risk_level = "medium"

        # Priority-ordered classification

        # Straddle: mention_spike > 3.0, upcoming catalyst, IV < 40
        if mention_spike_ratio > 3.0 and volatility < 40:
            recommended_strategy = "straddle"
            strategy_rationale = (
                f"High social attention spike ({mention_spike_ratio:.1f}x) with low IV ({volatility:.0f}%) "
                f"suggests upcoming catalyst. Straddle profits from volatility expansion."
            )
            suggested_dte = 60
            risk_level = "high"
        # Bear put spread: technical_score < 40 OR strong_bearish, rsi extreme
        elif technical_score < 40 or (conviction_lower == "strong_bearish" and (rsi > 70 or rsi < 30)):
            recommended_strategy = "bear_put_spread"
            direction_reason = (
                f"weak technicals (score {technical_score:.0f})"
                if technical_score < 40
                else f"strong bearish conviction with RSI {rsi:.0f}"
            )
            strategy_rationale = (
                f"Bearish bias from {direction_reason}. "
                f"Bear put spread limits risk while capitalizing on downside."
            )
            suggested_dte = 21
            risk_level = "medium"
        # Bull call spread: technical_score > 75, strong_bullish, rsi 50-70
        elif technical_score > 75 and conviction_lower == "strong_bullish" and 50 <= rsi <= 70:
            recommended_strategy = "bull_call_spread"
            strategy_rationale = (
                f"Strong aligned bullish signal (tech {technical_score:.0f}, strong_bullish, RSI {rsi:.0f}). "
                f"Bull call spread captures upside with defined risk."
            )
            suggested_dte = 21
            risk_level = "medium"
        # Covered call: IV 40-70%, tech 50-70, not bearish, 52w position > 60
        elif 40 <= volatility <= 70 and 50 <= technical_score <= 70 and conviction_lower not in ("bearish", "strong_bearish") and price_52w_position > 60:
            recommended_strategy = "covered_call"
            strategy_rationale = (
                f"Moderate IV ({volatility:.0f}%) ideal for premium collection, stock near 52w high "
                f"({price_52w_position:.0f}%). Covered call generates income on existing position."
            )
            suggested_dte = 35
            risk_level = "low"
        # Cash secured put: IV 40-70%, tech > 60, social_signal > 40, near support
        elif 40 <= volatility <= 70 and technical_score > 60 and social_signal > 40 and price_52w_position < 70:
            recommended_strategy = "cash_secured_put"
            strategy_rationale = (
                f"Solid fundamentals (tech {technical_score:.0f}, social {social_signal:.0f}) with elevated IV "
                f"({volatility:.0f}%). Sell puts at support for income or assignment at discount."
            )
            suggested_dte = 35
            risk_level = "low"
        # Iron condor: IV > 50, tech 40-60, bb 0.3-0.7
        elif volatility > 50 and 40 <= technical_score <= 60 and 0.3 <= bb_position <= 0.7:
            recommended_strategy = "iron_condor"
            strategy_rationale = (
                f"Neutral setup: IV {volatility:.0f}% (sell premium), tech neutral ({technical_score:.0f}), "
                f"BB mid-range ({bb_position:.2f}). Iron condor profits from range-bound price action."
            )
            suggested_dte = 35
            risk_level = "low"
        # Credit spread: IV > 50, directional bias from tech/social alignment
        elif volatility > 50 and (technical_score > 65 or technical_score < 35 or abs(social_signal - technical_score) < 20):
            recommended_strategy = "credit_spread"
            bias = "bull put" if technical_score > 60 else "bear call"
            strategy_rationale = (
                f"High IV ({volatility:.0f}%) with directional bias → {bias} credit spread. "
                f"Sell premium with defined risk; profit if stock stays above/below short strike."
            )
            suggested_dte = 21
            risk_level = "medium"
        # Fallback: covered call for moderate names
        elif volatility >= 20 and technical_score >= 40:
            recommended_strategy = "covered_call"
            strategy_rationale = (
                f"Moderate IV ({volatility:.0f}%) and acceptable technicals ({technical_score:.0f}). "
                f"Conservative covered call for income generation."
            )
            suggested_dte = 35
            risk_level = "low"
        else:
            # Skip — not suitable for options
            continue

        # --- Confidence (0-100) ---
        confidence = min(
            options_score
            + (10 if divergence < 15 else 0)  # Aligned signals = more confidence
            - (15 if divergence > 40 else 0),  # High divergence = less confidence
            100
        )
        confidence = max(int(confidence), 10)

        scored.append({
            "ticker": ticker,
            "price": round(price, 2),
            "volatility": round(volatility, 1),
            "sector": sector,
            "recommended_strategy": recommended_strategy,
            "options_score": round(options_score, 1),
            "strategy_rationale": strategy_rationale,
            "suggested_dte": suggested_dte,
            "risk_level": risk_level,
            "regime_context": regime_context,
            "confidence": confidence,
        })

    # --- 5. Sort by options_score, take top 12 ---
    scored.sort(key=lambda x: x["options_score"], reverse=True)
    top12 = scored[:12]

    # --- 6. Build output ---
    # Strategy distribution summary
    strategy_counts = {}
    for item in top12:
        s = item["recommended_strategy"]
        strategy_counts[s] = strategy_counts.get(s, 0) + 1

    avg_score = round(sum(x["options_score"] for x in top12) / max(len(top12), 1), 1) if top12 else 0
    avg_confidence = round(sum(x["confidence"] for x in top12) / max(len(top12), 1), 1) if top12 else 0

    return json.dumps({
        "status": "ok",
        "action": "options_top12",
        "report_file": os.path.basename(latest_report),
        "report_timestamp": report_timestamp,
        "market_state": market_state,
        "regime_context": regime_context,
        "total_candidates": len(all_candidates),
        "qualified_candidates": len(scored),
        "avg_options_score": avg_score,
        "avg_confidence": avg_confidence,
        "strategy_distribution": strategy_counts,
        "opportunities": top12,
        "timestamp": datetime.now(timezone.utc).isoformat()[:19],
    })


_ACTION_HANDLERS = {
    "news": _action_news,
    "macro": _action_macro,
    "fear_greed": _action_fear_greed,
    "earnings": _action_earnings,
    "sentiment": _action_sentiment,
    "volatility": _action_volatility,
    "regime": _action_regime,
    "alpha_watchlist": _action_alpha_watchlist,
    "options_top12": _action_options_top12,
}


def _handle_quant_market_intel(args: dict, **kw) -> str:
    """Main entry point for quant_market_intel tool."""
    action = args.get("action", "regime")
    handler = _ACTION_HANDLERS.get(action)
    if not handler:
        return json.dumps({"status": "error", "error": f"Unknown action: {action}. Available: {list(_ACTION_HANDLERS.keys())}"})
    try:
        return handler(args)
    except Exception as e:
        logger.error("quant_market_intel error (action=%s): %s", action, e)
        return json.dumps({"status": "error", "error": str(e)})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

registry.register(
    name="quant_market_intel",
    toolset="quant",
    schema={
        "name": "quant_market_intel",
"description": (
"Market intelligence tool — the 'eyes' for LLM-driven trading decisions. "
"Fetches news with sentiment analysis, cross-asset macro data (DXY, Gold, VIX, S&P500, BTC, yields), "
"crypto Fear & Greed Index, earnings calendar, implied volatility surface, "
"composite market regime assessment, alpha watchlist from scanner, "
"and top 12 options trading opportunities with strategy recommendations. "
"Actions: 'news' (symbol news + VADER sentiment), 'macro' (cross-asset snapshot), "
"'fear_greed' (crypto fear/greed index), 'earnings' (upcoming earnings), "
"'sentiment' (composite sentiment combining news+macro+fear/greed), "
"'volatility' (IV surface + put/call ratio + skew), "
"'regime' (composite regime: trending/ranging/crisis/stressed/contrarian + strategy hints), "
"'alpha_watchlist' (Alpha Scanner V4 candidates categorized by equity strategy), "
"'options_top12' (top 12 options opportunities with strategy/DTE/risk from Alpha Scanner + SA cache). "
"All data from free APIs — no API keys required."
),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
"enum": ["news", "macro", "fear_greed", "earnings", "sentiment", "volatility", "regime", "alpha_watchlist", "options_top12"],
"description": (
"'news': fetch news + VADER sentiment for a symbol. "
"'macro': cross-asset macro snapshot (DXY, Gold, VIX, SPX, BTC, yields). "
"'fear_greed': crypto Fear & Greed Index with 7-day trend. "
"'earnings': upcoming earnings calendar. "
"'sentiment': composite sentiment (news + fear/greed + cross-asset signals). "
"'volatility': IV surface overview, put/call ratio, skew analysis. "
"'regime': comprehensive market regime assessment (technical + macro + sentiment → strategy hints). "
"'alpha_watchlist': read latest Alpha Stock Finder V4 report and extract categorized trade candidates "
"(momentum_long, momentum_short, premium_selling, premium_buying, divergence_plays). "
"'options_top12': top 12 options trading opportunities from Alpha Scanner + SA cache, with specific "
"strategy recommendations (covered_call, cash_secured_put, bull_call_spread, bear_put_spread, "
"iron_condor, straddle, credit_spread), DTE guidance, risk levels, and regime context."
),
                    "default": "regime",
                },
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol (e.g. AAPL, SPY, BTC/USDT). Used by news, earnings, sentiment, volatility actions.",
                    "default": "AAPL",
                },
                "asset_class": {
                    "type": "string",
                    "enum": ["crypto", "stock", "fx"],
                    "description": "Asset class for regime action (affects which indicators are used).",
                    "default": "crypto",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of news articles to fetch.",
                    "default": 10,
                },
            },
            "required": ["action"],
        },
    },
    handler=_handle_quant_market_intel,
    check_fn=lambda: True,  # Always available — no API keys needed
)
