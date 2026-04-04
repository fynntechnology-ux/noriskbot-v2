"""
Shared state — written by the bot, read by the dashboard.
All writes are non-blocking; dashboard never blocks trading.
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Deque
import time


@dataclass
class PositionState:
    mint: str
    symbol: str
    name: str
    buy_order_id: str
    bought_at: float
    sol_spent: float
    bonding_at_buy: float        # bonding % when we bought
    peak_bonding: float          # highest bonding % seen before buy
    sell_order_id: str | None = None
    closed: bool = False
    sell_result: str = ""        # "ok" | "failed"
    token_accounts: object = None  # TokenAccounts — stored for local sell tx


@dataclass
class SignalEvent:
    ts: float
    kind: str        # "launch" | "signal" | "buy" | "sell" | "skip" | "error"
    mint: str
    symbol: str
    detail: str


@dataclass
class TrackedToken:
    mint: str
    symbol: str
    name: str
    created_at: float
    peak_bonding: float = 0.0
    current_bonding: float = 0.0
    last_update: float = field(default_factory=time.time)


class BotState:
    def __init__(self):
        self.started_at: float = time.time()

        # Active + closed positions
        self.positions: dict[str, PositionState] = {}

        # Live token tracking (mint → TrackedToken)
        self.tracked: dict[str, TrackedToken] = {}

        # Event log (capped at 200)
        self.events: Deque[SignalEvent] = deque(maxlen=200)

        # (symbol.lower, name.lower) → {mint: seen_at} — used for dup detection
        self.name_registry: dict[tuple[str, str], dict[str, float]] = {}

        # Cumulative stats
        self.total_buys: int = 0
        self.total_sells: int = 0
        self.total_sol_spent: float = 0.0
        self.total_sol_returned: float = 0.0
        self.sell_failures: int = 0

    # ------------------------------------------------------------------
    # Write helpers (called from bot / monitor / position manager)
    # ------------------------------------------------------------------

    def log(self, kind: str, mint: str, symbol: str, detail: str):
        self.events.appendleft(SignalEvent(
            ts=time.time(), kind=kind,
            mint=mint, symbol=symbol, detail=detail,
        ))

    def track_token(self, mint: str, symbol: str, name: str, created_at: float):
        self.tracked[mint] = TrackedToken(
            mint=mint, symbol=symbol, name=name, created_at=created_at,
        )
        # Register this (symbol, name) → {mint: seen_at} for dup detection
        key = (symbol.strip().lower(), name.strip().lower())
        self.name_registry.setdefault(key, {})[mint] = time.time()

    def is_duplicate(self, mint: str, symbol: str, name: str) -> bool:
        """Return True if another mint with the same symbol+name was seen within 60s."""
        key = (symbol.strip().lower(), name.strip().lower())
        cutoff = time.time() - 60
        recent = {m for m, ts in self.name_registry.get(key, {}).items()
                  if ts >= cutoff and m != mint}
        return len(recent) > 0

    def update_token_bonding(self, mint: str, bonding: float):
        t = self.tracked.get(mint)
        if t:
            t.current_bonding = bonding
            t.last_update = time.time()
            if bonding > t.peak_bonding:
                t.peak_bonding = bonding

    def remove_tracked(self, mint: str):
        self.tracked.pop(mint, None)

    def prune_name_registry(self):
        """Evict name_registry entries older than the 60s dup-detection window."""
        cutoff = time.time() - 60
        empty_keys = []
        for key, mints in self.name_registry.items():
            stale = [m for m, ts in mints.items() if ts < cutoff]
            for m in stale:
                del mints[m]
            if not mints:
                empty_keys.append(key)
        for key in empty_keys:
            del self.name_registry[key]

    def open_position(self, pos: PositionState):
        self.positions[pos.mint] = pos
        self.total_buys += 1
        self.total_sol_spent += pos.sol_spent

    def close_position(self, mint: str, sol_returned: float, success: bool):
        pos = self.positions.get(mint)
        if pos:
            pos.closed = True
            pos.sell_result = "ok" if success else "failed"
            self.total_sells += 1
            self.total_sol_returned += sol_returned
            if not success:
                self.sell_failures += 1

    # ------------------------------------------------------------------
    # Read helpers (called from dashboard — no side effects)
    # ------------------------------------------------------------------

    @property
    def open_positions(self) -> list[PositionState]:
        return [p for p in self.positions.values() if not p.closed]

    @property
    def closed_positions(self) -> list[PositionState]:
        return sorted(
            [p for p in self.positions.values() if p.closed],
            key=lambda p: p.bought_at, reverse=True,
        )

    @property
    def pnl_sol(self) -> float:
        return self.total_sol_returned - self.total_sol_spent

    @property
    def uptime_str(self) -> str:
        s = int(time.time() - self.started_at)
        h, m, s = s // 3600, (s % 3600) // 60, s % 60
        return f"{h:02d}:{m:02d}:{s:02d}"
