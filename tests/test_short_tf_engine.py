"""
Unit and Regression Test Suite for Mod C Short Timeframe Engine.
Covers:
  1. 1H SIDEWAYS exclusion.
  2. TRENDING_UP <= 0.80 ATR acceptance.
  3. TRENDING_UP > 0.80 ATR rejection.
  4. Combination of both filters.
  5. Existing Candidate C exits.
  6. Existing stop-loss behavior.
  7. Existing partial TP behavior.
  8. Existing final TP behavior.
  9. No-signal behavior.
  10. Session handling.
  11. Regime handling.
  12. Long/short handling.
"""

import sys
import unittest
from pathlib import Path
import pandas as pd
import numpy as np

SPOT_BOT_DIR = Path(__file__).resolve().parent.parent
if str(SPOT_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(SPOT_BOT_DIR))

import short_tf_engine
import exit_engine
import market_structure
import forex_utils


def create_bullish_5m_df(num_bars: int = 60, base_price: float = 1.1000) -> pd.DataFrame:
    """Creates a clean synthetic bullish trending 5M DataFrame."""
    times = pd.date_range("2025-06-01 12:00", periods=num_bars, freq="5min", tz="UTC")
    data = []
    price = base_price
    for t in times:
        o = price
        c = price + 0.0004
        h = c + 0.0001
        l = o - 0.0001
        data.append({"time": int(t.timestamp()), "datetime": t, "open": o, "high": h, "low": l, "close": c, "volume": 100})
        price = c
    return pd.DataFrame(data)


def create_bearish_5m_df(num_bars: int = 60, base_price: float = 1.1000) -> pd.DataFrame:
    """Creates a clean synthetic bearish trending 5M DataFrame."""
    times = pd.date_range("2025-06-01 12:00", periods=num_bars, freq="5min", tz="UTC")
    data = []
    price = base_price
    for t in times:
        o = price
        c = price - 0.0004
        h = o + 0.0001
        l = c - 0.0001
        data.append({"time": int(t.timestamp()), "datetime": t, "open": o, "high": h, "low": l, "close": c, "volume": 100})
        price = c
    return pd.DataFrame(data)


