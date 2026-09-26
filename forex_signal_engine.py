"""
forex_signal_engine.py
======================
Institutional Signal-Only Real-Time Forex Generation Engine.
Strictly Read-Only:
  - ZERO order execution.
  - ZERO broker connections for trading.
  - ZERO auto-trading capability.
  - Manual execution only by end users.

Architecture:
  - Multi-Timeframe Cascade: 1M -> 5M -> 15M -> 1H -> 4H.
  - Validated Phase 7 Mod C Entry Logic:
      1. 1H SIDEWAYS Exclusion (trend_1h == "SIDEWAYS" -> NO SIGNAL / HOLD)
      2. 4H TRENDING_UP Overextension Filter (dist_from_EMA50 <= 0.80 ATR; other regimes <= 1.40 ATR)
  - Exit Architecture (Candidate C Reference):
      SL at structural swing extreme, TP1 at +1.0R, TP2 at +2.0R.
  - Time Handling:
      Internal calculations: UTC only.
      Display timestamps: IST (UTC + 05:30).
  - Spread Gate:
      spread_cost <= 0.25 * ATR.
  - State Tracking & Duplicate Prevention:
      Unique Signal ID: {PAIR_CLEAN}-{YYYYMMDD}-{HHMM}
      Restart-safe persistence to data/signals_live_state.json.
  - Health & Safety Monitoring:
      Tracks feed freshness, latency, and marks SYSTEM STATUS as SAFE or UNSAFE.
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field, asdict
import pandas as pd
import numpy as np

spot_bot_dir = Path(__file__).resolve().parent
if str(spot_bot_dir) not in sys.path:
    sys.path.insert(0, str(spot_bot_dir))

import forex_utils
import forex_sessions
import indicators
import regime_detector
import market_structure
import short_tf_engine
import exit_engine
from logger import get_logger

logger = get_logger("forex_signal_engine")

DATA_DIR = spot_bot_dir / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "signals_live_state.json"
SIGNALS_LOG_FILE = DATA_DIR / "signals_live_log.csv"

SUPPORTED_PAIRS: List[str] = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/CHF",
    "AUD/USD",
    "USD/CAD",
    "NZD/USD",
]

IST_OFFSET = timedelta(hours=5, minutes=30)
MAX_STALE_SECONDS = 15 * 60  # 15 minutes max staleness for 5M candles


@dataclass
class FormattedSignal:
    pair: str
    direction: str  # "BUY", "SELL", or "NO SIGNAL"
    signal_time_ist: str
    signal_time_utc: str
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    risk_1r_pips: float
    risk_1r_price: float
    regime_4h: str
    trend_1h: str
    setup_5m_status: str
    spread_pips: float
    spread_gate_status: str
    signal_rule: str
    signal_id: str
    is_valid_signal: bool
    rejection_reason: str = ""
    data_latency_ms: float = 0.0

    def to_card(self) -> str:
        """Renders standard user-facing signal card for manual execution."""
        if self.direction == "BUY":
            header = "🟢 BUY SIGNAL"
        elif self.direction == "SELL":
            header = "🔴 SELL SIGNAL"
        else:
            header = "⚪ NO SIGNAL"

        lines = [
            "━━━━━━━━━━━━━━━━━━",
            header,
            f"{self.pair}",
            "━━━━━━━━━━━━━━━━━━",
            "",
            f"Time: {self.signal_time_ist} (IST)",
        ]

        if self.is_valid_signal:
            lines.extend([
                f"Entry: {self.entry_price:.5f}",
                f"SL:    {self.stop_loss:.5f}",
                f"TP1:   {self.take_profit_1:.5f} (+1R)",
                f"TP2:   {self.take_profit_2:.5f} (+2R)",
                f"Risk:  {self.risk_1r_pips:.1f} pips ({self.risk_1r_price:.5f})",
                "",
                f"4H Regime: {self.regime_4h}",
                f"1H Trend:  {self.trend_1h}",
                f"5M Setup:  {self.setup_5m_status}",
                f"Spread:    {self.spread_pips:.1f} pips",
                f"Status:    VALID",
                "",
                f"Signal ID: {self.signal_id}",
            ])
        else:
            lines.extend([
                f"Status:    NO SIGNAL",
                f"Reason:    {self.rejection_reason}",
                "",
                f"4H Regime: {self.regime_4h}",
                f"1H Trend:  {self.trend_1h}",
                f"Spread:    {self.spread_pips:.1f} pips",
                f"Gate:      {self.spread_gate_status}",
            ])

        lines.append("━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)


@dataclass
class EngineHealth:
    data_connection_status: str = "INITIALIZING"
    system_status: str = "INITIALIZING"  # "SAFE" or "UNSAFE"
    last_candle_time_utc: str = ""
    last_candle_time_ist: str = ""
    current_time_utc: str = ""
    current_time_ist: str = ""
    signals_count: int = 0
    no_signal_count: int = 0
    rejected_reasons_tally: Dict[str, int] = field(default_factory=dict)
    spread_status: Dict[str, str] = field(default_factory=dict)
    system_uptime_seconds: float = 0.0
    active_signals: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def convert_utc_to_ist(dt_utc: datetime) -> Tuple[datetime, str]:
    """Converts a UTC datetime to IST (UTC + 05:30) and returns (ist_dt, formatted_str)."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    dt_ist = dt_utc + IST_OFFSET
    return dt_ist, dt_ist.strftime("%Y-%m-%d %H:%M:%S IST")


