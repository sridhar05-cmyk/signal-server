"""
Modular Exit Engine for Short-Timeframe Forex (5M Primary / 1M Confirmation).

Implements 6 Candidate Exit Structures:
  Candidate A: ATR-based initial SL + Structure-aware SL + ATR-based trailing
  Candidate B: Structure SL + Multi-tier breakeven + Structure trailing
  Candidate C: Partial TP + Remaining position ATR trailing + Protect remaining position
  Candidate D: Regime-adaptive targets (trend -> larger, range -> tighter) + Dynamic trailing
  Candidate E: Momentum-aware exit (MACD/EMA cutoff, pullback-protected)
  Candidate F: Combined hybrid exit (synthesized best elements)
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd
import numpy as np

try:
    from . import indicators
    from . import market_structure
    from . import forex_utils
    from .logger import get_logger
except ImportError:
    import indicators
    import market_structure
    import forex_utils
    from logger import get_logger

logger = get_logger("exit_engine")


@dataclass
class ExitDecision:
    """Standardized output from an exit evaluation."""
    should_exit: bool = False
    exit_price: float = 0.0
    exit_reason: str = ""
    partial_pnl: float = 0.0
    is_partial_exit: bool = False
    new_sl: Optional[float] = None


class BaseExitCandidate:
    """Base class for exit engine candidates."""
    name: str = "BASE"

    def __init__(self, target_r: float = 1.5, **kwargs):
        self.target_r = target_r
        self.params = kwargs

    def update_position_state(
        self,
        pos: Dict[str, Any],
        c_open: float,
        c_high: float,
        c_low: float,
        c_close: float,
        window_df: pd.DataFrame,
        pair: str,
        pip_val: float,
        spread_cost: float,
    ) -> ExitDecision:
        raise NotImplementedError


class CandidateA_ATR_Trailing(BaseExitCandidate):
    """
    Candidate A:
      - ATR-based initial SL bounded by structure
      - ATR-based trailing stop activated once in profit (+1.0R)
      - Fixed target R (1.0R, 1.5R, 2.0R)
    """
    name = "Candidate A (ATR Trailing)"

    def update_position_state(
        self,
        pos: Dict[str, Any],
        c_open: float,
        c_high: float,
        c_low: float,
        c_close: float,
        window_df: pd.DataFrame,
        pair: str,
        pip_val: float,
        spread_cost: float,
    ) -> ExitDecision:
        direction = pos["direction"]
        entry_p = pos["entry_price"]
        curr_sl = pos["current_sl"]
        take_profit = pos["take_profit"]
        atr = pos["atr"]
        r_dist = pos["r_dist"]
        trail_mult = self.params.get("trail_atr_mult", 1.5)
        activation_r = self.params.get("activation_r", 1.0)
        bars_held = pos["bars_held"]

        if direction == "BUY":
            highest = max(pos.get("highest_price", entry_p), c_high)
            pos["highest_price"] = highest
            current_r = (highest - entry_p) / r_dist if r_dist > 0 else 0.0

            if current_r >= activation_r:
                pos["trail_active"] = True
                trail_sl = highest - (trail_mult * atr)
                curr_sl = max(curr_sl, trail_sl)
                pos["current_sl"] = curr_sl

            # Check exits: SL, TP, Stagnation
            if c_low <= curr_sl:
                exit_price = min(c_open, curr_sl)
                reason = "TRAILING_STOP" if pos.get("trail_active") else "STOP_LOSS"
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason=reason)
            elif c_high >= take_profit:
                exit_price = max(c_open, take_profit)
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason="TAKE_PROFIT")
            elif bars_held >= 30 and current_r < 0.2:
                return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="TIME_STAGNATION")

        else:  # SHORT
            lowest = min(pos.get("lowest_price", entry_p), c_low)
            pos["lowest_price"] = lowest
            current_r = (entry_p - lowest) / r_dist if r_dist > 0 else 0.0

            if current_r >= activation_r:
                pos["trail_active"] = True
                trail_sl = lowest + (trail_mult * atr)
                curr_sl = min(curr_sl, trail_sl)
                pos["current_sl"] = curr_sl

            if c_high >= curr_sl:
                exit_price = max(c_open, curr_sl)
                reason = "TRAILING_STOP" if pos.get("trail_active") else "STOP_LOSS"
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason=reason)
            elif c_low <= take_profit:
                exit_price = min(c_open, take_profit)
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason="TAKE_PROFIT")
            elif bars_held >= 30 and current_r < 0.2:
                return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="TIME_STAGNATION")

        return ExitDecision(should_exit=False, new_sl=curr_sl)


class CandidateB_MultiTier_BE_Structure(BaseExitCandidate):
    """
    Candidate B:
      - Structure SL (anchored at swing pivot)
      - Multi-tier breakeven:
          Tier 1 (+0.8R): move SL to -0.2R (cuts risk 80% without suffocating)
          Tier 2 (+1.2R): move SL to Breakeven (+ spread buffer)
          Tier 3 (+1.8R): lock +0.8R profit
      - Structure trailing: advance SL behind confirmed swing pivots
    """
    name = "Candidate B (Multi-Tier BE + Structure Trail)"

    def update_position_state(
        self,
        pos: Dict[str, Any],
        c_open: float,
        c_high: float,
        c_low: float,
        c_close: float,
        window_df: pd.DataFrame,
        pair: str,
        pip_val: float,
        spread_cost: float,
    ) -> ExitDecision:
        direction = pos["direction"]
        entry_p = pos["entry_price"]
        curr_sl = pos["current_sl"]
        take_profit = pos["take_profit"]
        atr = pos["atr"]
        r_dist = pos["r_dist"]
        bars_held = pos["bars_held"]
        spread_buffer = spread_cost * 1.2

        if direction == "BUY":
            highest = max(pos.get("highest_price", entry_p), c_high)
            pos["highest_price"] = highest
            current_r = (highest - entry_p) / r_dist if r_dist > 0 else 0.0

            # Tier 1: Risk reduction to -0.2R at +0.8R
            if current_r >= 0.8 and not pos.get("tier1_be", False):
                pos["tier1_be"] = True
                tier1_sl = entry_p - (0.2 * r_dist)
                curr_sl = max(curr_sl, tier1_sl)

            # Tier 2: Breakeven (+ spread buffer) at +1.2R
            if current_r >= 1.2 and not pos.get("tier2_be", False):
                pos["tier2_be"] = True
                curr_sl = max(curr_sl, entry_p + spread_buffer)

            # Tier 3: Lock +0.8R at +1.8R
            if current_r >= 1.8 and not pos.get("tier3_lock", False):
                pos["tier3_lock"] = True
                curr_sl = max(curr_sl, entry_p + (0.8 * r_dist))

            # Structure Trailing: advance behind validated swing low
            if len(window_df) >= 20 and current_r >= 1.2:
                struct = market_structure.detect_market_structure(window_df, pair=pair, pivot_bars=3)
                if struct.last_swing_low > entry_p and struct.swing_low_age >= 2:
                    curr_sl = max(curr_sl, struct.last_swing_low - (0.15 * atr))

            pos["current_sl"] = curr_sl

            if c_low <= curr_sl:
                exit_price = min(c_open, curr_sl)
                reason = "PROFIT_LOCK_STOP" if pos.get("tier3_lock") else ("BREAKEVEN_STOP" if pos.get("tier2_be") else "STOP_LOSS")
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason=reason)
            elif c_high >= take_profit:
                exit_price = max(c_open, take_profit)
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason="TAKE_PROFIT")
            elif bars_held >= 36 and current_r < 0.2:
                return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="TIME_STAGNATION")

        else:  # SHORT
            lowest = min(pos.get("lowest_price", entry_p), c_low)
            pos["lowest_price"] = lowest
            current_r = (entry_p - lowest) / r_dist if r_dist > 0 else 0.0

            if current_r >= 0.8 and not pos.get("tier1_be", False):
                pos["tier1_be"] = True
                tier1_sl = entry_p + (0.2 * r_dist)
                curr_sl = min(curr_sl, tier1_sl)

            if current_r >= 1.2 and not pos.get("tier2_be", False):
                pos["tier2_be"] = True
                curr_sl = min(curr_sl, entry_p - spread_buffer)

            if current_r >= 1.8 and not pos.get("tier3_lock", False):
                pos["tier3_lock"] = True
                curr_sl = min(curr_sl, entry_p - (0.8 * r_dist))

            if len(window_df) >= 20 and current_r >= 1.2:
                struct = market_structure.detect_market_structure(window_df, pair=pair, pivot_bars=3)
                if struct.last_swing_high < entry_p and struct.swing_high_age >= 2:
                    curr_sl = min(curr_sl, struct.last_swing_high + (0.15 * atr))

            pos["current_sl"] = curr_sl

            if c_high >= curr_sl:
                exit_price = max(c_open, curr_sl)
                reason = "PROFIT_LOCK_STOP" if pos.get("tier3_lock") else ("BREAKEVEN_STOP" if pos.get("tier2_be") else "STOP_LOSS")
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason=reason)
            elif c_low <= take_profit:
                exit_price = min(c_open, take_profit)
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason="TAKE_PROFIT")
            elif bars_held >= 36 and current_r < 0.2:
                return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="TIME_STAGNATION")

        return ExitDecision(should_exit=False, new_sl=curr_sl)


class CandidateC_Partial_TP_Trailing(BaseExitCandidate):
    """
    Candidate C:
      - Partial TP: Close 50% of position at Target 1 (+1.0R or +1.2R)
      - Protect remaining position: Move SL on remainder immediately to BE + spread buffer
      - Remaining position trails with ATR trail towards Target 2 (+2.0R)
    """
    name = "Candidate C (Partial TP + Protected Remainder Trail)"

    def update_position_state(
        self,
        pos: Dict[str, Any],
        c_open: float,
        c_high: float,
        c_low: float,
        c_close: float,
        window_df: pd.DataFrame,
        pair: str,
        pip_val: float,
        spread_cost: float,
    ) -> ExitDecision:
        direction = pos["direction"]
        entry_p = pos["entry_price"]
        curr_sl = pos["current_sl"]
        atr = pos["atr"]
        r_dist = pos["r_dist"]
        bars_held = pos["bars_held"]
        spread_buffer = spread_cost * 1.2
        target1_r = self.params.get("target1_r", 1.0)
        target2_r = self.target_r  # Final target for remainder

        if direction == "BUY":
            highest = max(pos.get("highest_price", entry_p), c_high)
            pos["highest_price"] = highest
            current_r = (highest - entry_p) / r_dist if r_dist > 0 else 0.0

            # 1. Check Partial TP trigger
            if current_r >= target1_r and not pos.get("partial_tp_taken", False):
                pos["partial_tp_taken"] = True
                target1_price = entry_p + (target1_r * r_dist)
                # Secure remainder at BE immediately
                curr_sl = max(curr_sl, entry_p + spread_buffer)
                pos["current_sl"] = curr_sl
                return ExitDecision(
                    should_exit=False,
                    is_partial_exit=True,
                    exit_price=target1_price,
                    exit_reason="PARTIAL_TP_50PCT",
                    new_sl=curr_sl,
                )

            # 2. Trail remainder if partial TP was taken
            if pos.get("partial_tp_taken", False):
                trail_sl = highest - (1.5 * atr)
                curr_sl = max(curr_sl, trail_sl)
                pos["current_sl"] = curr_sl

            # 3. Final Target 2 for remainder
            target2_price = entry_p + (target2_r * r_dist)
            if c_high >= target2_price and pos.get("partial_tp_taken", False):
                return ExitDecision(should_exit=True, exit_price=max(c_open, target2_price), exit_reason="TAKE_PROFIT_FINAL")

            # 4. SL exit (full loss if partial not taken, BE profit if partial taken)
            if c_low <= curr_sl:
                exit_price = min(c_open, curr_sl)
                reason = "PROTECTED_REMAINDER_STOP" if pos.get("partial_tp_taken") else "STOP_LOSS"
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason=reason)

            elif bars_held >= 36 and current_r < 0.2:
                return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="TIME_STAGNATION")

        else:  # SHORT
            lowest = min(pos.get("lowest_price", entry_p), c_low)
            pos["lowest_price"] = lowest
            current_r = (entry_p - lowest) / r_dist if r_dist > 0 else 0.0

            if current_r >= target1_r and not pos.get("partial_tp_taken", False):
                pos["partial_tp_taken"] = True
                target1_price = entry_p - (target1_r * r_dist)
                curr_sl = min(curr_sl, entry_p - spread_buffer)
                pos["current_sl"] = curr_sl
                return ExitDecision(
                    should_exit=False,
                    is_partial_exit=True,
                    exit_price=target1_price,
                    exit_reason="PARTIAL_TP_50PCT",
                    new_sl=curr_sl,
                )

            if pos.get("partial_tp_taken", False):
                trail_sl = lowest + (1.5 * atr)
                curr_sl = min(curr_sl, trail_sl)
                pos["current_sl"] = curr_sl

            target2_price = entry_p - (target2_r * r_dist)
            if c_low <= target2_price and pos.get("partial_tp_taken", False):
                return ExitDecision(should_exit=True, exit_price=min(c_open, target2_price), exit_reason="TAKE_PROFIT_FINAL")

            if c_high >= curr_sl:
                exit_price = max(c_open, curr_sl)
                reason = "PROTECTED_REMAINDER_STOP" if pos.get("partial_tp_taken") else "STOP_LOSS"
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason=reason)

            elif bars_held >= 36 and current_r < 0.2:
                return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="TIME_STAGNATION")

        return ExitDecision(should_exit=False, new_sl=curr_sl)


class CandidateD_Regime_Adaptive(BaseExitCandidate):
    """
    Candidate D:
      - Regime-Adaptive Targets:
          Trending Regime: Larger target (2.0R to 2.5R), wider ATR trail (1.8 ATR), BE trigger at +1.2R
          Ranging Regime: Tighter target (1.0R to 1.2R), tighter trail (1.0 ATR), BE trigger at +0.8R
    """
    name = "Candidate D (Regime-Adaptive Targets & Dynamic Trail)"

    def update_position_state(
        self,
        pos: Dict[str, Any],
        c_open: float,
        c_high: float,
        c_low: float,
        c_close: float,
        window_df: pd.DataFrame,
        pair: str,
        pip_val: float,
        spread_cost: float,
    ) -> ExitDecision:
        direction = pos["direction"]
        entry_p = pos["entry_price"]
        curr_sl = pos["current_sl"]
        atr = pos["atr"]
        r_dist = pos["r_dist"]
        regime = pos.get("regime", "RANGING")
        bars_held = pos["bars_held"]
        spread_buffer = spread_cost * 1.2

        is_trend = "TRENDING" in regime
        target_mult = 2.2 if is_trend else 1.2
        be_trigger = 1.2 if is_trend else 0.8
        trail_dist = 1.8 * atr if is_trend else 1.0 * atr

        if direction == "BUY":
            highest = max(pos.get("highest_price", entry_p), c_high)
            pos["highest_price"] = highest
            current_r = (highest - entry_p) / r_dist if r_dist > 0 else 0.0

            # Dynamic Breakeven
            if current_r >= be_trigger and not pos.get("be_reached", False):
                pos["be_reached"] = True
                curr_sl = max(curr_sl, entry_p + spread_buffer)

            # Dynamic Trailing
            if current_r >= (be_trigger + 0.3):
                pos["trailing_active"] = True
                curr_sl = max(curr_sl, highest - trail_dist)

            pos["current_sl"] = curr_sl
            dynamic_tp = entry_p + (target_mult * r_dist)

            if c_low <= curr_sl:
                exit_price = min(c_open, curr_sl)
                reason = "DYNAMIC_TRAILING_STOP" if pos.get("trailing_active") else ("BREAKEVEN_STOP" if pos.get("be_reached") else "STOP_LOSS")
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason=reason)
            elif c_high >= dynamic_tp:
                exit_price = max(c_open, dynamic_tp)
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason="REGIME_TAKE_PROFIT")
            elif bars_held >= (36 if is_trend else 20) and current_r < 0.2:
                return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="TIME_STAGNATION")

        else:  # SHORT
            lowest = min(pos.get("lowest_price", entry_p), c_low)
            pos["lowest_price"] = lowest
            current_r = (entry_p - lowest) / r_dist if r_dist > 0 else 0.0

            if current_r >= be_trigger and not pos.get("be_reached", False):
                pos["be_reached"] = True
                curr_sl = min(curr_sl, entry_p - spread_buffer)

            if current_r >= (be_trigger + 0.3):
                pos["trailing_active"] = True
                curr_sl = min(curr_sl, lowest + trail_dist)

            pos["current_sl"] = curr_sl
            dynamic_tp = entry_p - (target_mult * r_dist)

            if c_high >= curr_sl:
                exit_price = max(c_open, curr_sl)
                reason = "DYNAMIC_TRAILING_STOP" if pos.get("trailing_active") else ("BREAKEVEN_STOP" if pos.get("be_reached") else "STOP_LOSS")
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason=reason)
            elif c_low <= dynamic_tp:
                exit_price = min(c_open, dynamic_tp)
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason="REGIME_TAKE_PROFIT")
            elif bars_held >= (36 if is_trend else 20) and current_r < 0.2:
                return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="TIME_STAGNATION")

        return ExitDecision(should_exit=False, new_sl=curr_sl)


class CandidateE_Momentum_Aware(BaseExitCandidate):
    """
    Candidate E:
      - Momentum-Aware Exit:
          Detects momentum deterioration on MACD flip + adverse close across EMA21
          PREVENTS premature exit on normal pullbacks (pullbacks holding EMA21 are tolerated)
      - Fixed Target R (1.0R, 1.5R, 2.0R)
      - Breakeven trigger at +1.0R
    """
    name = "Candidate E (Momentum-Aware Exit)"

    def update_position_state(
        self,
        pos: Dict[str, Any],
        c_open: float,
        c_high: float,
        c_low: float,
        c_close: float,
        window_df: pd.DataFrame,
        pair: str,
        pip_val: float,
        spread_cost: float,
    ) -> ExitDecision:
        direction = pos["direction"]
        entry_p = pos["entry_price"]
        curr_sl = pos["current_sl"]
        take_profit = pos["take_profit"]
        r_dist = pos["r_dist"]
        bars_held = pos["bars_held"]
        spread_buffer = spread_cost * 1.2

        # Extract indicators from window_df
        ema21 = float(indicators.ema(window_df, 21).iloc[-1]) if len(window_df) >= 21 else entry_p
        _, _, macd_hist = indicators.macd(window_df, 12, 26, 9)
        m_hist_cur = float(macd_hist.iloc[-1]) if len(macd_hist) > 0 else 0.0

        if direction == "BUY":
            highest = max(pos.get("highest_price", entry_p), c_high)
            pos["highest_price"] = highest
            current_r = (highest - entry_p) / r_dist if r_dist > 0 else 0.0

            # Breakeven at +1.0R
            if current_r >= 1.0 and not pos.get("be_reached", False):
                pos["be_reached"] = True
                curr_sl = max(curr_sl, entry_p + spread_buffer)
                pos["current_sl"] = curr_sl

            # Momentum Deterioration Exit:
            # Requires adverse close below EMA21 AND negative MACD histogram
            # Normal pullbacks (closing above EMA21) are protected from premature exit
            if bars_held >= 3 and current_r >= 0.3:
                if c_close < ema21 and m_hist_cur < -0.0001:
                    return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="MOMENTUM_DETERIORATION")

            if c_low <= curr_sl:
                exit_price = min(c_open, curr_sl)
                reason = "BREAKEVEN_STOP" if pos.get("be_reached") else "STOP_LOSS"
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason=reason)
            elif c_high >= take_profit:
                exit_price = max(c_open, take_profit)
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason="TAKE_PROFIT")
            elif bars_held >= 36 and current_r < 0.2:
                return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="TIME_STAGNATION")

        else:  # SHORT
            lowest = min(pos.get("lowest_price", entry_p), c_low)
            pos["lowest_price"] = lowest
            current_r = (entry_p - lowest) / r_dist if r_dist > 0 else 0.0

            if current_r >= 1.0 and not pos.get("be_reached", False):
                pos["be_reached"] = True
                curr_sl = min(curr_sl, entry_p - spread_buffer)
                pos["current_sl"] = curr_sl

            if bars_held >= 3 and current_r >= 0.3:
                if c_close > ema21 and m_hist_cur > 0.0001:
                    return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="MOMENTUM_DETERIORATION")

            if c_high >= curr_sl:
                exit_price = max(c_open, curr_sl)
                reason = "BREAKEVEN_STOP" if pos.get("be_reached") else "STOP_LOSS"
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason=reason)
            elif c_low <= take_profit:
                exit_price = min(c_open, take_profit)
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason="TAKE_PROFIT")
            elif bars_held >= 36 and current_r < 0.2:
                return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="TIME_STAGNATION")

        return ExitDecision(should_exit=False, new_sl=curr_sl)


class CandidateF_Combined_Hybrid(BaseExitCandidate):
    """
    Candidate F:
      - Combined Hybrid Exit:
          1. Multi-tier BE (Tier 1: reduce risk to -0.2R at +0.8R; Tier 2: BE at +1.2R)
          2. Partial TP (50% profit taken at +1.2R)
          3. Structure + ATR Trailing on remainder (behind swing lows/highs + 1.5 ATR)
          4. Momentum cutoff (early exit if trend structure breaks)
          5. Regime adaptation (extended target 2.2R in trend, 1.5R in range)
    """
    name = "Candidate F (Combined Hybrid Exit)"

    def update_position_state(
        self,
        pos: Dict[str, Any],
        c_open: float,
        c_high: float,
        c_low: float,
        c_close: float,
        window_df: pd.DataFrame,
        pair: str,
        pip_val: float,
        spread_cost: float,
    ) -> ExitDecision:
        direction = pos["direction"]
        entry_p = pos["entry_price"]
        curr_sl = pos["current_sl"]
        atr = pos["atr"]
        r_dist = pos["r_dist"]
        regime = pos.get("regime", "RANGING")
        bars_held = pos["bars_held"]
        spread_buffer = spread_cost * 1.2
        is_trend = "TRENDING" in regime

        target1_r = 1.2
        target2_r = 2.2 if is_trend else 1.5

        # EMA21 and MACD for momentum failure check
        ema21 = float(indicators.ema(window_df, 21).iloc[-1]) if len(window_df) >= 21 else entry_p
        _, _, macd_hist = indicators.macd(window_df, 12, 26, 9)
        m_hist_cur = float(macd_hist.iloc[-1]) if len(macd_hist) > 0 else 0.0

        if direction == "BUY":
            highest = max(pos.get("highest_price", entry_p), c_high)
            pos["highest_price"] = highest
            current_r = (highest - entry_p) / r_dist if r_dist > 0 else 0.0

            # 1. Multi-tier BE: Tier 1 (-0.2R loss limit) at +0.8R
            if current_r >= 0.8 and not pos.get("tier1_be", False):
                pos["tier1_be"] = True
                curr_sl = max(curr_sl, entry_p - (0.2 * r_dist))

            # 2. Partial TP at +1.2R
            if current_r >= target1_r and not pos.get("partial_tp_taken", False):
                pos["partial_tp_taken"] = True
                curr_sl = max(curr_sl, entry_p + spread_buffer)  # Protect remainder at BE
                pos["current_sl"] = curr_sl
                target1_price = entry_p + (target1_r * r_dist)
                return ExitDecision(
                    should_exit=False,
                    is_partial_exit=True,
                    exit_price=target1_price,
                    exit_reason="PARTIAL_TP_50PCT",
                    new_sl=curr_sl,
                )

            # 3. Structure / ATR Trailing for remainder
            if pos.get("partial_tp_taken", False):
                trail_sl = highest - (1.5 * atr)
                if len(window_df) >= 20:
                    struct = market_structure.detect_market_structure(window_df, pair=pair, pivot_bars=3)
                    if struct.last_swing_low > entry_p and struct.swing_low_age >= 2:
                        trail_sl = max(trail_sl, struct.last_swing_low - (0.15 * atr))
                curr_sl = max(curr_sl, trail_sl)
                pos["current_sl"] = curr_sl

            # 4. Momentum cutoff after partial TP if trend leg fails
            if pos.get("partial_tp_taken", False) and current_r >= 1.4:
                if c_close < ema21 and m_hist_cur < -0.0001:
                    return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="MOMENTUM_FAILURE_CUTOFF")

            # 5. Final Target for remainder
            target2_price = entry_p + (target2_r * r_dist)
            if c_high >= target2_price and pos.get("partial_tp_taken", False):
                return ExitDecision(should_exit=True, exit_price=max(c_open, target2_price), exit_reason="HYBRID_TAKE_PROFIT")

            # 6. Stop Loss / BE Stop
            if c_low <= curr_sl:
                exit_price = min(c_open, curr_sl)
                reason = "PROTECTED_REMAINDER_STOP" if pos.get("partial_tp_taken") else ("BREAKEVEN_STOP" if pos.get("tier1_be") else "STOP_LOSS")
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason=reason)

            elif bars_held >= 36 and current_r < 0.2:
                return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="TIME_STAGNATION")

        else:  # SHORT
            lowest = min(pos.get("lowest_price", entry_p), c_low)
            pos["lowest_price"] = lowest
            current_r = (entry_p - lowest) / r_dist if r_dist > 0 else 0.0

            if current_r >= 0.8 and not pos.get("tier1_be", False):
                pos["tier1_be"] = True
                curr_sl = min(curr_sl, entry_p + (0.2 * r_dist))

            if current_r >= target1_r and not pos.get("partial_tp_taken", False):
                pos["partial_tp_taken"] = True
                curr_sl = min(curr_sl, entry_p - spread_buffer)
                pos["current_sl"] = curr_sl
                target1_price = entry_p - (target1_r * r_dist)
                return ExitDecision(
                    should_exit=False,
                    is_partial_exit=True,
                    exit_price=target1_price,
                    exit_reason="PARTIAL_TP_50PCT",
                    new_sl=curr_sl,
                )

            if pos.get("partial_tp_taken", False):
                trail_sl = lowest + (1.5 * atr)
                if len(window_df) >= 20:
                    struct = market_structure.detect_market_structure(window_df, pair=pair, pivot_bars=3)
                    if struct.last_swing_high < entry_p and struct.swing_high_age >= 2:
                        trail_sl = min(trail_sl, struct.last_swing_high + (0.15 * atr))
                curr_sl = min(curr_sl, trail_sl)
                pos["current_sl"] = curr_sl

            if pos.get("partial_tp_taken", False) and current_r >= 1.4:
                if c_close > ema21 and m_hist_cur > 0.0001:
                    return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="MOMENTUM_FAILURE_CUTOFF")

            target2_price = entry_p - (target2_r * r_dist)
            if c_low <= target2_price and pos.get("partial_tp_taken", False):
                return ExitDecision(should_exit=True, exit_price=min(c_open, target2_price), exit_reason="HYBRID_TAKE_PROFIT")

            if c_high >= curr_sl:
                exit_price = max(c_open, curr_sl)
                reason = "PROTECTED_REMAINDER_STOP" if pos.get("partial_tp_taken") else ("BREAKEVEN_STOP" if pos.get("tier1_be") else "STOP_LOSS")
                return ExitDecision(should_exit=True, exit_price=exit_price, exit_reason=reason)

            elif bars_held >= 36 and current_r < 0.2:
                return ExitDecision(should_exit=True, exit_price=c_close, exit_reason="TIME_STAGNATION")

        return ExitDecision(should_exit=False, new_sl=curr_sl)


def create_candidate(name: str, target_r: float = 1.5, **kwargs) -> BaseExitCandidate:
    """Factory function for exit candidates."""
    candidates = {
        "A": CandidateA_ATR_Trailing,
        "B": CandidateB_MultiTier_BE_Structure,
        "C": CandidateC_Partial_TP_Trailing,
        "D": CandidateD_Regime_Adaptive,
        "E": CandidateE_Momentum_Aware,
        "F": CandidateF_Combined_Hybrid,
    }
    c_cls = candidates.get(name.upper())
    if not c_cls:
        raise ValueError(f"Unknown candidate '{name}'. Available: A, B, C, D, E, F")
    return c_cls(target_r=target_r, **kwargs)
