"""
False Signal Elimination & Quality Filtering Engine for Forex.
Prevents common trading traps:
  1. Overextension Gate: Blocks buying parabolic tops or shorting bottoms (> 2.5x ATR from EMA21).
  2. Chop / Low-Volatility Squeeze Filter: Blocks trend entries in stagnant low-ADX compression.
  3. Candle Rejection & Weak Breakout Filter: Verifies candle close location and wick rejection.
  4. Anti-Churn Cooldown Tracker: Enforces bar spacing between trades and penalty after losses.
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np

try:
    from . import forex_utils
    from .logger import get_logger
except ImportError:
    import forex_utils
    from logger import get_logger

logger = get_logger("false_signal_filters")


@dataclass
class FilterResult:
    """Outcome of false signal evaluation."""
    passed: bool
    filter_name: str
    reason: str
    metrics: Dict[str, Any]


class ForexSignalFilter:
    """Evaluates entry setups against quantitative quality gates."""

    @staticmethod
    def check_overextension(
        curr_close: float,
        ema21: float,
        atr_val: float,
        rsi_val: float,
        direction: str,
    ) -> FilterResult:
        """
        Guards against chasing extended moves.
        Blocks Long if price > 2.5x ATR above EMA21 or RSI > 72.
        Blocks Short if price > 2.5x ATR below EMA21 or RSI < 28.
        """
        if atr_val <= 0:
            return FilterResult(passed=True, filter_name="OVEREXTENSION", reason="ATR is zero", metrics={})

        distance_atr = (curr_close - ema21) / atr_val

        if direction == "BUY":
            if distance_atr > 2.5:
                return FilterResult(
                    passed=False,
                    filter_name="OVEREXTENSION",
                    reason=f"Price is {distance_atr:.2f}x ATR above EMA21 (Overextended pump). Await pullback.",
                    metrics={"distance_atr": round(distance_atr, 2), "rsi": rsi_val},
                )
            if rsi_val > 72.0:
                return FilterResult(
                    passed=False,
                    filter_name="OVEREXTENSION",
                    reason=f"RSI is overbought ({rsi_val:.1f} > 72). High probability of mean-reversion pullback.",
                    metrics={"distance_atr": round(distance_atr, 2), "rsi": rsi_val},
                )
        elif direction == "SHORT":
            if distance_atr < -2.5:
                return FilterResult(
                    passed=False,
                    filter_name="OVEREXTENSION",
                    reason=f"Price is {abs(distance_atr):.2f}x ATR below EMA21 (Overextended dump). Await rally.",
                    metrics={"distance_atr": round(distance_atr, 2), "rsi": rsi_val},
                )
            if rsi_val < 28.0:
                return FilterResult(
                    passed=False,
                    filter_name="OVEREXTENSION",
                    reason=f"RSI is oversold ({rsi_val:.1f} < 28). High risk of bounce.",
                    metrics={"distance_atr": round(distance_atr, 2), "rsi": rsi_val},
                )

        return FilterResult(
            passed=True,
            filter_name="OVEREXTENSION",
            reason="Price and RSI within acceptable momentum zone.",
            metrics={"distance_atr": round(distance_atr, 2), "rsi": rsi_val},
        )

    @staticmethod
    def check_compression_chop(
        adx_val: float,
        bb_width: float,
        bb_width_50_avg: float,
    ) -> FilterResult:
        """
        Detects dead sideways chop or compressed Bollinger Band squeezes.
        Blocks trend entries when ADX < 18 and BB width < 0.65x of 50-bar average.
        """
        is_adx_low = adx_val < 18.0
        is_bb_squeezed = (bb_width < (bb_width_50_avg * 0.65)) if bb_width_50_avg > 0 else False

        if is_adx_low and is_bb_squeezed:
            return FilterResult(
                passed=False,
                filter_name="COMPRESSION_CHOP",
                reason=f"Low directional energy: ADX={adx_val:.1f} < 18 and BB width is squeezed. High whipsaw risk.",
                metrics={"adx": round(adx_val, 1), "bb_width_ratio": round(bb_width / bb_width_50_avg, 2) if bb_width_50_avg else 1.0},
            )

        return FilterResult(
            passed=True,
            filter_name="COMPRESSION_CHOP",
            reason="Market energy is sufficient for directional setup.",
            metrics={"adx": round(adx_val, 1)},
        )

    @staticmethod
    def check_candle_action(
        open_p: float,
        high_p: float,
        low_p: float,
        close_p: float,
        direction: str,
    ) -> FilterResult:
        """
        Validates that the trigger candle closed decisively in the direction of the trade:
        - For Long: close must be in the upper 55% of the bar's total range.
        - For Short: close must be in the lower 55% of the bar's total range.
        Rejects candles that printed massive opposing exhaustion wicks.
        """
        bar_range = high_p - low_p
        if bar_range <= 0:
            return FilterResult(passed=True, filter_name="CANDLE_ACTION", reason="Zero bar range", metrics={})

        close_location = (close_p - low_p) / bar_range  # 0.0 = low, 1.0 = high

        if direction == "BUY":
            if close_location < 0.45:
                return FilterResult(
                    passed=False,
                    filter_name="CANDLE_ACTION",
                    reason=f"Candle closed in bottom {close_location*100:.0f}% of range (Bearish rejection wick).",
                    metrics={"close_location": round(close_location, 2)},
                )
        elif direction == "SHORT":
            if close_location > 0.55:
                return FilterResult(
                    passed=False,
                    filter_name="CANDLE_ACTION",
                    reason=f"Candle closed in top {close_location*100:.0f}% of range (Bullish rejection wick).",
                    metrics={"close_location": round(close_location, 2)},
                )

        return FilterResult(
            passed=True,
            filter_name="CANDLE_ACTION",
            reason="Trigger candle closed decisively in signal direction.",
            metrics={"close_location": round(close_location, 2)},
        )
