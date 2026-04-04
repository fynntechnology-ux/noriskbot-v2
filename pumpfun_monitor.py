"""
Real-time monitor for pump.fun.

Data sources
------------
- PumpPortal WebSocket  : newToken events (token discovery)
- Helius Gatekeeper WS  : accountSubscribe on bonding curve PDAs (trade tracking)

Both feeds call the same _process_vsol() — whichever fires first wins.

Bonding curve progress formula (pump.fun):
  progress = (vSolInBondingCurve - VSOL_INIT) / (VSOL_MAX - VSOL_INIT)

  VSOL_INIT = 30 SOL  (virtual SOL at launch)
  VSOL_MAX  = 793 SOL (virtual SOL at graduation to Raydium)

Signal fires when:
  1. vSol has risen above VSOL_INIT + PEAK_SOL (someone bought in)
  2. vSol drops back to <= VSOL_INIT + ZERO_SOL (everyone sold out)
  3. Token is still younger than MAX_TOKEN_AGE_SECONDS
"""

import asyncio
import json
import time
from typing import Callable, Awaitable

import websockets
from solders.pubkey import Pubkey

import config
from helius_feed import HeliusAccountFeed
from solana_client import TokenAccounts, WrongProgramError
from state import BotState
from logger import get_logger

log = get_logger("monitor")

TokenCallback = Callable[[dict], Awaitable[None]]

# pump.fun bonding curve constants (in SOL)
VSOL_INIT = 30.0
VSOL_MAX  = 793.0

# Thresholds
PEAK_SOL = 3.0    # vSol must rise at least 3 SOL above init before we care
ZERO_SOL = 0.01   # trigger when vSol is within 0.01 SOL of init

# pump.fun program — used to derive bonding curve PDA
_PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")


def _derive_bonding_curve(mint: str) -> str:
    """Derive the pump.fun bonding curve PDA for a given mint."""
    mint_pk = Pubkey.from_string(mint)
    bc, _   = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pk)], _PUMP_PROGRAM)
    return str(bc)


def _progress(vsol: float) -> float:
    """Bonding curve progress as a percentage (0–100)."""
    return max(0.0, min((vsol - VSOL_INIT) / (VSOL_MAX - VSOL_INIT) * 100, 100.0))



class TokenWatch:
    """Per-token state tracked from account updates."""
    __slots__ = ("mint", "symbol", "name", "created_at",
                 "vsol", "peak_vsol", "had_activity",
                 "vtoken_raw", "accounts",
                 "last_dashboard_update")

    def __init__(self, mint: str, symbol: str, name: str, created_at: float):
        self.mint         = mint
        self.symbol       = symbol
        self.name         = name
        self.created_at   = created_at
        self.vsol         = VSOL_INIT
        self.peak_vsol      = VSOL_INIT
        self.had_activity   = False
        self.vtoken_raw:  int | None           = None
        self.accounts:    TokenAccounts | None = None
        self.last_dashboard_update: float      = 0.0


