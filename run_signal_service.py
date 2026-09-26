"""
run_signal_service.py
=====================
Production Signal-Only Runner & Real-Time Shadow Service.
Strictly Read-Only Observer:
  - Scans all 7 major currency pairs in real-time or historical streaming mode.
  - Displays human-readable signal cards with IST timestamps.
  - Continuously monitors system health, feed freshness, and spread gates.
  - Persists signals and duplicate-prevention state.
  - ⚠️ ZERO order execution. ZERO broker logins. ZERO trading keys.
"""

import os
import sys
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

spot_bot_dir = Path(__file__).resolve().parent
if str(spot_bot_dir) not in sys.path:
    sys.path.insert(0, str(spot_bot_dir))

import forex_utils
import forex_data_feed
import forex_signal_engine
from forex_signal_engine import get_signal_engine, SUPPORTED_PAIRS, convert_utc_to_ist
from logger import get_logger

logger = get_logger("signal_service")


def scan_all_pairs(engine: forex_signal_engine.ForexSignalEngine, verbose: bool = True) -> int:
    """Scans all 7 pairs and prints signal cards."""
    now_utc = datetime.now(timezone.utc)
    _, now_ist_str = convert_utc_to_ist(now_utc)

    if verbose:
        print("=" * 80)
        print(f"SPOT FOREX SIGNAL MONITOR · {now_ist_str} · {now_utc.strftime('%H:%M:%S UTC')}")
        print("MODE: SIGNAL-ONLY (MANUAL EXECUTION ONLY · ZERO BROKER TRADING)")
        print("=" * 80)

    valid_signals_found = 0
    latest_candle_time = None

    for pair in SUPPORTED_PAIRS:
        try:
            # Fetch latest 5M and 1M data causally via forex_data_feed
            # Fetch last 100 bars for complete indicator lookback
            df_5m = forex_data_feed.fetch_forex_candles(pair, "5m", count=100)
            df_1m = forex_data_feed.fetch_forex_candles(pair, "1m", count=100)
            df_15m = forex_data_feed.fetch_forex_candles(pair, "15m", count=100)
            df_1h = forex_data_feed.fetch_forex_candles(pair, "1h", count=100)
            df_4h = forex_data_feed.fetch_forex_candles(pair, "4h", count=100)

            if df_5m is not None and not df_5m.empty:
                last_dt_raw = df_5m.iloc[-1].get("datetime")
                if last_dt_raw is not None:
                    last_dt = pd.to_datetime(last_dt_raw, utc=True).to_pydatetime()
                    if latest_candle_time is None or last_dt > latest_candle_time:
                        latest_candle_time = last_dt

            # Evaluate signal causally
            t_eval_start = time.time()
            signal = engine.evaluate_pair(
                pair=pair,
                df_5m=df_5m,
                df_1m=df_1m,
                df_15m=df_15m,
                df_1h=df_1h,
                df_4h=df_4h,
                current_time_utc=now_utc,
                simulated_latency_ms=(time.time() - t_eval_start) * 1000.0,
            )

            if signal.is_valid_signal:
                valid_signals_found += 1
                if verbose:
                    print("\n" + signal.to_card())
            else:
                if verbose:
                    print(f"[{pair:<7}] ⚪ NO SIGNAL | Reason: {signal.rejection_reason} | 1H: {signal.trend_1h} | 4H: {signal.regime_4h} | Spread: {signal.spread_pips:.1f}p")

        except Exception as e:
            logger.error(f"Error evaluating {pair}: {e}")
            if verbose:
                print(f"[{pair:<7}] ⚠️ ERROR: {e}")

    engine.update_health(last_candle_utc=latest_candle_time, is_connected=True)
    health = engine.get_health_snapshot()

    if verbose:
        print("\n" + "-" * 80)
        print(f"SYSTEM STATUS: {health['system_status']} | Feed: {health['data_connection_status']} | Last Candle: {health['last_candle_time_ist']}")
        print(f"Signals Generated: {health['signals_count']} | No-Signal Count: {health['no_signal_count']} | Uptime: {health['system_uptime_seconds']}s")
        print("-" * 80 + "\n")

    return valid_signals_found


