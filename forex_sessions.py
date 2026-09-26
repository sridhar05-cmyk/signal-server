"""
Forex Market Session Awareness Engine.
Identifies global market sessions based on UTC timestamps:
  - ASIAN: 00:00 - 08:00 UTC (Tokyo, Sydney, Wellington)
  - LONDON: 07:00 - 16:00 UTC (Frankfurt, London main liquidity)
  - NEW_YORK: 12:00 - 21:00 UTC (US institutional trading)
  - LONDON_NY_OVERLAP: 12:00 - 16:00 UTC (Peak global liquidity & trend momentum)
  - ROLLOVER_DEAD_ZONE: 21:00 - 23:00 UTC (Daily settlement, illiquid chop, wide spreads)
"""

from datetime import datetime, timezone
from typing import Tuple, Dict, Any, Optional
import pandas as pd

# Session definitions (Hour in UTC)
SESSION_ASIAN = "ASIAN"
SESSION_LONDON = "LONDON"
SESSION_NEW_YORK = "NEW_YORK"
SESSION_LONDON_NY_OVERLAP = "LONDON_NY_OVERLAP"
SESSION_ROLLOVER = "ROLLOVER_DEAD_ZONE"
SESSION_OFF_HOURS = "OFF_HOURS"


def get_forex_session(dt: Optional[datetime]) -> str:
    """
    Determines active forex trading session for a given UTC datetime.
    """
    if dt is None:
        return SESSION_LONDON  # default fallback
    if not isinstance(dt, datetime):
        try:
            dt = pd.to_datetime(dt, utc=True).to_pydatetime()
        except Exception:
            return SESSION_LONDON

    hour = dt.hour
    weekday = dt.weekday()  # 0 = Monday, 6 = Sunday

    # Weekend check: Market closes Friday 21:00 UTC, reopens Sunday 21:00 UTC
    if weekday == 5:  # Saturday
        return SESSION_OFF_HOURS
    if weekday == 4 and hour >= 22:  # Friday late evening
        return SESSION_OFF_HOURS
    if weekday == 6 and hour < 21:  # Sunday before open
        return SESSION_OFF_HOURS

    # Daily Rollover / Spread Widening window
    if 21 <= hour < 23:
        return SESSION_ROLLOVER

    # London / New York Overlap (Highest Liquidity & Strongest Follow-through)
    if 12 <= hour < 16:
        return SESSION_LONDON_NY_OVERLAP

    # Core London Session
    if 7 <= hour < 16:
        return SESSION_LONDON

    # Core New York Session
    if 12 <= hour < 21:
        return SESSION_NEW_YORK

    # Asian Session
    if 0 <= hour < 8:
        return SESSION_ASIAN

    return SESSION_OFF_HOURS


def is_tradeable_session(dt: Optional[datetime], pair: str = "EUR/USD") -> Tuple[bool, str]:
    """
    Evaluates whether current session has adequate liquidity and tight spreads for trading.

    Returns:
        (can_trade: bool, reason: str)
    """
    session = get_forex_session(dt)

    if session == SESSION_OFF_HOURS:
        return False, "Market is closed for the weekend (Friday 22:00 to Sunday 21:00 UTC)."

    if session == SESSION_ROLLOVER:
        return False, "Daily rollover dead-zone (21:00-23:00 UTC): spreads widen significantly and liquidity thins."

    pair_clean = pair.upper()

    # European pairs (EUR, GBP, CHF) in Asian session often range quietly with fakeouts
    if session == SESSION_ASIAN:
        if any(c in pair_clean for c in ["JPY", "AUD", "NZD"]):
            return True, f"Asian session: Active domestic volume for {pair}."
        return False, f"Asian session (00:00-08:00 UTC): Low liquidity for European pair {pair}. Recommend London/NY."

    if session == SESSION_LONDON_NY_OVERLAP:
        return True, "London / New York Overlap: Peak global liquidity and optimal momentum."

    if session == SESSION_LONDON:
        return True, "London Session: High liquidity and active institutional trend volume."

    if session == SESSION_NEW_YORK:
        return True, "New York Session: Strong USD momentum and high liquidity."

    return True, f"Active session: {session}"


def get_session_weight(session: str) -> float:
    """Returns confidence score multiplier for the session."""
    weights = {
        SESSION_LONDON_NY_OVERLAP: 1.0,
        SESSION_LONDON: 0.9,
        SESSION_NEW_YORK: 0.85,
        SESSION_ASIAN: 0.65,
        SESSION_ROLLOVER: 0.0,
        SESSION_OFF_HOURS: 0.0,
    }
    return weights.get(session, 0.7)


def attach_forex_sessions(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized attachment of market session labels to OHLCV DataFrame."""
    if df.empty or "datetime" not in df.columns:
        return df

    res = df.copy()
    dt_series = pd.to_datetime(res["datetime"], utc=True)
    hours = dt_series.dt.hour
    weekdays = dt_series.dt.weekday

    # Vectorized condition list
    cond_rollover = (hours >= 21) & (hours < 23)
    cond_overlap = (hours >= 12) & (hours < 16)
    cond_london = (hours >= 7) & (hours < 16)
    cond_ny = (hours >= 12) & (hours < 21)
    cond_asian = (hours >= 0) & (hours < 8)

    sessions = []
    for h, w in zip(hours, weekdays):
        if w == 5 or (w == 4 and h >= 22) or (w == 6 and h < 21):
            sessions.append(SESSION_OFF_HOURS)
        elif 21 <= h < 23:
            sessions.append(SESSION_ROLLOVER)
        elif 12 <= h < 16:
            sessions.append(SESSION_LONDON_NY_OVERLAP)
        elif 7 <= h < 16:
            sessions.append(SESSION_LONDON)
        elif 12 <= h < 21:
            sessions.append(SESSION_NEW_YORK)
        elif 0 <= h < 8:
            sessions.append(SESSION_ASIAN)
        else:
            sessions.append(SESSION_OFF_HOURS)

    res["session"] = sessions
    return res


# Aliases for seamless interface compatibility
classify_session = get_forex_session
get_current_session = get_forex_session
is_session_tradable = is_tradeable_session
