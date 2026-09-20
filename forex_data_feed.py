"""
Real Live Forex Market Data Feed via Yahoo Finance (yfinance).
Free public market data feed for spot forex pairs with zero broker login or API keys required.
Completely replaces synthetic or broker-gated OTC data feeds.
Returns standard OHLCV DataFrames identical to data_feed.py for seamless integration
with indicators.py, regime_detector.py, and strategy.py.
"""

import time
from pathlib import Path
from typing import Optional, Dict, Tuple, List
import pandas as pd
import numpy as np
import yfinance as yf

try:
    from . import indicators
    from . import data_feed
    from .data_feed import attach_higher_timeframe_trend
    from .logger import get_logger
except ImportError:
    import indicators
    import data_feed
    from data_feed import attach_higher_timeframe_trend
    from logger import get_logger

logger = get_logger("forex_data_feed")

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

STANDARD_COLUMNS = ["time", "open", "high", "low", "close", "volume", "datetime"]

# Standard Yahoo Finance Spot Forex Ticker Mappings
FOREX_TICKER_MAP: Dict[str, str] = {
    "EUR/USD": "EURUSD=X",
    "GBP/JPY": "GBPJPY=X",
    "AUD/CAD": "AUDCAD=X",
    "NZD/CHF": "NZDCHF=X",
    "USD/COP": "COP=X",
    "GBP/AUD": "GBPAUD=X",
    "EUR/CAD": "EURCAD=X",
    "NZD/CAD": "NZDCAD=X",
    "USD/JPY": "USDJPY=X",
    "GBP/USD": "GBPUSD=X",
    "USD/CHF": "USDCHF=X",
    "AUD/USD": "AUDUSD=X",
    "USD/CAD": "USDCAD=X",
}


def normalize_forex_symbol(pair: str) -> Tuple[str, str]:
    """
    Normalizes forex pair string and returns (display_pair, yf_ticker).
    Examples:
      'EURUSD' -> ('EUR/USD', 'EURUSD=X')
      'EUR/USD' -> ('EUR/USD', 'EURUSD=X')
      'cop=x' -> ('USD/COP', 'COP=X')
    """
    clean = pair.strip().upper()
    if clean in FOREX_TICKER_MAP:
        return clean, FOREX_TICKER_MAP[clean]

    # Handle formats like 'EURUSD', 'EUR_USD', 'EUR-USD'
    clean_strip = clean.replace("/", "").replace("-", "").replace("_", "").replace("=X", "")
    if len(clean_strip) == 6:
        std_pair = f"{clean_strip[:3]}/{clean_strip[3:]}"
        if std_pair in FOREX_TICKER_MAP:
            return std_pair, FOREX_TICKER_MAP[std_pair]
        return std_pair, f"{clean_strip}=X"

    if clean == "COP" or clean == "COP=X":
        return "USD/COP", "COP=X"

    return clean, f"{clean.replace('/', '')}=X"


def is_forex_symbol(pair: str) -> bool:
    """Checks if a given symbol string is a spot Forex pair."""
    if not pair or not isinstance(pair, str):
        return False
    clean = pair.strip().upper()
    if clean.endswith("=X"):
        return True
    if clean in FOREX_TICKER_MAP:
        return True
    clean_strip = clean.replace("/", "").replace("-", "").replace("_", "").replace("=X", "")
    if len(clean_strip) == 6:
        std_pair = f"{clean_strip[:3]}/{clean_strip[3:]}"
        if std_pair in FOREX_TICKER_MAP:
            return True
    if clean_strip == "COP":
        return True
    return False


