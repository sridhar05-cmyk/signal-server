"""
Walk-Forward Historical Backtesting Engine with Adaptive Regime Detection.
Simulates bar-by-bar execution on historical Binance OHLCV candles (default: 6 months of 1h BTC/USDT).
Integrates:
  - Regime detection at every bar (TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOLATILITY).
  - Regime-specific strategy routing (Trend-following vs Mean-reversion vs Volatility skip).
  - Dynamic multi-stage trailing exits.
  - Granular performance reporting broken down by market regime.
"""

import sys
import math
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np

try:
    from . import config
    from . import data_feed
    from . import forex_data_feed
    from . import strategy
    from . import risk_manager
    from . import regime_detector
    from .logger import get_logger
except ImportError:
    import config
    import data_feed
    import forex_data_feed
    import strategy
    import risk_manager
    import regime_detector
    from logger import get_logger

logger = get_logger("backtest")

INITIAL_BALANCE: float = 10000.0        # Starting capital ($10,000 USDT)
BACKTEST_SYMBOL: str = "BTC/USDT"       # Asset to backtest
BACKTEST_TIMEFRAME: str = "1h"          # 1-hour candles
CANDLE_COUNT: int = 4320                # 4,320 bars * 1h = 180 days (6 months)
FEE_RATE: float = 0.001                 # 0.1% Binance spot fee per order


