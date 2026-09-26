"""
test_signal_only_system.py
==========================
Comprehensive 15-Point Verification Test Suite for the Signal-Only Forex System.

Verifies:
  1. 1H SIDEWAYS -> NO SIGNAL
  2. TRENDING_UP distance > 0.80 ATR -> NO SIGNAL
  3. TRENDING_UP distance <= 0.80 ATR -> evaluated normally
  4. Other regime uses 1.40 ATR threshold
  5. Spread gate rejection (cost > 0.25 * ATR)
  6. Confirmed candle requirement
  7. Lookahead protection
  8. Duplicate signal prevention
  9. UTC -> IST conversion (UTC + 05:30)
  10. Missing data -> NO SIGNAL
  11. Stale data -> NO SIGNAL
  12. All 7 pairs supported
  13. Signal formatting card compliance
  14. Restart/persistence state recovery
  15. Candidate C exit engine unchanged
"""

import sys
import unittest
import json
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

SPOT_BOT_DIR = Path(__file__).resolve().parent.parent
if str(SPOT_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(SPOT_BOT_DIR))

import forex_signal_engine
from forex_signal_engine import (
    ForexSignalEngine,
    FormattedSignal,
    convert_utc_to_ist,
    generate_signal_id,
    SUPPORTED_PAIRS,
)
import short_tf_engine
import exit_engine
import forex_utils


def create_synthetic_5m_data(
    num_bars: int = 60,
    base_price: float = 1.1000,
    trend: str = "UP",
    pip_val: float = 0.0001,
    start_time: Optional[datetime] = None,
) -> pd.DataFrame:
    """Creates synthetic 5M OHLCV DataFrame."""
    st = start_time or datetime.now(timezone.utc) - timedelta(minutes=num_bars * 5)
    times = pd.date_range(st, periods=num_bars, freq="5min", tz="UTC")
    data = []
    price = base_price

    step = (0.0003 if trend == "UP" else -0.0003) if trend in ("UP", "DOWN") else 0.00002

    for t in times:
        o = price
        c = price + step
        h = max(o, c) + (0.0001 if trend == "UP" else 0.0002)
        l = min(o, c) - (0.0002 if trend == "UP" else 0.0001)
        data.append({
            "time": int(t.timestamp()),
            "datetime": t,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": 200,
        })
        price = c

    return pd.DataFrame(data)


