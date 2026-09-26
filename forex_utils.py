"""
Forex Utility and Currency Specification Engine.
Provides pair-aware pip math, pipette handling, realistic spread models,
decimal formatting, and candle data integrity validation for major Forex pairs.
"""

from typing import Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np

# Institutional retail spread benchmarks (in pips) for major and key cross pairs
FOREX_SPECS: Dict[str, Dict[str, Any]] = {
    # 7 Major Forex Pairs
    "EUR/USD": {"pip_size": 0.0001, "pipette_size": 0.00001, "decimals": 5, "spread_pips": 1.1, "is_major": True},
    "GBP/USD": {"pip_size": 0.0001, "pipette_size": 0.00001, "decimals": 5, "spread_pips": 1.4, "is_major": True},
    "USD/JPY": {"pip_size": 0.01,   "pipette_size": 0.001,   "decimals": 3, "spread_pips": 1.2, "is_major": True},
    "USD/CHF": {"pip_size": 0.0001, "pipette_size": 0.00001, "decimals": 5, "spread_pips": 1.5, "is_major": True},
    "AUD/USD": {"pip_size": 0.0001, "pipette_size": 0.00001, "decimals": 5, "spread_pips": 1.2, "is_major": True},
    "USD/CAD": {"pip_size": 0.0001, "pipette_size": 0.00001, "decimals": 5, "spread_pips": 1.4, "is_major": True},
    "NZD/USD": {"pip_size": 0.0001, "pipette_size": 0.00001, "decimals": 5, "spread_pips": 1.6, "is_major": True},
    # Major Crosses
    "GBP/JPY": {"pip_size": 0.01,   "pipette_size": 0.001,   "decimals": 3, "spread_pips": 2.0, "is_major": False},
    "EUR/CAD": {"pip_size": 0.0001, "pipette_size": 0.00001, "decimals": 5, "spread_pips": 1.8, "is_major": False},
    "AUD/CAD": {"pip_size": 0.0001, "pipette_size": 0.00001, "decimals": 5, "spread_pips": 1.7, "is_major": False},
    "NZD/CHF": {"pip_size": 0.0001, "pipette_size": 0.00001, "decimals": 5, "spread_pips": 2.1, "is_major": False},
    "GBP/AUD": {"pip_size": 0.0001, "pipette_size": 0.00001, "decimals": 5, "spread_pips": 2.2, "is_major": False},
    "NZD/CAD": {"pip_size": 0.0001, "pipette_size": 0.00001, "decimals": 5, "spread_pips": 2.0, "is_major": False},
}


def clean_forex_symbol(pair: str) -> str:
    """Normalizes any symbol format to standard slash notation (e.g. 'EURUSD' -> 'EUR/USD')."""
    if not pair:
        return "EUR/USD"
    s = pair.strip().upper().replace("-", "").replace("_", "").replace("=X", "")
    if "/" in s:
        return s
    if len(s) == 6:
        return f"{s[:3]}/{s[3:]}"
    return s


def is_jpy_pair(pair: str) -> bool:
    """Returns True if the currency pair contains JPY."""
    return "JPY" in clean_forex_symbol(pair)


def get_pip_size(pair: str) -> float:
    """
    Returns 1 pip value in price units:
    0.01 for JPY pairs, 0.0001 for non-JPY pairs.
    """
    std = clean_forex_symbol(pair)
    if std in FOREX_SPECS:
        return FOREX_SPECS[std]["pip_size"]
    return 0.01 if is_jpy_pair(pair) else 0.0001


def get_pipette_size(pair: str) -> float:
    """Returns fractional pip (pipette) size: 0.001 for JPY, 0.00001 for non-JPY."""
    std = clean_forex_symbol(pair)
    if std in FOREX_SPECS:
        return FOREX_SPECS[std]["pipette_size"]
    return 0.001 if is_jpy_pair(pair) else 0.00001


