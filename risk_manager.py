"""
Risk Management and Position Sizing Engine for Binance Spot Trading.
Calculates position sizing strictly based on fixed risk percentage and stop-loss distance.
Enforces non-negotiable hard caps (maximum 2% account risk per trade), daily drawdown halts,
and programmatic prohibition of Martingale or post-loss stake scaling.
"""

import datetime
from dataclasses import dataclass
from typing import Tuple, Dict, Any

try:
    from . import config
    from .logger import get_logger
except ImportError:
    import config
    from logger import get_logger

logger = get_logger("risk_manager")


@dataclass
class PositionSizing:
    """Detailed position sizing outcome."""
    dollar_risk: float          # Capital at risk if stop loss is hit ($)
    risk_pct: float             # Risk as a fraction of account balance
    stop_loss_distance: float   # Price difference between entry and stop loss
    position_size: float        # Quantity of asset units to purchase (e.g. BTC)
    position_value: float       # Total capital outlay required in quote currency ($)
    is_cash_capped: bool        # True if constrained by available spot cash


class SpotRiskManager:
    """
    Guards spot trading capital:
      1. Dynamic position sizing: Risk % / Stop-Loss Distance.
      2. Non-negotiable hard risk ceiling: <= 2% per trade.
      3. Strict prohibition of Martingale or size expansion after a loss.
      4. Daily loss limit and daily trade count circuit breakers.
    """

    def __init__(
        self,
        risk_per_trade_pct: float = None,
        daily_max_loss: float = None,
        daily_max_trades: int = None,
    ) -> None:
        raw_risk = risk_per_trade_pct if risk_per_trade_pct is not None else config.RISK_PER_TRADE_PCT

        # ⚠️ NON-NEGOTIABLE HARD CEILING: Hard-cap risk per trade at 2% (0.02)
        if raw_risk > config.MAX_ALLOWED_RISK_PCT:
            logger.warning(
                f"[RISK CLAMP] Configured risk {raw_risk * 100:.2f}% exceeds hard limit of "
                f"{config.MAX_ALLOWED_RISK_PCT * 100:.1f}%. Strictly clamping to 2.0%."
            )
            self.risk_per_trade_pct: float = config.MAX_ALLOWED_RISK_PCT
        else:
            self.risk_per_trade_pct: float = max(0.001, float(raw_risk))

        self.daily_max_loss: float = float(daily_max_loss if daily_max_loss is not None else config.DAILY_MAX_LOSS)
        self.daily_max_trades: int = int(daily_max_trades if daily_max_trades is not None else config.DAILY_MAX_TRADES)

        # Invariant validations
        if self.daily_max_loss <= 0:
            raise ValueError("DAILY_MAX_LOSS must be strictly positive.")
        if self.daily_max_trades <= 0:
            raise ValueError("DAILY_MAX_TRADES must be a positive integer.")

        # Daily tracking state (midnight local rollover)
        self.current_date: datetime.date = datetime.date.today()
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.daily_wins: int = 0
        self.daily_losses: int = 0
        self.is_halted: bool = False
        self.halt_reason: str = ""

        # Anti-Martingale invariant tracking
        self.last_trade_loss: bool = False
        self.last_dollar_risk: float = 0.0

        logger.info(
            f"SpotRiskManager initialized | RiskPerTrade: {self.risk_per_trade_pct * 100:.1f}% (Hard-Cap: 2.0%) | "
            f"DailyMaxLoss: ${self.daily_max_loss:,.2f} | DailyMaxTrades: {self.daily_max_trades}"
        )

    def check_date_rollover(self, current_date: Optional[datetime.date] = None) -> None:
        """Resets daily performance tracking at midnight (supports simulated bar dates)."""
        today = current_date if current_date is not None else datetime.date.today()
        if today != self.current_date:
            logger.debug(
                f"[MIDNIGHT RESET] Spot risk counters reset for {today}. "
                f"Prior day PnL: ${self.daily_pnl:+,.2f} over {self.daily_trades} trades."
            )
            self.current_date = today
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.daily_wins = 0
            self.daily_losses = 0
            self.is_halted = False
            self.halt_reason = ""

    def can_trade(
        self, current_balance: float, current_date: Optional[datetime.date] = None
    ) -> Tuple[bool, str]:
        """
        Pre-trade risk verification check.
        Must be checked before any spot entry order is placed.
        """
        self.check_date_rollover(current_date)

        if current_balance <= 0:
            return False, "Insufficient balance: Account equity is zero or negative."

        if self.is_halted:
            return False, f"Trading halted for today ({self.current_date}): {self.halt_reason}"

        # 1. Daily loss circuit breaker
        if self.daily_pnl <= -abs(self.daily_max_loss):
            self.is_halted = True
            self.halt_reason = (
                f"DAILY MAX LOSS HIT: Cumulative loss of ${abs(self.daily_pnl):,.2f} "
                f"breached limit of ${self.daily_max_loss:,.2f}"
            )
            logger.critical(f"[RISK HALT] {self.halt_reason}. Halting spot entries for today.")
            return False, self.halt_reason

        # 2. Daily trade count cap
        if self.daily_trades >= self.daily_max_trades:
            self.is_halted = True
            self.halt_reason = f"DAILY MAX TRADES HIT: Reached cap of {self.daily_max_trades} trades."
            logger.warning(f"[RISK HALT] {self.halt_reason}. Halting spot entries until tomorrow.")
            return False, self.halt_reason

        return True, "Risk checks passed. Order permitted."

    def calculate_position_size(
        self,
        account_balance: float,
        entry_price: float,
        stop_loss_price: float,
    ) -> PositionSizing:
        """
        Calculates position size strictly adhering to:
          Position Size = (Account Balance * Risk %) / Stop-Loss Distance

        Safety Guarantees:
          - Hard capped at <= 2.0% of account equity.
          - Never scales upward after a loss (Anti-Martingale check).
          - Cannot exceed 100% of available cash on non-leveraged spot.
        """
        if entry_price <= 0:
            raise ValueError(f"Invalid entry_price: {entry_price}")
        if stop_loss_price <= 0:
            raise ValueError(f"Invalid stop_loss_price: {stop_loss_price}")

        stop_loss_distance = abs(entry_price - stop_loss_price)
        if stop_loss_distance <= 0:
            raise ValueError("Stop loss cannot equal entry price.")

        # Non-negotiable 2% hard cap check
        effective_risk_pct = min(self.risk_per_trade_pct, config.MAX_ALLOWED_RISK_PCT)
        target_dollar_risk = account_balance * effective_risk_pct

        # ANTI-MARTINGALE ENFORCEMENT IN CODE:
        # If the prior trade was a loss, strictly forbid expanding dollar risk beyond
        # what the current equity curve naturally permits (risk naturally scales down in drawdown).
        if self.last_trade_loss and self.last_dollar_risk > 0:
            if target_dollar_risk > self.last_dollar_risk:
                # Disallow risk inflation after loss
                target_dollar_risk = min(target_dollar_risk, self.last_dollar_risk)

        # Asset units to purchase (e.g. BTC)
        position_units = target_dollar_risk / stop_loss_distance
        total_cost_usdt = position_units * entry_price

        # Spot capital ceiling: Cannot exceed total cash balance (no leverage)
        is_cash_capped = False
        if total_cost_usdt > account_balance:
            is_cash_capped = True
            total_cost_usdt = account_balance
            position_units = total_cost_usdt / entry_price
            # Actual dollar risk adjusts down if cash constrained
            target_dollar_risk = position_units * stop_loss_distance

        self.last_dollar_risk = target_dollar_risk

        return PositionSizing(
            dollar_risk=round(target_dollar_risk, 2),
            risk_pct=round(effective_risk_pct, 4),
            stop_loss_distance=round(stop_loss_distance, 4),
            position_size=round(position_units, 6),
            position_value=round(total_cost_usdt, 2),
            is_cash_capped=is_cash_capped,
        )

    def record_trade_result(
        self, result: str, pnl: float, current_date: Optional[datetime.date] = None
    ) -> None:
        """
        Records the outcome of a closed spot trade and evaluates daily circuit breakers.
        """
        self.check_date_rollover(current_date)

        res_upper = str(result).upper()
        self.daily_trades += 1
        self.daily_pnl += pnl

        if res_upper == "WIN" or pnl > 0:
            self.daily_wins += 1
            self.last_trade_loss = False
        else:
            self.daily_losses += 1
            self.last_trade_loss = True

        logger.info(
            f"[SPOT TRADE RECORDED] Trade #{self.daily_trades} ({res_upper}) | "
            f"Trade PnL: ${pnl:+,.2f} | Cumulative Daily PnL: ${self.daily_pnl:+,.2f} "
            f"(Wins: {self.daily_wins}, Losses: {self.daily_losses})"
        )

        # Immediate threshold evaluation
        if self.daily_pnl <= -abs(self.daily_max_loss):
            self.is_halted = True
            self.halt_reason = (
                f"DAILY MAX LOSS HIT: Cumulative loss of ${abs(self.daily_pnl):,.2f} "
                f"hit limit of ${self.daily_max_loss:,.2f}"
            )
            logger.critical(f"[RISK LIMIT HIT] {self.halt_reason}! Spot trading halted for today.")
        elif self.daily_trades >= self.daily_max_trades:
            self.is_halted = True
            self.halt_reason = f"DAILY MAX TRADES HIT: Executed {self.daily_trades}/{self.daily_max_trades} trades."
            logger.warning(f"[RISK LIMIT HIT] {self.halt_reason}! Daily trade quota reached.")

    def get_stats(self) -> Dict[str, Any]:
        """Returns snapshot of risk parameters and daily performance."""
        self._check_day_rollover()
        return {
            "date": str(self.current_date),
            "daily_trades": self.daily_trades,
            "daily_max_trades": self.daily_max_trades,
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_max_loss": round(self.daily_max_loss, 2),
            "wins": self.daily_wins,
            "losses": self.daily_losses,
            "is_halted": self.is_halted,
            "halt_reason": self.halt_reason,
            "risk_per_trade_pct": self.risk_per_trade_pct,
        }
