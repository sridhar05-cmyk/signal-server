"""
Configuration Settings for Binance Spot Trading & Research Engine.
Enforces non-negotiable risk limits, capital protection defaults,
and market data parameters (no credentials required for market data/backtesting).
"""

import os
from pathlib import Path

# ==============================================================================
# MARKET & DATA FEED SETTINGS (PUBLIC DATA ONLY - NO API KEYS NEEDED)
# ==============================================================================
DEFAULT_SYMBOL: str = "BTC/USDT"
BINANCE_PAIR: str = "BTCUSDT"
SUPPORTED_TIMEFRAMES: list[str] = ["15m", "1h", "4h"]
DEFAULT_TIMEFRAME: str = "1h"

# ==============================================================================
# CAPITAL & SIMULATION SETTINGS
# ==============================================================================
INITIAL_BALANCE: float = float(os.getenv("SPOT_INITIAL_BALANCE", "10000.0"))

# ==============================================================================
# RISK MANAGEMENT INVARIANTS (HARD-CODED CAPS)
# ==============================================================================
# Target risk per trade as a fraction of account equity (e.g. 0.01 = 1.0%)
# ⚠️ HARD SAFETY RULE: Hard-capped at 2% (0.02) maximum in code.
# Config values above 2% are strictly rejected by validate_config().
MAX_ALLOWED_RISK_PCT: float = 0.02
RISK_PER_TRADE_PCT: float = float(os.getenv("SPOT_RISK_PER_TRADE_PCT", "0.01"))

# Daily loss limit in dollars - trading halts for the day if cumulative loss hits this
DAILY_MAX_LOSS: float = float(os.getenv("SPOT_DAILY_MAX_LOSS", "500.0"))

# Daily maximum trade execution cap
DAILY_MAX_TRADES: int = int(os.getenv("SPOT_DAILY_MAX_TRADES", "10"))

# ==============================================================================
# STRATEGY RISK-REWARD & ATR PARAMETERS
# ==============================================================================
ATR_PERIOD: int = 14
STOP_LOSS_ATR_MULT: float = 2.0     # 2x ATR below entry for Long stop loss
TAKE_PROFIT_ATR_MULT: float = 3.0   # 3x ATR above entry for Long take profit (1:1.5 min R:R)

# ==============================================================================
# SYSTEM RUNTIME MODE
# ==============================================================================
BACKTEST_MODE: bool = True
LIVE_TRADING: bool = False  # Strictly False in this research & backtesting phase


def validate_config() -> None:
    """
    Validates configuration to ensure strict risk enforcement.
    Raises ValueError if risk boundaries or safety invariants are breached.
    """
    global RISK_PER_TRADE_PCT

    if RISK_PER_TRADE_PCT > MAX_ALLOWED_RISK_PCT:
        raise ValueError(
            f"Safety Invariant Violation: RISK_PER_TRADE_PCT ({RISK_PER_TRADE_PCT * 100:.1f}%) "
            f"exceeds the non-negotiable hard cap of {MAX_ALLOWED_RISK_PCT * 100:.1f}%."
        )

    if RISK_PER_TRADE_PCT <= 0:
        raise ValueError("RISK_PER_TRADE_PCT must be strictly positive.")

    if DAILY_MAX_LOSS <= 0:
        raise ValueError("DAILY_MAX_LOSS must be strictly positive.")

    if DAILY_MAX_TRADES <= 0:
        raise ValueError("DAILY_MAX_TRADES must be a positive integer.")

    if DEFAULT_TIMEFRAME not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"DEFAULT_TIMEFRAME '{DEFAULT_TIMEFRAME}' not supported. Options: {SUPPORTED_TIMEFRAMES}")
