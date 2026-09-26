"""
Walk-Forward Historical Backtesting Engine for Major Forex Pairs.
Simulates bar-by-bar execution strictly on closed candles with zero lookahead bias.

Features:
  - Realistic Forex spread and transaction cost modeling (no artificial crypto fee on forex).
  - Pair-aware pip/pipette calculation for both JPY and non-JPY major pairs.
  - Session performance tracking (Asian, London, New York, London/NY Overlap, Rollover).
  - Market structure and regime-specific performance breakdowns.
  - Long vs Short directional analytics.
  - Expectancy ($ and R-multiple) and Max Drawdown tracking.
  - Chronological walk-forward split (In-Sample training vs Out-of-Sample validation).
  - Anti-churn cooldown enforcement (eliminates overtrading).
"""

import sys
import os
import math
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
import numpy as np

try:
    from . import config
    from . import data_feed
    from . import forex_data_feed
    from . import forex_utils
    from . import forex_sessions
    from . import strategy
    from . import risk_manager
    from . import regime_detector
    from .logger import get_logger
except ImportError:
    import config
    import data_feed
    import forex_data_feed
    import forex_utils
    import forex_sessions
    import strategy
    import risk_manager
    import regime_detector
    from logger import get_logger

logger = get_logger("backtest")

INITIAL_BALANCE: float = 10000.0        # Starting capital ($10,000)
BACKTEST_SYMBOL: str = "EUR/USD"        # Primary default pair
BACKTEST_TIMEFRAME: str = "1h"          # 1-hour candles
CANDLE_COUNT: int = 1000                # ~1,000 bars
CRYPTO_FEE_RATE: float = 0.001          # 0.1% Binance spot fee (crypto only)

MAJOR_FOREX_PAIRS: List[str] = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/CHF",
    "AUD/USD",
    "USD/CAD",
    "NZD/USD",
]


