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
from geyser_feed import GeyserFeed
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
MIN_TRADES = 3    # minimum number of buy transactions required

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
                 "vtoken_raw", "accounts", "creator_wallet",
                 "last_dashboard_update", "vsol_timestamp", "trade_count")

    def __init__(self, mint: str, symbol: str, name: str, created_at: float,
                 creator_wallet: str = ""):
        self.mint         = mint
        self.symbol       = symbol
        self.name         = name
        self.created_at   = created_at
        self.creator_wallet = creator_wallet
        self.vsol         = VSOL_INIT
        self.peak_vsol      = VSOL_INIT
        self.had_activity   = False
        self.vtoken_raw:  int | None           = None
        self.accounts:    TokenAccounts | None = None
        self.last_dashboard_update: float      = 0.0
        self.vsol_timestamp: float             = 0.0
        self.trade_count: int                  = 0


class PumpFunMonitor:
    def __init__(self, on_signal: TokenCallback, solana_client, state: BotState):
        self._on_signal      = on_signal
        self._state          = state
        self._solana_client  = solana_client
        self._watching: dict[str, TokenWatch] = {}
        # Mints with open positions — still subscribed for price tracking
        self._position_callbacks: dict[str, Callable[[float, int], None]] = {}
        self._ws             = None
        self._pending_unsubs: set[str]        = set()
        self._geyser = GeyserFeed(
            on_account=self._on_geyser_update,
            on_transaction=self._on_geyser_transaction,
            on_slot=self._on_geyser_slot,
        )
        self._bc_to_mint = {}  # bc_addr hex -> mint
        self._current_slot = 0
        self._last_pp_msg    = time.time()  # watchdog: last PumpPortal message time

    def register_position(self, mint: str, callback: Callable[[float, int], None]):
        """Called by PositionManager after a buy — keeps Helius feed alive for price tracking."""
        self._position_callbacks[mint] = callback

    def unregister_position(self, mint: str):
        """Called by PositionManager when position closes — unsubscribe Helius."""
        self._position_callbacks.pop(mint, None)
        asyncio.create_task(self._geyser.unsubscribe(mint))

    # ------------------------------------------------------------------
    # VSol processing — called by both PumpPortal trades and Helius feed
    # ------------------------------------------------------------------

    def _on_geyser_update(self, bc_pubkey_bytes: bytes, vsol: float, vtoken_raw: int):
        """Called by GeyserFeed with parsed reserves + bonding curve pubkey bytes."""
        bc_hex = bc_pubkey_bytes.hex() if isinstance(bc_pubkey_bytes, bytes) else ""
        mint = self._bc_to_mint.get(bc_hex)
        if not mint:
            return
        # Feed open position price tracker if one exists
        cb = self._position_callbacks.get(mint)
        if cb:
            cb(vsol, vtoken_raw)
        watch = self._watching.get(mint)
        if watch:
            watch.vtoken_raw = vtoken_raw
            # If sell was detected via gRPC tx feed, process immediately
            if getattr(watch, '_geyser_sell_detected', False):
                watch._geyser_sell_detected = False
                log.debug("Geyser fast signal for %s vsol=%.4f", mint[:8], vsol)
            self._process_vsol(watch, vsol)

    def _on_geyser_transaction(self, tx_update, slot: int):
        """Called when a pump.fun sell transaction is detected.
        Match bonding curve accounts to tracked mints and fire signal."""
        try:
            txn = tx_update.transaction.transaction
            msg = txn.message
            for acc_bytes in msg.account_keys:
                acc_hex = acc_bytes.hex() if isinstance(acc_bytes, bytes) else ""
                if acc_hex in self._bc_to_mint:
                    mint = self._bc_to_mint[acc_hex]
                    watch = self._watching.get(mint)
                    if watch and watch.vsol > 0:
                        log.debug("Geyser sell detected on %s at slot=%d — fast signal", mint[:8], slot)
                        # Fire signal immediately — the account update will confirm vSol
                        asyncio.create_task(self._fire_if_dump(watch))
        except Exception:
            pass

    async def _fire_if_dump(self, watch):
        """Quick signal check from gRPC tx feed — fire if vSol is near zero."""
        try:
            if getattr(watch, '_fired', False):
                return
            age = time.time() - watch.t0
            if age > config.MAX_TOKEN_AGE_SECONDS:
                return
            vsol = watch.vsol
            if vsol - VSOL_INIT <= VSOL_MIN_DUMP and watch.peak_vsol - VSOL_INIT >= VSOL_MIN_ACTIVITY:
                peak_pct = (watch.peak_vsol - VSOL_INIT) / VSOL_INIT * 100
                log.info("SIGNAL (fast) %s  bonding≈0%%  peak=%.3f%%  age=%.1fs",
                         watch.mint, peak_pct, age)
                watch._fired = True
                await self._fire(watch, age, peak_pct)
        except Exception:
            pass

    def _on_geyser_slot(self, slot: int, status):
        """Track current slot."""
        self._current_slot = slot

    def _on_trade(self, msg: dict):
        """Called for PumpPortal tokenTrade events (backup feed)."""
        mint = msg.get("mint")
        watch = self._watching.get(mint)
        if not watch:
            return
        vsol_raw = msg.get("vSolInBondingCurve")
        if vsol_raw is None:
            return
        # PumpPortal sends vtoken as JS float — truncates large numbers.
        # If vtoken < 10^12 it's definitely truncated (real is ~10^15).
        # Only use it if Helius hasn't provided a value yet.
        vtoken_raw = msg.get("vTokensInBondingCurve")
        if vtoken_raw is not None and watch.vtoken_raw is None:
            v = int(vtoken_raw)
            if v > 1_000_000_000_000:  # >10^12 = likely correct
                watch.vtoken_raw = v
            # else: skip truncated value, wait for Helius
        self._process_vsol(watch, float(vsol_raw))

    async def _prefetch_accounts(self, mint: str):
        """
        Call PumpPortal trade-local for a dummy buy to obtain the static account
        list for this token, then derive creator_vault on-chain via Alchemy.
        """
        watch = self._watching.get(mint)
        if not watch:
            return
        try:
            accounts = await self._solana_client.prefetch_token_accounts(mint)
            if not (watch and accounts):
                return

            # Derive vault on-chain via Alchemy (replaces PumpPortal vault)
            bc_addr = accounts.bonding_curve
            derived_vault = await self._solana_client._derive_creator_vault(mint, bc_addr)
            if derived_vault:
                from solana_client import TokenAccounts
                accounts = TokenAccounts(
                    assoc_user          = accounts.assoc_user,
                    bonding_curve       = accounts.bonding_curve,
                    assoc_bonding_curve = accounts.assoc_bonding_curve,
                    creator_vault       = derived_vault,
                    pump_const1         = accounts.pump_const1,
                    unk16               = accounts.unk16,
                )

            watch.accounts = accounts
            log.debug("%s  accounts prefetched (vault=%s)", mint[:8], accounts.creator_vault[:12])
        except WrongProgramError:
            # Unsupported program — stop watching immediately
            if mint in self._watching:
                self._state.remove_tracked(mint)
                del self._watching[mint]
            await self._geyser.unsubscribe(mint)
            log.debug("%s  wrong program — dropped", mint[:8])
        except Exception as exc:
            log.warning("prefetch_accounts failed for %s: %s", mint[:8], exc)

    def _process_vsol(self, watch: TokenWatch, vsol: float):
        """Core signal logic — idempotent, safe to call from multiple feeds."""
        mint = watch.mint

        # Already fired / removed
        if mint not in self._watching:
            return

        # Count trades when vsol increases
        if vsol > watch.vsol:
            watch.trade_count += 1

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

        # Fallback vtoken: if Helius is dead and PumpPortal truncated the value,
        # use the known pump.fun initial vtoken reserves
        vtoken = watch.vtoken_raw
        if vtoken is None or vtoken < 1_000_000_000_000:
            vtoken = 1_073_000_000_000_000  # pump.fun initial vtoken (10^15)
            log.warning("vtoken_raw truncated or missing (%s) — using fallback for %s",
                        watch.vtoken_raw, watch.mint[:8])

        await self._on_signal({
            "mint":             watch.mint,
            "symbol":           watch.symbol,
            "name":             watch.name,
            "bonding_pct":      0.0,
            "peak_bonding_pct": peak_pct,
            "age_seconds":      age,
            "vsol_lamports":    int(watch.vsol * 1e9),
            "vtoken_raw":       vtoken,
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
                await self._geyser.unsubscribe(mint)
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

    async def _pp_watchdog(self):
        """Force PumpPortal reconnection if no messages for 60s."""
        while True:
            await asyncio.sleep(15)
            if self._ws is None:
                continue
            elapsed = time.time() - self._last_pp_msg
            if elapsed > 60:
                log.warning("PumpPortal silent for %.0fs — forcing reconnect", elapsed)
                try:
                    await self._ws.close()
                except Exception:
                    pass
                self._ws = None

    async def run(self):
        asyncio.create_task(self._reaper())
        asyncio.create_task(self._geyser.run())
        asyncio.create_task(self._pp_watchdog())
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
                        self._last_pp_msg = time.time()
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
                log.warning("PumpPortal disconnected (%s). Reconnecting in 2s…", exc)
                self._ws = None
                await asyncio.sleep(2)
            except Exception as exc:
                log.error("PumpPortal error: %s", exc, exc_info=True)
                self._ws = None
                await asyncio.sleep(5)

    async def _on_new_token(self, msg: dict):
        mint = msg.get("mint")
        if not mint or mint in self._watching:
            return

        symbol     = msg.get("symbol", "???")
        name       = msg.get("name", "")
        created_at = float(msg.get("timestamp", time.time()))
        age        = time.time() - created_at

        # Extract creator wallet from PumpPortal create event
        creator_wallet = msg.get("traderPublicKey") or msg.get("creator") or ""
        if not creator_wallet:
            log.warning("create event missing creator for %s — keys: %s", mint[:8], list(msg.keys()))

        if age > config.MAX_TOKEN_AGE_SECONDS:
            log.debug("Stale event %s (age=%.0fs) — skipping", mint[:8], age)
            return

        log.info("Tracking  %s  sym=%s  name=%s  creator=%s", mint, symbol, name, creator_wallet[:12] if creator_wallet else "???")

        watch = TokenWatch(mint, symbol, name, created_at, creator_wallet=creator_wallet)
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

        # Map bonding curve pubkey hex to mint for gRPC updates
        from solders.pubkey import Pubkey
        try:
            bc_hex = bytes(Pubkey.from_string(bc_addr)).hex()
            self._bc_to_mint[bc_hex] = mint
            log.debug("%s  bc_to_mint mapped: %s", mint[:8], bc_hex[:16])
        except Exception as exc:
            log.warning("bc_to_mint failed for %s: %s", mint[:8], exc)

        async def _helius_sub():
            try:
                await self._geyser.subscribe(mint, bc_addr)
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
