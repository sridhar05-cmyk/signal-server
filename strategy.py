"""
Adaptive Forex Trading Strategy Engine with Market Structure & Regime Detection.
Optimized specifically for major Forex pairs (EUR/USD, GBP/USD, USD/JPY, USD/CHF, AUD/USD, USD/CAD, NZD/USD).

Key Pillars:
  1. Pair-Aware Pip & Pipette Precision (3 decimals for JPY, 5 for non-JPY).
  2. Market Structure Confirmation (Higher Highs/Lows, Lower Highs/Lows, Break of Structure).
  3. Session Liquidity Awareness (London / NY Overlap prioritized, rollover dead-zone avoided).
  4. False Signal Filters (Overextension gate, chop squeeze, candle close location).
  5. Multi-Stage Trailing Exits (Breakeven at 1.0x ATR, trailing at 2.0x ATR, structural reversal fallback).
  6. ⚠️ Zero live execution pathways — strict read-only observation.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
import pandas as pd
import numpy as np

try:
    from . import config
    from . import indicators
    from . import regime_detector
    from . import forex_utils
    from . import forex_sessions
    from . import market_structure
    from . import false_signal_filters
    from . import confidence_engine
    from . import short_tf_engine
    from .logger import get_logger
except ImportError:
    import config
    import indicators
    import regime_detector
    import forex_utils
    import forex_sessions
    import market_structure
    import false_signal_filters
    import confidence_engine
    import short_tf_engine
    from logger import get_logger

logger = get_logger("strategy")


@dataclass
class EntryDecision:
    """Structure encapsulating trade entry evaluation."""
    signal: str                  # "BUY", "SHORT", or "HOLD"
    entry_price: float
    stop_loss: float
    take_profit: float
    atr_value: float
    risk_reward_ratio: float
    regime: str                  # "TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOLATILITY"
    rule_set: str                # e.g. "TREND_FOLLOWING_LONG", "MEAN_REVERSION_LONG", etc.
    snapshot: Dict[str, Any]
    confidence: Optional[confidence_engine.ConfidenceScore] = None


@dataclass
class ExitDecision:
    """Structure encapsulating trade exit evaluation."""
    should_exit: bool
    exit_reason: str             # "STOP_LOSS", "BREAKEVEN_STOP", "TRAILING_STOP", "TAKE_PROFIT", "TREND_REVERSAL"
    exit_price: float
    snapshot: Dict[str, Any]


def _round_price(val: float, ref_price: float, pair: Optional[str] = None) -> float:
    """Rounds prices dynamically based on forex pair specifications."""
    if val is None:
        return 0.0
    if pair:
        return forex_utils.round_forex_price(val, pair)
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
    pair: str = "EUR/USD",
    timeframe: str = "1h",
    df_1m: Optional[pd.DataFrame] = None,
    df_15m: Optional[pd.DataFrame] = None,
    df_1h: Optional[pd.DataFrame] = None,
    df_4h: Optional[pd.DataFrame] = None,
    last_signal_time: Optional[int] = None,
) -> EntryDecision:
    """
    Adaptive entry evaluation that branches strategy logic based on detected market regime,
    market structure, and session liquidity for Forex pairs.
    Directs 5M and 1M requests through the short-timeframe confirmation engine.
    """
    # =========================================================================
    # SHORT-TIMEFRAME PIPELINE: 5M PRIMARY SIGNAL WITH 1M CONFIRMATION
    # =========================================================================
    if str(timeframe).lower() in ("5m", "1m"):
        curr_p = float(df["close"].iloc[-1]) if (df is not None and len(df) > 0) else 0.0
        curr_dt = df["datetime"].iloc[-1] if (df is not None and "datetime" in df.columns) else None
        mtf_ctx = short_tf_engine.build_mtf_context(
            pair=pair,
            df_4h=df_4h,
            df_1h=df_1h,
            df_15m=df_15m,
            current_time=curr_dt,
            curr_price=curr_p,
        )
        if htf_trend:
            mtf_ctx.trend_1h = htf_trend
        if regime_override:
            mtf_ctx.regime_4h = regime_override.regime

        candidate = short_tf_engine.evaluate_5m_setup(df, context=mtf_ctx, pair=pair)
        if str(timeframe).lower() == "1m":
            final_res = short_tf_engine.confirm_1m_entry(
                df_1m=df_1m if df_1m is not None else df,
                candidate_5m=candidate,
                pair=pair,
                last_signal_time=last_signal_time,
            )
        else:
            final_res = candidate

        return EntryDecision(
            signal=final_res.signal,
            entry_price=_round_price(final_res.entry_price, final_res.entry_price, pair),
            stop_loss=_round_price(final_res.stop_loss, final_res.entry_price, pair),
            take_profit=_round_price(final_res.take_profit, final_res.entry_price, pair),
            atr_value=_round_price(final_res.atr_value, final_res.entry_price, pair),
            risk_reward_ratio=final_res.risk_reward_ratio,
            regime=final_res.regime,
            rule_set=final_res.rule_set,
            snapshot=final_res.snapshot,
            confidence=final_res.confidence,
        )

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
    curr_time = df.loc[curr_idx, "datetime"] if "datetime" in df.columns else None

    # Technical Indicators
    ema_9 = indicators.ema(df, 9)
    ema_21 = indicators.ema(df, 21)
    rsi_series = indicators.rsi(df, 14)
    _, _, m_hist = indicators.macd(df, 12, 26, 9)
    bb_upper, bb_middle, bb_lower = indicators.bollinger_bands(df, 20, 2.0)
    atr_series = indicators.atr(df, 14)
    adx_series = indicators.adx(df, 14)

    ema_9_val = float(ema_9.iloc[-1])
    ema_21_val = float(ema_21.iloc[-1])
    rsi_val = float(rsi_series.iloc[-1])
    m_hist_val = float(m_hist.iloc[-1])
    m_hist_prev_val = float(m_hist.iloc[-2]) if len(m_hist) >= 2 else m_hist_val
    bb_up_val = float(bb_upper.iloc[-1])
    bb_mid_val = float(bb_middle.iloc[-1])
    bb_low_val = float(bb_lower.iloc[-1])
    atr_val = float(atr_series.iloc[-1])
    adx_val = float(adx_series.iloc[-1])

    # Market Structure & Session Awareness
    structure = market_structure.detect_market_structure(df, pair=pair, pivot_bars=3)
    active_session = forex_sessions.get_forex_session(curr_time)
    can_trade_session, session_reason = forex_sessions.is_tradeable_session(curr_time, pair=pair)

    # Candle range close location (0.0 = low, 1.0 = high)
    bar_range = curr_high - curr_low
    close_location = (curr_close - curr_low) / bar_range if bar_range > 0 else 0.5

    pip_size = forex_utils.get_pip_size(pair)
    min_sl_pips_count, max_sl_pips_count = forex_utils.get_pair_sl_bounds_pips(pair)
    min_sl_pips = min_sl_pips_count * pip_size
    max_sl_pips = max_sl_pips_count * pip_size

    snapshot_base = {
        "pair": pair,
        "regime": current_regime,
        "htf_trend": htf_trend,
        "session": active_session,
        "price": curr_close,
        "ema9": ema_9_val,
        "ema21": ema_21_val,
        "rsi": rsi_val,
        "macd_hist": m_hist_val,
        "bb_upper": bb_up_val,
        "bb_lower": bb_low_val,
        "atr": atr_val,
        "adx": adx_val,
        "structure_trend": structure.structure_trend,
        "is_hh_hl": structure.is_higher_highs_lows,
        "is_lh_ll": structure.is_lower_highs_lows,
        "bos_bullish": structure.bos_bullish,
        "bos_bearish": structure.bos_bearish,
    }

    # ==============================================================================
    # GATE 0: SESSION LIQUIDITY GATE (Avoid rollover illiquid chop)
    # ==============================================================================
    if not can_trade_session:
        logger.debug(f"[{pair}] Entry held due to session filter: {session_reason}")
        return EntryDecision(
            signal="HOLD",
            entry_price=curr_close,
            stop_loss=0.0,
            take_profit=0.0,
            atr_value=_round_price(atr_val, curr_close, pair),
            risk_reward_ratio=0.0,
            regime=current_regime,
            rule_set="SESSION_FILTER_HOLD",
            snapshot=snapshot_base,
        )

    # ==============================================================================
    # GATE 1: HIGH_VOLATILITY SAFETY CIRCUIT BREAKER
    # ==============================================================================
    if current_regime == "HIGH_VOLATILITY":
        return EntryDecision(
            signal="HOLD",
            entry_price=curr_close,
            stop_loss=0.0,
            take_profit=0.0,
            atr_value=_round_price(atr_val, curr_close, pair),
            risk_reward_ratio=0.0,
            regime=current_regime,
            rule_set="VOLATILITY_CIRCUIT_BREAKER",
            snapshot=snapshot_base,
        )

    # ==============================================================================
    # REGIME 2: TRENDING_UP (TREND-FOLLOWING PULLBACK LONGS)
    # ==============================================================================
    if current_regime == "TRENDING_UP":
        # 1. Confluence checks
        c1_ema = bool(ema_9_val > ema_21_val)
        c2_macd = bool(m_hist_val > 0.0 or (m_hist_val > m_hist_prev_val and m_hist_val > -0.0003))
        # Pullback test: Candle tested EMA9/21 region and confirmed resumption with bullish close above EMA9
        c3_pullback = bool(curr_low <= (ema_9_val + 0.3 * atr_val) and curr_close >= (ema_21_val - 0.3 * atr_val))
        c3_reversal = bool((curr_close > curr_open) and (curr_close >= ema_9_val))
        c4_rsi = bool(45.0 <= rsi_val <= 68.0)
        # Market structure confirmation
        c5_structure = bool(structure.is_higher_highs_lows or structure.bos_bullish or structure.structure_trend == "BULLISH")
        c6_htf = bool(htf_trend == "UP")

        confluence_dict = {
            "ema9_gt_ema21": c1_ema,
            "macd_positive_or_improving": c2_macd,
            "pullback_tested_ema": c3_pullback,
            "bullish_resumption_candle": c3_reversal,
            "rsi_sweet_spot": c4_rsi,
            "market_structure_bullish": c5_structure,
            "htf_macro_aligned": c6_htf,
        }
        score = sum(1 for met in confluence_dict.values() if met)
        snapshot_base["confluence"] = confluence_dict
        snapshot_base["confluence_score"] = f"{score}/7"

        if score >= 5 and c3_pullback and c3_reversal:
            # 2. False Signal Quality Filters
            res_overext = false_signal_filters.ForexSignalFilter.check_overextension(
                curr_close, ema_21_val, atr_val, rsi_val, "BUY"
            )
            res_chop = false_signal_filters.ForexSignalFilter.check_compression_chop(
                adx_val, regime_info.bb_width, regime_info.bb_width_50_avg
            )
            res_candle = false_signal_filters.ForexSignalFilter.check_candle_action(
                curr_open, curr_high, curr_low, curr_close, "BUY"
            )

            if not res_overext.passed or not res_chop.passed or not res_candle.passed:
                reject_reason = (
                    res_overext.reason if not res_overext.passed else (res_chop.reason if not res_chop.passed else res_candle.reason)
                )
                logger.debug(f"[{pair}] BUY signal filtered out: {reject_reason}")
                return EntryDecision(
                    signal="HOLD",
                    entry_price=curr_close,
                    stop_loss=0.0,
                    take_profit=0.0,
                    atr_value=_round_price(atr_val, curr_close, pair),
                    risk_reward_ratio=0.0,
                    regime=current_regime,
                    rule_set="QUALITY_FILTER_HOLD",
                    snapshot=snapshot_base,
                )

            # 3. Transparent Confidence Score
            htf_ema = float(df["htf_ema_200"].iloc[-1]) if "htf_ema_200" in df.columns else None
            confidence = confidence_engine.compute_confidence_score(
                direction="BUY",
                curr_close=curr_close,
                htf_trend=htf_trend or "UP",
                htf_ema_200=htf_ema,
                structure=structure,
                session=active_session,
                rsi_val=rsi_val,
                macd_hist=m_hist_val,
                macd_hist_prev=m_hist_prev_val,
                atr_ratio=regime_info.atr_ratio,
                close_location=close_location,
            )

            if confidence.score >= 60 and adx_val >= 20.0:
                # 4. Pair-Aware Dynamic Stop-Loss & Take-Profit
                # Use structural SL (below swing low) clamped within [12 pips, 60 pips]
                raw_sl_dist = curr_close - structure.structural_sl_long
                if raw_sl_dist < min_sl_pips:
                    sl_dist = max(min_sl_pips, 1.5 * atr_val)
                elif raw_sl_dist > max_sl_pips:
                    sl_dist = min(max_sl_pips, 2.0 * atr_val)
                else:
                    sl_dist = raw_sl_dist

                stop_loss = max(0.00001, curr_close - sl_dist)
                # Take profit at minimum 1:1.6 to 1:2.0 R:R
                rr_target = 1.8
                take_profit = curr_close + (rr_target * sl_dist)
                actual_rr = (take_profit - curr_close) / (curr_close - stop_loss) if (curr_close - stop_loss) > 0 else 0.0

                logger.info(
                    f"[{pair} ENTRY SIGNAL: BUY] Confidence: {confidence.score}/100 ({confidence.tier}) | "
                    f"Close: {forex_utils.format_forex_price(curr_close, pair)} | "
                    f"SL: {forex_utils.format_forex_price(stop_loss, pair)} (-{forex_utils.price_diff_to_pips(curr_close - stop_loss, pair):.1f}p) | "
                    f"TP: {forex_utils.format_forex_price(take_profit, pair)} (+{forex_utils.price_diff_to_pips(take_profit - curr_close, pair):.1f}p)"
                )
                return EntryDecision(
                    signal="BUY",
                    entry_price=_round_price(curr_close, curr_close, pair),
                    stop_loss=_round_price(stop_loss, curr_close, pair),
                    take_profit=_round_price(take_profit, curr_close, pair),
                    atr_value=_round_price(atr_val, curr_close, pair),
                    risk_reward_ratio=round(actual_rr, 2),
                    regime=current_regime,
                    rule_set="FOREX_TREND_PULLBACK_LONG",
                    snapshot=snapshot_base,
                    confidence=confidence,
                )

    # ==============================================================================
    # REGIME 3: TRENDING_DOWN (TREND-FOLLOWING RALLY SHORTS)
    # ==============================================================================
    elif current_regime == "TRENDING_DOWN":
        c1_ema = bool(ema_9_val < ema_21_val)
        c2_macd = bool(m_hist_val < 0.0 or (m_hist_val < m_hist_prev_val and m_hist_val < 0.0003))
        # Rally test: Candle tested EMA9/21 region and confirmed resumption with bearish close below EMA9
        c3_rally = bool(curr_high >= (ema_9_val - 0.3 * atr_val) and curr_close <= (ema_21_val + 0.3 * atr_val))
        c3_reversal = bool((curr_close < curr_open) and (curr_close <= ema_9_val))
        c4_rsi = bool(32.0 <= rsi_val <= 55.0)
        c5_structure = bool(structure.is_lower_highs_lows or structure.bos_bearish or structure.structure_trend == "BEARISH")
        c6_htf = bool(htf_trend == "DOWN")

        confluence_dict = {
            "ema9_lt_ema21": c1_ema,
            "macd_negative_or_declining": c2_macd,
            "rally_tested_ema": c3_rally,
            "bearish_resumption_candle": c3_reversal,
            "rsi_sweet_spot": c4_rsi,
            "market_structure_bearish": c5_structure,
            "htf_macro_aligned": c6_htf,
        }
        score = sum(1 for met in confluence_dict.values() if met)
        snapshot_base["confluence"] = confluence_dict
        snapshot_base["confluence_score"] = f"{score}/7"

        if score >= 5 and c3_rally and c3_reversal:
            res_overext = false_signal_filters.ForexSignalFilter.check_overextension(
                curr_close, ema_21_val, atr_val, rsi_val, "SHORT"
            )
            res_chop = false_signal_filters.ForexSignalFilter.check_compression_chop(
                adx_val, regime_info.bb_width, regime_info.bb_width_50_avg
            )
            res_candle = false_signal_filters.ForexSignalFilter.check_candle_action(
                curr_open, curr_high, curr_low, curr_close, "SHORT"
            )

            if not res_overext.passed or not res_chop.passed or not res_candle.passed:
                reject_reason = (
                    res_overext.reason if not res_overext.passed else (res_chop.reason if not res_chop.passed else res_candle.reason)
                )
                logger.debug(f"[{pair}] SHORT signal filtered out: {reject_reason}")
                return EntryDecision(
                    signal="HOLD",
                    entry_price=curr_close,
                    stop_loss=0.0,
                    take_profit=0.0,
                    atr_value=_round_price(atr_val, curr_close, pair),
                    risk_reward_ratio=0.0,
                    regime=current_regime,
                    rule_set="QUALITY_FILTER_HOLD",
                    snapshot=snapshot_base,
                )

            htf_ema = float(df["htf_ema_200"].iloc[-1]) if "htf_ema_200" in df.columns else None
            confidence = confidence_engine.compute_confidence_score(
                direction="SHORT",
                curr_close=curr_close,
                htf_trend=htf_trend or "DOWN",
                htf_ema_200=htf_ema,
                structure=structure,
                session=active_session,
                rsi_val=rsi_val,
                macd_hist=m_hist_val,
                macd_hist_prev=m_hist_prev_val,
                atr_ratio=regime_info.atr_ratio,
                close_location=close_location,
            )

            if confidence.score >= 60 and adx_val >= 20.0:
                raw_sl_dist = structure.structural_sl_short - curr_close
                if raw_sl_dist < min_sl_pips:
                    sl_dist = max(min_sl_pips, 1.5 * atr_val)
                elif raw_sl_dist > max_sl_pips:
                    sl_dist = min(max_sl_pips, 2.0 * atr_val)
                else:
                    sl_dist = raw_sl_dist

                stop_loss = curr_close + sl_dist
                rr_target = 1.8
                take_profit = max(0.00001, curr_close - (rr_target * sl_dist))
                actual_rr = (curr_close - take_profit) / (stop_loss - curr_close) if (stop_loss - curr_close) > 0 else 0.0

                logger.info(
                    f"[{pair} ENTRY SIGNAL: SHORT] Confidence: {confidence.score}/100 ({confidence.tier}) | "
                    f"Close: {forex_utils.format_forex_price(curr_close, pair)} | "
                    f"SL: {forex_utils.format_forex_price(stop_loss, pair)} (+{forex_utils.price_diff_to_pips(stop_loss - curr_close, pair):.1f}p) | "
                    f"TP: {forex_utils.format_forex_price(take_profit, pair)} (-{forex_utils.price_diff_to_pips(curr_close - take_profit, pair):.1f}p)"
                )
                return EntryDecision(
                    signal="SHORT",
                    entry_price=_round_price(curr_close, curr_close, pair),
                    stop_loss=_round_price(stop_loss, curr_close, pair),
                    take_profit=_round_price(take_profit, curr_close, pair),
                    atr_value=_round_price(atr_val, curr_close, pair),
                    risk_reward_ratio=round(actual_rr, 2),
                    regime=current_regime,
                    rule_set="FOREX_TREND_RALLY_SHORT",
                    snapshot=snapshot_base,
                    confidence=confidence,
                )

    # ==============================================================================
    # REGIME 4: RANGING/CHOPPY (STRICT MEAN-REVERSION AT BOUNDARIES)
    # ==============================================================================
    elif current_regime in ("RANGING", "LOW_VOLATILITY"):
        # Mean reversion on outer Bollinger Band rejection when market is not trending (ADX < 24)
        prev_low = float(df["low"].iloc[-2]) if len(df) >= 2 else curr_low
        prev_high = float(df["high"].iloc[-2]) if len(df) >= 2 else curr_high
        prev_bb_low = float(bb_lower.iloc[-2]) if len(bb_lower) >= 2 else bb_low_val
        prev_bb_up = float(bb_upper.iloc[-2]) if len(bb_upper) >= 2 else bb_up_val

        mr_long = bool((rsi_val <= 38.0) and (curr_low <= bb_low_val or prev_low <= prev_bb_low) and (curr_close > curr_open) and (adx_val < 24.0))
        mr_short = bool((rsi_val >= 62.0) and (curr_high >= bb_up_val or prev_high >= prev_bb_up) and (curr_close < curr_open) and (adx_val < 24.0))

        if mr_long:
            sl_dist = max(min_sl_pips, 1.2 * atr_val)
            stop_loss = max(0.00001, curr_close - sl_dist)
            # Target middle band (20 SMA mean)
            take_profit = max(curr_close + sl_dist, bb_mid_val)
            actual_rr = (take_profit - curr_close) / (curr_close - stop_loss) if (curr_close - stop_loss) > 0 else 0.0

            return EntryDecision(
                signal="BUY",
                entry_price=_round_price(curr_close, curr_close, pair),
                stop_loss=_round_price(stop_loss, curr_close, pair),
                take_profit=_round_price(take_profit, curr_close, pair),
                atr_value=_round_price(atr_val, curr_close, pair),
                risk_reward_ratio=round(actual_rr, 2),
                regime=current_regime,
                rule_set="FOREX_MEAN_REVERSION_LONG",
                snapshot=snapshot_base,
            )

        if mr_short:
            sl_dist = max(min_sl_pips, 1.2 * atr_val)
            stop_loss = curr_close + sl_dist
            take_profit = min(curr_close - sl_dist, bb_mid_val)
            actual_rr = (curr_close - take_profit) / (stop_loss - curr_close) if (stop_loss - curr_close) > 0 else 0.0

            return EntryDecision(
                signal="SHORT",
                entry_price=_round_price(curr_close, curr_close, pair),
                stop_loss=_round_price(stop_loss, curr_close, pair),
                take_profit=_round_price(take_profit, curr_close, pair),
                atr_value=_round_price(atr_val, curr_close, pair),
                risk_reward_ratio=round(actual_rr, 2),
                regime=current_regime,
                rule_set="FOREX_MEAN_REVERSION_SHORT",
                snapshot=snapshot_base,
            )

    return EntryDecision(
        signal="HOLD",
        entry_price=curr_close,
        stop_loss=0.0,
        take_profit=0.0,
        atr_value=_round_price(atr_val, curr_close, pair),
        risk_reward_ratio=0.0,
        regime=current_regime,
        rule_set="NO_SETUP_MATCH",
        snapshot=snapshot_base,
    )


def generate_signal(
    df: pd.DataFrame,
    htf_trend: Optional[str] = None,
    regime_override: Optional[regime_detector.RegimeResult] = None,
    pair: str = "EUR/USD",
    timeframe: str = "1h",
    df_1m: Optional[pd.DataFrame] = None,
    df_15m: Optional[pd.DataFrame] = None,
    df_1h: Optional[pd.DataFrame] = None,
    df_4h: Optional[pd.DataFrame] = None,
    last_signal_time: Optional[int] = None,
) -> EntryDecision:
    """Wrapper around evaluate_entry maintaining backward compatibility."""
    return evaluate_entry(
        df=df,
        htf_trend=htf_trend,
        regime_override=regime_override,
        pair=pair,
        timeframe=timeframe,
        df_1m=df_1m,
        df_15m=df_15m,
        df_1h=df_1h,
        df_4h=df_4h,
        last_signal_time=last_signal_time,
    )


def evaluate_exit(
    curr_bar: pd.Series,
    position: Dict[str, Any],
    df_history: pd.DataFrame,
    pair: str = "EUR/USD",
) -> ExitDecision:
    """
    Evaluates exit conditions for active positions with pair-accurate pip decimals
    and robust asymmetric reward-to-risk exits:
      1. TP target reached -> TAKE_PROFIT
      2. SL hit -> STOP_LOSS / BREAKEVEN_STOP / TRAILING_STOP
      3. Move to Breakeven (+ spread buffer) ONLY when trade reaches +1.0R profit (eliminating the premature +0.3 ATR choke).
      4. Dynamic ATR + Structure trailing stop once trade reaches +1.5R profit (trailing 1.5x ATR behind extreme).
      5. Stagnation exit: if held for 36+ bars and hovering near breakeven (current_r < 0.2).
    """
    direction = str(position.get("direction", "BUY")).upper()
    entry_price = float(position["entry_price"])
    take_profit = float(position["take_profit"])
    atr = float(position["atr"])
    current_sl = float(position["stop_loss"])
    bars_held = int(position.get("bars_held", 0))

    curr_open = float(curr_bar["open"])
    curr_high = float(curr_bar["high"])
    curr_low = float(curr_bar["low"])
    curr_close = float(curr_bar["close"])

    # Calculate initial risk distance (1.0R)
    initial_sl = float(position.get("initial_sl", current_sl))
    r_distance = abs(entry_price - initial_sl)
    pip_val = forex_utils.get_pip_size(pair)
    if r_distance <= 0:
        r_distance = max(atr, 15.0 * pip_val)

    reached_1r = position.get("reached_1r", False)
    reached_trail = position.get("reached_trail", False)

    spread_buffer = forex_utils.get_spread_cost_in_price(pair) * 1.2

    # -------------------------------------------------------------------------
    # BUY POSITION EXITS
    # -------------------------------------------------------------------------
    if direction == "BUY":
        highest_price = max(position.get("highest_price", entry_price), curr_high)
        position["highest_price"] = highest_price
        profit_distance = highest_price - entry_price
        current_r = profit_distance / r_distance

        # 1. Validated Breakeven: move stop to Entry + spread buffer ONLY at +1.0R
        if current_r >= 1.0:
            reached_1r = True
            position["reached_1r"] = True
            current_sl = max(current_sl, entry_price + spread_buffer)

        # 2. Dynamic Structure + ATR Trailing Stop at +1.5R
        if current_r >= 1.5:
            reached_trail = True
            position["reached_trail"] = True
            trailing_level = highest_price - (1.5 * atr)
            current_sl = max(current_sl, trailing_level)

            # Check market structure for confirmed higher swing low
            if df_history is not None and len(df_history) >= 20:
                struct = market_structure.detect_market_structure(df_history, pair=pair, pivot_bars=3)
                if struct.last_swing_low > entry_price and struct.swing_low_age >= 3:
                    current_sl = max(current_sl, struct.last_swing_low - (0.2 * atr))

        position["stop_loss"] = current_sl

        # Check SL hit
        if curr_low <= current_sl:
            exit_price = min(curr_open, current_sl)
            reason = "TRAILING_STOP" if reached_trail else ("BREAKEVEN_STOP" if reached_1r else "STOP_LOSS")
            return ExitDecision(
                should_exit=True,
                exit_reason=reason,
                exit_price=_round_price(exit_price, entry_price, pair),
                snapshot={},
            )

        # Check TP hit
        if curr_high >= take_profit:
            exit_price = max(curr_open, take_profit)
            return ExitDecision(
                should_exit=True,
                exit_reason="TAKE_PROFIT",
                exit_price=_round_price(exit_price, entry_price, pair),
                snapshot={},
            )

        # Stagnation exit (24 bars on short-TF, 36 on higher-TF if hovering near breakeven)
        tf = str(position.get("timeframe", "1h")).lower()
        stag_limit = 24 if tf in ("5m", "1m") else 36
        if bars_held >= stag_limit and current_r < 0.2:
            return ExitDecision(
                should_exit=True,
                exit_reason="TIME_STAGNATION",
                exit_price=_round_price(curr_close, entry_price, pair),
                snapshot={},
            )

    # -------------------------------------------------------------------------
    # SHORT POSITION EXITS
    # -------------------------------------------------------------------------
    else:
        lowest_price = min(position.get("lowest_price", entry_price), curr_low)
        position["lowest_price"] = lowest_price
        profit_distance = entry_price - lowest_price
        current_r = profit_distance / r_distance

        # 1. Validated Breakeven: move stop to Entry - spread buffer ONLY at +1.0R
        if current_r >= 1.0:
            reached_1r = True
            position["reached_1r"] = True
            current_sl = min(current_sl, entry_price - spread_buffer)

        # 2. Dynamic Structure + ATR Trailing Stop at +1.5R
        if current_r >= 1.5:
            reached_trail = True
            position["reached_trail"] = True
            trailing_level = lowest_price + (1.5 * atr)
            current_sl = min(current_sl, trailing_level)

            if df_history is not None and len(df_history) >= 20:
                struct = market_structure.detect_market_structure(df_history, pair=pair, pivot_bars=3)
                if struct.last_swing_high < entry_price and struct.swing_high_age >= 3:
                    current_sl = min(current_sl, struct.last_swing_high + (0.2 * atr))

        position["stop_loss"] = current_sl

        # Check SL hit
        if curr_high >= current_sl:
            exit_price = max(curr_open, current_sl)
            reason = "TRAILING_STOP" if reached_trail else ("BREAKEVEN_STOP" if reached_1r else "STOP_LOSS")
            return ExitDecision(
                should_exit=True,
                exit_reason=reason,
                exit_price=_round_price(exit_price, entry_price, pair),
                snapshot={},
            )

        # Check TP hit
        if curr_low <= take_profit:
            exit_price = min(curr_open, take_profit)
            return ExitDecision(
                should_exit=True,
                exit_reason="TAKE_PROFIT",
                exit_price=_round_price(exit_price, entry_price, pair),
                snapshot={},
            )

        # Stagnation exit (24 bars on short-TF, 36 on higher-TF if hovering near breakeven)
        tf = str(position.get("timeframe", "1h")).lower()
        stag_limit = 24 if tf in ("5m", "1m") else 36
        if bars_held >= stag_limit and current_r < 0.2:
            return ExitDecision(
                should_exit=True,
                exit_reason="TIME_STAGNATION",
                exit_price=_round_price(curr_close, entry_price, pair),
                snapshot={},
            )

    return ExitDecision(should_exit=False, exit_reason="", exit_price=0.0, snapshot={})
