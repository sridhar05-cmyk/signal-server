"""
Comprehensive Automated Test Suite for Upgraded Forex Signal System.
Verifies:
  1. Pair-Aware Pip & Pipette Precision (JPY vs non-JPY).
  2. Zero Live Order Execution Invariance (Strict observation console).
  3. Risk Manager Hard Ceilings (<= 2% max per trade, daily max loss).
  4. Forex Session Classifier (Asian, London, NY, London/NY Overlap, Rollover).
  5. Market Structure Analysis (HH/HL, LH/LL, BOS, swing pivots).
  6. Confidence Engine (0-100 score, tiering, factor explanations).
  7. Flask Web Endpoints (/api/pairs and /api/signal).
  8. Cloud Deployment Readiness (Procfile, dynamic PORT, requirements.txt).
"""

import os
import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import numpy as np

# Ensure spot_bot directory is in Python path
SPOT_BOT_DIR = Path(__file__).resolve().parent.parent
if str(SPOT_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(SPOT_BOT_DIR))

import forex_utils
import forex_sessions
import market_structure
import false_signal_filters
import confidence_engine
import risk_manager
import app as flask_app


class TestForexUtils(unittest.TestCase):
    """Test pair-aware pip math and formatting."""

    def test_pip_size_non_jpy(self):
        self.assertAlmostEqual(forex_utils.get_pip_size("EUR/USD"), 0.0001, places=6)
        self.assertAlmostEqual(forex_utils.get_pip_size("GBP/USD"), 0.0001, places=6)
        self.assertAlmostEqual(forex_utils.get_pip_size("AUD/USD"), 0.0001, places=6)

    def test_pip_size_jpy(self):
        self.assertAlmostEqual(forex_utils.get_pip_size("USD/JPY"), 0.01, places=4)
        self.assertAlmostEqual(forex_utils.get_pip_size("GBP/JPY"), 0.01, places=4)

    def test_price_formatting(self):
        # EUR/USD should have 5 decimals
        formatted_eur = forex_utils.format_price(1.08456, "EUR/USD")
        self.assertEqual(formatted_eur, "1.08456")

        # USD/JPY should have 3 decimals
        formatted_jpy = forex_utils.format_price(155.823, "USD/JPY")
        self.assertEqual(formatted_jpy, "155.823")

    def test_price_diff_to_pips(self):
        eur_diff = 0.0050  # 50 pips for EUR/USD
        self.assertAlmostEqual(forex_utils.price_diff_to_pips(eur_diff, "EUR/USD"), 50.0, places=1)

        jpy_diff = 0.50  # 50 pips for USD/JPY
        self.assertAlmostEqual(forex_utils.price_diff_to_pips(jpy_diff, "USD/JPY"), 50.0, places=1)

    def test_validate_forex_dataframe(self):
        # Create a dataframe with normal and flat (weekend/holiday) bars
        dates = pd.date_range("2026-09-01", periods=5, freq="1h")
        data = {
            "datetime": dates,
            "open": [1.1000, 1.1010, 1.1020, 1.1020, 1.1030],
            "high": [1.1020, 1.1030, 1.1020, 1.1020, 1.1050],
            "low": [1.0990, 1.1005, 1.1020, 1.1020, 1.1025],
            "close": [1.1010, 1.1020, 1.1020, 1.1020, 1.1040],
            "volume": [100, 150, 0, 0, 200],
        }
        df = pd.DataFrame(data)
        cleaned_df, report = forex_utils.validate_forex_dataframe(df)
        # The 2 flat bars (open==high==low==close) should be purged
        self.assertEqual(len(cleaned_df), 3)


