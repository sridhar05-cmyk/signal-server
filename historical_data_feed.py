"""
Historical Forex Data Feed & Resampling Adapter via Dukascopy ECN.
===================================================================

Provides institutional-grade, tick-derived 1-minute historical data for spot Forex pairs.
Features:
  - 100% Free public access with zero broker login or API keys required.
  - Multi-year continuous historical depth (2007 - present).
  - True ECN Bid & Ask quotes with exact UTC timestamps (+00:00, zero DST skew).
  - Native 1M -> 5M -> 15M -> 1H -> 4H resampling engine preserving strict OHLCV math.
  - Multi-timeframe hierarchy builder ensuring 100% synchronization across timeframes.
  - Comprehensive data validation (continuity, duplicates, flat bars, abnormal spikes).
  - Local disk caching to avoid redundant downloads.
  - Preserves existing live/Yahoo forex_data_feed.py intact.
"""

import os
import sys
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple, List, Any
import pandas as pd
import numpy as np

import dukascopy_python
import dukascopy_python.instruments as inst

# Path setup
spot_bot_dir = Path(__file__).resolve().parent
if str(spot_bot_dir) not in sys.path:
    sys.path.insert(0, str(spot_bot_dir))

import forex_utils
import forex_sessions

logger = logging.getLogger("spot_bot.historical_data_feed")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

HISTORICAL_DATA_DIR = spot_bot_dir / "data" / "historical"
HISTORICAL_DATA_DIR.mkdir(parents=True, exist_ok=True)

STANDARD_COLUMNS = ["time", "open", "high", "low", "close", "volume", "datetime"]

# Dukascopy major instrument mappings
DUKASCOPY_PAIRS: Dict[str, str] = {
    "EUR/USD": "EUR/USD",
    "GBP/USD": "GBP/USD",
    "USD/JPY": "USD/JPY",
    "USD/CHF": "USD/CHF",
    "AUD/USD": "AUD/USD",
    "USD/CAD": "USD/CAD",
    "NZD/USD": "NZD/USD",
}

INTERVAL_MAP = {
    "1m": dukascopy_python.INTERVAL_MIN_1,
    "5m": dukascopy_python.INTERVAL_MIN_5,
    "10m": dukascopy_python.INTERVAL_MIN_10,
    "15m": dukascopy_python.INTERVAL_MIN_15,
    "30m": dukascopy_python.INTERVAL_MIN_30,
    "1h": dukascopy_python.INTERVAL_HOUR_1,
    "4h": dukascopy_python.INTERVAL_HOUR_4,
    "1d": dukascopy_python.INTERVAL_DAY_1,
}


def normalize_pair_symbol(pair: str) -> str:
    """Normalizes symbol string to standard 'EUR/USD' format."""
    clean = pair.strip().upper()
    if clean in DUKASCOPY_PAIRS:
        return clean
    clean_strip = clean.replace("/", "").replace("-", "").replace("_", "").replace("=X", "")
    if len(clean_strip) == 6:
        std = f"{clean_strip[:3]}/{clean_strip[3:]}"
        if std in DUKASCOPY_PAIRS:
            return std
    return clean


def get_cache_path(pair: str, timeframe: str, start_dt: datetime, end_dt: datetime) -> Path:
    """Generates standard cache path for a pair, timeframe, and date window."""
    clean_pair = normalize_pair_symbol(pair).replace("/", "_")
    s_str = start_dt.strftime("%Y%m%d")
    e_str = end_dt.strftime("%Y%m%d")
    return HISTORICAL_DATA_DIR / f"duka_{clean_pair}_{timeframe}_{s_str}_{e_str}.csv"


