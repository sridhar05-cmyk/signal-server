"""
Signal Research Panel - Flask Backend.
Strict Read-Only Observation & Research Web Interface for Binance Spot Bot.
Endpoints:
  - GET / : Serves single-page research dashboard.
  - GET /api/pairs : Lists symbols with backtest availability status.
  - GET /api/signal?symbol=X&timeframe=Y : Fetches live indicator readings, regime, and merges authentic backtest metrics.
⚠️ ZERO execution pathways: No buy/sell/order/place_order calls. No broker connections.
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
import pandas as pd
from flask import Flask, render_template, request, jsonify

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Path resolution
MODULE_DIR = Path(__file__).resolve().parent
DATA_DIR = MODULE_DIR / "data"
CACHE_FILE_PATH = DATA_DIR / "backtest_cache.json"

try:
    from . import config
    from . import data_feed
    from . import forex_data_feed
    from . import forex_utils
    from . import forex_sessions
    from . import indicators
    from . import regime_detector
    from . import strategy
    from .logger import get_logger
except ImportError:
    import config
    import data_feed
    import forex_data_feed
    import forex_utils
    import forex_sessions
    import indicators
    import regime_detector
    import strategy
    from logger import get_logger

logger = get_logger("web_app")

app = Flask(
    __name__,
    template_folder=str(MODULE_DIR / "templates"),
    static_folder=str(MODULE_DIR / "static"),
)

SUPPORTED_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"]
DEFAULT_SYMBOL = "EUR/USD"
DEFAULT_TIMEFRAME = "1h"
MIN_CONFIDENCE_TRADES = 30

CRYPTO_SYMBOLS = [
    {"symbol": "BTC/USDT", "name": "Bitcoin", "base": "BTC", "quote": "USDT"},
    {"symbol": "ETH/USDT", "name": "Ethereum", "base": "ETH", "quote": "USDT"},
    {"symbol": "SOL/USDT", "name": "Solana", "base": "SOL", "quote": "USDT"},
    {"symbol": "BNB/USDT", "name": "BNB", "base": "BNB", "quote": "USDT"},
    {"symbol": "XRP/USDT", "name": "XRP", "base": "XRP", "quote": "USDT"},
    {"symbol": "DOGE/USDT", "name": "Dogecoin", "base": "DOGE", "quote": "USDT"},
    {"symbol": "ADA/USDT", "name": "Cardano", "base": "ADA", "quote": "USDT"},
    {"symbol": "AVAX/USDT", "name": "Avalanche", "base": "AVAX", "quote": "USDT"},
]

FOREX_SYMBOLS = [
    {"symbol": "EUR/USD", "name": "Euro / US Dollar", "base": "EUR", "quote": "USD"},
    {"symbol": "GBP/USD", "name": "British Pound / US Dollar", "base": "GBP", "quote": "USD"},
    {"symbol": "USD/JPY", "name": "US Dollar / Japanese Yen", "base": "USD", "quote": "JPY"},
    {"symbol": "USD/CHF", "name": "US Dollar / Swiss Franc", "base": "USD", "quote": "CHF"},
    {"symbol": "AUD/USD", "name": "Australian Dollar / US Dollar", "base": "AUD", "quote": "USD"},
    {"symbol": "USD/CAD", "name": "US Dollar / Canadian Dollar", "base": "USD", "quote": "CAD"},
    {"symbol": "NZD/USD", "name": "New Zealand Dollar / US Dollar", "base": "NZD", "quote": "USD"},
    {"symbol": "GBP/JPY", "name": "British Pound / Japanese Yen", "base": "GBP", "quote": "JPY"},
    {"symbol": "AUD/CAD", "name": "Australian Dollar / Canadian Dollar", "base": "AUD", "quote": "CAD"},
    {"symbol": "NZD/CHF", "name": "New Zealand Dollar / Swiss Franc", "base": "NZD", "quote": "CHF"},
    {"symbol": "GBP/AUD", "name": "British Pound / Australian Dollar", "base": "GBP", "quote": "AUD"},
    {"symbol": "EUR/CAD", "name": "Euro / Canadian Dollar", "base": "EUR", "quote": "CAD"},
    {"symbol": "NZD/CAD", "name": "New Zealand Dollar / Canadian Dollar", "base": "NZD", "quote": "CAD"},
]


def load_backtest_cache() -> Dict[str, Any]:
    """Reads persistent backtest cache from disk."""
    if CACHE_FILE_PATH.exists():
        try:
            with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning(f"Failed to read {CACHE_FILE_PATH.name}: {exc}")
    return {}


def get_symbol_trade_stats(cache: Dict[str, Any], symbol: str) -> tuple[int, int]:
    """Returns (total_trades_all_timeframes, max_trades_single_timeframe) for a symbol."""
    sym_cache = cache.get(symbol, {})
    if not isinstance(sym_cache, dict):
        return 0, 0
    total_trades = 0
    max_tf_trades = 0
    for tf, regimes in sym_cache.items():
        if isinstance(regimes, dict):
            tf_trades = 0
            for reg, stats in regimes.items():
                if isinstance(stats, dict):
                    tf_trades += int(stats.get("trades", stats.get("total_trades", 0)))
            total_trades += tf_trades
            max_tf_trades = max(max_tf_trades, tf_trades)
    return total_trades, max_tf_trades


def format_price_val(val: float, is_forex: bool = False) -> float:
    """Formats price or price-derived indicator values with suitable decimal precision."""
    if val is None or pd.isna(val):
        return 0.0
    val_f = float(val)
    if is_forex:
        if val_f < 5:
            return round(val_f, 5)
        elif val_f < 50:
            return round(val_f, 4)
        elif val_f < 500:
            return round(val_f, 3)
        else:
            return round(val_f, 2)
    return round(val_f, 2) if val_f >= 1 else round(val_f, 4)


def compute_indicator_snapshot(df: pd.DataFrame, is_forex: bool = False) -> Dict[str, Any]:
    """Computes technical indicator readings on the latest candle slice."""
    curr_close = float(df["close"].iloc[-1])
    ema_9_val = float(indicators.ema(df, 9).iloc[-1])
    ema_21_val = float(indicators.ema(df, 21).iloc[-1])
    rsi_val = float(indicators.rsi(df, 14).iloc[-1])
    _, _, macd_hist = indicators.macd(df, 12, 26, 9)
    macd_hist_val = float(macd_hist.iloc[-1])
    bb_up, bb_mid, bb_low = indicators.bollinger_bands(df, 20, 2.0)
    bb_up_val = float(bb_up.iloc[-1])
    bb_mid_val = float(bb_mid.iloc[-1])
    bb_low_val = float(bb_low.iloc[-1])
    atr_series = indicators.atr(df, 14)
    atr_val = float(atr_series.iloc[-1])

    # Position relative to Bollinger Bands
    band_width = bb_up_val - bb_low_val
    if band_width > 0:
        pct_b = (curr_close - bb_low_val) / band_width
        pct_str = f"{pct_b * 100:.1f}%"
        if curr_close >= bb_up_val:
            bb_pos = f"Above Upper ({pct_str})"
        elif curr_close <= bb_low_val:
            bb_pos = f"Below Lower ({pct_str})"
        elif curr_close >= bb_mid_val:
            bb_pos = f"Upper Band ({pct_str})"
        else:
            bb_pos = f"Lower Band ({pct_str})"
    else:
        bb_pos = "Flat (0.0%)"

    return {
        "price": format_price_val(curr_close, is_forex),
        "ema9": format_price_val(ema_9_val, is_forex),
        "ema21": format_price_val(ema_21_val, is_forex),
        "rsi": round(rsi_val, 2),
        "macd_hist": round(macd_hist_val, 6 if is_forex else 4),
        "bb_up": format_price_val(bb_up_val, is_forex),
        "bb_mid": format_price_val(bb_mid_val, is_forex),
        "bb_low": format_price_val(bb_low_val, is_forex),
        "bb_pos": bb_pos,
        "atr": format_price_val(atr_val, is_forex),
    }


@app.route("/")
def index():
    """Renders the Live Forex Signal Dashboard."""
    return render_template("index.html")


@app.route("/research")
def research():
    """Renders the previous Signal Research Panel."""
    return render_template("research.html")



@app.route("/api/pairs", methods=["GET"])
def get_pairs():
    """
    Returns available trading pairs organized into two groups:
    - Crypto (Binance)
    - Forex (Live Market)

    Pairs with >= 30 backtested trades in at least one timeframe are marked enabled: true.
    Pairs with insufficient trades (< 30) or no cache are marked enabled: false.
    """
    cache = load_backtest_cache()

    def process_group(items: List[Dict[str, str]], is_forex_group: bool = False) -> List[Dict[str, Any]]:
        res = []
        for item in items:
            sym = item["symbol"]
            total_trades, max_tf_trades = get_symbol_trade_stats(cache, sym)
            is_enabled = max_tf_trades >= MIN_CONFIDENCE_TRADES

            if is_enabled:
                status_label = f"N={max_tf_trades} trades"
            elif total_trades > 0:
                status_label = f"N={total_trades} (< 30)"
            else:
                status_label = "Untested"

            res.append({
                "symbol": sym,
                "name": item["name"],
                "base": item["base"],
                "quote": item["quote"],
                "enabled": is_enabled,
                "status_label": status_label,
                "is_forex": is_forex_group,
                "max_trades": max_tf_trades,
                "total_trades": total_trades,
            })
        return res

    crypto_pairs = process_group(CRYPTO_SYMBOLS, is_forex_group=False)
    forex_pairs = process_group(FOREX_SYMBOLS, is_forex_group=True)

    return jsonify({
        "success": True,
        "default_symbol": DEFAULT_SYMBOL,
        "timeframes": SUPPORTED_TIMEFRAMES,
        "default_timeframe": DEFAULT_TIMEFRAME,
        "groups": [
            {
                "name": "Crypto (Binance)",
                "pairs": crypto_pairs,
            },
            {
                "name": "Forex (Live Market)",
                "pairs": forex_pairs,
            },
        ],
        "pairs": crypto_pairs + forex_pairs,
    })


@app.route("/api/signal", methods=["GET"])
def get_signal():
    """
    Evaluates current live candle reading for pair + timeframe.
    Merges live signal with empirical backtest stats from backtest_cache.json.
    Strict Invariant: If sample size < 30 trades for the exact pair+timeframe+regime,
    returns insufficient_data: true and suppresses win rate/profit factor.
    """
    symbol = request.args.get("symbol", DEFAULT_SYMBOL).strip().upper()
    timeframe = request.args.get("timeframe", DEFAULT_TIMEFRAME).strip().lower()

    if timeframe not in SUPPORTED_TIMEFRAMES:
        return jsonify({
            "success": False,
            "error": f"Unsupported timeframe '{timeframe}'. Allowed: {SUPPORTED_TIMEFRAMES}",
        }), 400

    try:
        is_forex = forex_data_feed.is_forex_symbol(symbol)

        # 1. Fetch live candles & macro HTF trend according to asset class
        df_1m_extra = None
        df_15m_extra = None
        df_1h_extra = None
        df_4h_extra = None

        if is_forex:
            logger.info(f"API request: Fetching live {timeframe} forex candles for {symbol} via Yahoo Finance...")
            df_live = forex_data_feed.fetch_forex_candles(pair=symbol, timeframe=timeframe, count=100)
            if df_live.empty or len(df_live) < 25:
                return jsonify({
                    "success": False,
                    "error": f"Insufficient live forex candle data returned for {symbol} ({timeframe}).",
                }), 502

            # Retrieve higher-timeframe context for multi-timeframe short-TF engine
            df_4h_extra = forex_data_feed.fetch_forex_candles(pair=symbol, timeframe="4h", count=50, use_cache=True)
            df_1h_extra = forex_data_feed.fetch_forex_candles(pair=symbol, timeframe="1h", count=100, use_cache=True)
            df_15m_extra = forex_data_feed.fetch_forex_candles(pair=symbol, timeframe="15m", count=100, use_cache=True)

            if timeframe == "1m":
                df_1m_extra = df_live
                # Fetch 5M for primary setup
                df_5m_live = forex_data_feed.fetch_forex_candles(pair=symbol, timeframe="5m", count=100, use_cache=True)
                df_setup = df_5m_live if not df_5m_live.empty else df_live
            elif timeframe == "5m":
                df_setup = df_live
                df_1m_extra = forex_data_feed.fetch_forex_candles(pair=symbol, timeframe="1m", count=50, use_cache=True)
            else:
                df_setup = df_live

            df_htf = forex_data_feed.fetch_higher_timeframe_forex_data(
                pair=symbol,
                timeframe="4h",
                limit=500,
                ema_period=200,
                use_cache=True,
            )
            df_merged = forex_data_feed.attach_higher_timeframe_trend(df_setup, df_htf)
            data_source_label = "Live market data — Yahoo Finance"
        else:
            logger.info(f"API request: Fetching live {timeframe} crypto candles for {symbol} via Binance...")
            df_live = data_feed.fetch_live_candles(symbol=symbol, timeframe=timeframe, count=100)
            if df_live.empty or len(df_live) < 35:
                return jsonify({
                    "success": False,
                    "error": f"Insufficient live candle data returned for {symbol} ({timeframe}).",
                }), 502

            df_htf = data_feed.fetch_higher_timeframe_data(
                symbol=symbol,
                timeframe="4h",
                limit=500,
                ema_period=200,
                use_cache=True,
            )
            df_merged = data_feed.attach_higher_timeframe_trend(df_live, df_htf)
            data_source_label = "Live market data — Binance"

        htf_trend_val = str(df_merged["htf_trend"].iloc[-1])

        # 2. Regime classification
        regime_res = regime_detector.detect_regime(df_merged, htf_trend=htf_trend_val)
        regime_name = regime_res.regime

        # 3. Signal evaluation
        decision = strategy.generate_signal(
            df=df_merged,
            htf_trend=htf_trend_val,
            regime_override=regime_res,
            pair=symbol,
            timeframe=timeframe,
            df_1m=df_1m_extra,
            df_15m=df_15m_extra,
            df_1h=df_1h_extra,
            df_4h=df_4h_extra,
        )

        if decision.signal == "BUY":
            signal_label = "LONG"
        elif decision.signal == "SHORT":
            signal_label = "SHORT"
        else:
            signal_label = "NO_SIGNAL"

        # 4. Compute indicator snapshot
        snap = compute_indicator_snapshot(df_merged, is_forex=is_forex)

        # 5. Pull cached backtest statistics for exact pair + timeframe + regime
        cache = load_backtest_cache()
        sym_cache = cache.get(symbol, {})
        tf_cache = sym_cache.get(timeframe, {})
        regime_stat = tf_cache.get(regime_name, {})

        sample_size = int(regime_stat.get("trades", regime_stat.get("total_trades", 0)))
        has_sufficient_data = sample_size >= MIN_CONFIDENCE_TRADES

        backtest_payload: Dict[str, Any] = {
            "insufficient_data": not has_sufficient_data,
            "sample_size": sample_size,
            "min_required_trades": MIN_CONFIDENCE_TRADES,
            "regime": regime_name,
        }

        if has_sufficient_data:
            backtest_payload["win_rate"] = float(regime_stat.get("win_rate", regime_stat.get("win_rate_pct", 0.0)))
            backtest_payload["profit_factor"] = float(regime_stat.get("profit_factor", 0.0))
            backtest_payload["net_pnl"] = float(regime_stat.get("net_pnl", 0.0))
            backtest_payload["wins"] = int(regime_stat.get("wins", 0))
            backtest_payload["losses"] = int(regime_stat.get("losses", 0))
            backtest_payload["confidence_label"] = f"Empirical Data (N={sample_size} trades)"
        else:
            # Strict compliance: do not provide synthetic/estimated win rate numbers when n < 30
            backtest_payload["win_rate"] = None
            backtest_payload["profit_factor"] = None
            backtest_payload["confidence_label"] = "Insufficient data — not enough completed trades yet"

        latest_bar = df_merged.iloc[-1]
        ts_val = latest_bar["datetime"] if "datetime" in latest_bar else datetime.now(timezone.utc)
        ts_str = ts_val.strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(ts_val, pd.Timestamp) else str(ts_val)

        # 6. Session awareness
        if is_forex:
            dt = ts_val.to_pydatetime() if isinstance(ts_val, pd.Timestamp) else (ts_val if isinstance(ts_val, datetime) else datetime.now(timezone.utc))
            session_name = forex_sessions.classify_session(dt)
        else:
            session_name = "24/7 Market"

        # 7. Pip distances for suggested SL / TP
        pip_size = forex_utils.get_pip_size(symbol) if is_forex else 1.0
        entry_p = decision.entry_price if decision.entry_price > 0 else snap["price"]
        sl_dist = abs(entry_p - decision.stop_loss) if decision.stop_loss > 0 else 0.0
        tp_dist = abs(decision.take_profit - entry_p) if decision.take_profit > 0 else 0.0
        sl_pips = round(sl_dist / pip_size, 1) if (is_forex and pip_size > 0) else round(sl_dist, 2)
        tp_pips = round(tp_dist / pip_size, 1) if (is_forex and pip_size > 0) else round(tp_dist, 2)

        # 8. Confidence breakdown
        conf = decision.confidence
        confidence_payload = {
            "score": conf.score if conf else 0,
            "tier": conf.tier if conf else "LOW",
            "passed": conf.passed_minimum_threshold if conf else False,
            "factors": conf.factors if conf else [],
            "breakdown": conf.breakdown if conf else {},
            "summary": conf.summary if conf else "",
        }

        # Include overall & out-of-sample backtest metrics if cached
        overall_bt = tf_cache.get("_overall", {})
        oos_bt = tf_cache.get("_out_of_sample", {})
        if overall_bt:
            backtest_payload["overall_win_rate"] = overall_bt.get("win_rate")
            backtest_payload["overall_profit_factor"] = overall_bt.get("profit_factor")
            backtest_payload["overall_trades"] = overall_bt.get("trades")
        if oos_bt:
            backtest_payload["oos_win_rate"] = oos_bt.get("win_rate")
            backtest_payload["oos_profit_factor"] = oos_bt.get("profit_factor")
            backtest_payload["oos_trades"] = oos_bt.get("trades")

        return jsonify({
            "success": True,
            "symbol": symbol,
            "is_forex": is_forex,
            "data_source": data_source_label,
            "timeframe": timeframe,
            "timestamp": ts_str,
            "signal": signal_label,
            "regime": regime_name,
            "rule_set": decision.rule_set,
            "session": {
                "name": session_name,
                "is_active_liquidity": session_name in ("LONDON", "NEW_YORK", "LONDON_NY_OVERLAP"),
                "is_rollover": session_name == "ROLLOVER",
            },
            "confidence": confidence_payload,
            "suggested_levels": {
                "entry": format_price_val(entry_p, is_forex),
                "stop_loss": format_price_val(decision.stop_loss, is_forex),
                "take_profit": format_price_val(decision.take_profit, is_forex),
                "sl_pips": sl_pips,
                "tp_pips": tp_pips,
                "risk_reward_ratio": round(decision.risk_reward_ratio, 2),
            },
            "macro_htf_trend": htf_trend_val,
            "indicators": {
                "price": snap["price"],
                "ema9": snap["ema9"],
                "ema21": snap["ema21"],
                "rsi": snap["rsi"],
                "macd_hist": snap["macd_hist"],
                "bb_upper": snap["bb_up"],
                "bb_middle": snap["bb_mid"],
                "bb_lower": snap["bb_low"],
                "bb_position": snap["bb_pos"],
                "atr": snap["atr"],
            },
            "backtest": backtest_payload,
            "disclaimer": "Read-only research observation. ZERO live orders or trades placed.",
        })

    except Exception as exc:
        logger.error(f"Error processing signal API for {symbol} ({timeframe}): {exc}")
        return jsonify({
            "success": False,
            "error": f"Internal evaluation error: {str(exc)}",
        }), 500


_last_signals_eval_ts: float = 0.0
_signals_cache_data: Dict[str, Any] = {}


@app.route("/api/forex/signals", methods=["GET"])
def api_forex_signals():
    """
    Returns live signal cards and metadata across all 7 major FX pairs.
    Strictly Read-Only: zero order placement, zero broker connections.
    """
    global _last_signals_eval_ts, _signals_cache_data
    try:
        import time as _pytime
        import forex_signal_engine
        engine = forex_signal_engine.get_signal_engine()
        
        force_refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
        now_ts = _pytime.time()

        # Return cached evaluation if fresh (< 25s old) and not forced
        if not force_refresh and (now_ts - _last_signals_eval_ts < 25.0) and _signals_cache_data:
            return jsonify(_signals_cache_data)

        results = {}
        latest_candle_time = None

        for pair in forex_signal_engine.SUPPORTED_PAIRS:
            df_5m = forex_data_feed.fetch_forex_candles(pair, "5m", count=60)
            df_1m = forex_data_feed.fetch_forex_candles(pair, "1m", count=60)
            df_15m = forex_data_feed.fetch_forex_candles(pair, "15m", count=60)
            df_1h = forex_data_feed.fetch_forex_candles(pair, "1h", count=60)
            df_4h = forex_data_feed.fetch_forex_candles(pair, "4h", count=60)

            if df_5m is not None and not df_5m.empty:
                c_raw = df_5m.iloc[-1].get("datetime")
                if c_raw is not None:
                    if isinstance(c_raw, str):
                        c_dt = pd.to_datetime(c_raw, utc=True).to_pydatetime()
                    elif isinstance(c_raw, pd.Timestamp):
                        c_dt = c_raw.to_pydatetime()
                    else:
                        c_dt = c_raw
                    if latest_candle_time is None or (c_dt and c_dt > latest_candle_time):
                        latest_candle_time = c_dt

            evaluated_sig = engine.evaluate_pair(pair, df_5m, df_1m, df_15m, df_1h, df_4h)
            requested_tf = request.args.get("timeframe", "5M").upper()
            card_dict = {
                "pair": evaluated_sig.pair,
                "direction": evaluated_sig.direction,
                "timeframe": requested_tf,
                "signal_time_ist": evaluated_sig.signal_time_ist,
                "signal_time_utc": evaluated_sig.signal_time_utc,
                "entry_price": evaluated_sig.entry_price,
                "stop_loss": evaluated_sig.stop_loss,
                "take_profit_1": evaluated_sig.take_profit_1,
                "take_profit_2": evaluated_sig.take_profit_2,
                "risk_1r_pips": evaluated_sig.risk_1r_pips,
                "regime_4h": evaluated_sig.regime_4h,
                "trend_1h": evaluated_sig.trend_1h,
                "spread_pips": evaluated_sig.spread_pips,
                "signal_id": evaluated_sig.signal_id,
                "is_valid_signal": evaluated_sig.is_valid_signal,
                "rejection_reason": evaluated_sig.rejection_reason,
                "card": evaluated_sig.to_card(),
            }
            results[pair] = card_dict
            engine.health.active_signals[pair] = card_dict

        engine.update_health(last_candle_utc=latest_candle_time, is_connected=True)
        _last_signals_eval_ts = _pytime.time()
        _signals_cache_data = {
            "success": True,
            "mode": "SIGNAL_ONLY (MANUAL EXECUTION ONLY)",
            "broker_trading": "DISABLED",
            "health": engine.get_health_snapshot(),
            "signals": results,
        }
        return jsonify(_signals_cache_data)
    except Exception as exc:
        logger.error(f"Error in api_forex_signals: {exc}")
        return jsonify({"success": False, "error": str(exc)}), 500



@app.route("/api/forex/health", methods=["GET"])
def api_forex_health():
    """Returns engine health, feed freshness, and safety status."""
    try:
        import forex_signal_engine
        engine = forex_signal_engine.get_signal_engine()
        engine.update_health(is_connected=True)
        return jsonify({
            "success": True,
            "mode": "SIGNAL_ONLY",
            "broker_trading": "DISABLED",
            "health": engine.get_health_snapshot(),
        })
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


def run_web_server(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
    """Starts local Flask development server."""
    print("=" * 80)
    print("           SPOT BOT SIGNAL RESEARCH PANEL - LOCAL SERVER")
    print("=" * 80)
    print(f" URL: http://{host}:{port}")
    print(" Status: Strict Read-Only Observer | Zero Execution Pathways")
    print(" Press Ctrl+C to terminate.")
    print("=" * 80 + "\n")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