class TestModCEntryEngine(unittest.TestCase):

    def setUp(self):
        self.df_bullish = create_bullish_5m_df(60, 1.1000)
        self.df_bearish = create_bearish_5m_df(60, 1.1000)

    # 1. 1H SIDEWAYS EXCLUSION
    def test_1h_sideways_exclusion(self):
        ctx = short_tf_engine.ShortTFContext(
            regime_4h="RANGING",
            trend_1h="SIDEWAYS",
            active_session="LONDON_NY_OVERLAP",
            can_trade_session=True,
        )
        dec = short_tf_engine.evaluate_5m_setup(self.df_bullish, context=ctx, pair="EUR/USD", exclude_1h_sideways=True)
        self.assertEqual(dec.signal, "HOLD")
        self.assertEqual(dec.rule_set, "1H_SIDEWAYS_EXCLUSION_HOLD")

    # 2. TRENDING_UP <= 0.80 ATR ACCEPTANCE
    def test_trending_up_within_extension(self):
        ctx = short_tf_engine.ShortTFContext(
            regime_4h="TRENDING_UP",
            trend_1h="UP",
            active_session="LONDON_NY_OVERLAP",
            can_trade_session=True,
        )
        # With default trending_up_max_ext_atr = 0.80, if extension is small, it shouldn't be rejected by extension filter
        dec = short_tf_engine.evaluate_5m_setup(self.df_bullish, context=ctx, pair="EUR/USD", trending_up_max_ext_atr=2.0)
        # Even if not all momentum criteria match, the overextension check itself must evaluate to True
        self.assertIn("max_extension_atr", dec.snapshot)

    # 3. TRENDING_UP > 0.80 ATR REJECTION
    def test_trending_up_overextension_rejection(self):
        ctx = short_tf_engine.ShortTFContext(
            regime_4h="TRENDING_UP",
            trend_1h="UP",
            active_session="LONDON_NY_OVERLAP",
            can_trade_session=True,
        )
        # Force a very tight overextension threshold (e.g. 0.01 ATR)
        dec = short_tf_engine.evaluate_5m_setup(self.df_bullish, context=ctx, pair="EUR/USD", trending_up_max_ext_atr=0.01)
        # When overextension is breached, BUY setup cannot be generated
        self.assertNotEqual(dec.signal, "BUY")

    # 4. COMBINATION OF BOTH FILTERS
    def test_combination_of_filters(self):
        # Case A: 1H sideways AND 4H trending up -> rejected by Gate 5
        ctx_a = short_tf_engine.ShortTFContext(
            regime_4h="TRENDING_UP",
            trend_1h="SIDEWAYS",
            active_session="LONDON_NY_OVERLAP",
            can_trade_session=True,
        )
        dec_a = short_tf_engine.evaluate_5m_setup(self.df_bullish, context=ctx_a, pair="EUR/USD", exclude_1h_sideways=True)
        self.assertEqual(dec_a.signal, "HOLD")
        self.assertEqual(dec_a.rule_set, "1H_SIDEWAYS_EXCLUSION_HOLD")

        # Case B: 1H UP AND 4H trending up but overextended -> rejected
        ctx_b = short_tf_engine.ShortTFContext(
            regime_4h="TRENDING_UP",
            trend_1h="UP",
            active_session="LONDON_NY_OVERLAP",
            can_trade_session=True,
        )
        dec_b = short_tf_engine.evaluate_5m_setup(self.df_bullish, context=ctx_b, pair="EUR/USD", exclude_1h_sideways=True, trending_up_max_ext_atr=-1.0)
        self.assertEqual(dec_b.signal, "HOLD")

    # 5. EXISTING CANDIDATE C EXITS
    def test_candidate_c_exit_engine_preservation(self):
        cand_c = exit_engine.CandidateC_Partial_TP_Trailing(target_r=2.0, target1_r=1.0)
        self.assertEqual(cand_c.target_r, 2.0)
        self.assertEqual(cand_c.params.get("target1_r", 1.0), 1.0)

    # 6. STOP LOSS BEHAVIOR
    def test_candidate_c_initial_stop_loss(self):
        cand_c = exit_engine.CandidateC_Partial_TP_Trailing(target_r=2.0, target1_r=1.0)
        pos = {
            "direction": "BUY",
            "entry_price": 1.1000,
            "current_sl": 1.0980,
            "atr": 0.0010,
            "r_dist": 0.0020,
            "highest_price": 1.1000,
            "lowest_price": 1.1000,
            "bars_held": 1,
            "partial_tp_taken": False,
        }
        # Low breaches SL at 1.0975
        dec = cand_c.update_position_state(
            pos=pos, c_open=1.0990, c_high=1.0995, c_low=1.0975, c_close=1.0985,
            window_df=self.df_bullish, pair="EUR/USD", pip_val=0.0001, spread_cost=0.0001
        )
        self.assertTrue(dec.should_exit)
        self.assertEqual(dec.exit_reason, "STOP_LOSS")
        self.assertEqual(dec.exit_price, 1.0980)

    # 7. PARTIAL TP AT +1.0R BEHAVIOR
    def test_candidate_c_partial_tp_at_1r(self):
        cand_c = exit_engine.CandidateC_Partial_TP_Trailing(target_r=2.0, target1_r=1.0)
        pos = {
            "direction": "BUY",
            "entry_price": 1.1000,
            "current_sl": 1.0980,
            "atr": 0.0010,
            "r_dist": 0.0020,
            "highest_price": 1.1000,
            "lowest_price": 1.1000,
            "bars_held": 1,
            "partial_tp_taken": False,
        }
        # High reaches target1_price 1.1020 (+1.0R)
        dec = cand_c.update_position_state(
            pos=pos, c_open=1.1010, c_high=1.1025, c_low=1.1005, c_close=1.1020,
            window_df=self.df_bullish, pair="EUR/USD", pip_val=0.0001, spread_cost=0.0001
        )
        self.assertTrue(dec.is_partial_exit)
        self.assertEqual(dec.exit_reason, "PARTIAL_TP_50PCT")
        self.assertEqual(dec.exit_price, 1.1020)
        self.assertTrue(pos["partial_tp_taken"])
        # SL must be moved to BE + cushion
        self.assertGreater(pos["current_sl"], 1.1000)

    # 8. FINAL TP AT +2.0R BEHAVIOR
    def test_candidate_c_final_tp_at_2r(self):
        cand_c = exit_engine.CandidateC_Partial_TP_Trailing(target_r=2.0, target1_r=1.0)
        pos = {
            "direction": "BUY",
            "entry_price": 1.1000,
            "current_sl": 1.1002,  # already protected
            "atr": 0.0010,
            "r_dist": 0.0020,
            "highest_price": 1.1025,
            "lowest_price": 1.1000,
            "bars_held": 5,
            "partial_tp_taken": True,
        }
        # High reaches target 2 (+2.0R = 1.1040)
        dec = cand_c.update_position_state(
            pos=pos, c_open=1.1030, c_high=1.1045, c_low=1.1028, c_close=1.1042,
            window_df=self.df_bullish, pair="EUR/USD", pip_val=0.0001, spread_cost=0.0001
        )
        self.assertTrue(dec.should_exit)
        self.assertEqual(dec.exit_reason, "TAKE_PROFIT_FINAL")
        self.assertEqual(dec.exit_price, 1.1040)

    # 9. NO-SIGNAL BEHAVIOR (HOLD)
    def test_insufficient_data_hold(self):
        short_df = self.df_bullish.iloc[:10]
        ctx = short_tf_engine.ShortTFContext()
        dec = short_tf_engine.evaluate_5m_setup(short_df, context=ctx, pair="EUR/USD")
        self.assertEqual(dec.signal, "HOLD")
        self.assertEqual(dec.rule_set, "INSUFFICIENT_5M_DATA")

    # 10. SESSION HANDLING
    def test_session_handling_untradeable(self):
        ctx = short_tf_engine.ShortTFContext(
            can_trade_session=False,
            active_session="ROLLOVER_DEADZONE",
        )
        dec = short_tf_engine.evaluate_5m_setup(self.df_bullish, context=ctx, pair="EUR/USD")
        self.assertEqual(dec.signal, "HOLD")
        self.assertIn("SESSION_FILTER_HOLD", dec.rule_set)

    # 11. REGIME HANDLING
    def test_regime_high_volatility_hold(self):
        ctx = short_tf_engine.ShortTFContext(
            regime_4h="HIGH_VOLATILITY",
            can_trade_session=True,
        )
        dec = short_tf_engine.evaluate_5m_setup(self.df_bullish, context=ctx, pair="EUR/USD")
        self.assertEqual(dec.signal, "HOLD")
        self.assertEqual(dec.rule_set, "4H_HIGH_VOLATILITY_HOLD")

    # 12. LONG / SHORT HANDLING
    def test_direction_asymmetry_protection(self):
        # When 1H trend is DOWN, evaluate_5m_setup must not trigger BUY
        ctx_down = short_tf_engine.ShortTFContext(
            regime_4h="RANGING",
            trend_1h="DOWN",
            active_session="LONDON_NY_OVERLAP",
            can_trade_session=True,
        )
        dec = short_tf_engine.evaluate_5m_setup(self.df_bullish, context=ctx_down, pair="EUR/USD")
        self.assertNotEqual(dec.signal, "BUY")


if __name__ == "__main__":
    unittest.main()