def fetch_dukascopy_raw(
    pair: str,
    start_dt: datetime,
    end_dt: datetime,
    timeframe: str = "1m",
    offer_side: str = "bid",
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Downloads historical OHLCV data from Dukascopy ECN and returns standard DataFrame.

    Args:
        pair: Forex pair, e.g. 'EUR/USD'.
        start_dt: Start datetime (UTC).
        end_dt: End datetime (UTC).
        timeframe: '1m', '5m', '15m', '1h', '4h'.
        offer_side: 'bid' or 'ask'.
        use_cache: Whether to read/write local disk cache.

    Returns:
        Standard DataFrame with ['time', 'open', 'high', 'low', 'close', 'volume', 'datetime'].
    """
    std_pair = normalize_pair_symbol(pair)
    if std_pair not in DUKASCOPY_PAIRS:
        raise ValueError(f"Unsupported Forex pair: {pair}. Supported: {list(DUKASCOPY_PAIRS.keys())}")

    cache_file = get_cache_path(std_pair, timeframe, start_dt, end_dt)

    if use_cache and cache_file.exists():
        try:
            df_cached = pd.read_csv(cache_file)
            if len(df_cached) > 0 and all(c in df_cached.columns for c in STANDARD_COLUMNS):
                df_cached["datetime"] = pd.to_datetime(df_cached["datetime"], utc=True)
                logger.info(f"[{std_pair}] Loaded {len(df_cached)} {timeframe} bars from local cache: {cache_file.name}")
                return df_cached[STANDARD_COLUMNS]
        except Exception as e:
            logger.warning(f"Error reading cache {cache_file.name}: {e}. Re-fetching from Dukascopy.")

    dk_interval = INTERVAL_MAP.get(timeframe.lower())
    if dk_interval is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}. Supported: {list(INTERVAL_MAP.keys())}")

    dk_offer = dukascopy_python.OFFER_SIDE_BID if offer_side.lower() == "bid" else dukascopy_python.OFFER_SIDE_ASK
    inst_symbol = DUKASCOPY_PAIRS[std_pair]

    logger.info(f"[{std_pair}] Fetching historical {timeframe} data ({offer_side.upper()}) from Dukascopy: {start_dt.strftime('%Y-%m-%d')} -> {end_dt.strftime('%Y-%m-%d')}...")
    t0 = time.time()

    try:
        raw_df = dukascopy_python.fetch(
            instrument=inst_symbol,
            interval=dk_interval,
            offer_side=dk_offer,
            start=start_dt,
            end=end_dt,
        )
    except Exception as exc:
        logger.error(f"[{std_pair}] Dukascopy fetch error: {exc}")
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    elapsed = time.time() - t0

    if raw_df is None or raw_df.empty:
        logger.warning(f"[{std_pair}] No data returned from Dukascopy for period {start_dt} to {end_dt}.")
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    df = raw_df.copy()
    if isinstance(df.index, pd.DatetimeIndex):
        df["datetime"] = pd.to_datetime(df.index, utc=True)
    elif "timestamp" in df.columns:
        df["datetime"] = pd.to_datetime(df["timestamp"], utc=True)
    elif "time" in df.columns:
        df["datetime"] = pd.to_datetime(df["time"], utc=True)
    else:
        df["datetime"] = pd.to_datetime(df.iloc[:, 0], utc=True)

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    else:
        df["volume"] = 0.0

    df = df.dropna(subset=["open", "high", "low", "close"])
    df["time"] = df["datetime"].apply(lambda x: int(x.timestamp() * 1000))
    df = df.sort_values(by="time").drop_duplicates(subset=["time"]).reset_index(drop=True)
    df = df[STANDARD_COLUMNS]

    logger.info(f"[{std_pair}] Successfully loaded {len(df)} {timeframe} bars from Dukascopy in {elapsed:.2f}s.")

    if use_cache and len(df) > 0:
        try:
            df.to_csv(cache_file, index=False)
            logger.info(f"[{std_pair}] Saved {len(df)} bars to cache {cache_file.name}")
        except Exception as exc:
            logger.warning(f"Failed to write cache {cache_file.name}: {exc}")

    return df


def resample_m1_dataframe(df_1m: pd.DataFrame, target_tf: str = "5m") -> pd.DataFrame:
    """
    Resamples 1-minute OHLCV DataFrame into target timeframe ('5m', '15m', '1h', '4h', '1d').

    Strict rules applied:
      - 'open': first value of the interval
      - 'high': maximum value of the interval
      - 'low': minimum value of the interval
      - 'close': last value of the interval
      - 'volume': sum of volume within the interval
      - 'datetime': start timestamp of the candle (standard Forex convention)
      - Empty intervals (weekends, holiday closures) are dropped.
    """
    if df_1m is None or df_1m.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    rule_map = {
        "5m": "5min",
        "10m": "10min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1h",
        "4h": "4h",
        "1d": "1D",
    }
    freq = rule_map.get(target_tf.lower())
    if freq is None:
        raise ValueError(f"Unsupported target timeframe for resampling: {target_tf}")

    df = df_1m.copy()
    if not isinstance(df["datetime"].dtype, pd.DatetimeTZDtype):
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)

    df = df.set_index("datetime").sort_index()

    # Resample with origin at standard epoch / midnight
    resampled = df.resample(freq, label="left", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["open", "high", "low", "close"]).reset_index()

    resampled["time"] = resampled["datetime"].apply(lambda x: int(x.timestamp() * 1000))
    resampled = resampled[STANDARD_COLUMNS].reset_index(drop=True)
    return resampled


def build_mtf_hierarchy_from_1m(df_1m: pd.DataFrame, pair: str = "EUR/USD") -> Dict[str, pd.DataFrame]:
    """
    Constructs a complete, 100% mathematically synchronized multi-timeframe hierarchy
    from a single clean 1M historical dataset.

    Returns:
        {
            "1m": df_1m,
            "5m": df_5m,
            "15m": df_15m,
            "1h": df_1h,
            "4h": df_4h
        }
    """
    logger.info(f"[{pair}] Generating synchronized MTF hierarchy from {len(df_1m)} 1M bars...")
    df_5m = resample_m1_dataframe(df_1m, target_tf="5m")
    df_15m = resample_m1_dataframe(df_1m, target_tf="15m")
    df_1h = resample_m1_dataframe(df_1m, target_tf="1h")
    df_4h = resample_m1_dataframe(df_1m, target_tf="4h")

    logger.info(f"[{pair}] MTF Hierarchy built: 1M={len(df_1m)}, 5M={len(df_5m)}, 15M={len(df_15m)}, 1H={len(df_1h)}, 4H={len(df_4h)}")
    return {
        "1m": df_1m,
        "5m": df_5m,
        "15m": df_15m,
        "1h": df_1h,
        "4h": df_4h,
    }


def validate_historical_dataset(df: pd.DataFrame, pair: str, timeframe: str = "1m") -> Dict[str, Any]:
    """
    Performs comprehensive data quality audit on a historical Forex dataset.

    Checks:
      - Total bars & date range
      - OHLC structural validity (H >= O, H >= C, L <= O, L <= C, all > 0)
      - Duplicate timestamps
      - Missing bars count & maximum gap
      - Flat bars (O == H == L == C)
      - Abnormal candles (range > 10 * average range)
      - Weekend handling
      - Session distribution (ASIAN, LONDON, OVERLAP, NEW_YORK)
    """
    if df is None or df.empty:
        return {"valid": False, "error": "Empty DataFrame"}

    n = len(df)
    datetimes = pd.to_datetime(df["datetime"], utc=True)
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    volumes = df["volume"].values

    # 1. OHLC structural validity
    invalid_high = np.sum((highs < opens) | (highs < closes))
    invalid_low = np.sum((lows > opens) | (lows > closes))
    zero_negative = np.sum((opens <= 0) | (highs <= 0) | (lows <= 0) | (closes <= 0))
    ohlc_valid = bool(invalid_high == 0 and invalid_low == 0 and zero_negative == 0)

    # 2. Duplicate timestamps
    dup_count = int(df["time"].duplicated().sum())

    # 3. Flat bar percentage (O == H == L == C)
    flat_mask = (opens == highs) & (highs == lows) & (lows == closes)
    flat_count = int(np.sum(flat_mask))
    flat_pct = round((flat_count / n) * 100.0, 3)

    # 4. Weekend bar check (Saturday 00:00 UTC to Sunday 17:00 UTC)
    # Most Forex operates Sun 21:00/22:00 to Fri 21:00/22:00 UTC
    day_of_week = datetimes.dt.dayofweek  # 5=Sat, 6=Sun
    sat_bars = int(np.sum(day_of_week == 5))
    sun_early_bars = int(np.sum((day_of_week == 6) & (datetimes.dt.hour < 20)))

    # 5. Timestamp continuity & gaps
    diffs_seconds = datetimes.diff().dt.total_seconds().values[1:]
    expected_step_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
    exp_step = expected_step_map.get(timeframe.lower(), 60)

    # Exclude weekend gaps (gaps > 40 hours)
    intraday_gap_mask = (diffs_seconds > (exp_step * 1.5)) & (diffs_seconds < 144000)
    intraday_gaps = int(np.sum(intraday_gap_mask))
    max_gap_sec = float(np.max(diffs_seconds)) if len(diffs_seconds) > 0 else 0.0

    # 6. Abnormal candle spikes (range > 10 * median range)
    ranges = highs - lows
    med_range = float(np.median(ranges[ranges > 0])) if np.sum(ranges > 0) > 0 else 0.0001
    abnormal_count = int(np.sum(ranges > (10.0 * med_range))) if med_range > 0 else 0

    # 7. Session distribution
    session_counts = {"ASIAN": 0, "LONDON": 0, "LONDON_NY_OVERLAP": 0, "NEW_YORK": 0}
    for dt in datetimes:
        s = forex_sessions.classify_session(dt.to_pydatetime())
        if s in session_counts:
            session_counts[s] += 1

    return {
        "valid": ohlc_valid and dup_count == 0,
        "pair": pair,
        "timeframe": timeframe,
        "total_bars": n,
        "start_date": str(datetimes.iloc[0])[:19],
        "end_date": str(datetimes.iloc[-1])[:19],
        "ohlc_valid": ohlc_valid,
        "invalid_high_bars": int(invalid_high),
        "invalid_low_bars": int(invalid_low),
        "zero_negative_bars": int(zero_negative),
        "duplicate_bars": dup_count,
        "flat_bars": flat_count,
        "flat_percentage": flat_pct,
        "sat_bars": sat_bars,
        "sun_early_bars": sun_early_bars,
        "intraday_gaps": intraday_gaps,
        "max_gap_hours": round(max_gap_sec / 3600.0, 2),
        "median_range_pips": round(med_range / forex_utils.get_pip_size(pair), 2),
        "abnormal_candles": abnormal_count,
        "session_distribution": {
            s: {"count": c, "pct": round((c / n) * 100.0, 1)}
            for s, c in session_counts.items()
        },
    }


def compare_resampled_with_yahoo(
    df_duka_5m: pd.DataFrame,
    df_yahoo_5m: pd.DataFrame,
    pair: str,
) -> Dict[str, Any]:
    """
    Compares Dukascopy resampled 5M data against Yahoo Finance 5M data
    strictly on overlapping timestamps for data quality verification.
    """
    pip_val = forex_utils.get_pip_size(pair)

    # Normalize datetimes to UTC
    d_duka = df_duka_5m.copy()
    d_duka["dt"] = pd.to_datetime(d_duka["datetime"], utc=True)
    d_duka = d_duka.set_index("dt")

    d_yf = df_yahoo_5m.copy()
    d_yf["dt"] = pd.to_datetime(d_yf["datetime"], utc=True)
    d_yf = d_yf.set_index("dt")

    # Find common timestamps
    common_idx = d_duka.index.intersection(d_yf.index)
    overlap_bars = len(common_idx)

    if overlap_bars < 50:
        return {
            "pair": pair,
            "overlap_bars": overlap_bars,
            "error": "Insufficient overlapping bars (<50)",
        }

    sub_duka = d_duka.loc[common_idx]
    sub_yf = d_yf.loc[common_idx]

    close_duka = sub_duka["close"].values
    close_yf = sub_yf["close"].values

    # Absolute difference in pips
    diff_pips = np.abs(close_duka - close_yf) / pip_val
    mae_pips = float(np.mean(diff_pips))
    median_diff_pips = float(np.median(diff_pips))
    max_diff_pips = float(np.max(diff_pips))

    # Correlation
    corr = float(np.corrcoef(close_duka, close_yf)[0, 1])

    # Tolerance thresholds
    pct_within_1pip = float(np.mean(diff_pips <= 1.0) * 100.0)
    pct_within_2pips = float(np.mean(diff_pips <= 2.0) * 100.0)
    pct_within_5pips = float(np.mean(diff_pips <= 5.0) * 100.0)

    return {
        "pair": pair,
        "overlap_bars": overlap_bars,
        "start_overlap": str(common_idx[0])[:19],
        "end_overlap": str(common_idx[-1])[:19],
        "correlation": round(corr, 6),
        "mae_pips": round(mae_pips, 2),
        "median_diff_pips": round(median_diff_pips, 2),
        "max_diff_pips": round(max_diff_pips, 2),
        "pct_within_1pip": round(pct_within_1pip, 1),
        "pct_within_2pips": round(pct_within_2pips, 1),
        "pct_within_5pips": round(pct_within_5pips, 1),
    }
