"""
Transparent Composite Confidence Scoring Engine for Forex Signals.
Calculates an objective 0-100 probability-alignment score based on:
  1. Higher Timeframe Alignment (4h 200 EMA) - 25 pts
  2. Market Structure (HH/HL/LH/LL & BOS) - 25 pts
  3. Market Session & Institutional Liquidity - 20 pts
  4. Momentum & Indicator Confluence (RSI, MACD, EMA stack) - 15 pts
  5. Candle Quality & Volatility State - 15 pts

Returns numerical score, confidence tier ("HIGH", "MEDIUM", "LOW"),
and granular natural-language bullet points explaining WHY the signal scored.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from datetime import datetime

try:
    from . import forex_sessions
    from .market_structure import MarketStructure
except ImportError:
    import forex_sessions
    from market_structure import MarketStructure


@dataclass
class ConfidenceScore:
    """Comprehensive confidence evaluation outcome."""
    score: int                           # 0 to 100
    tier: str                            # "HIGH", "MEDIUM", "LOW"
    passed_minimum_threshold: bool       # True if score >= 55
    factors: List[str] = field(default_factory=list)
    breakdown: Dict[str, int] = field(default_factory=dict)
    summary: str = ""


def compute_confidence_score(
    direction: str,                      # "BUY" or "SHORT"
    curr_close: float,
    htf_trend: str,                      # "UP" or "DOWN"
    htf_ema_200: Optional[float],
    structure: MarketStructure,
    session: str,
    rsi_val: float,
    macd_hist: float,
    macd_hist_prev: float,
    atr_ratio: float,
    close_location: float,
) -> ConfidenceScore:
    """
    Computes transparent, weighted 0-100 confidence score for a signal.
    """
    if direction not in ("BUY", "SHORT"):
        return ConfidenceScore(
            score=0,
            tier="LOW",
            passed_minimum_threshold=False,
            factors=["No directional bias established."],
            breakdown={},
            summary="Neutral market condition. No setup confirmed.",
        )

    score_htf = 0
    score_structure = 0
    score_session = 0
    score_momentum = 0
    score_candle = 0
    factors: List[str] = []

    is_buy = direction == "BUY"

    # 1. Higher Timeframe 4h 200 EMA Macro Alignment (Max 25 pts)
    macro_aligned = (htf_trend == "UP" if is_buy else htf_trend == "DOWN")
    if macro_aligned:
        score_htf += 15
        factors.append(f"Macro HTF 4h 200 EMA is sloping {htf_trend} (Aligned)")
    else:
        factors.append(f"Macro HTF 4h 200 EMA is sloping {htf_trend} (Counter-trend caution)")

    if htf_ema_200 is not None and htf_ema_200 > 0:
        price_above_htf = curr_close > htf_ema_200
        if (is_buy and price_above_htf) or (not is_buy and not price_above_htf):
            score_htf += 10
            factors.append("Price is trading on the dominant side of 4h 200 EMA")

    # 2. Market Structure Alignment (Max 25 pts)
    if is_buy:
        if structure.is_higher_highs_lows:
            score_structure += 15
            factors.append("Market Structure confirmed: Higher Highs & Higher Lows (HH/HL)")
        elif structure.structure_trend == "BULLISH":
            score_structure += 10
            factors.append("Market Structure leaning Bullish")

        if structure.bos_bullish:
            score_structure += 10
            factors.append("Break of Structure (BOS): Candle closed above prior swing high")
    else:
        if structure.is_lower_highs_lows:
            score_structure += 15
            factors.append("Market Structure confirmed: Lower Highs & Lower Lows (LH/LL)")
        elif structure.structure_trend == "BEARISH":
            score_structure += 10
            factors.append("Market Structure leaning Bearish")

        if structure.bos_bearish:
            score_structure += 10
            factors.append("Break of Structure (BOS): Candle closed below prior swing low")

    # 3. Market Session & Institutional Liquidity (Max 20 pts)
    if session == forex_sessions.SESSION_LONDON_NY_OVERLAP:
        score_session += 20
        factors.append("London / New York Overlap: Peak institutional volume and liquidity")
    elif session == forex_sessions.SESSION_LONDON:
        score_session += 15
        factors.append("London Session: Active European institutional trend volume")
    elif session == forex_sessions.SESSION_NEW_YORK:
        score_session += 14
        factors.append("New York Session: Active US market liquidity")
    elif session == forex_sessions.SESSION_ASIAN:
        score_session += 8
        factors.append("Asian Session: Moderate liquidity")
    else:
        score_session += 0
        factors.append("Off-peak / Rollover: Low liquidity condition")

    # 4. Momentum & Indicator Confluence (Max 15 pts)
    if is_buy:
        if 42.0 <= rsi_val <= 62.0:
            score_momentum += 8
            factors.append(f"RSI ({rsi_val:.1f}) in ideal trend continuation zone (42-62)")
        elif 35.0 <= rsi_val < 42.0:
            score_momentum += 5
            factors.append(f"RSI ({rsi_val:.1f}) in pullback support zone")

        if macd_hist > 0 and macd_hist >= macd_hist_prev:
            score_momentum += 7
            factors.append("MACD histogram is positive and expanding")
        elif macd_hist > -0.0003 and macd_hist > macd_hist_prev:
            score_momentum += 4
            factors.append("MACD histogram turning upward")
    else:
        if 38.0 <= rsi_val <= 58.0:
            score_momentum += 8
            factors.append(f"RSI ({rsi_val:.1f}) in ideal short continuation zone (38-58)")
        elif 58.0 < rsi_val <= 65.0:
            score_momentum += 5
            factors.append(f"RSI ({rsi_val:.1f}) in rally resistance zone")

        if macd_hist < 0 and macd_hist <= macd_hist_prev:
            score_momentum += 7
            factors.append("MACD histogram is negative and declining")
        elif macd_hist < 0.0003 and macd_hist < macd_hist_prev:
            score_momentum += 4
            factors.append("MACD histogram turning downward")

    # 5. Candle Quality & Volatility State (Max 15 pts)
    if (is_buy and close_location >= 0.55) or (not is_buy and close_location <= 0.45):
        score_candle += 8
        factors.append("Candle closed with strong directional body / rejection wick")

    if 0.7 <= atr_ratio <= 1.4:
        score_candle += 7
        factors.append(f"Volatility is stable (ATR ratio {atr_ratio:.2f}x of baseline)")
    elif atr_ratio < 0.7:
        score_candle += 3
        factors.append(f"Volatility is low (ATR ratio {atr_ratio:.2f}x)")

    total_score = min(100, max(0, score_htf + score_structure + score_session + score_momentum + score_candle))

    if total_score >= 70:
        tier = "HIGH"
    elif total_score >= 50:
        tier = "MEDIUM"
    else:
        tier = "LOW"

    breakdown = {
        "macro_htf": score_htf,
        "market_structure": score_structure,
        "session_liquidity": score_session,
        "momentum": score_momentum,
        "candle_volatility": score_candle,
    }

    summary = f"Confidence {total_score}/100 ({tier}) | {direction} setup supported by {len(factors)} confluence factors."

    return ConfidenceScore(
        score=total_score,
        tier=tier,
        passed_minimum_threshold=(total_score >= 50),
        factors=factors,
        breakdown=breakdown,
        summary=summary,
    )