def get_decimal_places(pair: str) -> int:
    """Returns standard display precision (3 for JPY, 5 for non-JPY)."""
    std = clean_forex_symbol(pair)
    if std in FOREX_SPECS:
        return FOREX_SPECS[std]["decimals"]
    return 3 if is_jpy_pair(pair) else 5


def get_pair_spread_pips(pair: str) -> float:
    """Returns standard spread in pips for the given pair."""
    std = clean_forex_symbol(pair)
    if std in FOREX_SPECS:
        return FOREX_SPECS[std]["spread_pips"]
    return 1.8 if is_jpy_pair(pair) else 1.5


def get_pair_sl_bounds_pips(pair: str) -> Tuple[float, float]:
    """
    Returns (min_sl_pips, max_sl_pips) tailored to the asset's typical 1h volatility.
    JPY pairs have wider pip ranges (25-120 pips).
    GBP pairs have higher volatility (20-80 pips).
    Standard majors (EUR, AUD, CAD, CHF, NZD) range (15-65 pips).
    """
    std = clean_forex_symbol(pair)
    if is_jpy_pair(std):
        return (25.0, 120.0)
    if "GBP" in std:
        return (20.0, 80.0)
    return (15.0, 65.0)


def price_diff_to_pips(diff: float, pair: str) -> float:
    """Converts absolute price difference to pips."""
    pip = get_pip_size(pair)
    return round(abs(float(diff)) / pip, 1)


def pips_to_price(pips: float, pair: str) -> float:
    """Converts pips to price difference."""
    pip = get_pip_size(pair)
    return float(pips) * pip


def round_forex_price(price: float, pair: str) -> float:
    """Rounds price to pair-accurate decimals without truncation error."""
    if price is None or np.isnan(price):
        return 0.0
    decimals = get_decimal_places(pair)
    return round(float(price), decimals)


def format_forex_price(price: float, pair: str) -> str:
    """Formats price into accurate string representation with correct decimals."""
    if price is None or np.isnan(price):
        return "--"
    decimals = get_decimal_places(pair)
    return f"{float(price):.{decimals}f}"


format_price = format_forex_price


def get_spread_cost_in_price(pair: str) -> float:
    """Returns spread penalty in price terms (spread_pips * pip_size)."""
    spread_pips = get_pair_spread_pips(pair)
    pip_size = get_pip_size(pair)
    return spread_pips * pip_size



FUTURES_1M_PROXIES: Dict[str, str] = {
    "EUR/USD": "6E=F",
    "GBP/USD": "6B=F",
    "AUD/USD": "6A=F",
    "NZD/USD": "6N=F",
}


def get_futures_proxy_ticker(pair: str) -> Optional[str]:
    """Returns CME Futures proxy ticker for pairs with synthetic/flat 1M spot streams."""
    std = clean_forex_symbol(pair)
    return FUTURES_1M_PROXIES.get(std, None)


def is_futures_proxy_required(pair: str) -> bool:
    """Returns True if the pair requires CME futures proxy for 1M timeframe."""
    std = clean_forex_symbol(pair)
    return std in FUTURES_1M_PROXIES


def check_timestamp_continuity(df: pd.DataFrame, timeframe: str = "1m") -> Dict[str, Any]:
    """
    Validates timestamp continuity and checks for abnormal data gaps.
    """
    if df is None or len(df) < 2 or "time" not in df.columns:
        return {"continuous": False, "large_gaps_gt_15m": 0, "max_gap_minutes": 0.0, "continuity_score": 0.0}

    diffs_min = df["time"].diff().iloc[1:] / 60000.0
    large_gaps = int((diffs_min > 15.0).sum())
    max_gap = float(diffs_min.max()) if len(diffs_min) > 0 else 0.0

    # Expected step in minutes
    step_min = 1.0 if timeframe == "1m" else (5.0 if timeframe == "5m" else 60.0)
    normal_steps = int((diffs_min <= (step_min * 2.0)).sum())
    continuity_score = round(normal_steps / len(diffs_min) * 100.0, 1) if len(diffs_min) > 0 else 0.0

    return {
        "continuous": large_gaps <= 50,  # weekend gaps or rollover halts are expected in 7d
        "large_gaps_gt_15m": large_gaps,
        "max_gap_minutes": round(max_gap, 1),
        "continuity_score": continuity_score,
    }


