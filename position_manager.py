"""
Tracks open positions and fires auto-sell after HOLD_TIME_SECONDS.
"""

import asyncio
import time
from dataclasses import dataclass, field

import config
from state import BotState, PositionState
from logger import get_logger

log = get_logger("positions")


class PositionManager:
    def __init__(self, solana_client, state: BotState):
        self._solana = solana_client
        self._state = state

    @property
    def open_count(self) -> int:
        return len(self._state.open_positions)

    def has_position(self, mint: str) -> bool:
        return mint in self._state.positions

    async def open(self, mint: str, symbol: str, name: str,
                   buy_order_id: str, bonding_at_buy: float, peak_bonding: float):
        pos = PositionState(
            mint=mint,
            symbol=symbol,
            name=name,
            buy_order_id=buy_order_id,
            bought_at=time.time(),
            sol_spent=config.BUY_AMOUNT_SOL,
            bonding_at_buy=bonding_at_buy,
            peak_bonding=peak_bonding,
        )
        self._state.open_position(pos)
        self._state.log("buy", mint, symbol,
                        f"bought {config.BUY_AMOUNT_SOL} SOL  order={buy_order_id[:12]}…")
        log.info("Position opened  %s  hold=%ds", mint, config.HOLD_TIME_SECONDS)
        asyncio.create_task(self._auto_sell(pos))

    async def _auto_sell(self, pos: PositionState):
        await asyncio.sleep(config.HOLD_TIME_SECONDS)

        if pos.closed:
            return

        log.info("Hold time elapsed. Selling %s…", pos.mint)

        for attempt in range(1, 4):
            try:
                order_id = await self._solana.sell_all(pos.mint)
                pos.sell_order_id = order_id
                result = await self._solana.wait_for_order(order_id, label="SELL")
                sol_back = float(result.get("output_amount", 0)) / config.LAMPORTS_PER_SOL
                self._state.close_position(pos.mint, sol_back, success=True)
                self._state.log("sell", pos.mint, pos.symbol,
                                f"sold  returned≈{sol_back:.4f} SOL  order={order_id[:12]}…")
                held = time.time() - pos.bought_at
                log.info("SELL complete  %s  held=%.1fs", pos.mint, held)
                return
            except Exception as exc:
                log.error("SELL attempt %d failed for %s: %s", attempt, pos.mint, exc)
                self._state.log("error", pos.mint, pos.symbol,
                                f"sell attempt {attempt} failed: {exc}")
                if attempt < 3:
                    await asyncio.sleep(3)

        self._state.close_position(pos.mint, 0.0, success=False)
        log.error("All sell attempts failed for %s", pos.mint)