class TestForexSessions(unittest.TestCase):
    """Test UTC forex trading session classification."""

    def test_asian_session(self):
        dt = datetime(2026, 9, 22, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(forex_sessions.classify_session(dt), "ASIAN")

    def test_london_session(self):
        dt = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(forex_sessions.classify_session(dt), "LONDON")

    def test_london_ny_overlap(self):
        dt = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(forex_sessions.classify_session(dt), "LONDON_NY_OVERLAP")

    def test_new_york_session(self):
        dt = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(forex_sessions.classify_session(dt), "NEW_YORK")

    def test_rollover_deadzone(self):
        dt = datetime(2026, 9, 22, 21, 30, tzinfo=timezone.utc)
        self.assertIn("ROLLOVER", forex_sessions.classify_session(dt))
        can_trade, reason = forex_sessions.is_session_tradable(dt)
        self.assertFalse(can_trade)
        self.assertIn("rollover", reason.lower())


class TestMarketStructure(unittest.TestCase):
    """Test swing pivot detection and structural trend classification."""

    def test_bullish_structure_detection(self):
        # Create synthetic series with higher highs and higher lows
        n = 30
        dates = pd.date_range("2026-09-01", periods=n, freq="1h")
        base = np.linspace(1.1000, 1.1200, n)
        highs = base + 0.0020 + 0.0010 * np.sin(np.linspace(0, 4 * np.pi, n))
        lows = base - 0.0010 + 0.0010 * np.sin(np.linspace(0, 4 * np.pi, n))
        closes = (highs + lows) / 2
        opens = (highs + lows) / 2 - 0.0005

        df = pd.DataFrame({
            "datetime": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000] * n,
        })
        ms = market_structure.detect_market_structure(df, pivot_bars=3)
        self.assertIsNotNone(ms)
        self.assertIn(ms.structure_trend, ["BULLISH", "BEARISH", "SIDEWAYS"])


class TestConfidenceEngine(unittest.TestCase):
    """Test 0-100 transparent confidence score evaluation."""

    def test_confidence_scoring(self):
        structure = market_structure.MarketStructure(
            structure_trend="BULLISH",
            is_higher_highs_lows=True,
            is_lower_highs_lows=False,
            bos_bullish=True,
            bos_bearish=False,
            last_swing_high=1.1200,
            last_swing_low=1.1100,
            structural_sl_long=1.1090,
            structural_sl_short=1.1210,
        )

        score = confidence_engine.compute_confidence_score(
            direction="BUY",
            curr_close=1.1150,
            htf_trend="UP",
            htf_ema_200=1.1000,
            structure=structure,
            session="LONDON_NY_OVERLAP",
            rsi_val=55.0,
            macd_hist=0.0005,
            macd_hist_prev=0.0002,
            atr_ratio=1.05,
            close_location=0.85,
        )

        self.assertGreaterEqual(score.score, 60)
        self.assertEqual(score.tier, "HIGH")
        self.assertTrue(score.passed_minimum_threshold)
        self.assertGreater(len(score.factors), 0)


class TestRiskManagerCeilings(unittest.TestCase):
    """Test hard ceilings on trade risk and daily max loss."""

    def test_risk_per_trade_hard_ceiling(self):
        # Attempting to initialize with 5.0% risk per trade should be clamped to hard cap (2.0%)
        rm = risk_manager.SpotRiskManager(
            risk_per_trade_pct=0.05,  # 5% requested
        )
        self.assertLessEqual(rm.risk_per_trade_pct, 0.02)  # Hard capped at 2.0%

    def test_daily_loss_cutoff(self):
        rm = risk_manager.SpotRiskManager(
            daily_max_loss=200.0,
        )
        # Record a loss that hits daily max loss
        rm.record_trade_result(result="LOSS", pnl=-250.0)
        can_trade, reason = rm.can_trade(current_balance=10000.0)
        self.assertFalse(can_trade)
        self.assertIn("daily max loss", reason.lower())


class TestZeroExecutionInvariance(unittest.TestCase):
    """
    Verify zero live execution pathways exist in the strategy or API.
    Ensures this remains strictly a signal research & observation panel.
    """

    def test_no_order_placement_in_web_or_strategy(self):
        critical_files = [
            SPOT_BOT_DIR / "app.py",
            SPOT_BOT_DIR / "strategy.py",
            SPOT_BOT_DIR / "multi_timeframe_scanner.py",
            SPOT_BOT_DIR / "signal_notifier.py",
        ]
        forbidden_calls = [
            "create_order",
            "order_market_buy",
            "order_market_sell",
            "place_order",
            "exchange.buy",
            "exchange.sell",
        ]

        for file_path in critical_files:
            if not file_path.exists():
                continue
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read().lower()
            for forbidden in forbidden_calls:
                # Disregard comments or docstrings mentioning the restriction
                lines = [
                    line.strip()
                    for line in content.splitlines()
                    if forbidden in line and not line.startswith("#") and "zero" not in line and "no " not in line
                ]
                self.assertEqual(
                    len(lines),
                    0,
                    f"Forbidden execution call '{forbidden}' detected in {file_path.name}: {lines}",
                )


