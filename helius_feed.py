"""
Helius Gatekeeper RPC — accountSubscribe feed.

Maintains a single persistent WebSocket and multiplexes accountSubscribe
requests for pump.fun bonding curve accounts.

Callback: on_update(mint: str, vsol: float, vtoken_raw: int)
  vsol       — virtual SOL reserves in SOL (float)
  vtoken_raw — virtual token reserves in raw u64 lamports
"""

import asyncio
import base64
import json
import struct
import time
from typing import Callable

import websockets

import config
from logger import get_logger

log = get_logger("helius")

# pump.fun bonding curve Anchor account layout:
#   [0:8]   discriminator
#   [8:16]  virtualTokenReserves  (u64 LE)
#   [16:24] virtualSolReserves    (u64 LE)  ← vSolInBondingCurve in lamports
_VTOKEN_OFFSET = 8
_VSOL_OFFSET   = 16


def _parse_reserves(data_b64: str) -> tuple[float, int] | None:
    """Returns (vsol_float, vtoken_raw_int) or None on parse failure."""
    try:
        data = base64.b64decode(data_b64)
        if len(data) < 24:
            return None
        vtoken_raw = struct.unpack_from("<Q", data, _VTOKEN_OFFSET)[0]
        vsol       = struct.unpack_from("<Q", data, _VSOL_OFFSET)[0] / 1e9
        return vsol, vtoken_raw
    except Exception:
        return None


class HeliusAccountFeed:
    """
    Subscribes to Solana account updates via Helius Gatekeeper WebSocket.
    Call subscribe(mint, bonding_curve_address) to track a token.
    """

    def __init__(self, on_update: Callable[[str, float, int], None]):
        self._on_update    = on_update
        self._ws           = None
        self._req_id       = 1

        # Persistent: mint → bonding curve address (survives reconnects)
        self._mint_to_bc:   dict[str, str] = {}
        # Ephemeral (reset on reconnect): sub_id ↔ mint
        self._sub_to_mint:  dict[int, str] = {}
        self._mint_to_sub:  dict[str, int] = {}
        # In-flight subscribe requests: req_id → mint
        self._pending_reqs: dict[int, str] = {}
        self._last_msg      = time.time()  # watchdog: last message time

    def _next_id(self) -> int:
        rid = self._req_id
        self._req_id += 1
        return rid

    # ------------------------------------------------------------------

    async def subscribe(self, mint: str, bonding_curve: str):
        """Register a bonding curve account to watch. Idempotent."""
        if mint in self._mint_to_bc:
            return
        self._mint_to_bc[mint] = bonding_curve
        if self._ws is not None:
            try:
                await self._send_subscribe(mint, bonding_curve)
            except Exception:
                pass  # will re-subscribe on next reconnect

    async def unsubscribe(self, mint: str):
        """Stop watching a token."""
        self._mint_to_bc.pop(mint, None)
        sub_id = self._mint_to_sub.pop(mint, None)
        if sub_id is not None:
            self._sub_to_mint.pop(sub_id, None)
            if self._ws is not None:
                try:
                    await self._ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id":      self._next_id(),
                        "method":  "accountUnsubscribe",
                        "params":  [sub_id],
                    }))
                except Exception:
                    pass

    async def _send_subscribe(self, mint: str, bonding_curve: str):
        rid = self._next_id()
        self._pending_reqs[rid] = mint
        await self._ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id":      rid,
            "method":  "accountSubscribe",
            "params":  [
                bonding_curve,
                {"encoding": "base64", "commitment": "processed"},
            ],
        }))
        log.debug("accountSubscribe  bc=%s  mint=%s", bonding_curve[:8], mint[:8])

    # ------------------------------------------------------------------

    async def _watchdog(self):
        """Force reconnection if no messages for 60s."""
        while True:
            await asyncio.sleep(15)
            if self._ws is None:
                continue
            elapsed = time.time() - self._last_msg
            if elapsed > 60:
                log.warning("Helius silent for %.0fs — forcing reconnect", elapsed)
                try:
                    await self._ws.close()
                except Exception:
                    pass
                self._ws = None

    async def run(self):
        asyncio.create_task(self._watchdog())
        log.info("Connecting to Helius Gatekeeper…")
        while True:
            try:
                async with websockets.connect(
                    config.HELIUS_RPC_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2**18,  # 256KB — plenty for account updates
                ) as ws:
                    self._ws = ws
                    # Clear ephemeral state from previous connection
                    self._sub_to_mint.clear()
                    self._mint_to_sub.clear()
                    self._pending_reqs.clear()

                    log.info("Helius connected. Re-subscribing %d account(s)…",
                             len(self._mint_to_bc))
                    for mint, bc in list(self._mint_to_bc.items()):
                        await self._send_subscribe(mint, bc)

                    async for raw in ws:
                        self._last_msg = time.time()
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if isinstance(msg, dict):
                            self._handle(msg)

            except (websockets.exceptions.ConnectionClosed, OSError,
                    asyncio.TimeoutError) as exc:
                log.warning("Helius disconnected (%s). Reconnecting in 2s…", exc)
                self._ws = None
                await asyncio.sleep(2)
            except Exception as exc:
                log.error("Helius WS error: %s", exc, exc_info=True)
                self._ws = None
                await asyncio.sleep(5)

    def _handle(self, msg: dict):
        # Subscription confirmation: {"id": rid, "result": <sub_id int>}
        if ("result" in msg and isinstance(msg["result"], int)
                and "id" in msg and "method" not in msg):
            rid    = msg["id"]
            sub_id = msg["result"]
            mint   = self._pending_reqs.pop(rid, None)
            if mint:
                self._sub_to_mint[sub_id] = mint
                self._mint_to_sub[mint]   = sub_id
                log.debug("Helius sub_id=%d  mint=%s", sub_id, mint[:8])
            return

        # Account notification
        if msg.get("method") != "accountNotification":
            return
        params = msg.get("params", {})
        sub_id = params.get("subscription")
        mint   = self._sub_to_mint.get(sub_id)
        if not mint:
            return
        data_list = params.get("result", {}).get("value", {}).get("data")
        if not isinstance(data_list, list) or not data_list:
            return
        reserves = _parse_reserves(data_list[0])
        if reserves is not None:
            vsol, vtoken_raw = reserves
            self._on_update(mint, vsol, vtoken_raw)
