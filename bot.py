"""
Main bot — wires monitor → buy logic → position manager.
"""

import asyncio

import config
from solana_client import SolanaClient
from pumpfun_monitor import PumpFunMonitor
from position_manager import PositionManager
from state import BotState
from logger import get_logger

log = get_logger("bot")


class PumpSnipeBot:
    def __init__(self, state: BotState):
        self._state = state
        self._solana  = SolanaClient()
        self._positions = PositionManager(self._solana, state)
        self._monitor = PumpFunMonitor(
            on_signal=self._on_signal,
            solana_client=self._solana,
            state=state,
        )
        self._buy_lock = asyncio.Lock()
        self._claimed: set[str] = set()   # mints reserved but not yet in positions

    async def _on_signal(self, signal: dict):
        mint   = signal["mint"]
        symbol = signal.get("symbol", "???")
        name   = signal.get("name", "")

        if self._positions.has_position(mint):
            return

        # Skip if another coin with the same symbol+name launched in this window
        if self._state.is_duplicate(mint, symbol, name):
            self._state.log("skip", mint, symbol,
                            f"duplicate name/symbol detected — skipping")
            log.warning("Duplicate symbol+name for %s (%s / %s) — skipping buy.",
                        mint, symbol, name)
            return

        # Reserve slot under lock — release before the actual buy RPC call
        async with self._buy_lock:
            active = self._positions.open_count + len(self._claimed)
            if active >= config.MAX_CONCURRENT_POSITIONS:
                self._state.log("skip", mint, symbol,
                                f"max positions ({config.MAX_CONCURRENT_POSITIONS}) reached")
                log.warning("Max positions reached. Skipping %s.", mint)
                return
            if self._positions.has_position(mint) or mint in self._claimed:
                return
            buy_lamports = int(config.BUY_AMOUNT_SOL * config.LAMPORTS_PER_SOL)
            required     = buy_lamports + config.MIN_BUY_BUFFER_LAMPORTS
            if self._solana._wallet_balance_lamports < required:
                bal_sol = self._solana._wallet_balance_lamports / config.LAMPORTS_PER_SOL
                log.warning("Insufficient balance %.4f SOL (need %.4f) — skipping %s",
                            bal_sol, required / config.LAMPORTS_PER_SOL, mint)
                self._state.log("skip", mint, symbol,
                                f"insufficient balance {bal_sol:.4f} SOL")
                return
            self._claimed.add(mint)

        # Buy runs outside the lock — concurrent buys proceed in parallel
        log.info("Buying  %s  (age=%.1fs)", mint, signal["age_seconds"])
        try:
            order_id = await self._solana.buy(
                mint_str        = mint,
                sol_amount      = config.BUY_AMOUNT_SOL,
                token_accounts  = signal.get("token_accounts"),
                vsol_lamports   = signal.get("vsol_lamports"),
                vtoken_raw      = signal.get("vtoken_raw"),
                creator_pubkey  = signal.get("creator_pubkey"),
            )
            await self._positions.open(
                mint=mint,
                symbol=symbol,
                name=name,
                buy_order_id=order_id,
                bonding_at_buy=signal["bonding_pct"],
                peak_bonding=signal["peak_bonding_pct"],
            )
            asyncio.create_task(self._solana.refresh_balance())
            asyncio.create_task(self._confirm_buy(order_id, mint, symbol))

        except Exception as exc:
            self._state.log("error", mint, symbol, f"buy failed: {exc}")
            log.error("BUY failed for %s: %s", mint, exc, exc_info=True)
        finally:
            self._claimed.discard(mint)

    async def _confirm_buy(self, order_id: str, mint: str, symbol: str):
        try:
            result = await self._solana.wait_for_order(order_id, label="BUY")
            actual = result.get("sol_spent", 0)
            if actual > 0 and mint in self._state.positions:
                self._state.positions[mint].sol_spent = actual
        except Exception as exc:
            self._state.log("warn", mint, symbol, f"buy confirm failed: {exc}")
            log.warning("Buy confirmation failed for %s: %s", mint, exc)

    async def run(self):
        log.info("=" * 60)
        log.info("pump.fun sniper bot starting")
        log.info("  wallet        : %s", config.WALLET_ADDRESS)
        log.info("  buy amount    : %.3f SOL", config.BUY_AMOUNT_SOL)
        log.info("  hold time     : %ds", config.HOLD_TIME_SECONDS)
        log.info("  max token age : %ds", config.MAX_TOKEN_AGE_SECONDS)
        log.info("  trigger       : 0.0%% bonding curve (everyone sold out)")
        log.info("  slippage      : %.0f%%", config.SLIPPAGE * 100)
        log.info("  cu price      : %d micro-lamports/CU", config.COMPUTE_UNIT_PRICE)
        log.info("  sender tip    : %d lamports", config.SENDER_TIP_LAMPORTS)
        log.info("  max positions : %d", config.MAX_CONCURRENT_POSITIONS)
        log.info("=" * 60)

        await self._solana.warmup()

        try:
            await self._monitor.run()
        finally:
            await self._solana.close()