def generate_signal_id(pair: str, dt_utc: datetime) -> str:
    """Generates unique deterministic signal ID: e.g. EURUSD-20260926-1730."""
    clean_pair = pair.replace("/", "").replace("_", "").upper()
    ts_str = dt_utc.strftime("%Y%m%d-%H%M")
    return f"{clean_pair}-{ts_str}"


class ForexSignalEngine:
    """
    Production-Grade Signal-Only Engine orchestrating Mod C entry evaluation
    and Candidate C risk parameters.
    """

    def __init__(self, data_feed_module=None, historical_feed_module=None):
        self.data_feed = data_feed_module
        self.historical_feed = historical_feed_module
        self.start_time = time.time()
        self.health = EngineHealth()
        self.last_signal_times: Dict[str, int] = {}
        self.seen_signal_ids: set = set()
        self._load_state()

    def _load_state(self):
        """Loads persistent signal history to prevent duplicate alerts on process restart."""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.seen_signal_ids = set(data.get("seen_signal_ids", []))
                    self.last_signal_times = data.get("last_signal_times", {})
                    self.health.signals_count = data.get("signals_count", 0)
                    self.health.no_signal_count = data.get("no_signal_count", 0)
                    self.health.rejected_reasons_tally = data.get("rejected_reasons_tally", {})
            except Exception as e:
                logger.warning(f"Could not load state from {STATE_FILE}: {e}")

    def _save_state(self):
        """Saves persistent signal history to disk."""
        try:
            payload = {
                "seen_signal_ids": list(self.seen_signal_ids)[-1000:],  # keep last 1000
                "last_signal_times": self.last_signal_times,
                "signals_count": self.health.signals_count,
                "no_signal_count": self.health.no_signal_count,
                "rejected_reasons_tally": self.health.rejected_reasons_tally,
                "last_saved_utc": datetime.now(timezone.utc).isoformat(),
            }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to persist state: {e}")

    def evaluate_pair(
        self,
        pair: str,
        df_5m: pd.DataFrame,
        df_1m: Optional[pd.DataFrame] = None,
        df_15m: Optional[pd.DataFrame] = None,
        df_1h: Optional[pd.DataFrame] = None,
        df_4h: Optional[pd.DataFrame] = None,
        current_time_utc: Optional[datetime] = None,
        simulated_latency_ms: float = 0.0,
    ) -> FormattedSignal:
        """
        Evaluates a single pair against the Mod C signal criteria with full causal safety.
        Returns a FormattedSignal object with either a valid BUY/SELL signal or NO SIGNAL.
        """
        now_utc = current_time_utc or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        _, now_ist_str = convert_utc_to_ist(now_utc)

        pip_val = forex_utils.get_pip_size(pair)
        spread_pips = forex_utils.get_pair_spread_pips(pair)
        spread_cost = spread_pips * pip_val

        # Gate 1: Check data availability and minimum length
        if df_5m is None or len(df_5m) < 35:
            reason = "INSUFFICIENT_5M_DATA"
            self._record_rejection(reason)
            return self._build_no_signal(pair, now_utc, spread_pips, "UNKNOWN", "UNKNOWN", reason)

        # Gate 2: Causal check - only use closed/confirmed candle
        # The latest bar in df_5m must be a fully closed candle
        last_5m_bar = df_5m.iloc[-1]
        candle_dt_raw = last_5m_bar.get("datetime")
        if candle_dt_raw is not None:
            if isinstance(candle_dt_raw, str):
                candle_dt_utc = pd.to_datetime(candle_dt_raw, utc=True).to_pydatetime()
            elif isinstance(candle_dt_raw, pd.Timestamp):
                candle_dt_utc = candle_dt_raw.to_pydatetime()
            else:
                candle_dt_utc = candle_dt_raw
            if candle_dt_utc.tzinfo is None:
                candle_dt_utc = candle_dt_utc.replace(tzinfo=timezone.utc)
        else:
            candle_dt_utc = now_utc

        # Check for stale data (e.g. market closed or feed broken)
        age_seconds = (now_utc - candle_dt_utc).total_seconds()
        if age_seconds > MAX_STALE_SECONDS:
            reason = f"STALE_MARKET_DATA ({int(age_seconds)}s old)"
            self._record_rejection("STALE_MARKET_DATA")
            return self._build_no_signal(pair, candle_dt_utc, spread_pips, "UNKNOWN", "UNKNOWN", reason)

        # Gate: Duplicate Signal Prevention (Unique Signal ID check)
        sig_id = generate_signal_id(pair, candle_dt_utc)
        if sig_id in self.seen_signal_ids:
            reason = f"DUPLICATE_SIGNAL_ALREADY_ISSUED ({sig_id})"
            self._record_rejection("DUPLICATE_SIGNAL_SUPPRESSED")
            return self._build_no_signal(pair, candle_dt_utc, spread_pips, "UNKNOWN", "UNKNOWN", reason)

        # Build Multi-Timeframe Context causally
        ctx = short_tf_engine.build_mtf_context(
            pair=pair,
            df_4h=df_4h,
            df_1h=df_1h,
            df_15m=df_15m,
            current_time=pd.Timestamp(candle_dt_utc),
            curr_price=float(last_5m_bar["close"]),
        )

        regime_4h = ctx.regime_4h
        trend_1h = ctx.trend_1h

        # Gate 3: Mod C Filter A - 1H SIDEWAYS Exclusion
        if trend_1h == "SIDEWAYS":
            reason = "1H_SIDEWAYS_EXCLUSION_HOLD"
            self._record_rejection(reason)
            return self._build_no_signal(pair, candle_dt_utc, spread_pips, regime_4h, trend_1h, reason)

        # Gate 4: Session Filter
        if not ctx.can_trade_session:
            reason = f"SESSION_FILTER_HOLD_{ctx.active_session}"
            self._record_rejection("SESSION_FILTER_HOLD")
            return self._build_no_signal(pair, candle_dt_utc, spread_pips, regime_4h, trend_1h, reason)

        # Evaluate 5M Primary Setup via short_tf_engine with Mod C parameters
        decision_5m = short_tf_engine.evaluate_5m_setup(
            df_5m=df_5m,
            context=ctx,
            pair=pair,
            exclude_1h_sideways=True,
            trending_up_max_ext_atr=0.80,
            default_max_ext_atr=1.40,
        )

        atr_val = decision_5m.atr_value if decision_5m.atr_value > 0 else (10.0 * pip_val)
        spread_gate_ok = (spread_cost <= 0.25 * atr_val)
        spread_gate_status = "PASS" if spread_gate_ok else "REJECTED (Cost > 0.25 ATR)"

        # Gate 5: Spread Gate Protection
        if not spread_gate_ok:
            reason = "SPREAD_EXCESSIVE_HOLD"
            self._record_rejection(reason)
            return self._build_no_signal(pair, candle_dt_utc, spread_pips, regime_4h, trend_1h, reason, spread_gate_status)

        # Gate 6: 5M Setup Match
        if decision_5m.signal == "HOLD":
            reason = decision_5m.rule_set
            self._record_rejection(reason)
            return self._build_no_signal(pair, candle_dt_utc, spread_pips, regime_4h, trend_1h, reason, spread_gate_status)

        # Gate 7: 1M Micro Confirmation
        last_sig_time = self.last_signal_times.get(pair)
        final_decision = short_tf_engine.confirm_1m_entry(
            df_1m=df_1m,
            candidate_5m=decision_5m,
            pair=pair,
            last_signal_time=last_sig_time,
        )

        if final_decision.signal == "HOLD":
            reason = final_decision.rule_set
            self._record_rejection(reason)
            return self._build_no_signal(pair, candle_dt_utc, spread_pips, regime_4h, trend_1h, reason, spread_gate_status)

        # Gate 8: Duplicate Signal Prevention (Unique Signal ID check)
        sig_id = generate_signal_id(pair, candle_dt_utc)
        if sig_id in self.seen_signal_ids:
            reason = f"DUPLICATE_SIGNAL_ALREADY_ISSUED ({sig_id})"
            self._record_rejection("DUPLICATE_SIGNAL_SUPPRESSED")
            return self._build_no_signal(pair, candle_dt_utc, spread_pips, regime_4h, trend_1h, reason, spread_gate_status)

        # Valid Signal Triggered! Calculate exact Risk, SL, TP1, and TP2
        entry_price = forex_utils.round_forex_price(final_decision.entry_price, pair)
        stop_loss = forex_utils.round_forex_price(final_decision.stop_loss, pair)
        sl_dist = abs(entry_price - stop_loss)
        sl_pips = sl_dist / pip_val

        direction = "BUY" if final_decision.signal == "BUY" else "SELL"

        # Calculate TP1 (+1.0R) and TP2 (+2.0R) based on Candidate C architecture
        if direction == "BUY":
            tp1 = forex_utils.round_forex_price(entry_price + sl_dist, pair)
            tp2 = forex_utils.round_forex_price(entry_price + (2.0 * sl_dist), pair)
        else:
            tp1 = forex_utils.round_forex_price(entry_price - sl_dist, pair)
            tp2 = forex_utils.round_forex_price(entry_price - (2.0 * sl_dist), pair)

        _, ist_time_str = convert_utc_to_ist(candle_dt_utc)

        formatted_sig = FormattedSignal(
            pair=pair,
            direction=direction,
            signal_time_ist=ist_time_str,
            signal_time_utc=candle_dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            risk_1r_pips=round(sl_pips, 1),
            risk_1r_price=round(sl_dist, 5),
            regime_4h=regime_4h,
            trend_1h=trend_1h,
            setup_5m_status="VALID",
            spread_pips=round(spread_pips, 1),
            spread_gate_status="PASS",
            signal_rule=final_decision.rule_set,
            signal_id=sig_id,
            is_valid_signal=True,
            rejection_reason="NONE",
            data_latency_ms=round(simulated_latency_ms, 2),
        )

        # Mark as seen and update state
        self.seen_signal_ids.add(sig_id)
        if "time" in last_5m_bar:
            self.last_signal_times[pair] = int(last_5m_bar["time"]) * 1000
        else:
            self.last_signal_times[pair] = int(candle_dt_utc.timestamp() * 1000)

        self.health.signals_count += 1
        self.health.active_signals[pair] = asdict(formatted_sig)
        self._log_signal_to_csv(formatted_sig)
        self._save_state()

        logger.info(f"VALID SIGNAL ISSUED: {pair} {direction} at {entry_price} (ID: {sig_id})")
        return formatted_sig

    def _build_no_signal(
        self,
        pair: str,
        dt_utc: datetime,
        spread_pips: float,
        regime_4h: str,
        trend_1h: str,
        reason: str,
        spread_gate_status: str = "UNKNOWN",
    ) -> FormattedSignal:
        _, ist_time_str = convert_utc_to_ist(dt_utc)
        sig = FormattedSignal(
            pair=pair,
            direction="NO SIGNAL",
            signal_time_ist=ist_time_str,
            signal_time_utc=dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
            entry_price=0.0,
            stop_loss=0.0,
            take_profit_1=0.0,
            take_profit_2=0.0,
            risk_1r_pips=0.0,
            risk_1r_price=0.0,
            regime_4h=regime_4h,
            trend_1h=trend_1h,
            setup_5m_status="HOLD",
            spread_pips=round(spread_pips, 1),
            spread_gate_status=spread_gate_status,
            signal_rule=reason,
            signal_id=generate_signal_id(pair, dt_utc),
            is_valid_signal=False,
            rejection_reason=reason,
        )
        self.health.active_signals[pair] = asdict(sig)
        return sig

    def _record_rejection(self, reason: str):
        self.health.no_signal_count += 1
        self.health.rejected_reasons_tally[reason] = (
            self.health.rejected_reasons_tally.get(reason, 0) + 1
        )

    def _log_signal_to_csv(self, sig: FormattedSignal):
        file_exists = SIGNALS_LOG_FILE.exists()
        try:
            with open(SIGNALS_LOG_FILE, "a", newline="", encoding="utf-8") as f:
                import csv
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow([
                        "signal_id", "timestamp_utc", "timestamp_ist", "pair",
                        "direction", "entry_price", "stop_loss", "tp1", "tp2",
                        "risk_1r_pips", "regime_4h", "trend_1h", "spread_pips",
                        "rule_set"
                    ])
                writer.writerow([
                    sig.signal_id, sig.signal_time_utc, sig.signal_time_ist, sig.pair,
                    sig.direction, sig.entry_price, sig.stop_loss, sig.take_profit_1, sig.take_profit_2,
                    sig.risk_1r_pips, sig.regime_4h, sig.trend_1h, sig.spread_pips,
                    sig.signal_rule
                ])
        except Exception as e:
            logger.error(f"Failed to append to {SIGNALS_LOG_FILE}: {e}")

    def update_health(self, last_candle_utc: Optional[datetime] = None, is_connected: bool = True):
        now_utc = datetime.now(timezone.utc)
        _, now_ist_str = convert_utc_to_ist(now_utc)
        self.health.current_time_utc = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
        self.health.current_time_ist = now_ist_str
        self.health.system_uptime_seconds = round(time.time() - self.start_time, 1)

        if last_candle_utc is not None:
            if last_candle_utc.tzinfo is None:
                last_candle_utc = last_candle_utc.replace(tzinfo=timezone.utc)
            _, l_ist_str = convert_utc_to_ist(last_candle_utc)
            self.health.last_candle_time_utc = last_candle_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
            self.health.last_candle_time_ist = l_ist_str

            age = (now_utc - last_candle_utc).total_seconds()
            if is_connected and age <= MAX_STALE_SECONDS:
                self.health.data_connection_status = "CONNECTED"
                self.health.system_status = "SAFE"
            else:
                self.health.data_connection_status = "DISCONNECTED_OR_STALE"
                self.health.system_status = "UNSAFE"
        else:
            self.health.data_connection_status = "NO_DATA"
            self.health.system_status = "UNSAFE"

        # Check spreads
        for pair in SUPPORTED_PAIRS:
            sp = forex_utils.get_pair_spread_pips(pair)
            max_sp = 2.5 if "GBP" not in pair else 3.0
            self.health.spread_status[pair] = f"{sp:.1f} pips ({'NORMAL' if sp <= max_sp else 'WIDE'})"

    def get_health_snapshot(self) -> Dict[str, Any]:
        return asdict(self.health)


# Global singleton engine instance for web/CLI access
_global_engine: Optional[ForexSignalEngine] = None


def get_signal_engine() -> ForexSignalEngine:
    global _global_engine
    if _global_engine is None:
        _global_engine = ForexSignalEngine()
    return _global_engine
