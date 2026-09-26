"""
Market Structure and Swing Analysis Engine for Forex Trading.
Analyzes price action structure objectively without lookahead bias:
  - Swing Highs & Swing Lows (pivot points confirmed strictly on candle close)
  - Structural Trend Classification (Higher Highs / Higher Lows vs Lower Highs / Lower Lows)
  - Break of Structure (BOS) indicating trend continuation
  - Dynamic Structural Support and Resistance levels for invalidation stops
"""

from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np

try:
    from . import forex_utils
except ImportError:
    import forex_utils


@dataclass
class MarketStructure:
    """Encapsulates market structure state at the current closed candle."""
    structure_trend: str         # "BULLISH", "BEARISH", "SIDEWAYS"
    is_higher_highs_lows: bool   # True if last 2 swings formed Higher High + Higher Low
    is_lower_highs_lows: bool    # True if last 2 swings formed Lower High + Lower Low
    bos_bullish: bool            # True if current close broke above previous swing high
    bos_bearish: bool            # True if current close broke below previous swing low
    last_swing_high: float       # Price of the most recent confirmed swing high
    last_swing_low: float        # Price of the most recent confirmed swing low
    prev_swing_high: float = 0.0 # Price of the swing high prior to last
    prev_swing_low: float = 0.0  # Price of the swing low prior to last
    structural_sl_long: float = 0.0 # Logical invalidation level for Longs (below last swing low)
    structural_sl_short: float = 0.0 # Logical invalidation level for Shorts (above last swing high)
    swing_high_age: int = 0      # How many bars ago the last swing high occurred
    swing_low_age: int = 0       # How many bars ago the last swing low occurred


def detect_market_structure(
    df: pd.DataFrame,
    pair: str = "EUR/USD",
    pivot_bars: int = 3,
) -> MarketStructure:
    """
    Computes market structure strictly using closed bars up to df.iloc[-1].

    A swing high at bar (t - pivot_bars) is confirmed only when bars
    (t - pivot_bars + 1) through t have completed and closed lower.
    This guarantees zero look-ahead bias during real-time inference and backtesting.

    Args:
        df: DataFrame with OHLC bars.
        pair: Currency pair string for pip sizing.
        pivot_bars: Number of bars on each side required to confirm a swing point (default: 3).

    Returns:
        MarketStructure dataclass instance.
    """
    min_required = (pivot_bars * 2) + 10
    if df is None or len(df) < min_required:
        ref_price = float(df["close"].iloc[-1]) if df is not None and len(df) > 0 else 1.0
        return MarketStructure(
            structure_trend="SIDEWAYS",
            is_higher_highs_lows=False,
            is_lower_highs_lows=False,
            bos_bullish=False,
            bos_bearish=False,
            last_swing_high=ref_price,
            last_swing_low=ref_price,
            prev_swing_high=ref_price,
            prev_swing_low=ref_price,
            structural_sl_long=ref_price * 0.99,
            structural_sl_short=ref_price * 1.01,
            swing_high_age=0,
            swing_low_age=0,
        )

    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    n = len(df)
    current_close = float(closes[-1])

    # Detect confirmed swing highs and lows
    # Bar k is a swing if highs[k] > highs[k - pivot : k + pivot]
    # To avoid lookahead, we only search up to bar (n - 1 - pivot_bars)
    swing_highs: List[Tuple[int, float]] = []  # (bar_index, price)
    swing_lows: List[Tuple[int, float]] = []

    search_end = n - pivot_bars
    for k in range(pivot_bars, search_end):
        # Swing High check
        is_sh = True
        val_h = highs[k]
        for offset in range(1, pivot_bars + 1):
            if highs[k - offset] >= val_h or highs[k + offset] >= val_h:
                is_sh = False
                break
        if is_sh:
            swing_highs.append((k, float(val_h)))

        # Swing Low check
        is_sl = True
        val_l = lows[k]
        for offset in range(1, pivot_bars + 1):
            if lows[k - offset] <= val_l or lows[k + offset] <= val_l:
                is_sl = False
                break
        if is_sl:
            swing_lows.append((k, float(val_l)))

    pip_size = forex_utils.get_pip_size(pair)
    buffer_pips = 8.0 * pip_size  # 8 pips safety buffer beyond swing extreme

    # Fallback if fewer than 2 swings identified
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        last_h = swing_highs[-1][1] if swing_highs else float(highs[-pivot_bars:].max())
        last_l = swing_lows[-1][1] if swing_lows else float(lows[-pivot_bars:].min())
        return MarketStructure(
            structure_trend="SIDEWAYS",
            is_higher_highs_lows=False,
            is_lower_highs_lows=False,
            bos_bullish=current_close > last_h,
            bos_bearish=current_close < last_l,
            last_swing_high=last_h,
            last_swing_low=last_l,
            prev_swing_high=last_h,
            prev_swing_low=last_l,
            structural_sl_long=forex_utils.round_forex_price(last_l - buffer_pips, pair),
            structural_sl_short=forex_utils.round_forex_price(last_h + buffer_pips, pair),
            swing_high_age=n - swing_highs[-1][0] if swing_highs else 0,
            swing_low_age=n - swing_lows[-1][0] if swing_lows else 0,
        )

    # We have at least 2 confirmed swing highs and 2 swing lows
    last_sh_idx, last_sh = swing_highs[-1]
    prev_sh_idx, prev_sh = swing_highs[-2]

    last_sl_idx, last_sl = swing_lows[-1]
    prev_sl_idx, prev_sl = swing_lows[-2]

    is_hh = last_sh > prev_sh
    is_hl = last_sl > prev_sl
    is_lh = last_sh < prev_sh
    is_ll = last_sl < prev_sl

    is_higher_highs_lows = is_hh and is_hl
    is_lower_highs_lows = is_lh and is_ll

    # Break of Structure (BOS)
    bos_bullish = current_close > last_sh
    bos_bearish = current_close < last_sl

    # Trend classification
    if is_higher_highs_lows or (bos_bullish and not is_lower_highs_lows):
        trend = "BULLISH"
    elif is_lower_highs_lows or (bos_bearish and not is_higher_highs_lows):
        trend = "BEARISH"
    else:
        trend = "SIDEWAYS"

    # Structural SL with pip safety margin
    sl_long = forex_utils.round_forex_price(last_sl - buffer_pips, pair)
    sl_short = forex_utils.round_forex_price(last_sh + buffer_pips, pair)

    return MarketStructure(
        structure_trend=trend,
        is_higher_highs_lows=is_higher_highs_lows,
        is_lower_highs_lows=is_lower_highs_lows,
        bos_bullish=bos_bullish,
        bos_bearish=bos_bearish,
        last_swing_high=last_sh,
        last_swing_low=last_sl,
        prev_swing_high=prev_sh,
        prev_swing_low=prev_sl,
        structural_sl_long=sl_long,
        structural_sl_short=sl_short,
        swing_high_age=n - last_sh_idx,
        swing_low_age=n - last_sl_idx,
    )


# Alias
analyze_market_structure = detect_market_structure