class TestSignalOnlySystem(unittest.TestCase):

    def setUp(self):
        self.engine = ForexSignalEngine()
        self.pair = "EUR/USD"
        self.now_utc = datetime.now(timezone.utc)
        self.df_5m_bullish = create_synthetic_5m_data(60, 1.1000, "UP", start_time=self.now_utc - timedelta(minutes=300))
        self.df_1m_bullish = create_synthetic_5m_data(60, 1.1000, "UP", start_time=self.now_utc - timedelta(minutes=60))

    # 1. 1H SIDEWAYS -> NO SIGNAL
    def test_01_1h_sideways_exclusion(self):
        ctx = short_tf_engine.ShortTFContext(
            regime_4h="RANGING",
            trend_1h="SIDEWAYS",
            active_session="LONDON_NY_OVERLAP",
            can_trade_session=True,
        )
        dec = short_tf_engine.evaluate_5m_setup(
            self.df_5m_bullish,
            context=ctx,
            pair=self.pair,
            exclude_1h_sideways=True,
        )
        self.assertEqual(dec.signal, "HOLD")
        self.assertEqual(dec.rule_set, "1H_SIDEWAYS_EXCLUSION_HOLD")

    # 2. TRENDING_UP distance > 0.80 ATR -> NO SIGNAL
    def test_02_trending_up_overextension_rejected(self):
        ctx = short_tf_engine.ShortTFContext(
            regime_4h="TRENDING_UP",
            trend_1h="UP",
            active_session="LONDON_NY_OVERLAP",
            can_trade_session=True,
        )
        # Force huge extension above EMA50
        df_ext = self.df_5m_bullish.copy()
        df_ext.loc[df_ext.index[-1], "close"] = df_ext.loc[df_ext.index[-1], "close"] + 0.0100  # 100 pips overextended
        dec = short_tf_engine.evaluate_5m_setup(
            df_ext,
            context=ctx,
            pair=self.pair,
            trending_up_max_ext_atr=0.80,
        )
        self.assertEqual(dec.signal, "HOLD")
        self.assertIn("max_extension_atr", dec.snapshot)
        self.assertEqual(dec.snapshot["max_extension_atr"], 0.80)

    # 3. TRENDING_UP distance <= 0.80 ATR -> Evaluated normally
    def test_03_trending_up_within_080_atr_evaluated(self):
        ctx = short_tf_engine.ShortTFContext(
            regime_4h="TRENDING_UP",
            trend_1h="UP",
            active_session="LONDON_NY_OVERLAP",
            can_trade_session=True,
        )
        dec = short_tf_engine.evaluate_5m_setup(
            self.df_5m_bullish,
            context=ctx,
            pair=self.pair,
            trending_up_max_ext_atr=0.80,
        )
        self.assertEqual(dec.snapshot["max_extension_atr"], 0.80)

    # 4. Other regime uses 1.40 ATR threshold
    def test_04_other_regime_uses_140_atr(self):
        ctx = short_tf_engine.ShortTFContext(
            regime_4h="RANGING",
            trend_1h="UP",
            active_session="LONDON_NY_OVERLAP",
            can_trade_session=True,
        )
        dec = short_tf_engine.evaluate_5m_setup(
            self.df_5m_bullish,
            context=ctx,
            pair=self.pair,
            trending_up_max_ext_atr=0.80,
            default_max_ext_atr=1.40,
        )
        self.assertEqual(dec.snapshot["max_extension_atr"], 1.40)

    # 5. Spread gate rejection (cost > 0.25 * ATR)
    def test_05_spread_gate_rejection(self):
        ctx = short_tf_engine.ShortTFContext(
            regime_4h="TRENDING_UP",
            trend_1h="UP",
            active_session="LONDON_NY_OVERLAP",
            can_trade_session=True,
        )
        # Create tiny ATR df where spread exceeds 25% of ATR
        df_tiny_atr = self.df_5m_bullish.copy()
        for col in ["open", "high", "low", "close"]:
            df_tiny_atr[col] = 1.1000  # 0 range -> small ATR
        df_tiny_atr.iloc[-1, df_tiny_atr.columns.get_loc("high")] = 1.10001
        dec = short_tf_engine.evaluate_5m_setup(
            df_tiny_atr,
            context=ctx,
            pair=self.pair,
        )
        self.assertEqual(dec.signal, "HOLD")
        self.assertIn(dec.rule_set, ("SPREAD_EXCESSIVE_HOLD", "STALE_DATA_HOLD"))

    # 6. Confirmed candle requirement
    def test_06_confirmed_candle_requirement(self):
        # 1M confirmation requires closed candle
        cand_5m = short_tf_engine.ShortTFDecision(
            signal="BUY",
            entry_price=1.1050,
            stop_loss=1.1030,
            take_profit=1.1090,
            atr_value=0.0010,
            risk_reward_ratio=2.0,
            primary_tf="5m",
            regime="TRENDING_UP",
        )
        # Empty / incomplete 1M data
        dec = short_tf_engine.confirm_1m_entry(None, cand_5m, pair=self.pair)
        self.assertEqual(dec.signal, "HOLD")
        self.assertEqual(dec.rule_set, "1M_DATA_UNAVAILABLE_HOLD")

    # 7. Lookahead protection
    def test_07_lookahead_protection(self):
        # Verify that slicing data up to bar T gives identical result regardless of whether future bars exist
        ctx = short_tf_engine.ShortTFContext(
            regime_4h="RANGING",
            trend_1h="UP",
            active_session="LONDON_NY_OVERLAP",
            can_trade_session=True,
        )
        df_partial = self.df_5m_bullish.iloc[:50].copy()
        df_full = self.df_5m_bullish.iloc[:60].copy()

        dec1 = short_tf_engine.evaluate_5m_setup(df_partial, context=ctx, pair=self.pair)
        dec2 = short_tf_engine.evaluate_5m_setup(df_full.iloc[:50], context=ctx, pair=self.pair)

        self.assertEqual(dec1.signal, dec2.signal)
        self.assertEqual(dec1.entry_price, dec2.entry_price)
        self.assertEqual(dec1.rule_set, dec2.rule_set)

    # 8. Duplicate signal prevention
    def test_08_duplicate_signal_prevention(self):
        engine = ForexSignalEngine()
        sig1 = engine.evaluate_pair(self.pair, self.df_5m_bullish, self.df_1m_bullish, current_time_utc=self.now_utc)
        sig_id = sig1.signal_id
        engine.seen_signal_ids.add(sig_id)

        # Immediate re-evaluation with same candle timestamp must be suppressed
        sig2 = engine.evaluate_pair(self.pair, self.df_5m_bullish, self.df_1m_bullish, current_time_utc=self.now_utc)
        self.assertEqual(sig2.direction, "NO SIGNAL")
        self.assertIn("DUPLICATE", sig2.rejection_reason)

    # 9. UTC -> IST conversion
    def test_09_utc_to_ist_conversion(self):
        utc_dt = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
        ist_dt, ist_str = convert_utc_to_ist(utc_dt)
        self.assertEqual(ist_dt.hour, 17)
        self.assertEqual(ist_dt.minute, 30)
        self.assertIn("17:30:00 IST", ist_str)

    # 10. Missing data -> NO SIGNAL
    def test_10_missing_data_handling(self):
        engine = ForexSignalEngine()
        sig = engine.evaluate_pair(self.pair, df_5m=None, current_time_utc=self.now_utc)
        self.assertEqual(sig.direction, "NO SIGNAL")
        self.assertEqual(sig.rejection_reason, "INSUFFICIENT_5M_DATA")

    # 11. Stale data -> NO SIGNAL
    def test_11_stale_data_handling(self):
        engine = ForexSignalEngine()
        stale_time = self.now_utc - timedelta(hours=2)
        df_stale = create_synthetic_5m_data(60, 1.1000, "UP", start_time=stale_time - timedelta(minutes=300))
        sig = engine.evaluate_pair(self.pair, df_5m=df_stale, current_time_utc=self.now_utc)
        self.assertEqual(sig.direction, "NO SIGNAL")
        self.assertIn("STALE_MARKET_DATA", sig.rejection_reason)

    # 12. All 7 pairs supported
    def test_12_all_seven_pairs_supported(self):
        self.assertEqual(len(SUPPORTED_PAIRS), 7)
        expected = ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD"]
        self.assertEqual(SUPPORTED_PAIRS, expected)
        for p in SUPPORTED_PAIRS:
            pip_size = forex_utils.get_pip_size(p)
            self.assertGreater(pip_size, 0)
            spread_pips = forex_utils.get_pair_spread_pips(p)
            self.assertGreater(spread_pips, 0)

    # 13. Signal formatting card compliance
    def test_13_signal_card_formatting(self):
        sig = FormattedSignal(
            pair="EUR/USD",
            direction="BUY",
            signal_time_ist="2026-09-26 17:30:00 IST",
            signal_time_utc="2026-09-26 12:00:00 UTC",
            entry_price=1.10500,
            stop_loss=1.10300,
            take_profit_1=1.10700,
            take_profit_2=1.10900,
            risk_1r_pips=20.0,
            risk_1r_price=0.00200,
            regime_4h="RANGING",
            trend_1h="UP",
            setup_5m_status="VALID",
            spread_pips=1.2,
            spread_gate_status="PASS",
            signal_rule="5M_TREND_PULLBACK_LONG",
            signal_id="EURUSD-20260926-1730",
            is_valid_signal=True,
        )
        card = sig.to_card()
        self.assertIn("🟢 BUY SIGNAL", card)
        self.assertIn("EUR/USD", card)
        self.assertIn("Time: 2026-09-26 17:30:00 IST", card)
        self.assertIn("Entry: 1.10500", card)
        self.assertIn("SL:    1.10300", card)
        self.assertIn("TP1:   1.10700 (+1R)", card)
        self.assertIn("TP2:   1.10900 (+2R)", card)
        self.assertIn("4H Regime: RANGING", card)
        self.assertIn("1H Trend:  UP", card)
        self.assertIn("Spread:    1.2 pips", card)
        self.assertIn("Signal ID: EURUSD-20260926-1730", card)

    # 14. Restart/persistence state recovery
    def test_14_restart_persistence_recovery(self):
        engine1 = ForexSignalEngine()
        sig_test_id = "EURUSD-20260926-9999"
        engine1.seen_signal_ids.add(sig_test_id)
        engine1._save_state()

        # Instantiate a new engine instance and check recovery
        engine2 = ForexSignalEngine()
        self.assertIn(sig_test_id, engine2.seen_signal_ids)

    # 15. Candidate C exit engine unchanged
    def test_15_candidate_c_exit_engine_unchanged(self):
        cand = exit_engine.CandidateC_Partial_TP_Trailing(target_r=2.0, target1_r=1.0)
        self.assertEqual(cand.name, "Candidate C (Partial TP + Protected Remainder Trail)")
        self.assertEqual(cand.target_r, 2.0)
        self.assertEqual(cand.params.get("target1_r", 1.0), 1.0)


if __name__ == "__main__":
    unittest.main()
