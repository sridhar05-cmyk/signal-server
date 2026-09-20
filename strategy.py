"""
Adaptive Spot Trading Strategy Engine with Market Regime Detection.
Dynamically routes to regime-tailored strategy logic:
  - TRENDING_UP: Trend-following pullback Long entries (EMA9>EMA21, MACD>0, price near EMA9).
  - TRENDING_DOWN: Trend-following rally Short entries (EMA9<EMA21, MACD<0, price near EMA9).
  - RANGING/CHOPPY: Mean-reversion entries (RSI oversold/overbought at Bollinger Band edges, 1.5x ATR target).
  - HIGH_VOLATILITY: Strict safety circuit breaker — blocks all entries (returns NO_SIGNAL).

Includes dynamic multi-stage trailing exits (breakeven at 1x ATR, trail at 2x ATR, TP at target).
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple
import pandas as pd

try:
    from . import config
    from . import indicators
    from . import regime_detector
    from .logger import get_logger
except ImportError:
    import config
    import indicators
    import regime_detector
    from logger import get_logger

logger = get_logger("strategy")


@dataclass
class EntryDecision:
    """Structure encapsulating spot trade entry evaluation."""
    signal: str                  # "BUY", "SHORT", or "HOLD"
    entry_price: float
    stop_loss: float
    take_profit: float
    atr_value: float
    risk_reward_ratio: float
    regime: str                  # "TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOLATILITY"
    rule_set: str                # e.g. "TREND_FOLLOWING_LONG", "MEAN_REVERSION_LONG", etc.
    snapshot: Dict[str, Any]


@dataclass
class ExitDecision:
    """Structure encapsulating spot trade exit evaluation."""
    should_exit: bool
    exit_reason: str             # "STOP_LOSS", "BREAKEVEN_STOP", "TRAILING_STOP", "TAKE_PROFIT", "TREND_REVERSAL"
    exit_price: float
    snapshot: Dict[str, Any]


def _round_price(val: float, ref_price: float) -> float:
    """Rounds prices and offsets dynamically based on asset scale."""
    if val is None:
        return 0.0
    if ref_price < 5:
        return round(float(val), 5)
    elif ref_price < 50:
        return round(float(val), 4)
    elif ref_price < 500:
        return round(float(val), 3)
    return round(float(val), 2)


def evaluate_entry(
    df: pd.DataFrame,
    htf_trend: Optional[str] = None,
    regime_override: Optional[regime_detector.RegimeResult] = None,
) -> EntryDecision:
    """
    Adaptive entry evaluation that branches strategy logic based on detected market regime.
    """
    min_candles = 35
    if df is None or len(df) < min_candles:
        return EntryDecision(
            signal="HOLD",
            entry_price=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            atr_value=0.0,
            risk_reward_ratio=0.0,
            regime="UNKNOWN",
            rule_set="INSUFFICIENT_DATA",
            snapshot={},
        )

    # 1. Market Regime Detection
    if regime_override is not None:
        regime_info = regime_override
    else:
        regime_info = regime_detector.detect_regime(df, htf_trend=htf_trend)

    current_regime = regime_info.regime

    # Current bar readings
    curr_idx = df.index[-1]
    curr_close = float(df.loc[curr_idx, "close"])
    curr_open = float(df.loc[curr_idx, "open"])
    curr_high = float(df.loc[curr_idx, "high"])
    curr_low = float(df.loc[curr_idx, "low"])

    # Compute Core Indicators
    ema_9_series = indicators.ema(df, 9)
    ema_21_series = indicators.ema(df, 21)
    rsi_series = indicators.rsi(df, 14)
    macd_line, macd_signal, macd_hist = indicators.macd(df, 12, 26, 9)
    bb_upper, bb_middle, bb_lower = indicators.bollinger_bands(df, 20, 2.0)
    atr_series = indicators.atr(df, config.ATR_PERIOD)

    ema_9_val = float(ema_9_series.iloc[-1])
    ema_21_val = float(ema_21_series.iloc[-1])
    rsi_val = float(rsi_series.iloc[-1])
    rsi_prev_val = float(rsi_series.iloc[-2]) if len(rsi_series) > 1 else rsi_val

    m_hist_val = float(macd_hist.iloc[-1])
    m_hist_prev_val = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else m_hist_val

    bb_up_val = float(bb_upper.iloc[-1])
    bb_mid_val = float(bb_middle.iloc[-1])
    bb_low_val = float(bb_lower.iloc[-1])
    atr_val = float(atr_series.iloc[-1])

    snapshot_base = {
        "regime": current_regime,
        "htf_trend": htf_trend,
        "price": curr_close,
        "ema9": ema_9_val,
        "ema21": ema_21_val,
        "rsi": rsi_val,
        "macd_hist": m_hist_val,
        "bb_upper": bb_up_val,
        "bb_lower": bb_low_val,
        "atr": atr_val,
    }

    # ==============================================================================
    # REGIME 1: HIGH_VOLATILITY (SAFETY CIRCUIT BREAKER - SKIP TRADING)
    # ==============================================================================
    if current_regime == "HIGH_VOLATILITY":
        logger.info(
            f"[REGIME: HIGH_VOLATILITY] ATR ratio {regime_info.atr_ratio:.2f}x above baseline. "
            f"Trade entry BLOCKED for capital preservation."
        )
        return EntryDecision(
            signal="HOLD",
            entry_price=curr_close,
            stop_loss=0.0,
            take_profit=0.0,
            atr_value=_round_price(atr_val, curr_close),
            risk_reward_ratio=0.0,
            regime=current_regime,
            rule_set="VOLATILITY_CIRCUIT_BREAKER",
            snapshot=snapshot_base,
        )

    # ==============================================================================
    # REGIME 2: TRENDING_UP (TREND-FOLLOWING PULLBACK LONGS)
    # ==============================================================================
    if current_regime == "TRENDING_UP":
        # Rule Set: Trend-following pullback entry
        c1_ema = bool(ema_9_val > ema_21_val)
        c2_macd = bool(m_hist_val > 0.0 or (m_hist_val > m_hist_prev_val and m_hist_val > -0.0002))
        c3_pullback = bool(curr_low <= (ema_9_val * 1.006) and curr_close >= (ema_21_val * 0.998))
        c4_rsi = bool(40.0 <= rsi_val <= 65.0)

        trend_long_conditions = {
            "ema9_gt_ema21": c1_ema,
            "macd_hist_positive": c2_macd,
            "pullback_near_ema9": c3_pullback,
            "rsi_momentum_40_65": c4_rsi,
        }
        score = sum(1 for met in trend_long_conditions.values() if met)
        snapshot_base["trend_long_conditions"] = trend_long_conditions
        snapshot_base["score"] = f"{score}/4"

        if score >= 3:
            stop_loss = max(0.00001, curr_close - (config.STOP_LOSS_ATR_MULT * atr_val))
            take_profit = curr_close + (config.TAKE_PROFIT_ATR_MULT * atr_val)
            rr = (take_profit - curr_close) / (curr_close - stop_loss) if (curr_close - stop_loss) > 0 else 0.0

            logger.info(
                f"[ENTRY SIGNAL: BUY] Regime: TRENDING_UP (Trend-Following) | Confluence: {score}/4 | "
                f"Close: ${curr_close:,.4f} | SL: ${stop_loss:,.4f} | TP: ${take_profit:,.4f} | R:R: 1:{rr:.2f}"
            )
            return EntryDecision(
                signal="BUY",
                entry_price=curr_close,
                stop_loss=_round_price(stop_loss, curr_close),
                take_profit=_round_price(take_profit, curr_close),
                atr_value=_round_price(atr_val, curr_close),
                risk_reward_ratio=round(rr, 2),
                regime=current_regime,
                rule_set="TREND_FOLLOWING_LONG",
                snapshot=snapshot_base,
            )

    # ==============================================================================
    # REGIME 3: TRENDING_DOWN (TREND-FOLLOWING RALLY SHORTS)
    # ==============================================================================
    elif current_regime == "TRENDING_DOWN":
        # Rule Set: Trend-following rally short entry
        c1_ema = bool(ema_9_val < ema_21_val)
        c2_macd = bool(m_hist_val < 0.0 or (m_hist_val < m_hist_prev_val and m_hist_val < 0.0002))
        c3_rally = bool(curr_high >= (ema_9_val * 0.994) and curr_close <= (ema_21_val * 1.002))
        c4_rsi = bool(35.0 <= rsi_val <= 60.0)

        trend_short_conditions = {
            "ema9_lt_ema21": c1_ema,
            "macd_hist_negative": c2_macd,
            "rally_near_ema9": c3_rally,
            "rsi_momentum_35_60": c4_rsi,
        }
        score = sum(1 for met in trend_short_conditions.values() if met)
        snapshot_base["trend_short_conditions"] = trend_short_conditions
        snapshot_base["score"] = f"{score}/4"

        if score >= 3:
            stop_loss = curr_close + (config.STOP_LOSS_ATR_MULT * atr_val)
            take_profit = max(0.00001, curr_close - (config.TAKE_PROFIT_ATR_MULT * atr_val))
            rr = (curr_close - take_profit) / (stop_loss - curr_close) if (stop_loss - curr_close) > 0 else 0.0

            logger.info(
                f"[ENTRY SIGNAL: SHORT] Regime: TRENDING_DOWN (Trend-Following) | Confluence: {score}/4 | "
                f"Close: ${curr_close:,.4f} | SL: ${stop_loss:,.4f} | TP: ${take_profit:,.4f} | R:R: 1:{rr:.2f}"
            )
            return EntryDecision(
                signal="SHORT",
                entry_price=curr_close,
                stop_loss=_round_price(stop_loss, curr_close),
                take_profit=_round_price(take_profit, curr_close),
                atr_value=_round_price(atr_val, curr_close),
                risk_reward_ratio=round(rr, 2),
                regime=current_regime,
                rule_set="TREND_FOLLOWING_SHORT",
                snapshot=snapshot_base,
            )

    # ==============================================================================
    # REGIME 4: RANGING/CHOPPY (MEAN-REVERSION BOUNDARY FADES)
    # ==============================================================================
    elif current_regime == "RANGING":
        # Mean Reversion Long: RSI oversold near lower Bollinger Band
        mr_long = bool(rsi_val <= 38.0 and (curr_low <= (bb_low_val * 1.008) or curr_close <= (bb_low_val * 1.01)))

        # Mean Reversion Short: RSI overbought near upper Bollinger Band
        mr_short = bool(rsi_val >= 62.0 and (curr_high >= (bb_up_val * 0.992) or curr_close >= (bb_up_val * 0.99)))

        snapshot_base["ranging_mean_reversion"] = {"mr_long": mr_long, "mr_short": mr_short}

        # Target middle band with conservative 1.5x ATR target
        tp_target_atr = 1.5 * atr_val
        sl_risk_atr = 1.5 * atr_val

        if mr_long:
            stop_loss = max(0.00001, curr_close - sl_risk_atr)
            take_profit = curr_close + tp_target_atr
            logger.info(
                f"[ENTRY SIGNAL: BUY] Regime: RANGING (Mean-Reversion Long) | RSI: {rsi_val:.1f} at Lower BB | "
                f"Close: ${curr_close:,.4f} | SL: ${stop_loss:,.4f} | TP: ${take_profit:,.4f} (1.5x ATR)"
            )
            return EntryDecision(
                signal="BUY",
                entry_price=curr_close,
                stop_loss=_round_price(stop_loss, curr_close),
                take_profit=_round_price(take_profit, curr_close),
                atr_value=_round_price(atr_val, curr_close),
                risk_reward_ratio=1.0,
                regime=current_regime,
                rule_set="MEAN_REVERSION_LONG",
                snapshot=snapshot_base,
            )

        if mr_short:
            stop_loss = curr_close + sl_risk_atr
            take_profit = max(0.00001, curr_close - tp_target_atr)
            logger.info(
                f"[ENTRY SIGNAL: SHORT] Regime: RANGING (Mean-Reversion Short) | RSI: {rsi_val:.1f} at Upper BB | "
                f"Close: ${curr_close:,.4f} | SL: ${stop_loss:,.4f} | TP: ${take_profit:,.4f} (1.5x ATR)"
            )
            return EntryDecision(
                signal="SHORT",
                entry_price=curr_close,
                stop_loss=_round_price(stop_loss, curr_close),
                take_profit=_round_price(take_profit, curr_close),
                atr_value=_round_price(atr_val, curr_close),
                risk_reward_ratio=1.0,
                regime=current_regime,
                rule_set="MEAN_REVERSION_SHORT",
                snapshot=snapshot_base,
            )

    return EntryDecision(
        signal="HOLD",
        entry_price=curr_close,
        stop_loss=0.0,
        take_profit=0.0,
        atr_value=_round_price(atr_val, curr_close),
        risk_reward_ratio=0.0,
        regime=current_regime,
        rule_set="NO_SETUP_MATCH",
        snapshot=snapshot_base,
    )


def generate_signal(
    df: pd.DataFrame,
    htf_trend: Optional[str] = None,
    regime_override: Optional[regime_detector.RegimeResult] = None,
) -> EntryDecision:
    """
    Evaluates entry signal across market regimes (wrapper around evaluate_entry).
    Returns EntryDecision with signal: 'BUY', 'SHORT', or 'HOLD'.
    """
    return evaluate_entry(df=df, htf_trend=htf_trend, regime_override=regime_override)


def evaluate_exit(
    curr_bar: pd.Series,
    position: Dict[str, Any],
    df_history: pd.DataFrame,
) -> ExitDecision:
    """
    Evaluates exit conditions for active positions (supports both BUY and SHORT trades).
    Implements multi-stage trailing protection:
      - 1x ATR Profit -> Move Stop to Breakeven
      - 2x ATR Profit -> Trail Stop at 1x ATR behind peak
      - Target reached -> Take Profit
      - EMA cross fallback -> Only if price never reached 1x ATR profit
    """
    direction = str(position.get("direction", "BUY")).upper()
    entry_price = float(position["entry_price"])
    take_profit = float(position["take_profit"])
    atr = float(position["atr"])
    current_sl = float(position["stop_loss"])

    curr_open = float(curr_bar["open"])
    curr_high = float(curr_bar["high"])
    curr_low = float(curr_bar["low"])
    curr_close = float(curr_bar["close"])

    reached_1x_atr = position.get("reached_1x_atr", False)
    reached_2x_atr = position.get("reached_2x_atr", False)

    # -------------------------------------------------------------------------
    # LONG POSITION EXITS (direction == "BUY")
    # -------------------------------------------------------------------------
    if direction == "BUY":
        highest_price = max(position.get("highest_price", entry_price), curr_high)
        position["highest_price"] = highest_price
        profit_distance = highest_price - entry_price

        # Advance to breakeven at 1x ATR
        if profit_distance >= (1.0 * atr):
            reached_1x_atr = True
            position["reached_1x_atr"] = True
            current_sl = max(current_sl, entry_price)

        # Trail stop at 2x ATR
        if profit_distance >= (2.0 * atr):
            reached_2x_atr = True
            position["reached_2x_atr"] = True
            trailing_level = highest_price - (1.0 * atr)
            current_sl = max(current_sl, trailing_level)

        position["stop_loss"] = current_sl

        # Check SL hit
        if curr_low <= current_sl:
            exit_price = min(curr_open, current_sl)
            reason = "TRAILING_STOP" if reached_2x_atr else ("BREAKEVEN_STOP" if reached_1x_atr else "STOP_LOSS")
            return ExitDecision(should_exit=True, exit_reason=reason, exit_price=round(exit_price, 2), snapshot={})

        # Check TP hit
        if curr_high >= take_profit:
            exit_price = max(curr_open, take_profit)
            return ExitDecision(should_exit=True, exit_reason="TAKE_PROFIT", exit_price=round(exit_price, 2), snapshot={})

        # EMA cross fallback (only if never reached 1x ATR)
        if not reached_1x_atr:
            ema_9 = float(indicators.ema(df_history, 9).iloc[-1])
            ema_21 = float(indicators.ema(df_history, 21).iloc[-1])
            if ema_9 < ema_21:
                return ExitDecision(should_exit=True, exit_reason="TREND_REVERSAL", exit_price=round(curr_close, 2), snapshot={})

    # -------------------------------------------------------------------------
    # SHORT POSITION EXITS (direction == "SHORT")
    # -------------------------------------------------------------------------
    else:
        lowest_price = min(position.get("lowest_price", entry_price), curr_low)
        position["lowest_price"] = lowest_price
        profit_distance = entry_price - lowest_price

        # Advance to breakeven at 1x ATR
        if profit_distance >= (1.0 * atr):
            reached_1x_atr = True
            position["reached_1x_atr"] = True
            current_sl = min(current_sl, entry_price)

        # Trail stop at 2x ATR
        if profit_distance >= (2.0 * atr):
            reached_2x_atr = True
            position["reached_2x_atr"] = True
            trailing_level = lowest_price + (1.0 * atr)
            current_sl = min(current_sl, trailing_level)

        position["stop_loss"] = current_sl

        # Check SL hit
        if curr_high >= current_sl:
            exit_price = max(curr_open, current_sl)
            reason = "TRAILING_STOP" if reached_2x_atr else ("BREAKEVEN_STOP" if reached_1x_atr else "STOP_LOSS")
            return ExitDecision(should_exit=True, exit_reason=reason, exit_price=round(exit_price, 2), snapshot={})

        # Check TP hit
        if curr_low <= take_profit:
            exit_price = min(curr_open, take_profit)
            return ExitDecision(should_exit=True, exit_reason="TAKE_PROFIT", exit_price=round(exit_price, 2), snapshot={})

        # EMA cross fallback (only if never reached 1x ATR)
        if not reached_1x_atr:
            ema_9 = float(indicators.ema(df_history, 9).iloc[-1])
            ema_21 = float(indicators.ema(df_history, 21).iloc[-1])
            if ema_9 > ema_21:
                return ExitDecision(should_exit=True, exit_reason="TREND_REVERSAL", exit_price=round(curr_close, 2), snapshot={})

    return ExitDecision(should_exit=False, exit_reason="", exit_price=0.0, snapshot={})