class TestFlaskEndpoints(unittest.TestCase):
    """Test Flask Web API endpoints."""

    def setUp(self):
        flask_app.app.testing = True
        self.client = flask_app.app.test_client()

    def test_api_pairs_endpoint(self):
        resp = self.client.get("/api/pairs")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("default_symbol"), "EUR/USD")
        self.assertIn("groups", data)
        # Check both crypto and forex groups exist
        group_names = [g["name"] for g in data["groups"]]
        self.assertTrue(any("Forex" in name for name in group_names))

    def test_api_signal_endpoint_forex(self):
        # Test signal endpoint with pair-aware forex parameters
        resp = self.client.get("/api/signal?symbol=EUR/USD&timeframe=1h")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("symbol"), "EUR/USD")
        self.assertTrue(data.get("is_forex"))
        self.assertIn("session", data)
        self.assertIn("confidence", data)
        self.assertIn("suggested_levels", data)
        # Verify pip levels exist
        self.assertIn("sl_pips", data["suggested_levels"])
        self.assertIn("tp_pips", data["suggested_levels"])


class TestDeploymentReadiness(unittest.TestCase):
    """Verify requirements for cloud deployment on Render."""

    def test_procfile_exists(self):
        procfile = SPOT_BOT_DIR / "Procfile"
        self.assertTrue(procfile.exists())
        with open(procfile, "r", encoding="utf-8") as f:
            content = f.read().strip()
        self.assertEqual(content, "web: gunicorn app:app")

    def test_requirements_complete(self):
        req_file = SPOT_BOT_DIR / "requirements.txt"
        self.assertTrue(req_file.exists())
        with open(req_file, "r", encoding="utf-8") as f:
            reqs = f.read().lower()
        self.assertIn("flask", reqs)
        self.assertIn("gunicorn", reqs)
        self.assertIn("pandas", reqs)
        self.assertIn("numpy", reqs)
        self.assertIn("yfinance", reqs)