def replay_historical_window(engine: forex_signal_engine.ForexSignalEngine, num_bars: int = 50):
    """Replays verified historical/shadow data for testing and offline signal inspection."""
    print("=" * 80)
    print(f"HISTORICAL / SHADOW REPLAY MODE (Evaluating {num_bars} 5M Candles)")
    print("=" * 80)

    DATA_DIR = spot_bot_dir / "data"
    for pair in SUPPORTED_PAIRS:
        clean = pair.replace("/", "_")
        csv_5m = DATA_DIR / f"forex_{clean}_5m_17000.csv"
        csv_1m = DATA_DIR / f"forex_{clean}_1m_10000.csv"
        csv_15m = DATA_DIR / f"forex_{clean}_15m_6000.csv"
        csv_1h = DATA_DIR / f"forex_{clean}_1h_6000.csv"
        csv_4h = DATA_DIR / f"forex_{clean}_4h_1500.csv"

        if not csv_5m.exists():
            continue

        df_5m_all = pd.read_csv(csv_5m)
        df_5m_all["datetime"] = pd.to_datetime(df_5m_all["datetime"], utc=True)
        df_1m_all = pd.read_csv(csv_1m) if csv_1m.exists() else df_5m_all
        df_1m_all["datetime"] = pd.to_datetime(df_1m_all["datetime"], utc=True)
        df_15m_all = pd.read_csv(csv_15m) if csv_15m.exists() else df_5m_all
        df_15m_all["datetime"] = pd.to_datetime(df_15m_all["datetime"], utc=True)
        df_1h_all = pd.read_csv(csv_1h) if csv_1h.exists() else df_5m_all
        df_1h_all["datetime"] = pd.to_datetime(df_1h_all["datetime"], utc=True)
        df_4h_all = pd.read_csv(csv_4h) if csv_4h.exists() else df_5m_all
        df_4h_all["datetime"] = pd.to_datetime(df_4h_all["datetime"], utc=True)

        # Slice last num_bars
        df_5m_slice = df_5m_all.tail(num_bars).reset_index(drop=True)
        last_dt = df_5m_slice.iloc[-1]["datetime"].to_pydatetime()

        sig = engine.evaluate_pair(
            pair=pair,
            df_5m=df_5m_slice,
            df_1m=df_1m_all.tail(num_bars * 5).reset_index(drop=True),
            df_15m=df_15m_all.tail(num_bars).reset_index(drop=True),
            df_1h=df_1h_all.tail(num_bars).reset_index(drop=True),
            df_4h=df_4h_all.tail(num_bars).reset_index(drop=True),
            current_time_utc=last_dt,
        )

        if sig.is_valid_signal:
            print("\n" + sig.to_card())
        else:
            print(f"[{pair:<7}] ⚪ NO SIGNAL | Reason: {sig.rejection_reason} | 1H: {sig.trend_1h} | 4H: {sig.regime_4h} | Time (IST): {sig.signal_time_ist}")

    print("\n" + "=" * 80)
    print("Replay evaluation complete.")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="Forex Signal-Only Live Monitoring Service")
    parser.add_argument("--once", action="store_true", help="Run a single scan iteration and exit")
    parser.add_argument("--replay", action="store_true", help="Replay historical/shadow data slice")
    parser.add_argument("--bars", type=int, default=60, help="Number of bars for replay (default: 60)")
    parser.add_argument("--interval", type=int, default=60, help="Scan interval in seconds (default: 60)")
    args = parser.parse_args()

    engine = get_signal_engine()
    print("Initializing Forex Signal-Only Service...")
    print("Governance: Broker trading disabled. Pure signal notification mode.")

    if args.replay:
        replay_historical_window(engine, num_bars=args.bars)
        return

    if args.once:
        scan_all_pairs(engine, verbose=True)
        return

    try:
        while True:
            scan_all_pairs(engine, verbose=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopping Forex Signal-Only Service gracefully.")


if __name__ == "__main__":
    main()
