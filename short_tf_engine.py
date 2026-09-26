"""
Short-Timeframe Forex Signal Engine: 5M Primary Signals with 1M Entry Confirmation.

Architecture & Signal Flow:
  4H Market Regime
  -> 1H Directional Trend / Context
  -> 15M Market Structure & Key Levels / Barrier Check
  -> 5M PRIMARY BUY/SELL Setup Generator
  -> 1M Final Entry Confirmation (closed-candle, anti-flicker, anti-duplicate)
  -> Output: BUY / SELL / NO_SIGNAL (HOLD)

Constraints:
  - 1M NEVER generates an independent signal when 5M disagrees.
  - Closed-candle confirmation only (zero intrabar flickering).
  - Anti-duplicate debounce prevents consecutive signals on the same candle.
  - Realistic spread and slippage filters.
  - Zero live broker order placement.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List
import pandas as pd
import numpy as np

try:
    from . import indicators
    from . import regime_detector
    from . import market_structure
    from . import forex_utils
    from . import forex_sessions
    from . import confidence_engine
    from .logger import get_logger
except ImportError:
    import indicators
    import regime_detector
    import market_structure
    import forex_utils
    import forex_sessions
    import confidence_engine
    from logger import get_logger

logger = get_logger("short_tf_engine")


@dataclass
class ShortTFContext:
    """Multi-timeframe contextual state across 4H, 1H, and 15M."""
    regime_4h: str = "RANGING"
    trend_1h: str = "SIDEWAYS"
    ema50_1h: float = 0.0
    ema200_1h: float = 0.0
    structure_15m: Optional[market_structure.MarketStructure] = None
    is_opposed_15m_long: bool = False
    is_opposed_15m_short: bool = False
    active_session: str = "NEW_YORK"
    can_trade_session: bool = True
    session_reason: str = ""


@dataclass
class ShortTFDecision:
    """Standardized decision payload for 5M and 1M short-timeframe operations."""
    signal: str = "HOLD"  # "BUY", "SHORT", or "HOLD"
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    atr_value: float = 0.0
    risk_reward_ratio: float = 0.0
    primary_tf: str = "5m"
    confirmation_tf: str = "1m"
    is_confirmed_1m: bool = False
    regime: str = "RANGING"
    rule_set: str = "NO_SETUP_MATCH"
    snapshot: Dict[str, Any] = field(default_factory=dict)
    confidence: Optional[confidence_engine.ConfidenceResult] = None


def build_mtf_context(
    pair: str,
    df_4h: Optional[pd.DataFrame] = None,
    df_1h: Optional[pd.DataFrame] = None,
    df_15m: Optional[pd.DataFrame] = None,
    current_time: Optional[pd.Timestamp] = None,
    curr_price: float = 0.0,
) -> ShortTFContext:
    """
    Builds the higher-timeframe context filter (4H Regime, 1H Trend, 15M Structure).
    """
    ctx = ShortTFContext()

    # Determine active trading session
    dt = current_time.to_pydatetime() if isinstance(current_time, pd.Timestamp) else datetime.now(timezone.utc)
    ctx.active_session = forex_sessions.classify_session(dt)
    ctx.can_trade_session, ctx.session_reason = forex_sessions.is_tradeable_session(dt, pair=pair)

    # 1. 4H Market Regime
    if df_4h is not None and len(df_4h) >= 30:
        res_4h = regime_detector.detect_regime(df_4h)
        ctx.regime_4h = res_4h.regime
    else:
        ctx.regime_4h = "RANGING"

    # 2. 1H Directional Trend & EMA Stack
    if df_1h is not None and len(df_1h) >= 50:
        c_1h = df_1h["close"].astype(float)
        ema50_s = indicators.ema(df_1h, 50)
        ema200_s = indicators.ema(df_1h, min(200, len(df_1h) - 1)) if len(df_1h) >= 100 else ema50_s
        ctx.ema50_1h = float(ema50_s.iloc[-1])
        ctx.ema200_1h = float(ema200_s.iloc[-1])

        # Trend direction
        if ctx.ema50_1h > ctx.ema200_1h and float(c_1h.iloc[-1]) > ctx.ema50_1h:
            ctx.trend_1h = "UP"
        elif ctx.ema50_1h < ctx.ema200_1h and float(c_1h.iloc[-1]) < ctx.ema50_1h:
            ctx.trend_1h = "DOWN"
        else:
            ctx.trend_1h = "SIDEWAYS"
    else:
        ctx.trend_1h = "SIDEWAYS"

    # 3. 15M Market Structure & S/R Barriers
    if df_15m is not None and len(df_15m) >= 25:
        ctx.structure_15m = market_structure.detect_market_structure(df_15m, pair=pair, pivot_bars=3)
        atr_15m_s = indicators.atr(df_15m, 14)
        atr_15m = float(atr_15m_s.iloc[-1]) if not atr_15m_s.empty else 0.0010

        # Check proximity to opposing 15M swing pivots
        if curr_price > 0 and ctx.structure_15m.last_swing_high > 0:
            dist_to_res = ctx.structure_15m.last_swing_high - curr_price
            if 0 < dist_to_res < (0.5 * atr_15m):
                ctx.is_opposed_15m_long = True

        if curr_price > 0 and ctx.structure_15m.last_swing_low > 0:
            dist_to_sup = curr_price - ctx.structure_15m.last_swing_low
            if 0 < dist_to_sup < (0.5 * atr_15m):
                ctx.is_opposed_15m_short = True

    return ctx


def evaluate_5m_setup(
    df_5m: pd.DataFrame,
    context: ShortTFContext,
    pair: str = "EUR/USD",
    exclude_1h_sideways: bool = True,
    trending_up_max_ext_atr: float = 0.80,
    default_max_ext_atr: float = 1.40,
) -> ShortTFDecision:
    """
    Primary 5-Minute Setup Generator with Phase 7 Mod C Entry Quality Filters:
      - 1H SIDEWAYS Exclusion (Mod A): Rejects entry when 1H trend is SIDEWAYS
      - 4H TRENDING_UP Overextension Filter (Mod B): Caps 5M EMA50 extension at 0.80 ATR in TRENDING_UP
      - 4H / 1H / 15M context gates
      - 5M EMA alignment (9/21/50)
      - 5M RSI sweet spot
      - 5M MACD momentum
      - 5M ADX trend strength
      - Dynamic ATR & pair-aware SL/TP bounds
      - Session liquidity & spread filter
      - Candle-close confirmation
    """
    pip_val = forex_utils.get_pip_size(pair)
    min_candles = 35

    if df_5m is None or len(df_5m) < min_candles:
        return ShortTFDecision(
            signal="HOLD",
            primary_tf="5m",
            regime=context.regime_4h,
            rule_set="INSUFFICIENT_5M_DATA",
        )

    # 1. Gate: 4H High Volatility Circuit Breaker
    if context.regime_4h == "HIGH_VOLATILITY":
        return ShortTFDecision(
            signal="HOLD",
            entry_price=float(df_5m["close"].iloc[-1]),
            primary_tf="5m",
            regime=context.regime_4h,
            rule_set="4H_HIGH_VOLATILITY_HOLD",
        )

    # 2. Gate: Session Liquidity (Avoid rollover dead-zone 21:00-23:00 UTC)
    if not context.can_trade_session:
        return ShortTFDecision(
            signal="HOLD",
            entry_price=float(df_5m["close"].iloc[-1]),
            primary_tf="5m",
            regime=context.regime_4h,
            rule_set=f"SESSION_FILTER_HOLD_{context.active_session}",
        )

    # Extract 5M series and indicators
    c = df_5m["close"].astype(float)
    o = df_5m["open"].astype(float)
    h = df_5m["high"].astype(float)
    l = df_5m["low"].astype(float)

    curr_close = float(c.iloc[-1])
    curr_open = float(o.iloc[-1])
    curr_high = float(h.iloc[-1])
    curr_low = float(l.iloc[-1])
    bar_range = curr_high - curr_low

    ema_9 = indicators.ema(df_5m, 9)
    ema_21 = indicators.ema(df_5m, 21)
    ema_50 = indicators.ema(df_5m, 50) if len(df_5m) >= 50 else ema_21
    rsi_s = indicators.rsi(df_5m, 14)
    macd_line, macd_sig, macd_hist = indicators.macd(df_5m, 12, 26, 9)
    adx_s = indicators.adx(df_5m, 14)
    atr_s = indicators.atr(df_5m, 14)
    bb_mid, bb_upper, bb_lower = indicators.bollinger_bands(df_5m, 20, 2.0)

    ema_9_val = float(ema_9.iloc[-1])
    ema_21_val = float(ema_21.iloc[-1])
    ema_50_val = float(ema_50.iloc[-1])
    rsi_val = float(rsi_s.iloc[-1])
    m_hist_val = float(macd_hist.iloc[-1])
    m_hist_prev_val = float(macd_hist.iloc[-2]) if len(macd_hist) >= 2 else m_hist_val
    adx_val = float(adx_s.iloc[-1])
    atr_val = float(atr_s.iloc[-1]) if not atr_s.empty else (10.0 * pip_val)
    if atr_val <= 0:
        atr_val = 10.0 * pip_val

    # 3. Gate: Spread Filter (Spread cost cannot exceed 25% of 5M ATR)
    spread_cost = forex_utils.get_spread_cost_in_price(pair)
    spread_pips = forex_utils.get_pair_spread_pips(pair)
    max_pair_spread = 2.5 if "JPY" not in pair else 3.0
    if (spread_cost > 0.25 * atr_val) or (spread_pips > max_pair_spread):
        return ShortTFDecision(
            signal="HOLD",
            entry_price=curr_close,
            atr_value=atr_val,
            primary_tf="5m",
            regime=context.regime_4h,
            rule_set="SPREAD_EXCESSIVE_HOLD",
        )

    # 4. Gate: Flat-bar / Stale data filter
    if bar_range == 0.0 or (len(df_5m) >= 2 and float(c.iloc[-2]) == curr_close and float(o.iloc[-2]) == curr_open):
        return ShortTFDecision(
            signal="HOLD",
            entry_price=curr_close,
            primary_tf="5m",
            regime=context.regime_4h,
            rule_set="STALE_DATA_HOLD",
        )

    # 5. Gate: Mod C Filter A - 1H SIDEWAYS Exclusion
    if exclude_1h_sideways and context.trend_1h == "SIDEWAYS":
        return ShortTFDecision(
            signal="HOLD",
            entry_price=curr_close,
            atr_value=atr_val,
            primary_tf="5m",
            regime=context.regime_4h,
            rule_set="1H_SIDEWAYS_EXCLUSION_HOLD",
        )

    # Detect 5M Market Structure
    struct_5m = market_structure.detect_market_structure(df_5m, pair=pair, pivot_bars=3)

    # Pair-aware SL bounds for 5M timeframe (tight short-TF stops)
    min_sl_pips = 8.0 if "JPY" not in pair else 10.0
    max_sl_pips = 30.0 if "JPY" not in pair else 35.0
    min_sl_dist = min_sl_pips * pip_val
    max_sl_dist = max_sl_pips * pip_val

    # Mod C Filter B: 4H TRENDING_UP Overextension Filter
    max_extension_atr = trending_up_max_ext_atr if context.regime_4h == "TRENDING_UP" else default_max_ext_atr
    ext_long_atr = (curr_close - ema_50_val) / atr_val
    ext_short_atr = (ema_50_val - curr_close) / atr_val
    not_overextended_long = bool((curr_close - ema_50_val) <= (max_extension_atr * atr_val))
    not_overextended_short = bool((ema_50_val - curr_close) <= (max_extension_atr * atr_val))

    snapshot_base = {
        "pair": pair,
        "timeframe": "5m",
        "price": curr_close,
        "session": context.active_session,
        "regime_4h": context.regime_4h,
        "trend_1h": context.trend_1h,
        "ema9": ema_9_val,
        "ema21": ema_21_val,
        "ema50": ema_50_val,
        "rsi": rsi_val,
        "macd_hist": m_hist_val,
        "adx": adx_val,
        "atr": atr_val,
        "ext_5m_atr_long": round(ext_long_atr, 3),
        "ext_5m_atr_short": round(ext_short_atr, 3),
        "max_extension_atr": max_extension_atr,
        "is_opposed_15m_long": context.is_opposed_15m_long,
        "is_opposed_15m_short": context.is_opposed_15m_short,
    }

    # =========================================================================
    # 5M BUY SETUP EVALUATION
    # =========================================================================
    long_aligned = bool((context.trend_1h == "UP" if exclude_1h_sideways else context.trend_1h in ("UP", "SIDEWAYS")) and not context.is_opposed_15m_long)
    ema_bullish_stack = bool(ema_9_val > ema_21_val and curr_close >= ema_50_val)
    pullback_tested_ema = bool(curr_low <= (ema_9_val + 0.3 * atr_val) and curr_close >= (ema_21_val - 0.3 * atr_val))
    bullish_resumption = bool(curr_close > curr_open and curr_close >= ema_9_val)
    rsi_sweet_spot_long = bool(42.0 <= rsi_val <= 68.0)
    macd_improving_long = bool(m_hist_val > 0.0 or (m_hist_val > m_hist_prev_val and m_hist_val > -0.0002))
    adx_momentum = bool(adx_val >= 20.0)

    # Opposing wick check: upper rejection wick cannot exceed 40% of bar range
    upper_wick = curr_high - max(curr_open, curr_close)
    wick_ok_long = bool(bar_range > 0 and (upper_wick / bar_range) <= 0.40)

    if (
        long_aligned
        and ema_bullish_stack
        and pullback_tested_ema
        and bullish_resumption
        and rsi_sweet_spot_long
        and macd_improving_long
        and adx_momentum
        and wick_ok_long
        and not_overextended_long
    ):
        # Calculate dynamic SL
        raw_sl_dist = curr_close - struct_5m.structural_sl_long
        if raw_sl_dist < min_sl_dist:
            sl_dist = max(min_sl_dist, 1.4 * atr_val)
        elif raw_sl_dist > max_sl_dist:
            sl_dist = min(max_sl_dist, 2.0 * atr_val)
        else:
            sl_dist = raw_sl_dist

        stop_loss = max(0.00001, curr_close - sl_dist)
        take_profit = curr_close + (1.8 * sl_dist)
        rr = (take_profit - curr_close) / (curr_close - stop_loss) if (curr_close - stop_loss) > 0 else 0.0

        if rr >= 1.5:
            return ShortTFDecision(
                signal="BUY",
                entry_price=curr_close,
                stop_loss=stop_loss,
                take_profit=take_profit,
                atr_value=atr_val,
                risk_reward_ratio=round(rr, 2),
                primary_tf="5m",
                confirmation_tf="1m",
                is_confirmed_1m=False,
                regime=context.regime_4h,
                rule_set="5M_TREND_PULLBACK_LONG_PENDING_1M",
                snapshot=snapshot_base,
            )

    # =========================================================================
    # 5M SHORT SETUP EVALUATION
    # =========================================================================
    short_aligned = bool((context.trend_1h == "DOWN" if exclude_1h_sideways else context.trend_1h in ("DOWN", "SIDEWAYS")) and not context.is_opposed_15m_short)
    ema_bearish_stack = bool(ema_9_val < ema_21_val and curr_close <= ema_50_val)
    rally_tested_ema = bool(curr_high >= (ema_9_val - 0.3 * atr_val) and curr_close <= (ema_21_val + 0.3 * atr_val))
    bearish_resumption = bool(curr_close < curr_open and curr_close <= ema_9_val)
    rsi_sweet_spot_short = bool(32.0 <= rsi_val <= 58.0)
    macd_declining_short = bool(m_hist_val < 0.0 or (m_hist_val < m_hist_prev_val and m_hist_val < 0.0002))

    # Opposing wick check: lower rejection wick cannot exceed 40% of bar range
    lower_wick = min(curr_open, curr_close) - curr_low
    wick_ok_short = bool(bar_range > 0 and (lower_wick / bar_range) <= 0.40)

    if (
        short_aligned
        and ema_bearish_stack
        and rally_tested_ema
        and bearish_resumption
        and rsi_sweet_spot_short
        and macd_declining_short
        and adx_momentum
        and wick_ok_short
        and not_overextended_short
    ):
        raw_sl_dist = struct_5m.structural_sl_short - curr_close
        if raw_sl_dist < min_sl_dist:
            sl_dist = max(min_sl_dist, 1.4 * atr_val)
        elif raw_sl_dist > max_sl_dist:
            sl_dist = min(max_sl_dist, 2.0 * atr_val)
        else:
            sl_dist = raw_sl_dist

        stop_loss = curr_close + sl_dist
        take_profit = max(0.00001, curr_close - (1.8 * sl_dist))
        rr = (curr_close - take_profit) / (stop_loss - curr_close) if (stop_loss - curr_close) > 0 else 0.0

        if rr >= 1.5:
            return ShortTFDecision(
                signal="SHORT",
                entry_price=curr_close,
                stop_loss=stop_loss,
                take_profit=take_profit,
                atr_value=atr_val,
                risk_reward_ratio=round(rr, 2),
                primary_tf="5m",
                confirmation_tf="1m",
                is_confirmed_1m=False,
                regime=context.regime_4h,
                rule_set="5M_TREND_RALLY_SHORT_PENDING_1M",
                snapshot=snapshot_base,
            )

    return ShortTFDecision(
        signal="HOLD",
        entry_price=curr_close,
        atr_value=atr_val,
        primary_tf="5m",
        regime=context.regime_4h,
        rule_set="NO_5M_SETUP_MATCH",
        snapshot=snapshot_base,
    )


def confirm_1m_entry(
    df_1m: Optional[pd.DataFrame],
    candidate_5m: ShortTFDecision,
    pair: str = "EUR/USD",
    last_signal_time: Optional[int] = None,
) -> ShortTFDecision:
    """
    Final 1-Minute Entry Confirmation Layer.
    CRITICAL RULES:
      1. 1M NEVER generates an independent signal when 5M is HOLD / NO_SIGNAL.
      2. Anti-flicker: requires completed 1M candle close.
      3. Verifies 1M momentum & direction align with candidate 5M direction.
      4. Anti-duplicate debounce: blocks multiple triggers in the same setup window.
    """
    # Rule 1: Disagreement with 5M produces immediate HOLD
    if candidate_5m.signal not in ("BUY", "SHORT"):
        return candidate_5m

    # If no 1M data is provided, return candidate with pending tag or hold
    if df_1m is None or len(df_1m) < 15:
        logger.debug(f"[{pair}] 1M data unavailable or insufficient for confirmation.")
        unconfirmed = ShortTFDecision(
            signal="HOLD",
            entry_price=candidate_5m.entry_price,
            stop_loss=candidate_5m.stop_loss,
            take_profit=candidate_5m.take_profit,
            atr_value=candidate_5m.atr_value,
            risk_reward_ratio=candidate_5m.risk_reward_ratio,
            primary_tf="5m",
            confirmation_tf="1m",
            is_confirmed_1m=False,
            regime=candidate_5m.regime,
            rule_set="1M_DATA_UNAVAILABLE_HOLD",
            snapshot=candidate_5m.snapshot,
        )
        return unconfirmed

    # Rule 4: Anti-duplicate debounce (prevents multiple signals within 300 seconds / 5 mins)
    c_1m = df_1m["close"].astype(float)
    o_1m = df_1m["open"].astype(float)
    t_1m = df_1m["time"].values if "time" in df_1m.columns else None

    if t_1m is not None and len(t_1m) > 0 and last_signal_time is not None:
        curr_1m_timestamp = int(t_1m[-1])
        if (curr_1m_timestamp - last_signal_time) < (5 * 60 * 1000):
            # Within same 5m debounce window
            debounced = ShortTFDecision(
                signal="HOLD",
                entry_price=candidate_5m.entry_price,
                primary_tf="5m",
                confirmation_tf="1m",
                is_confirmed_1m=False,
                regime=candidate_5m.regime,
                rule_set="1M_DEBOUNCE_COOLDOWN_HOLD",
                snapshot=candidate_5m.snapshot,
            )
            return debounced

    curr_1m_close = float(c_1m.iloc[-1])
    curr_1m_open = float(o_1m.iloc[-1])
    ema_9_1m = float(indicators.ema(df_1m, 9).iloc[-1])
    rsi_1m = float(indicators.rsi(df_1m, 14).iloc[-1])

    is_futures = False
    proxy_sym = ""
    if "is_futures_proxy" in df_1m.columns:
        is_futures = bool(df_1m["is_futures_proxy"].iloc[-1])
        proxy_sym = str(df_1m["proxy_ticker"].iloc[-1]) if "proxy_ticker" in df_1m.columns else ""
    elif forex_utils.is_futures_proxy_required(pair):
        is_futures = True
        proxy_sym = forex_utils.get_futures_proxy_ticker(pair) or ""

    # Derive spot-accurate entry price preserving spot pip precision
    if is_futures:
        intrabar_delta = curr_1m_close - curr_1m_open
        effective_entry = forex_utils.round_forex_price(candidate_5m.entry_price + intrabar_delta, pair)
    else:
        effective_entry = forex_utils.round_forex_price(curr_1m_close, pair)

    snapshot_out = dict(candidate_5m.snapshot)
    snapshot_out["is_futures_proxy"] = is_futures
    snapshot_out["proxy_ticker"] = proxy_sym

    # Rule 3: Directional momentum confirmation on closed candle
    if candidate_5m.signal == "BUY":
        # 1M candle must be green and close above 1M EMA9 with RSI >= 45
        green_candle = bool(curr_1m_close > curr_1m_open)
        momentum_1m = bool(curr_1m_close >= (ema_9_1m - 0.00005) and rsi_1m >= 45.0)

        if green_candle and momentum_1m:
            confirmed = ShortTFDecision(
                signal="BUY",
                entry_price=effective_entry,
                stop_loss=candidate_5m.stop_loss,
                take_profit=candidate_5m.take_profit,
                atr_value=candidate_5m.atr_value,
                risk_reward_ratio=candidate_5m.risk_reward_ratio,
                primary_tf="5m",
                confirmation_tf="1m",
                is_confirmed_1m=True,
                regime=candidate_5m.regime,
                rule_set="5M_PRIMARY_1M_CONFIRMED_BUY",
                snapshot=snapshot_out,
            )
            return confirmed
        else:
            return ShortTFDecision(
                signal="HOLD",
                entry_price=effective_entry,
                primary_tf="5m",
                confirmation_tf="1m",
                is_confirmed_1m=False,
                regime=candidate_5m.regime,
                rule_set="1M_CONFIRMATION_PENDING",
                snapshot=snapshot_out,
            )

    elif candidate_5m.signal == "SHORT":
        # 1M candle must be red and close below 1M EMA9 with RSI <= 55
        red_candle = bool(curr_1m_close < curr_1m_open)
        momentum_1m = bool(curr_1m_close <= (ema_9_1m + 0.00005) and rsi_1m <= 55.0)

        if red_candle and momentum_1m:
            confirmed = ShortTFDecision(
                signal="SHORT",
                entry_price=effective_entry,
                stop_loss=candidate_5m.stop_loss,
                take_profit=candidate_5m.take_profit,
                atr_value=candidate_5m.atr_value,
                risk_reward_ratio=candidate_5m.risk_reward_ratio,
                primary_tf="5m",
                confirmation_tf="1m",
                is_confirmed_1m=True,
                regime=candidate_5m.regime,
                rule_set="5M_PRIMARY_1M_CONFIRMED_SHORT",
                snapshot=snapshot_out,
            )
            return confirmed
        else:
            return ShortTFDecision(
                signal="HOLD",
                entry_price=effective_entry,
                primary_tf="5m",
                confirmation_tf="1m",
                is_confirmed_1m=False,
                regime=candidate_5m.regime,
                rule_set="1M_CONFIRMATION_PENDING",
                snapshot=snapshot_out,
            )

    return candidate_5m