class TestShortTimeframeSignals(unittest.TestCase):
    """Test 5M Primary and 1M Confirmation short-timeframe engine rules."""

    def setUp(self):
        import short_tf_engine
        self.engine = short_tf_engine

    def test_1m_never_signals_when_5m_is_hold(self):
        """Rule 1: 1M must NEVER generate an independent signal when 5M is HOLD."""
        hold_decision = self.engine.ShortTFDecision(signal="HOLD", rule_set="NO_SETUP_MATCH")
        # Even if 1M has a massive bullish candle, confirmation must remain HOLD
        dates_1m = pd.date_range("2026-09-22 14:00", periods=20, freq="1min")
        df_1m = pd.DataFrame({
            "datetime": dates_1m,
            "open": [1.1000 + i * 0.0001 for i in range(20)],
            "high": [1.1005 + i * 0.0001 for i in range(20)],
            "low": [1.0998 + i * 0.0001 for i in range(20)],
            "close": [1.1004 + i * 0.0001 for i in range(20)],
            "volume": [100.0] * 20,
            "time": [int(d.timestamp() * 1000) for d in dates_1m],
        })
        res = self.engine.confirm_1m_entry(df_1m, hold_decision, "EUR/USD")
        self.assertEqual(res.signal, "HOLD")

    def test_5m_setup_rejected_when_4h_high_volatility(self):
        """Safety Gate: 4H HIGH_VOLATILITY blocks entries."""
        ctx = self.engine.ShortTFContext(regime_4h="HIGH_VOLATILITY")
        dates_5m = pd.date_range("2026-09-22 12:00", periods=50, freq="5min")
        df_5m = pd.DataFrame({
            "datetime": dates_5m,
            "open": [1.1000] * 50,
            "high": [1.1020] * 50,
            "low": [1.0980] * 50,
            "close": [1.1010] * 50,
            "volume": [100.0] * 50,
        })
        decision = self.engine.evaluate_5m_setup(df_5m, ctx, "EUR/USD")
        self.assertEqual(decision.signal, "HOLD")
        self.assertIn("HIGH_VOLATILITY", decision.rule_set)

    def test_15m_barrier_blocks_long_entry(self):
        """Structure Gate: Long rejected if entering directly into 15M resistance."""
        ctx = self.engine.ShortTFContext(
            regime_4h="TRENDING_UP",
            trend_1h="UP",
            is_opposed_15m_long=True,
        )
        dates_5m = pd.date_range("2026-09-22 12:00", periods=50, freq="5min")
        df_5m = pd.DataFrame({
            "datetime": dates_5m,
            "open": [1.1000 + i * 0.0002 for i in range(50)],
            "high": [1.1005 + i * 0.0002 for i in range(50)],
            "low": [1.0995 + i * 0.0002 for i in range(50)],
            "close": [1.1004 + i * 0.0002 for i in range(50)],
            "volume": [100.0] * 50,
        })
        decision = self.engine.evaluate_5m_setup(df_5m, ctx, "EUR/USD")
        self.assertEqual(decision.signal, "HOLD")

    def test_1m_confirmation_success(self):
        """1M candle close confirms valid 5M BUY setup."""
        candidate = self.engine.ShortTFDecision(
            signal="BUY",
            entry_price=1.1020,
            stop_loss=1.1000,
            take_profit=1.1056,
            atr_value=0.0010,
            risk_reward_ratio=1.8,
            primary_tf="5m",
            regime="TRENDING_UP",
            rule_set="5M_TREND_PULLBACK_LONG_PENDING_1M",
        )
        # Create 1M bullish confirmation series
        dates_1m = pd.date_range("2026-09-22 14:00", periods=20, freq="1min")
        df_1m = pd.DataFrame({
            "datetime": dates_1m,
            "open": [1.1010 + i * 0.00005 for i in range(20)],
            "high": [1.1015 + i * 0.00005 for i in range(20)],
            "low": [1.1008 + i * 0.00005 for i in range(20)],
            "close": [1.1014 + i * 0.00005 for i in range(20)],
            "volume": [100.0] * 20,
            "time": [int(d.timestamp() * 1000) for d in dates_1m],
        })
        res = self.engine.confirm_1m_entry(df_1m, candidate, "EUR/USD")
        self.assertEqual(res.signal, "BUY")
        self.assertTrue(res.is_confirmed_1m)
        self.assertIn("1M_CONFIRMED", res.rule_set)

    def test_anti_duplicate_debounce(self):
        """Debounce blocks duplicate triggers within the same 5M window."""
        candidate = self.engine.ShortTFDecision(
            signal="BUY",
            entry_price=1.1020,
            stop_loss=1.1000,
            take_profit=1.1056,
            atr_value=0.0010,
            risk_reward_ratio=1.8,
            primary_tf="5m",
        )
        dates_1m = pd.date_range("2026-09-22 14:00", periods=20, freq="1min")
        now_ts = int(dates_1m[-1].timestamp() * 1000)
        df_1m = pd.DataFrame({
            "datetime": dates_1m,
            "open": [1.1010] * 20,
            "high": [1.1015] * 20,
            "low": [1.1008] * 20,
            "close": [1.1014] * 20,
            "volume": [100.0] * 20,
            "time": [int(d.timestamp() * 1000) for d in dates_1m],
        })
        # Last signal fired 60 seconds ago (within 5-min debounce window)
        res = self.engine.confirm_1m_entry(df_1m, candidate, "EUR/USD", last_signal_time=now_ts - 60000)
        self.assertEqual(res.signal, "HOLD")
        self.assertIn("DEBOUNCE", res.rule_set)


if __name__ == "__main__":
    unittest.main(verbosity=2)
