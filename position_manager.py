"""
Tracks open positions and fires auto-sell on take profit or HOLD_TIME_SECONDS timeout.

Exit logic:
  - Take profit: sell when estimated current value >= sol_spent * (1 + TAKE_PROFIT_PCT/100)
  - Hold timer:  sell after HOLD_TIME_SECONDS regardless of price
  - Whichever fires first wins; the other is cancelled.

Current value estimate:
  value_sol = (tokens_held * vsol) / vtoken_raw
  This is the AMM output if we sold right now at current reserves.
"""

import asyncio
import time
from dataclasses import dataclass, field

import config
from state import BotState, PositionState
from logger import get_logger

log = get_logger("positions")


class PositionManager:
    def __init__(self, solana_client, state: BotState, monitor=None):
        self._solana  = solana_client
        self._state   = state
        self._monitor = monitor  # PumpFunMonitor — for live price feed

    @property
    def open_count(self) -> int:
        return len(self._state.open_positions)

    def has_position(self, mint: str) -> bool:
        return mint in self._state.positions

    async def open(self, mint: str, symbol: str, name: str,
                   buy_order_id: str, bonding_at_buy: float, peak_bonding: float,
                   token_accounts=None, tokens_held: int = 0):
        pos = PositionState(
            mint=mint,
            symbol=symbol,
            name=name,
            buy_order_id=buy_order_id,
            bought_at=time.time(),
            sol_spent=config.BUY_AMOUNT_SOL,
            bonding_at_buy=bonding_at_buy,
            peak_bonding=peak_bonding,
            token_accounts=token_accounts,
        )
        self._state.open_position(pos)
        self._state.log("buy", mint, symbol,
                        f"bought {config.BUY_AMOUNT_SOL} SOL  order={buy_order_id[:12]}…")
        log.info("Position opened  %s  hold=%ds  tp=+%.0f%%",
                 mint, config.HOLD_TIME_SECONDS, config.TAKE_PROFIT_PCT)

        # Event fired by price monitor to trigger early exit
        tp_event = asyncio.Event()

        # Register live price callback with monitor
        if self._monitor:
            # We need the actual token balance from chain before we can evaluate P&L
            # Fetch it once now, then track from live updates
            tokens_held_holder = [tokens_held]

            def _on_price_update(vsol: float, vtoken_raw: int):
                if pos.closed or not vtoken_raw:
                    return
                held = tokens_held_holder[0]
                if not held:
                    return
                current_value = (held * vsol) / vtoken_raw
                gain_pct = (current_value / config.BUY_AMOUNT_SOL - 1.0) * 100.0
                if gain_pct >= config.TAKE_PROFIT_PCT:
                    log.info("TAKE PROFIT  %s  gain=+%.1f%%  value=%.4f SOL",
                             mint, gain_pct, current_value)
                    tp_event.set()

            self._monitor.register_position(mint, _on_price_update)
        else:
            tokens_held_holder = [tokens_held]

        asyncio.create_task(self._auto_sell(pos, tp_event, tokens_held_holder))

    async def _auto_sell(self, pos: PositionState, tp_event: asyncio.Event,
                         tokens_held_holder: list):
        # Fetch actual token balance from chain (needed for accurate P&L math)
        if not tokens_held_holder[0]:
            for _ in range(10):
                bal = await self._solana._fetch_ata_balance(pos.mint)
                if bal:
                    tokens_held_holder[0] = bal
                    break
                await asyncio.sleep(1)

        # Race: take profit vs hold timer
        hold_task = asyncio.create_task(asyncio.sleep(config.HOLD_TIME_SECONDS))
        tp_task   = asyncio.create_task(tp_event.wait())

        done, pending = await asyncio.wait(
            [hold_task, tp_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

        if pos.closed:
            if self._monitor:
                self._monitor.unregister_position(pos.mint)
            return

        reason = "take_profit" if tp_task in done else "hold_timer"
        log.info("Selling %s  reason=%s", pos.mint, reason)

        if self._monitor:
            self._monitor.unregister_position(pos.mint)

        for attempt in range(1, 4):
            try:
                order_id, tx_b64 = await self._solana.sell_all(pos.mint, pos.token_accounts)
                pos.sell_order_id = order_id
                result = await self._solana.wait_for_order(order_id, label="SELL", tx_b64=tx_b64)
                sol_back = float(result.get("output_amount", 0)) / config.LAMPORTS_PER_SOL
                self._state.close_position(pos.mint, sol_back, success=True)
                self._state.log("sell", pos.mint, pos.symbol,
                                f"sold [{reason}]  returned≈{sol_back:.4f} SOL  order={order_id[:12]}…")
                held = time.time() - pos.bought_at
                log.info("SELL complete  %s  held=%.1fs  reason=%s", pos.mint, held, reason)
                await self._solana.refresh_balance()
                return
            except Exception as exc:
                log.error("SELL attempt %d failed for %s: %s", attempt, pos.mint, exc)
                self._state.log("error", pos.mint, pos.symbol,
                                f"sell attempt {attempt} failed: {exc}")
                if attempt < 3:
                    delay = 10 if "-32429" in str(exc) else 3
                    await asyncio.sleep(delay)

        self._state.close_position(pos.mint, 0.0, success=False)
        log.error("All sell attempts failed for %s", pos.mint)