def run_spot_backtest(
    symbol: str = BACKTEST_SYMBOL,
    timeframe: str = BACKTEST_TIMEFRAME,
    candle_count: int = CANDLE_COUNT,
    initial_balance: float = INITIAL_BALANCE,
    walk_forward_split: float = 0.70,   # 70% In-Sample / 30% Out-of-Sample
) -> Dict[str, Any]:
    """
    Executes walk-forward bar-by-bar backtest with realistic Forex spread and session tracking.
    """
    std_symbol = forex_utils.clean_forex_symbol(symbol)
    is_forex = (
        std_symbol in forex_data_feed.FOREX_TICKER_MAP
        or any(c in std_symbol for c in ["EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY"])
        and "USDT" not in std_symbol
    )

    print("\n" + "=" * 85)
    print(f" {'FOREX' if is_forex else 'CRYPTO'} QUANTITATIVE RESEARCH & WALK-FORWARD BACKTEST")
    print(f" Pair Tested: {std_symbol} | Timeframe: {timeframe} | Requested Depth: {candle_count} bars")
    if is_forex:
        spread_pips = forex_utils.get_pair_spread_pips(std_symbol)
        pip_val = forex_utils.get_pip_size(std_symbol)
        print(f" Cost Model : Forex Bid-Ask Spread ({spread_pips:.1f} pips / ~${spread_pips * pip_val:.5f})")
    else:
        print(f" Cost Model : Crypto Spot Fee ({CRYPTO_FEE_RATE * 100:.2f}% per order)")
    print("=" * 85 + "\n")

    # 1. Fetch historical candle data
    if is_forex:
        print(f"--> [FOREX] Fetching {candle_count} historical {timeframe} candles for {std_symbol}...")
        df = forex_data_feed.fetch_forex_candles(
            pair=std_symbol,
            timeframe=timeframe,
            count=candle_count,
            use_cache=True,
        )
        print(f"--> [FOREX] Fetching higher-timeframe 4h candles for macro trend filter...")
        df_htf = forex_data_feed.fetch_higher_timeframe_forex_data(
            pair=std_symbol,
            timeframe="4h",
            limit=1500,
            ema_period=200,
            use_cache=True,
        )
    else:
        print(f"--> [CRYPTO] Fetching {candle_count} historical {timeframe} candles for {std_symbol}...")
        df = data_feed.fetch_historical_candles(
            symbol=std_symbol,
            timeframe=timeframe,
            limit=candle_count,
            use_cache=True,
        )
        print(f"--> [CRYPTO] Fetching higher-timeframe 4h candles for macro trend filter...")
        df_htf = data_feed.fetch_higher_timeframe_data(
            symbol=std_symbol,
            timeframe="4h",
            limit=1500,
            ema_period=200,
            use_cache=True,
        )

    # Attach HTF trend strictly without lookahead bias
    df = data_feed.attach_higher_timeframe_trend(df, df_htf)
    if is_forex:
        df = forex_sessions.attach_forex_sessions(df)

    total_bars = len(df)
    if total_bars < 50:
        print(f"[ERROR] Insufficient candle history ({total_bars} bars). Aborting.")
        return {}

    date_start = df["datetime"].iloc[0].strftime("%Y-%m-%d %H:%M")
    date_end = df["datetime"].iloc[-1].strftime("%Y-%m-%d %H:%M")
    print(f"--> Dataset: {total_bars} bars from {date_start} to {date_end} UTC.\n")

    # 2. Risk Manager & State Tracking
    spot_risk = risk_manager.SpotRiskManager(
        risk_per_trade_pct=config.RISK_PER_TRADE_PCT,
        daily_max_loss=config.DAILY_MAX_LOSS,
        daily_max_trades=config.DAILY_MAX_TRADES,
    )

    current_balance = initial_balance
    peak_balance = initial_balance
    max_drawdown_dollar = 0.0
    max_drawdown_pct = 0.0

    active_position: Optional[Dict[str, Any]] = None
    trades_history: List[Dict[str, Any]] = []
    equity_curve: List[float] = [initial_balance]
    hourly_returns: List[float] = []

    # Cooldown tracking
    last_exit_bar = -99
    last_exit_was_loss = False

    # Spread cost in price units
    half_spread_price = (forex_utils.get_spread_cost_in_price(std_symbol) / 2.0) if is_forex else 0.0

    # Walk-forward split index
    warmup_bars = 45
    usable_bars = total_bars - warmup_bars
    split_bar_idx = warmup_bars + int(usable_bars * walk_forward_split)

    print(f"--> Walk-Forward Partition: In-Sample (Bars {warmup_bars}..{split_bar_idx}) | Out-of-Sample (Bars {split_bar_idx}..{total_bars})")
    print("--> Commencing bar-by-bar simulation...")

    # 3. Simulation Loop
    for i in range(warmup_bars, total_bars):
        curr_bar = df.iloc[i]
        bar_date = curr_bar["datetime"].date() if "datetime" in curr_bar else None
        bar_session = curr_bar.get("session", "UNKNOWN")
        window_df = df.iloc[: i + 1]
        prior_balance = current_balance
        is_oos = (i >= split_bar_idx)

        # Detect regime for current bar
        htf_trend_val = str(curr_bar.get("htf_trend", "UP"))
        regime_result = regime_detector.detect_regime(window_df, htf_trend=htf_trend_val)

        # --- A. CHECK EXIT CONDITIONS IF POSITION ACTIVE ---
        if active_position is not None:
            active_position["bars_held"] = active_position.get("bars_held", 0) + 1

            exit_decision = strategy.evaluate_exit(
                curr_bar=curr_bar,
                position=active_position,
                df_history=window_df,
                pair=std_symbol,
            )

            if exit_decision.should_exit:
                exit_price = exit_decision.exit_price
                position_size = active_position["size"]
                cost_basis = active_position["cost"]
                direction = active_position["direction"]
                dollar_risk = active_position.get("dollar_risk", cost_basis * 0.01)

                if is_forex:
                    # Apply half-spread on exit
                    eff_exit = (exit_price - half_spread_price) if direction == "BUY" else (exit_price + half_spread_price)
                    eff_entry = active_position["eff_entry"]
                    if direction == "BUY":
                        net_pnl = (eff_exit - eff_entry) * position_size
                    else:
                        net_pnl = (eff_entry - eff_exit) * position_size
                    total_fees = 0.0
                else:
                    if direction == "BUY":
                        gross_pnl = (exit_price - active_position["entry_price"]) * position_size
                    else:
                        gross_pnl = (active_position["entry_price"] - exit_price) * position_size
                    total_fees = (cost_basis + (exit_price * position_size)) * CRYPTO_FEE_RATE
                    net_pnl = gross_pnl - total_fees

                return_pct = (net_pnl / cost_basis) * 100 if cost_basis > 0 else 0.0
                r_multiple = (net_pnl / dollar_risk) if dollar_risk > 0 else 0.0

                current_balance += net_pnl
                outcome_label = "WIN" if net_pnl > 0 else "LOSS"

                spot_risk.record_trade_result(outcome_label, net_pnl, current_date=bar_date)

                last_exit_bar = i
                last_exit_was_loss = (net_pnl <= 0)

                trade_record = {
                    "trade_num": len(trades_history) + 1,
                    "direction": direction,
                    "regime": active_position["regime"],
                    "session": active_position.get("session", bar_session),
                    "rule_set": active_position["rule_set"],
                    "entry_time": active_position["entry_time"],
                    "exit_time": curr_bar["datetime"],
                    "entry_price": active_position["entry_price"],
                    "exit_price": exit_price,
                    "size": position_size,
                    "cost": cost_basis,
                    "dollar_risk": round(dollar_risk, 2),
                    "exit_reason": exit_decision.exit_reason,
                    "outcome": outcome_label,
                    "net_pnl": round(net_pnl, 2),
                    "r_multiple": round(r_multiple, 2),
                    "return_pct": round(return_pct, 2),
                    "bars_held": active_position["bars_held"],
                    "is_oos": is_oos,
                    "balance_after": round(current_balance, 2),
                }
                trades_history.append(trade_record)
                active_position = None

        # --- B. CHECK ENTRY CONDITIONS IF NOT IN A POSITION ---
        if active_position is None:
            # 1. Anti-churn cooldown check
            cooldown_bars = 6 if last_exit_was_loss else 4
            is_cooled_down = (i - last_exit_bar) >= cooldown_bars

            # 2. Risk manager allowance check
            can_enter_risk, _ = spot_risk.can_trade(current_balance, current_date=bar_date)

            if is_cooled_down and can_enter_risk:
                entry_decision = strategy.evaluate_entry(
                    df=window_df,
                    htf_trend=htf_trend_val,
                    regime_override=regime_result,
                    pair=std_symbol,
                )

                if entry_decision.signal in ("BUY", "SHORT"):
                    max_lev = 10.0 if is_forex else 1.0
                    sizing = spot_risk.calculate_position_size(
                        account_balance=current_balance,
                        entry_price=entry_decision.entry_price,
                        stop_loss_price=entry_decision.stop_loss,
                        max_leverage=max_lev,
                    )

                    if sizing.position_size > 0 and sizing.position_value <= ((current_balance * max_lev) + 0.1):
                        # Effective entry price accounting for half-spread
                        market_entry = entry_decision.entry_price
                        if is_forex:
                            eff_entry = (market_entry + half_spread_price) if entry_decision.signal == "BUY" else (market_entry - half_spread_price)
                        else:
                            eff_entry = market_entry

                        active_position = {
                            "direction": entry_decision.signal,
                            "entry_price": market_entry,
                            "eff_entry": eff_entry,
                            "size": sizing.position_size,
                            "cost": sizing.position_value,
                            "dollar_risk": sizing.dollar_risk,
                            "stop_loss": entry_decision.stop_loss,
                            "take_profit": entry_decision.take_profit,
                            "atr": entry_decision.atr_value,
                            "entry_time": curr_bar["datetime"],
                            "entry_bar": i,
                            "bars_held": 0,
                            "regime": entry_decision.regime,
                            "session": bar_session,
                            "rule_set": entry_decision.rule_set,
                        }

        # Track Drawdown
        if current_balance > peak_balance:
            peak_balance = current_balance
        dd_dollar = peak_balance - current_balance
        dd_pct = (dd_dollar / peak_balance) * 100 if peak_balance > 0 else 0.0

        if dd_dollar > max_drawdown_dollar:
            max_drawdown_dollar = dd_dollar
        if dd_pct > max_drawdown_pct:
            max_drawdown_pct = dd_pct

        equity_curve.append(current_balance)
        bar_return = (current_balance - prior_balance) / prior_balance if prior_balance > 0 else 0.0
        hourly_returns.append(bar_return)

    # Close open position at end of simulation
    if active_position is not None:
        last_bar = df.iloc[-1]
        exit_price = float(last_bar["close"])
        position_size = active_position["size"]
        cost_basis = active_position["cost"]
        direction = active_position["direction"]
        dollar_risk = active_position.get("dollar_risk", cost_basis * 0.01)

        if is_forex:
            eff_exit = (exit_price - half_spread_price) if direction == "BUY" else (exit_price + half_spread_price)
            eff_entry = active_position["eff_entry"]
            net_pnl = (eff_exit - eff_entry) * position_size if direction == "BUY" else (eff_entry - eff_exit) * position_size
        else:
            gross = (exit_price - active_position["entry_price"]) * position_size if direction == "BUY" else (active_position["entry_price"] - exit_price) * position_size
            net_pnl = gross - ((cost_basis + (exit_price * position_size)) * CRYPTO_FEE_RATE)

        current_balance += net_pnl
        outcome_label = "WIN" if net_pnl > 0 else "LOSS"
        r_multiple = (net_pnl / dollar_risk) if dollar_risk > 0 else 0.0

        trades_history.append({
            "trade_num": len(trades_history) + 1,
            "direction": direction,
            "regime": active_position["regime"],
            "session": active_position.get("session", "UNKNOWN"),
            "rule_set": active_position["rule_set"],
            "entry_time": active_position["entry_time"],
            "exit_time": last_bar["datetime"],
            "entry_price": active_position["entry_price"],
            "exit_price": exit_price,
            "size": position_size,
            "cost": cost_basis,
            "dollar_risk": round(dollar_risk, 2),
            "exit_reason": "BACKTEST_END",
            "outcome": outcome_label,
            "net_pnl": round(net_pnl, 2),
            "r_multiple": round(r_multiple, 2),
            "return_pct": round((net_pnl / cost_basis) * 100, 2),
            "bars_held": total_bars - active_position["entry_bar"],
            "is_oos": True,
            "balance_after": round(current_balance, 2),
        })

    # ==============================================================================
    # COMPREHENSIVE PERFORMANCE CALCULATIONS
    # ==============================================================================
    def calculate_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        n_trades = len(trades)
        if n_trades == 0:
            return {
                "trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "net_pnl": 0.0, "avg_r": 0.0, "expectancy": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "win_loss_ratio": 0.0,
            }
        wins = [t for t in trades if t["outcome"] == "WIN"]
        losses = [t for t in trades if t["outcome"] == "LOSS"]
        w_count = len(wins)
        l_count = len(losses)
        wr = (w_count / n_trades * 100) if n_trades > 0 else 0.0

        gross_profit = sum(t["net_pnl"] for t in wins)
        gross_loss = abs(sum(t["net_pnl"] for t in losses))
        pf = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

        net_pnl = sum(t["net_pnl"] for t in trades)
        avg_r = (sum(t.get("r_multiple", 0.0) for t in trades) / n_trades) if n_trades > 0 else 0.0
        expectancy = net_pnl / n_trades if n_trades > 0 else 0.0

        avg_win = (gross_profit / w_count) if w_count > 0 else 0.0
        avg_loss = (gross_loss / l_count) if l_count > 0 else 0.0
        wl_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0

        return {
            "trades": n_trades,
            "wins": w_count,
            "losses": l_count,
            "win_rate": round(wr, 2),
            "profit_factor": round(pf, 2),
            "net_pnl": round(net_pnl, 2),
            "avg_r": round(avg_r, 2),
            "expectancy": round(expectancy, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "win_loss_ratio": round(wl_ratio, 2),
        }

    overall_metrics = calculate_metrics(trades_history)
    in_sample_trades = [t for t in trades_history if not t.get("is_oos", False)]
    out_of_sample_trades = [t for t in trades_history if t.get("is_oos", False)]
    is_metrics = calculate_metrics(in_sample_trades)
    oos_metrics = calculate_metrics(out_of_sample_trades)

    long_trades = [t for t in trades_history if t["direction"] == "BUY"]
    short_trades = [t for t in trades_history if t["direction"] == "SHORT"]
    long_metrics = calculate_metrics(long_trades)
    short_metrics = calculate_metrics(short_trades)

    # Regime breakdown
    regime_stats = {}
    for reg in ["TRENDING_UP", "TRENDING_DOWN", "RANGING"]:
        reg_trades = [t for t in trades_history if t["regime"] == reg]
        regime_stats[reg] = calculate_metrics(reg_trades)

    # Session breakdown
    session_stats = {}
    for sess in [
        forex_sessions.SESSION_LONDON_NY_OVERLAP,
        forex_sessions.SESSION_LONDON,
        forex_sessions.SESSION_NEW_YORK,
        forex_sessions.SESSION_ASIAN,
        forex_sessions.SESSION_ROLLOVER,
    ]:
        sess_trades = [t for t in trades_history if t.get("session") == sess]
        if sess_trades:
            session_stats[sess] = calculate_metrics(sess_trades)

    # ==============================================================================
    # PRINT STRUCTURED REPORT
    # ==============================================================================
    print("\n" + "=" * 85)
    print(f"               PERFORMANCE REPORT: {std_symbol} ({timeframe})")
    print("=" * 85)
    print(f" Initial Balance   : ${initial_balance:,.2f} | Final Balance: ${current_balance:,.2f}")
    print(f" Total Net P&L     : ${overall_metrics['net_pnl']:+,.2f} ({(overall_metrics['net_pnl'] / initial_balance)*100:+.2f}%)")
    print(f" Max Drawdown      : ${max_drawdown_dollar:,.2f} ({max_drawdown_pct:.2f}%)")
    print(f" Total Trades      : {overall_metrics['trades']} (Win Rate: {overall_metrics['win_rate']}%)")
    print(f" Profit Factor     : {overall_metrics['profit_factor']:.2f}")
    print(f" Expectancy / Trade: ${overall_metrics['expectancy']:+,.2f} ({overall_metrics['avg_r']:+.2f} R)")
    print(f" Win/Loss Ratio    : {overall_metrics['win_loss_ratio']:.2f}:1 (Avg Win: ${overall_metrics['avg_win']:,.2f} / Avg Loss: ${overall_metrics['avg_loss']:,.2f})")
    print("-" * 85)

    # --- 1. WALK-FORWARD CHRONOLOGICAL OUT-OF-SAMPLE TEST ---
    print(" 1. WALK-FORWARD CHRONOLOGICAL VALIDATION (In-Sample vs Out-of-Sample)")
    print("-" * 85)
    print(f"   [IN-SAMPLE TRAIN 70%]  : Trades: {is_metrics['trades']:<3} | WR: {is_metrics['win_rate']:>5.1f}% | PF: {is_metrics['profit_factor']:>4.2f} | PnL: ${is_metrics['net_pnl']:>+8.2f} | Avg R: {is_metrics['avg_r']:>+4.2f}R")
    print(f"   [OUT-OF-SAMPLE TEST 30%]: Trades: {oos_metrics['trades']:<3} | WR: {oos_metrics['win_rate']:>5.1f}% | PF: {oos_metrics['profit_factor']:>4.2f} | PnL: ${oos_metrics['net_pnl']:>+8.2f} | Avg R: {oos_metrics['avg_r']:>+4.2f}R")

    # --- 2. LONG VS SHORT PERFORMANCE ---
    print("\n" + "-" * 85)
    print(" 2. DIRECTIONAL PERFORMANCE (Long vs Short)")
    print("-" * 85)
    print(f"   [LONG (BUY)]           : Trades: {long_metrics['trades']:<3} | WR: {long_metrics['win_rate']:>5.1f}% | PF: {long_metrics['profit_factor']:>4.2f} | PnL: ${long_metrics['net_pnl']:>+8.2f}")
    print(f"   [SHORT (SELL)]         : Trades: {short_metrics['trades']:<3} | WR: {short_metrics['win_rate']:>5.1f}% | PF: {short_metrics['profit_factor']:>4.2f} | PnL: ${short_metrics['net_pnl']:>+8.2f}")

    # --- 3. MARKET REGIME PERFORMANCE ---
    print("\n" + "-" * 85)
    print(" 3. PERFORMANCE BY MARKET REGIME")
    print("-" * 85)
    for reg, stats in regime_stats.items():
        print(f"   [{reg:<16}] : Trades: {stats['trades']:<3} | WR: {stats['win_rate']:>5.1f}% | PF: {stats['profit_factor']:>4.2f} | PnL: ${stats['net_pnl']:>+8.2f}")

    # --- 4. SESSION PERFORMANCE ---
    if is_forex and session_stats:
        print("\n" + "-" * 85)
        print(" 4. PERFORMANCE BY FOREX SESSION")
        print("-" * 85)
        for sess, stats in session_stats.items():
            print(f"   [{sess:<20}] : Trades: {stats['trades']:<3} | WR: {stats['win_rate']:>5.1f}% | PF: {stats['profit_factor']:>4.2f} | PnL: ${stats['net_pnl']:>+8.2f}")

    # --- 5. EXIT REASON BREAKDOWN ---
    print("\n" + "-" * 85)
    print(" 5. EXIT REASON BREAKDOWN")
    print("-" * 85)
    for reason in ["TAKE_PROFIT", "TRAILING_STOP", "BREAKEVEN_STOP", "STOP_LOSS", "TREND_REVERSAL", "BACKTEST_END"]:
        cnt = sum(1 for t in trades_history if t["exit_reason"] == reason)
        if cnt > 0:
            print(f"   * {reason:<22}: {cnt} trades")
    print("=" * 85 + "\n")

    return {
        "symbol": std_symbol,
        "timeframe": timeframe,
        "initial_balance": initial_balance,
        "final_balance": round(current_balance, 2),
        "overall": overall_metrics,
        "in_sample": is_metrics,
        "out_of_sample": oos_metrics,
        "long_metrics": long_metrics,
        "short_metrics": short_metrics,
        "regime_stats": regime_stats,
        "session_stats": session_stats,
        "total_trades": overall_metrics["trades"],
        "win_rate": overall_metrics["win_rate"],
        "profit_factor": overall_metrics["profit_factor"],
        "max_drawdown_pct": round(max_drawdown_pct, 2),
    }


def run_all_major_forex_backtests(
    timeframes: Optional[List[str]] = None,
    candle_count: int = 1000,
) -> Dict[str, Any]:
    """
    Executes walk-forward backtests across all 7 major forex pairs on 1h and 4h timeframes.
    Persists updated results into data/backtest_cache.json and prints consolidated summary.
    """
    if timeframes is None:
        timeframes = ["1h", "4h"]

    cache_file = Path(__file__).resolve().parent / "data" / "backtest_cache.json"
    cache = {}
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    summary_rows = []

    print("\n" + "=" * 100)
    print("               BATCH MAJOR FOREX BACKTEST RUN (7 Major Pairs)")
    print(f"               Pairs: {', '.join(MAJOR_FOREX_PAIRS)}")
    print(f"               Timeframes: {', '.join(timeframes)}")
    print("=" * 100 + "\n")

    for pair in MAJOR_FOREX_PAIRS:
        if pair not in cache:
            cache[pair] = {}

        for tf in timeframes:
            print(f"\n>>> Running Backtest: {pair} on {tf} ({candle_count} bars)...")
            try:
                res = run_spot_backtest(symbol=pair, timeframe=tf, candle_count=candle_count)
                if res and "overall" in res and res["overall"]["trades"] > 0:
                    cache[pair][tf] = res["regime_stats"]
                    # Add overall and oos metrics into cache for app consumption
                    cache[pair][tf]["_overall"] = res["overall"]
                    cache[pair][tf]["_out_of_sample"] = res["out_of_sample"]

                    summary_rows.append({
                        "pair": pair,
                        "timeframe": tf,
                        "trades": res["overall"]["trades"],
                        "win_rate": f"{res['overall']['win_rate']:.1f}%",
                        "profit_factor": f"{res['overall']['profit_factor']:.2f}",
                        "oos_trades": res["out_of_sample"]["trades"],
                        "oos_wr": f"{res['out_of_sample']['win_rate']:.1f}%",
                        "oos_pf": f"{res['out_of_sample']['profit_factor']:.2f}",
                        "net_pnl": f"${res['overall']['net_pnl']:+,.2f}",
                    })
                else:
                    summary_rows.append({
                        "pair": pair,
                        "timeframe": tf,
                        "trades": 0,
                        "win_rate": "N/A",
                        "profit_factor": "N/A",
                        "oos_trades": 0,
                        "oos_wr": "N/A",
                        "oos_pf": "N/A",
                        "net_pnl": "$0.00",
                    })
            except Exception as exc:
                logger.error(f"Error backtesting {pair} ({tf}): {exc}")

    # Persist updated cache
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        print(f"\n[CACHE] Successfully persisted updated results to {cache_file.name}")
    except Exception as exc:
        logger.error(f"Failed to write {cache_file.name}: {exc}")

    # Print summary table
    print("\n" + "=" * 105)
    print("                     MAJOR FOREX BACKTEST SUMMARY RESULTS TABLE")
    print("=" * 105)
    header = f"{'Pair':<10} | {'TF':<4} | {'Trades':<7} | {'Win Rate':<9} | {'PF':<6} | {'OOS N':<6} | {'OOS WR':<8} | {'OOS PF':<7} | {'Net PnL':<12}"
    sep = f"{'-'*10}-+-{'-'*4}-+-{'-'*7}-+-{'-'*9}-+-{'-'*6}-+-{'-'*6}-+-{'-'*8}-+-{'-'*7}-+-{'-'*12}"
    print(header)
    print(sep)
    for r in summary_rows:
        print(f"{r['pair']:<10} | {r['timeframe']:<4} | {r['trades']:<7} | {r['win_rate']:<9} | {r['profit_factor']:<6} | {r['oos_trades']:<6} | {r['oos_wr']:<8} | {r['oos_pf']:<7} | {r['net_pnl']:<12}")
    print("=" * 105 + "\n")

    return cache


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Forex & Spot Bot Backtest Engine")
    parser.add_argument("--symbol", type=str, default=BACKTEST_SYMBOL, help="Pair (EUR/USD, GBP/USD, etc.)")
    parser.add_argument("--timeframe", type=str, default=BACKTEST_TIMEFRAME, help="Candle timeframe (1h, 4h)")
    parser.add_argument("--count", type=int, default=CANDLE_COUNT, help="Number of candles")
    parser.add_argument("--forex-majors", action="store_true", help="Run backtests across all 7 major forex pairs")

    args = parser.parse_args()

    if args.forex_majors:
        run_all_major_forex_backtests(candle_count=args.count)
    else:
        run_spot_backtest(symbol=args.symbol, timeframe=args.timeframe, candle_count=args.count)