def _format_yf_dataframe(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Standardizes yfinance history DataFrame into the spot_bot standard format:
    ['time', 'open', 'high', 'low', 'close', 'volume', 'datetime']
    """
    if df_raw is None or df_raw.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    df = df_raw.copy()
    if "Datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["Datetime"], utc=True)
    elif "Date" in df.columns:
        df["datetime"] = pd.to_datetime(df["Date"], utc=True)
    elif isinstance(df.index, pd.DatetimeIndex):
        df["datetime"] = pd.to_datetime(df.index, utc=True)
    else:
        df = df.reset_index()
        first_col = df.columns[0]
        df["datetime"] = pd.to_datetime(df[first_col], utc=True)

    # Rename OHLCV columns case-insensitively
    col_map = {}
    for col in df.columns:
        c_lower = str(col).lower()
        if c_lower in ("open", "high", "low", "close", "volume"):
            col_map[col] = c_lower
    df = df.rename(columns=col_map)

    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    else:
        df["volume"] = 0.0

    df = df.dropna(subset=["open", "high", "low", "close"])
    if df.empty:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    # Convert datetime to millisecond unix timestamp
    df["time"] = df["datetime"].apply(lambda x: int(x.timestamp() * 1000))
    df = df.sort_values(by="time").drop_duplicates(subset=["time"]).reset_index(drop=True)

    return df[STANDARD_COLUMNS]


def fetch_forex_candles(
    pair: str = "EUR/USD",
    timeframe: str = "1h",
    count: int = 500,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Fetches real live spot forex OHLCV candles from Yahoo Finance.

    Args:
        pair: Forex pair (e.g. 'EUR/USD', 'GBP/JPY').
        timeframe: Candle interval ('1m', '5m', '15m', '1h', '4h').
        count: Number of recent candles to retrieve.
        use_cache: If True, reads from / writes to local CSV cache.

    Returns:
        pd.DataFrame: DataFrame with ['time', 'open', 'high', 'low', 'close', 'volume', 'datetime'].
    """
    std_pair, yf_ticker = normalize_forex_symbol(pair)
    clean_pair_name = std_pair.replace("/", "_")
    cache_file = DATA_DIR / f"forex_{clean_pair_name}_{timeframe}_{count}.csv"

    # Check local cache first
    if use_cache and cache_file.exists():
        try:
            cached_df = pd.read_csv(cache_file)
            if len(cached_df) >= min(count, 50):
                cached_df["datetime"] = pd.to_datetime(cached_df["datetime"], utc=True)
                logger.debug(f"Loaded {len(cached_df)} candles for {std_pair} ({timeframe}) from cache {cache_file.name}")
                return cached_df.tail(count).reset_index(drop=True)
        except Exception as exc:
            logger.warning(f"Error reading cache {cache_file.name}: {exc}. Fetching from yfinance.")

    logger.info(f"Fetching {count} live {timeframe} forex candles for {std_pair} ({yf_ticker}) via Yahoo Finance...")

    # Configure yfinance period and interval
    try:
        ticker_obj = yf.Ticker(yf_ticker)

        if timeframe == "4h":
            # Resample from 1h candles
            period_str = "730d" if count > 200 else "60d"
            df_raw = ticker_obj.history(period=period_str, interval="1h")
            if df_raw.empty:
                logger.warning(f"No 1h data returned for {std_pair} to resample to 4h.")
                return pd.DataFrame(columns=STANDARD_COLUMNS)

            df_1h = _format_yf_dataframe(df_raw)
            # Resample 1h to 4h
            df_4h_resampled = df_1h.set_index("datetime").resample("4h", origin="start").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }).dropna().reset_index()

            df_4h_resampled["time"] = df_4h_resampled["datetime"].apply(lambda x: int(x.timestamp() * 1000))
            df = df_4h_resampled[STANDARD_COLUMNS]

        elif timeframe == "1h":
            period_str = "730d" if count > 600 else "60d"
            df_raw = ticker_obj.history(period=period_str, interval="1h")
            df = _format_yf_dataframe(df_raw)

        elif timeframe == "15m":
            period_str = "60d" if count > 500 else "30d"
            df_raw = ticker_obj.history(period=period_str, interval="15m")
            df = _format_yf_dataframe(df_raw)

        elif timeframe == "5m":
            period_str = "60d" if count > 500 else "10d"
            df_raw = ticker_obj.history(period=period_str, interval="5m")
            df = _format_yf_dataframe(df_raw)

        elif timeframe == "1m":
            period_str = "7d"
            df_raw = ticker_obj.history(period=period_str, interval="1m")
            df = _format_yf_dataframe(df_raw)

        else:
            period_str = "730d"
            df_raw = ticker_obj.history(period=period_str, interval="1h")
            df = _format_yf_dataframe(df_raw)

    except Exception as exc:
        logger.error(f"Network error fetching yfinance data for {std_pair} ({yf_ticker}): {exc}")
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    if df.empty or len(df) < 10:
        logger.warning(f"Insufficient or empty data returned for {std_pair} ({yf_ticker}).")
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    # Slice to requested count
    result_df = df.tail(count).reset_index(drop=True)

    # Save to local cache
    if use_cache and len(result_df) >= 30:
        try:
            result_df.to_csv(cache_file, index=False)
            logger.debug(f"Saved {len(result_df)} candles to {cache_file.name}")
        except Exception as exc:
            logger.warning(f"Could not write cache file {cache_file.name}: {exc}")

    logger.info(f"Successfully loaded {len(result_df)} live {timeframe} candles for {std_pair} from Yahoo Finance.")
    return result_df


def fetch_higher_timeframe_forex_data(
    pair: str = "EUR/USD",
    timeframe: str = "4h",
    limit: int = 500,
    ema_period: int = 200,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Fetches higher-timeframe forex candles (e.g. 4h) and computes the 200-period EMA
    and its slope to determine macro market regime.
    """
    df_htf = fetch_forex_candles(pair=pair, timeframe=timeframe, count=limit, use_cache=use_cache)
    if df_htf.empty or len(df_htf) < ema_period:
        logger.warning(f"Insufficient HTF forex data ({len(df_htf)} bars) to compute {ema_period} EMA for {pair}.")
        return df_htf

    df_htf["htf_ema_200"] = indicators.ema(df_htf, ema_period)
    df_htf["htf_ema_slope"] = df_htf["htf_ema_200"].diff()
    df_htf["htf_trend"] = df_htf["htf_ema_slope"].apply(lambda s: "UP" if s > 0 else "DOWN")

    # Timeframe duration in milliseconds (4h = 4 * 3600 * 1000 ms)
    tf_hours = 4 if "4h" in timeframe else (24 if "1d" in timeframe else 1)
    df_htf["available_time"] = df_htf["time"] + (tf_hours * 3600 * 1000)

    return df_htf