def validate_forex_dataframe(
    df: pd.DataFrame,
    pair: str = "EUR/USD",
    timeframe: str = "1m",
    min_clean_bars: int = 1,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Validates forex candlestick data:
      - Detects and rejects synthetic/stale streams (>50% flat bars O=H=L=C).
      - Drops isolated flat bars where open == high == low == close with 0 volume.
      - Checks for missing or non-positive OHLC values.
      - Sorts chronologically and removes duplicate timestamps.
      - Validates timestamp continuity.

    Returns:
        (clean_df, report_dict)
    """
    if df is None or df.empty:
        return pd.DataFrame(), {"valid": False, "reason": "DataFrame is empty", "pair": clean_forex_symbol(pair)}

    initial_count = len(df)
    clean_df = df.copy()

    # Ensure required columns
    required = ["open", "high", "low", "close"]
    for col in required:
        if col not in clean_df.columns:
            return pd.DataFrame(), {"valid": False, "reason": f"Missing column '{col}'", "pair": clean_forex_symbol(pair)}
        clean_df[col] = pd.to_numeric(clean_df[col], errors="coerce")

    # Drop NaN or non-positive prices
    clean_df = clean_df.dropna(subset=required)
    clean_df = clean_df[(clean_df["open"] > 0) & (clean_df["high"] > 0) & (clean_df["low"] > 0) & (clean_df["close"] > 0)]

    # Detect synthetic/stale flat bars (O == H == L == C)
    is_flat = (clean_df["open"] == clean_df["high"]) & (clean_df["high"] == clean_df["low"]) & (clean_df["low"] == clean_df["close"])
    flat_bars_removed = int(is_flat.sum())
    flat_ratio = float(flat_bars_removed) / initial_count if initial_count > 0 else 0.0

    # REJECT if stream is synthetic / flat (e.g. Yahoo Spot EUR/USD with >50% flat bars)
    if flat_ratio > 0.50:
        return pd.DataFrame(), {
            "valid": False,
            "reason": f"Synthetic/stale feed detected: {flat_ratio*100:.1f}% flat bars (O=H=L=C). Rejected.",
            "initial_bars": initial_count,
            "clean_bars": 0,
            "flat_bars_removed": flat_bars_removed,
            "flat_ratio_pct": round(flat_ratio * 100.0, 1),
            "pair": clean_forex_symbol(pair),
        }

    clean_df = clean_df[~is_flat].reset_index(drop=True)

    if "time" in clean_df.columns:
        clean_df = clean_df.sort_values(by="time").drop_duplicates(subset=["time"]).reset_index(drop=True)

    if len(clean_df) < min_clean_bars:
        return pd.DataFrame(), {
            "valid": False,
            "reason": f"Insufficient quality: {len(clean_df)} clean bars is less than required minimum {min_clean_bars}",
            "initial_bars": initial_count,
            "clean_bars": len(clean_df),
            "flat_bars_removed": flat_bars_removed,
            "pair": clean_forex_symbol(pair),
        }

    # Continuity validation
    continuity = check_timestamp_continuity(clean_df, timeframe=timeframe)

    report = {
        "valid": True,
        "initial_bars": initial_count,
        "clean_bars": len(clean_df),
        "flat_bars_removed": flat_bars_removed,
        "flat_ratio_pct": round(flat_ratio * 100.0, 1),
        "pair": clean_forex_symbol(pair),
        "continuity": continuity,
    }
    return clean_df, report

