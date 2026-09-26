"""
Technical Analysis Indicators for Binance Spot Trading Bot.
Pure, stateless mathematical functions using pandas and numpy.
Includes EMA, RSI, MACD, Bollinger Bands, and Average True Range (ATR).
"""

from typing import Tuple, Any, Union
import numpy as np
import pandas as pd


def _get_price_series(df: Any, col_name: str = "close") -> pd.Series:
    """Helper function to safely extract price series handling case variations and pd.Series."""
    if isinstance(df, pd.Series):
        return df.astype(float)
    if hasattr(df, "columns"):
        if col_name in df.columns:
            return df[col_name].astype(float)
        col_lower_map = {c.lower(): c for c in df.columns}
        if col_name.lower() in col_lower_map:
            return df[col_lower_map[col_name.lower()]].astype(float)
    raise KeyError(f"Price column '{col_name}' not found in DataFrame columns: {list(df.columns) if hasattr(df, 'columns') else type(df)}")


def ema(df: pd.DataFrame, period: int, price_col: str = "close") -> pd.Series:
    """Exponential Moving Average (EMA)."""
    if df.empty:
        return pd.Series(dtype=float)
    close = _get_price_series(df, price_col)
    return close.ewm(span=period, adjust=False).mean()


def rsi(df: pd.DataFrame, period: int = 14, price_col: str = "close") -> pd.Series:
    """Relative Strength Index (RSI) using Wilder's smoothing method."""
    if df.empty or len(df) < 2:
        return pd.Series(index=df.index, dtype=float)

    close = _get_price_series(df, price_col)
    delta = close.diff()

    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi_series = 100.0 - (100.0 / (1.0 + rs))

    zero_loss = (avg_loss == 0.0) & (avg_gain > 0.0)
    zero_gain = (avg_gain == 0.0) & (avg_loss > 0.0)
    both_zero = (avg_gain == 0.0) & (avg_loss == 0.0)

    rsi_series = rsi_series.mask(zero_loss, 100.0)
    rsi_series = rsi_series.mask(zero_gain, 0.0)
    rsi_series = rsi_series.mask(both_zero, 50.0)

    return rsi_series


def macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    price_col: str = "close",
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Moving Average Convergence Divergence (MACD)."""
    if df.empty:
        empty = pd.Series(dtype=float)
        return empty, empty, empty

    close = _get_price_series(df, price_col)
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()

    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    return macd_line, signal_line, histogram


def bollinger_bands(
    df: pd.DataFrame,
    period: int = 20,
    std_dev: float = 2.0,
    price_col: str = "close",
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands (upper, middle, lower)."""
    if df.empty:
        empty = pd.Series(dtype=float)
        return empty, empty, empty

    close = _get_price_series(df, price_col)
    middle_band = close.rolling(window=period, min_periods=period).mean()
    rolling_std = close.rolling(window=period, min_periods=period).std()

    upper_band = middle_band + (std_dev * rolling_std)
    lower_band = middle_band - (std_dev * rolling_std)

    return upper_band, middle_band, lower_band


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range (ATR).
    Crucial for spot trading volatility assessment and dynamic stop-loss / take-profit sizing.
    """
    if df.empty or len(df) < 2:
        return pd.Series(index=df.index, dtype=float)

    high = _get_price_series(df, "high")
    low = _get_price_series(df, "low")
    close = _get_price_series(df, "close")
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr_series = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    return atr_series


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average Directional Index (ADX).
    Measures trend strength on a scale of 0 to 100 regardless of direction.
    ADX > 25 indicates a strong trending market.
    ADX < 20 indicates a weak trend or ranging/choppy market.
    """
    if df.empty or len(df) < period:
        return pd.Series(index=df.index, dtype=float).fillna(0.0)

    high = _get_price_series(df, "high")
    low = _get_price_series(df, "low")
    close = _get_price_series(df, "close")
    prev_close = close.shift(1)
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    # True Range (TR)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Directional Movement (+DM, -DM)
    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm_series = pd.Series(plus_dm, index=df.index)
    minus_dm_series = pd.Series(minus_dm, index=df.index)

    # Wilder's smoothing
    smoothed_tr = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    smoothed_plus_dm = plus_dm_series.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    smoothed_minus_dm = minus_dm_series.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    # Directional Indicators (+DI, -DI)
    plus_di = 100.0 * (smoothed_plus_dm / smoothed_tr.replace(0.0, np.nan))
    minus_di = 100.0 * (smoothed_minus_dm / smoothed_tr.replace(0.0, np.nan))

    # Directional Movement Index (DX)
    di_sum = plus_di + minus_di
    di_diff = (plus_di - minus_di).abs()
    dx = 100.0 * (di_diff / di_sum.replace(0.0, np.nan))

    # ADX is smoothed DX
    adx_series = dx.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return adx_series.fillna(0.0)


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Convenience function that appends all indicators to a copy of df."""
    result = df.copy()
    result["ema_9"] = ema(result, 9)
    result["ema_21"] = ema(result, 21)
    result["rsi_14"] = rsi(result, 14)

    m_line, m_sig, m_hist = macd(result, 12, 26, 9)
    result["macd_line"] = m_line
    result["macd_signal"] = m_sig
    result["macd_hist"] = m_hist

    bb_up, bb_mid, bb_low = bollinger_bands(result, 20, 2.0)
    result["bb_upper"] = bb_up
    result["bb_middle"] = bb_mid
    result["bb_lower"] = bb_low

    result["atr_14"] = atr(result, 14)

    return result