class PumpFunMonitor:
    def __init__(self, on_signal: TokenCallback, solana_client, state: BotState):
        self._on_signal      = on_signal
        self._state          = state
        self._solana_client  = solana_client
        self._watching: dict[str, TokenWatch] = {}
        self._ws             = None
        self._pending_unsubs: set[str]        = set()
        self._helius         = HeliusAccountFeed(on_update=self._on_helius_update)

    # ------------------------------------------------------------------
    # VSol processing — called by both PumpPortal trades and Helius feed
    # ------------------------------------------------------------------

    def _on_helius_update(self, mint: str, vsol: float, vtoken_raw: int):
        """Called by HeliusAccountFeed with parsed reserves."""
        watch = self._watching.get(mint)
        if watch:
            watch.vtoken_raw = vtoken_raw
            self._process_vsol(watch, vsol)

    def _on_trade(self, msg: dict):
        """Called for PumpPortal tokenTrade events (backup feed)."""
        mint = msg.get("mint")
        watch = self._watching.get(mint)
        if not watch:
            return
        vsol_raw = msg.get("vSolInBondingCurve")
        if vsol_raw is None:
            return
        # Also capture vtoken so local build doesn't fall back when only
        # the PumpPortal feed fires before the Helius subscription arrives
        vtoken_raw = msg.get("vTokensInBondingCurve")
        if vtoken_raw is not None and watch.vtoken_raw is None:
            watch.vtoken_raw = int(vtoken_raw)
        self._process_vsol(watch, float(vsol_raw))

    async def _prefetch_accounts(self, mint: str):
        """
        Call PumpPortal trade-local for a dummy buy to obtain the static account
        list for this token, then cache it on the TokenWatch.

        Accounts extracted from the static list (indices into the non-ALT portion):
          [1] assoc_user          — user's token ATA
          [2] bonding_curve       — bonding curve PDA
          [3] assoc_bonding_curve — bonding curve's ATA
          [4] creator_vault       — creator vault PDA
          [6] unk16               — FLASHX8 per-token account
        """
        try:
            accounts = await self._solana_client.prefetch_token_accounts(mint)
            watch = self._watching.get(mint)
            if watch and accounts:
                watch.accounts = accounts
                log.debug("%s  accounts prefetched", mint[:8])
        except WrongProgramError:
            # Unsupported program — stop watching immediately
            if mint in self._watching:
                self._state.remove_tracked(mint)
                del self._watching[mint]
            await self._helius.unsubscribe(mint)
            log.debug("%s  wrong program — dropped", mint[:8])
        except Exception as exc:
            log.warning("prefetch_accounts failed for %s: %s", mint[:8], exc)

    def _process_vsol(self, watch: TokenWatch, vsol: float):
        """Core signal logic — idempotent, safe to call from multiple feeds."""
        mint = watch.mint

        # Already fired / removed
        if mint not in self._watching:
            return

        watch.vsol = vsol
        if vsol > watch.peak_vsol:
            watch.peak_vsol = vsol

        # Update dashboard
        now = time.time()
        if now - watch.last_dashboard_update >= 0.5:
            self._state.update_token_bonding(mint, _progress(vsol) / 100.0)
            watch.last_dashboard_update = now

        # Mark activity once vSol rises meaningfully
        if not watch.had_activity and (watch.peak_vsol - VSOL_INIT) >= PEAK_SOL:
            watch.had_activity = True
            log.debug("%s  activity!  vSol=%.3f  peak=%.3f%%",
                      mint[:8], vsol, _progress(watch.peak_vsol))
            self._state.log("launch", mint, watch.symbol,
                            f"activity  peak={_progress(watch.peak_vsol):.3f}%")

        age = time.time() - watch.created_at

        # Signal: had activity AND vSol is back near init AND token still fresh
        if (watch.had_activity
                and (vsol - VSOL_INIT) <= ZERO_SOL
                and age <= config.MAX_TOKEN_AGE_SECONDS):

            peak_pct = _progress(watch.peak_vsol)
            log.info("SIGNAL  %s  bonding≈0%%  peak=%.3f%%  age=%.1fs",
                     mint, peak_pct, age)
            self._state.log("signal", mint, watch.symbol,
                            f"bonding→0  peak={peak_pct:.3f}%  age={age:.1f}s")
            self._state.remove_tracked(mint)

            # Remove before firing so a second feed update doesn't double-fire
            del self._watching[mint]

            asyncio.create_task(self._fire(watch, age, peak_pct))

    async def _fire(self, watch: TokenWatch, age: float, peak_pct: float):
        # Use watch.vsol at fire time — fresher than signal-moment snapshot
        # since Helius may have updated it between signal detection and here
        await self._on_signal({
            "mint":             watch.mint,
            "symbol":           watch.symbol,
            "name":             watch.name,
            "bonding_pct":      0.0,
            "peak_bonding_pct": peak_pct,
            "age_seconds":      age,
            # Per-token data for local tx building (may be None if prefetch lost race)
            "vsol_lamports":    int(watch.vsol * 1e9),
            "vtoken_raw":       watch.vtoken_raw,
            "token_accounts":   watch.accounts,
        })

    # ------------------------------------------------------------------
    # Cleanup: remove tokens that aged out
    # ------------------------------------------------------------------

    async def _reaper(self):
        """Periodically evict tokens that exceeded MAX_TOKEN_AGE_SECONDS."""
        while True:
            await asyncio.sleep(5)
            now  = time.time()
            dead = [m for m, w in self._watching.items()
                    if now - w.created_at > config.MAX_TOKEN_AGE_SECONDS]
            for mint in dead:
                log.debug("%s  aged out — stopping watch", mint[:8])
                self._state.remove_tracked(mint)
                del self._watching[mint]
                await self._helius.unsubscribe(mint)
                self._pending_unsubs.add(mint)
                if self._ws:
                    try:
                        await self._ws.send(json.dumps(
                            {"method": "unsubscribeTokenTrade", "keys": [mint]}
                        ))
                        self._pending_unsubs.discard(mint)
                    except Exception:
                        pass
            self._state.prune_name_registry()

    # ------------------------------------------------------------------
    # WebSocket stream (PumpPortal — new token discovery)
    # ------------------------------------------------------------------

    async def run(self):
        asyncio.create_task(self._reaper())
        asyncio.create_task(self._helius.run())
        log.info("Connecting to PumpPortal WebSocket…")

        while True:
            try:
                async with websockets.connect(
                    config.PUMPPORTAL_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2**18,  # 256KB — plenty for token events
                ) as ws:
                    self._ws = ws
                    log.info("PumpPortal connected.")
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    if self._pending_unsubs:
                        await ws.send(json.dumps(
                            {"method": "unsubscribeTokenTrade", "keys": list(self._pending_unsubs)}
                        ))
                        self._pending_unsubs.clear()

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(msg, dict):
                            continue

                        tx_type = msg.get("txType", "")
                        if tx_type == "create":
                            await self._on_new_token(msg)
                        elif "vSolInBondingCurve" in msg:
                            # PumpPortal trade events as backup feed
                            self._on_trade(msg)

            except (websockets.exceptions.ConnectionClosed, OSError,
                    asyncio.TimeoutError) as exc:
                log.warning("PumpPortal disconnected (%s). Reconnecting in 0.3s…", exc)
                self._ws = None
                await asyncio.sleep(0.3)
            except Exception as exc:
                log.error("PumpPortal error: %s", exc, exc_info=True)
                self._ws = None
                await asyncio.sleep(0.3)

    async def _on_new_token(self, msg: dict):
        mint = msg.get("mint")
        if not mint or mint in self._watching:
            return

        symbol     = msg.get("symbol", "???")
        name       = msg.get("name", "")
        created_at = float(msg.get("timestamp", time.time()))
        age        = time.time() - created_at

        if age > config.MAX_TOKEN_AGE_SECONDS:
            log.debug("Stale event %s (age=%.0fs) — skipping", mint[:8], age)
            return

        log.info("Tracking  %s  sym=%s  name=%s", mint, symbol, name)

        watch = TokenWatch(mint, symbol, name, created_at)
        if "vSolInBondingCurve" in msg:
            watch.vsol      = float(msg["vSolInBondingCurve"])
            watch.peak_vsol = watch.vsol
        self._watching[mint] = watch

        self._state.track_token(mint, symbol, name, created_at)
        self._state.log("launch", mint, symbol, f"new token  age={age:.1f}s")

        # Prefetch per-token accounts in background (needed for local tx building)
        asyncio.create_task(self._prefetch_accounts(mint))

        # Subscribe Helius + PumpPortal trade in parallel
        bc_addr = msg.get("bondingCurveKey") or _derive_bonding_curve(mint)

        async def _helius_sub():
            try:
                await self._helius.subscribe(mint, bc_addr)
            except Exception as exc:
                log.warning("Helius subscribe failed for %s: %s", mint[:8], exc)

        async def _pp_trade_sub():
            if self._ws:
                try:
                    await self._ws.send(json.dumps(
                        {"method": "subscribeTokenTrade", "keys": [mint]}
                    ))
                except Exception as exc:
                    log.warning("PumpPortal trade subscribe failed for %s: %s", mint[:8], exc)

        await asyncio.gather(_helius_sub(), _pp_trade_sub())