def run_spot_backtest(
    symbol: str = BACKTEST_SYMBOL,
    timeframe: str = BACKTEST_TIMEFRAME,
    candle_count: int = CANDLE_COUNT,
    initial_balance: float = INITIAL_BALANCE,
    fee_rate: float = FEE_RATE,
) -> Dict[str, Any]:
    """
    Executes walk-forward bar-by-bar backtest with regime detection and regime performance breakdown.
    """
    print("\n" + "=" * 80)
    print(" BINANCE SPOT TRADING BOT -- ADAPTIVE REGIME RESEARCH & BACKTEST")
    print(" Regimes: TRENDING_UP (Pullback Longs) | TRENDING_DOWN (Rally Shorts) | RANGING (Mean-Reversion)")
    print(" Safety Filter: HIGH_VOLATILITY Circuit Breaker (Entries Skipped)")
    print("=" * 80)
    print(f" Initial Capital   : ${initial_balance:,.2f}")
    print(f" Symbol Tested     : {symbol}")
    print(f" Timeframe         : {timeframe}")
    print(f" Requested History : {candle_count} bars (~{candle_count // 24} days / 6 months)")
    print(f" Exchange Fee Rate : {fee_rate * 100:.2f}% per order")
    print("-" * 80 + "\n")

    # Determine whether symbol is Forex or Crypto
    is_forex = (
        symbol in forex_data_feed.FOREX_TICKER_MAP
        or any(curr in symbol for curr in ["EUR", "GBP", "AUD", "NZD", "COP", "CAD", "CHF", "JPY"])
        and "USDT" not in symbol
    )

    if is_forex:
        print(f"--> [FOREX] Fetching {candle_count} historical {timeframe} candles for {symbol} via Yahoo Finance...")
        df = forex_data_feed.fetch_forex_candles(
            pair=symbol,
            timeframe=timeframe,
            count=candle_count,
            use_cache=True,
        )
        print(f"--> [FOREX] Fetching higher-timeframe 4h candles for {symbol} macro trend filter...")
        df_htf = forex_data_feed.fetch_higher_timeframe_forex_data(
            pair=symbol,
            timeframe="4h",
            limit=1500,
            ema_period=200,
            use_cache=True,
        )
    else:
        # 1. Fetch historical candles from Binance public API
        print(f"--> [CRYPTO] Fetching {candle_count} historical {timeframe} candles for {symbol} via Binance...")
        df = data_feed.fetch_historical_candles(
            symbol=symbol,
            timeframe=timeframe,
            limit=candle_count,
            use_cache=True,
        )

        # 2. Fetch 4h Higher-Timeframe candles for 200 EMA macro trend filter
        print("--> [CRYPTO] Fetching higher-timeframe 4h candles for 200 EMA macro trend filter...")
        df_htf = data_feed.fetch_higher_timeframe_data(
            symbol=symbol,
            timeframe="4h",
            limit=1500,
            ema_period=200,
            use_cache=True,
        )

    # Attach HTF trend strictly without lookahead bias
    df = data_feed.attach_higher_timeframe_trend(df, df_htf)

    total_bars = len(df)
    if total_bars < 50:
        print(f"[ERROR] Insufficient candle history ({total_bars} bars). Aborting.")
        return {}

    date_start = df["datetime"].iloc[0].strftime("%Y-%m-%d %H:%M")
    date_end = df["datetime"].iloc[-1].strftime("%Y-%m-%d %H:%M")
    print(f"--> Loaded {total_bars} candles spanning from {date_start} to {date_end} UTC.\n")

    # 3. Initialize Risk Manager and Tracking State
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

    # Regime bar counters
    regime_bar_counts = {
        "TRENDING_UP": 0,
        "TRENDING_DOWN": 0,
        "RANGING": 0,
        "HIGH_VOLATILITY": 0,
    }

    print("--> Commencing adaptive walk-forward simulation bar-by-bar...")

    # 4. Simulation Loop (Warm-up 50 bars for rolling 50-avg indicators)
    warmup_bars = 50
    for i in range(warmup_bars, total_bars):
        curr_bar = df.iloc[i]
        bar_date = curr_bar["datetime"].date()
        window_df = df.iloc[: i + 1]
        prior_balance = current_balance

        # Detect regime for current bar
        htf_trend_val = str(curr_bar.get("htf_trend", "UP"))
        regime_result = regime_detector.detect_regime(window_df, htf_trend=htf_trend_val)
        regime_bar_counts[regime_result.regime] = regime_bar_counts.get(regime_result.regime, 0) + 1

        # --- A. CHECK EXIT CONDITIONS IF POSITION IS ACTIVE ---
        if active_position is not None:
            exit_decision = strategy.evaluate_exit(
                curr_bar=curr_bar,
                position=active_position,
                df_history=window_df,
            )

            if exit_decision.should_exit:
                exit_price = exit_decision.exit_price
                position_size = active_position["size"]
                cost_basis = active_position["cost"]
                direction = active_position["direction"]

                if direction == "BUY":
                    gross_pnl = (exit_price - active_position["entry_price"]) * position_size
                    total_fees = (cost_basis + (exit_price * position_size)) * fee_rate
                else:  # SHORT
                    gross_pnl = (active_position["entry_price"] - exit_price) * position_size
                    total_fees = (cost_basis + (exit_price * position_size)) * fee_rate

                net_pnl = gross_pnl - total_fees
                return_pct = (net_pnl / cost_basis) * 100 if cost_basis > 0 else 0.0

                current_balance += net_pnl

                outcome_label = "WIN" if net_pnl > 0 else "LOSS"
                spot_risk.record_trade_result(outcome_label, net_pnl, current_date=bar_date)

                bars_held = i - active_position["entry_bar"]

                trade_record = {
                    "trade_num": len(trades_history) + 1,
                    "direction": direction,
                    "regime": active_position["regime"],
                    "rule_set": active_position["rule_set"],
                    "entry_time": active_position["entry_time"],
                    "exit_time": curr_bar["datetime"],
                    "entry_price": active_position["entry_price"],
                    "exit_price": exit_price,
                    "size": position_size,
                    "cost": cost_basis,
                    "exit_reason": exit_decision.exit_reason,
                    "outcome": outcome_label,
                    "net_pnl": round(net_pnl, 2),
                    "return_pct": round(return_pct, 2),
                    "bars_held": bars_held,
                    "balance_after": round(current_balance, 2),
                }
                trades_history.append(trade_record)
                active_position = None

        # --- B. CHECK ENTRY CONDITIONS IF NOT IN A POSITION ---
        if active_position is None:
            can_enter, risk_reason = spot_risk.can_trade(current_balance, current_date=bar_date)

            if can_enter:
                entry_decision = strategy.evaluate_entry(
                    df=window_df,
                    htf_trend=htf_trend_val,
                    regime_override=regime_result,
                )

                if entry_decision.signal in ("BUY", "SHORT"):
                    sizing = spot_risk.calculate_position_size(
                        account_balance=current_balance,
                        entry_price=entry_decision.entry_price,
                        stop_loss_price=entry_decision.stop_loss,
                    )

                    if sizing.position_size > 0 and sizing.position_value <= current_balance:
                        active_position = {
                            "direction": entry_decision.signal,
                            "entry_price": entry_decision.entry_price,
                            "size": sizing.position_size,
                            "cost": sizing.position_value,
                            "stop_loss": entry_decision.stop_loss,
                            "take_profit": entry_decision.take_profit,
                            "atr": entry_decision.atr_value,
                            "entry_time": curr_bar["datetime"],
                            "entry_bar": i,
                            "regime": entry_decision.regime,
                            "rule_set": entry_decision.rule_set,
                        }

        # Track Drawdown and Equity Curve
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

        if direction == "BUY":
            gross_pnl = (exit_price - active_position["entry_price"]) * position_size
        else:
            gross_pnl = (active_position["entry_price"] - exit_price) * position_size

        total_fees = (cost_basis + (exit_price * position_size)) * fee_rate
        net_pnl = gross_pnl - total_fees
        return_pct = (net_pnl / cost_basis) * 100 if cost_basis > 0 else 0.0
        current_balance += net_pnl

        outcome_label = "WIN" if net_pnl > 0 else "LOSS"
        trades_history.append({
            "trade_num": len(trades_history) + 1,
            "direction": direction,
            "regime": active_position["regime"],
            "rule_set": active_position["rule_set"],
            "entry_time": active_position["entry_time"],
            "exit_time": last_bar["datetime"],
            "entry_price": active_position["entry_price"],
            "exit_price": exit_price,
            "size": position_size,
            "cost": cost_basis,
            "exit_reason": "BACKTEST_END",
            "outcome": outcome_label,
            "net_pnl": round(net_pnl, 2),
            "return_pct": round(return_pct, 2),
            "bars_held": total_bars - active_position["entry_bar"],
            "balance_after": round(current_balance, 2),
        })

    # ==============================================================================
    # COMPREHENSIVE PERFORMANCE & REGIME BREAKDOWN REPORT
    # ==============================================================================
    evaluated_bars = total_bars - warmup_bars
    total_trades = len(trades_history)
    wins = [t for t in trades_history if t["outcome"] == "WIN"]
    losses = [t for t in trades_history if t["outcome"] == "LOSS"]

    win_count = len(wins)
    loss_count = len(losses)
    win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0.0

    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss = abs(sum(t["net_pnl"] for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    avg_win = (gross_profit / win_count) if win_count > 0 else 0.0
    avg_loss = (gross_loss / loss_count) if loss_count > 0 else 0.0
    win_loss_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0

    net_profit = current_balance - initial_balance
    roi_pct = (net_profit / initial_balance) * 100

    returns_series = pd.Series(hourly_returns)
    std_ret = returns_series.std()
    sharpe_ratio = (returns_series.mean() / std_ret * math.sqrt(8760)) if std_ret > 0 else 0.0

    print("\n" + "=" * 80)
    print("               ADAPTIVE SPOT BOT PERFORMANCE REPORT")
    print("=" * 80)
    print(f" Period Evaluated       : {date_start} to {date_end} UTC ({evaluated_bars} bars)")
    print(f" Initial Capital        : ${initial_balance:,.2f}")
    print(f" Final Account Balance  : ${current_balance:,.2f}")
    print(f" Net Realized Profit/Loss: ${net_profit:+,.2f} ({roi_pct:+.2f}%)")
    print("-" * 80)
    print(f" Total Trades Executed  : {total_trades}")
    print(f" Winning Trades         : {win_count} ({win_rate:.2f}%)")
    print(f" Losing Trades          : {loss_count} ({100 - win_rate:.2f}%)")
    print(f" Average Win Size       : ${avg_win:,.2f}")
    print(f" Average Loss Size      : ${avg_loss:,.2f}")
    print(f" Win/Loss Size Ratio    : {win_loss_ratio:.2f}:1")
    print(f" Profit Factor          : {profit_factor:.2f} (Gross Profit / Gross Loss)")
    print(f" Max Drawdown ($)       : ${max_drawdown_dollar:,.2f}")
    print(f" Max Drawdown (%)       : {max_drawdown_pct:.2f}%")
    print(f" Annualized Sharpe Ratio: {sharpe_ratio:.2f}")

    # --- 1. MARKET REGIME TIME ALLOCATION ---
    print("\n" + "-" * 80)
    print(" 1. MARKET REGIME ALLOCATION (Time Spent in Each Condition)")
    print("-" * 80)
    for reg, count in regime_bar_counts.items():
        pct = (count / evaluated_bars * 100) if evaluated_bars > 0 else 0.0
        skip_note = " [SKIPPED - SAFETY FILTER]" if reg == "HIGH_VOLATILITY" else ""
        print(f"   * {reg:<16}: {count:>4} bars ({pct:>5.1f}%){skip_note}")

    # --- 2. PERFORMANCE BROKEN DOWN BY REGIME ---
    print("\n" + "-" * 80)
    print(" 2. PERFORMANCE BREAKDOWN BY REGIME (Where Edge Exists)")
    print("-" * 80)
    regimes_to_evaluate = ["TRENDING_UP", "TRENDING_DOWN", "RANGING"]

    regime_stats = {}
    for reg in regimes_to_evaluate:
        reg_trades = [t for t in trades_history if t["regime"] == reg]
        reg_total = len(reg_trades)
        reg_wins = [t for t in reg_trades if t["outcome"] == "WIN"]
        reg_losses = [t for t in reg_trades if t["outcome"] == "LOSS"]
        reg_win_count = len(reg_wins)
        reg_win_rate = (reg_win_count / reg_total * 100) if reg_total > 0 else 0.0

        reg_gp = sum(t["net_pnl"] for t in reg_wins)
        reg_gl = abs(sum(t["net_pnl"] for t in reg_losses))
        reg_pf = (reg_gp / reg_gl) if reg_gl > 0 else (999.0 if reg_gp > 0 else 0.0)
        reg_net = sum(t["net_pnl"] for t in reg_trades)

        regime_stats[reg] = {
            "trades": reg_total,
            "wins": reg_win_count,
            "losses": len(reg_losses),
            "win_rate": round(reg_win_rate, 2),
            "profit_factor": round(reg_pf, 2),
            "net_pnl": round(reg_net, 2),
        }

        print(f"   [{reg}]")
        print(f"     Trades Executed : {reg_total}")
        print(f"     Win Rate        : {reg_win_rate:.2f}% ({reg_win_count}W / {len(reg_losses)}L)")
        print(f"     Profit Factor   : {reg_pf:.2f}")
        print(f"     Net P&L         : ${reg_net:+,.2f}")
        print()

    # --- 3. EXIT REASON BREAKDOWN ---
    print("-" * 80)
    print(" 3. EXIT REASON BREAKDOWN")
    print("-" * 80)
    tp_exits = sum(1 for t in trades_history if t["exit_reason"] == "TAKE_PROFIT")
    trail_exits = sum(1 for t in trades_history if t["exit_reason"] == "TRAILING_STOP")
    be_exits = sum(1 for t in trades_history if t["exit_reason"] == "BREAKEVEN_STOP")
    sl_exits = sum(1 for t in trades_history if t["exit_reason"] == "STOP_LOSS")
    trend_exits = sum(1 for t in trades_history if t["exit_reason"] == "TREND_REVERSAL")
    end_exits = sum(1 for t in trades_history if t["exit_reason"] == "BACKTEST_END")

    print(f"   * Take-Profit Target          : {tp_exits}")
    print(f"   * Trailing Stop (2x ATR)      : {trail_exits}")
    print(f"   * Breakeven Stop (1x ATR)     : {be_exits}")
    print(f"   * Initial Stop-Loss (Risk SL) : {sl_exits}")
    print(f"   * Trend Reversal Fallback     : {trend_exits}")
    if end_exits:
        print(f"   * End of Dataset Exit         : {end_exits}")
    print("=" * 80)

    print("\n" + "!" * 80)
    print(" [CRITICAL RESEARCH DIRECTIVE]:")
    print(" \"A profit factor above 1.5 and win rate that beats the risk-reward breakeven")
    print(" are the minimum bar before considering demo trading.\"")
    print("!" * 80 + "\n")

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "initial_balance": initial_balance,
        "final_balance": round(current_balance, 2),
        "net_profit": round(net_profit, 2),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "regime_bar_counts": regime_bar_counts,
        "regime_stats": regime_stats,
    }


def run_all_forex_backtests(
    pairs: Optional[List[str]] = None,
    timeframes: Optional[List[str]] = None,
    candle_count: int = 1000,
) -> Dict[str, Any]:
    """
    Executes walk-forward backtests across all standard spot forex pairs for 1h and 4h timeframes.
    Caches results into data/backtest_cache.json and prints a consolidated summary table.
    """
    import json
    from pathlib import Path

    if pairs is None:
        pairs = [
            "EUR/USD",
            "GBP/JPY",
            "AUD/CAD",
            "NZD/CHF",
            "USD/COP",
            "GBP/AUD",
            "EUR/CAD",
            "NZD/CAD",
        ]
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

    print("\n" + "=" * 90)
    print("      BATCH FOREX SPOT BACKTEST ENGINE (Yahoo Finance Live Data)")
    print(f"      Pairs Tested: {', '.join(pairs)}")
    print(f"      Timeframes  : {', '.join(timeframes)}")
    print("=" * 90 + "\n")

    for pair in pairs:
        if pair not in cache:
            cache[pair] = {}

        for tf in timeframes:
            print(f"\n>>> Running Backtest: {pair} on {tf} ({candle_count} bars)...")
            try:
                res = run_spot_backtest(symbol=pair, timeframe=tf, candle_count=candle_count)
                if res and "regime_stats" in res:
                    cache[pair][tf] = res["regime_stats"]
                    summary_rows.append({
                        "pair": pair,
                        "timeframe": tf,
                        "trades": res["total_trades"],
                        "win_rate": f"{res['win_rate']:.2f}%",
                        "profit_factor": f"{res['profit_factor']:.2f}",
                        "net_pnl": f"${res['net_profit']:+,.2f}",
                    })
                else:
                    summary_rows.append({
                        "pair": pair,
                        "timeframe": tf,
                        "trades": 0,
                        "win_rate": "N/A",
                        "profit_factor": "N/A",
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
    print("\n" + "=" * 90)
    print("                      FOREX BACKTEST SUMMARY RESULTS TABLE")
    print("=" * 90)
    header = f"{'Pair':<12} | {'Timeframe':<10} | {'Trades':<8} | {'Win Rate':<12} | {'Profit Factor':<14} | {'Net PnL':<14}"
    sep = f"{'-'*12}-+-{'-'*10}-+-{'-'*8}-+-{'-'*12}-+-{'-'*14}-+-{'-'*14}"
    print(header)
    print(sep)
    for r in summary_rows:
        print(f"{r['pair']:<12} | {r['timeframe']:<10} | {r['trades']:<8} | {r['win_rate']:<12} | {r['profit_factor']:<14} | {r['net_pnl']:<14}")
    print("=" * 90 + "\n")

    return cache


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Spot Bot & Forex Backtest Engine")
    parser.add_argument("--symbol", type=str, default=BACKTEST_SYMBOL, help="Trading symbol (BTC/USDT, EUR/USD, etc.)")
    parser.add_argument("--timeframe", type=str, default=BACKTEST_TIMEFRAME, help="Candle interval (1h, 4h, etc.)")
    parser.add_argument("--count", type=int, default=CANDLE_COUNT, help="Number of bars to backtest")
    parser.add_argument("--forex-all", action="store_true", help="Run backtests across all 8 forex pairs on 1h and 4h")

    args = parser.parse_args()

    if args.forex_all:
        run_all_forex_backtests()
    else:
        run_spot_backtest(symbol=args.symbol, timeframe=args.timeframe, candle_count=args.count)
