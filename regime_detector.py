"""
Market Regime Detection Engine for Binance Spot Trading.
Classifies market conditions into one of four regimes:
  1. TRENDING_UP: 4h 200 EMA sloping up + consistent higher highs/higher lows + ADX > 25 (or steep EMA slope).
  2. TRENDING_DOWN: 4h 200 EMA sloping down + lower highs/lower lows + ADX > 25 (or steep downward EMA slope).
  3. RANGING/CHOPPY: Low directional momentum (ADX < 20) OR compressed Bollinger Band width (width < 50-bar avg).
  4. HIGH_VOLATILITY: Volatility surge where ATR is significantly elevated (>= 1.5x 50-bar avg) — skips trading.
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional
import numpy as np
import pandas as pd

try:
    from . import indicators
    from .logger import get_logger
except ImportError:
    import indicators
    from logger import get_logger

logger = get_logger("regime_detector")


@dataclass
class RegimeResult:
    """Encapsulates regime classification and quantitative diagnostic indicators."""
    regime: str                  # "TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOLATILITY"
    adx: float                   # ADX(14) value (trend strength)
    ema_slope: float             # 1h EMA(21) slope (% change over 5 bars)
    bb_width: float              # Bollinger Band width: (Upper - Lower) / Middle
    bb_width_50_avg: float       # 50-bar rolling average of BB width
    atr_ratio: float             # Current ATR(14) relative to its 50-bar average
    htf_trend: str               # 4h 200 EMA macro trend ("UP" or "DOWN")
    details: Dict[str, Any]


def detect_regime(
    df: pd.DataFrame,
    htf_trend: Optional[str] = None,
    volatility_threshold: float = 1.5,
) -> RegimeResult:
    """
    Classifies the current candlestick window into one of 4 market regimes.

    Args:
        df: DataFrame containing at least 50 bars of OHLCV candles.
        htf_trend: Optional Higher Timeframe trend override ("UP" or "DOWN").
        volatility_threshold: ATR multiplier to trigger HIGH_VOLATILITY regime (default: 1.5x).

    Returns:
        RegimeResult containing the regime label and supporting metrics.
    """
    min_required = 35
    if df is None or len(df) < min_required:
        return RegimeResult(
            regime="RANGING",
            adx=0.0,
            ema_slope=0.0,
            bb_width=0.0,
            bb_width_50_avg=0.0,
            atr_ratio=1.0,
            htf_trend="UNKNOWN",
            details={"reason": "Insufficient candle history"},
        )

    # 1. Macro HTF Trend determination
    if htf_trend is not None:
        macro_htf = htf_trend.upper()
    elif "htf_trend" in df.columns:
        macro_htf = str(df["htf_trend"].iloc[-1]).upper()
    else:
        macro_htf = "UP"

    # 2. Compute Core Diagnostics
    # ATR & Volatility ratio
    atr_series = indicators.atr(df, 14)
    atr_val = float(atr_series.iloc[-1])
    atr_50_avg_series = atr_series.rolling(window=50, min_periods=15).mean()
    atr_50_avg = float(atr_50_avg_series.iloc[-1]) if len(atr_50_avg_series) > 0 else atr_val
    atr_ratio = (atr_val / atr_50_avg) if atr_50_avg > 0 else 1.0

    # ADX Trend Strength
    adx_series = indicators.adx(df, 14)
    adx_val = float(adx_series.iloc[-1])

    # Bollinger Band Width
    bb_upper, bb_middle, bb_lower = indicators.bollinger_bands(df, 20, 2.0)
    mid_safe = bb_middle.replace(0.0, np.nan)
    bb_width_series = (bb_upper - bb_lower) / mid_safe
    bb_width_val = float(bb_width_series.iloc[-1])
    bb_width_50_series = bb_width_series.rolling(window=50, min_periods=15).mean()
    bb_width_50_avg = float(bb_width_50_series.iloc[-1]) if len(bb_width_50_series) > 0 else bb_width_val

    # EMA Slope (% change of EMA21 over last 5 bars)
    ema_21 = indicators.ema(df, 21)
    if len(ema_21) >= 6 and float(ema_21.iloc[-6]) > 0:
        ema_slope = ((float(ema_21.iloc[-1]) - float(ema_21.iloc[-6])) / float(ema_21.iloc[-6])) * 100.0
    else:
        ema_slope = 0.0

    # Higher Highs / Lower Lows Structure over last 20 bars
    lookback = min(20, len(df))
    recent_highs = df["high"].iloc[-lookback:].values
    recent_lows = df["low"].iloc[-lookback:].values

    mid_idx = lookback // 2
    is_higher_highs = bool(
        recent_highs[-1] > recent_highs[mid_idx] and recent_lows[-1] > recent_lows[mid_idx]
    )
    is_lower_lows = bool(
        recent_highs[-1] < recent_highs[mid_idx] and recent_lows[-1] < recent_lows[mid_idx]
    )

    details = {
        "adx": round(adx_val, 2),
        "ema_slope": round(ema_slope, 4),
        "bb_width": round(bb_width_val, 4),
        "bb_width_50_avg": round(bb_width_50_avg, 4),
        "atr_current": round(atr_val, 2),
        "atr_50_avg": round(atr_50_avg, 2),
        "atr_ratio": round(atr_ratio, 2),
        "htf_trend": macro_htf,
        "is_higher_highs": is_higher_highs,
        "is_lower_lows": is_lower_lows,
    }

    # ==============================================================================
    # REGIME CLASSIFICATION RULES HIERARCHY
    # ==============================================================================
    # Priority 1: High Volatility Circuit Breaker
    # If ATR is 1.5x+ above its 50-bar baseline, market is experiencing extreme erratic moves
    if atr_ratio >= volatility_threshold:
        logger.debug(
            f"[REGIME: HIGH_VOLATILITY] ATR ratio {atr_ratio:.2f} >= {volatility_threshold}x of 50-bar avg. "
            f"Trading suspended for capital preservation."
        )
        return RegimeResult(
            regime="HIGH_VOLATILITY",
            adx=round(adx_val, 2),
            ema_slope=round(ema_slope, 4),
            bb_width=round(bb_width_val, 4),
            bb_width_50_avg=round(bb_width_50_avg, 4),
            atr_ratio=round(atr_ratio, 2),
            htf_trend=macro_htf,
            details=details,
        )

    # Priority 2: Trending Up
    # 4h 200 EMA slopes up + price makes higher highs/lows + trend strength (ADX > 25 or steep slope)
    if macro_htf == "UP" and (is_higher_highs or ema_slope > 0.05) and (adx_val >= 25 or ema_slope > 0.15):
        logger.debug(f"[REGIME: TRENDING_UP] HTF: UP, ADX: {adx_val:.1f}, Slope: {ema_slope:.3f}%")
        return RegimeResult(
            regime="TRENDING_UP",
            adx=round(adx_val, 2),
            ema_slope=round(ema_slope, 4),
            bb_width=round(bb_width_val, 4),
            bb_width_50_avg=round(bb_width_50_avg, 4),
            atr_ratio=round(atr_ratio, 2),
            htf_trend=macro_htf,
            details=details,
        )

    # Priority 3: Trending Down
    # 4h 200 EMA slopes down + lower highs/lows + trend strength (ADX > 25 or steep downward slope)
    if macro_htf == "DOWN" and (is_lower_lows or ema_slope < -0.05) and (adx_val >= 25 or ema_slope < -0.15):
        logger.debug(f"[REGIME: TRENDING_DOWN] HTF: DOWN, ADX: {adx_val:.1f}, Slope: {ema_slope:.3f}%")
        return RegimeResult(
            regime="TRENDING_DOWN",
            adx=round(adx_val, 2),
            ema_slope=round(ema_slope, 4),
            bb_width=round(bb_width_val, 4),
            bb_width_50_avg=round(bb_width_50_avg, 4),
            atr_ratio=round(atr_ratio, 2),
            htf_trend=macro_htf,
            details=details,
        )

    # Priority 4: Ranging / Choppy
    # Low ADX (< 20) OR compressed Bollinger Band width with no strong directional slope
    logger.debug(
        f"[REGIME: RANGING] ADX: {adx_val:.1f}, BB Width: {bb_width_val:.4f} (50-avg: {bb_width_50_avg:.4f})"
    )
    return RegimeResult(
        regime="RANGING",
        adx=round(adx_val, 2),
        ema_slope=round(ema_slope, 4),
        bb_width=round(bb_width_val, 4),
        bb_width_50_avg=round(bb_width_50_avg, 4),
        atr_ratio=round(atr_ratio, 2),
        htf_trend=macro_htf,
        details=details,
    )
