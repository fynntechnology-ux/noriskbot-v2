"""
Direct on-chain Solana client for pump.fun trading.

Transaction flow:
  1. POST pumpportal.fun/api/trade-local  →  unsigned transaction bytes
  2. Sign locally with our keypair
  3. Submit via Helius Sender (staked connection, priority inclusion)

PumpPortal only provides the correctly-structured transaction — it never
executes anything on our behalf. Signing and broadcasting are fully local.
"""

import asyncio
import base64
import time

import aiohttp
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

import config
from logger import get_logger

log = get_logger("solana")

PUMPPORTAL_TRADE_URL = "https://pumpportal.fun/api/trade-local"


class SolanaClient:
    def __init__(self):
        self._keypair = Keypair.from_base58_string(config.WALLET_PRIVATE_KEY)
        self._pubkey  = self._keypair.pubkey()
        connector = aiohttp.TCPConnector(
            limit=20, ttl_dns_cache=300, enable_cleanup_closed=True,
        )
        self._session = aiohttp.ClientSession(connector=connector)
        log.info("Wallet: %s", self._pubkey)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def warmup(self):
        """Pre-warm TCP connections to all endpoints before the first trade."""
        async def _warm_helius():
            try:
                await self._rpc({"method": "getHealth"})
                log.info("Helius RPC warmed up")
            except Exception as exc:
                log.warning("Helius warmup failed (non-fatal): %s", exc)

        async def _warm_pumpportal():
            try:
                async with self._session.post(
                    PUMPPORTAL_TRADE_URL,
                    json={},
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    await resp.read()
            except Exception:
                pass  # expected to fail — we just want the connection open
            log.info("PumpPortal connection warmed up")

        await asyncio.gather(_warm_helius(), _warm_pumpportal())

    # ── RPC ──────────────────────────────────────────────────────────────────

    async def _rpc(self, body: dict, timeout: float = 5.0) -> dict:
        body.setdefault("jsonrpc", "2.0")
        body.setdefault("id", 1)
        async with self._session.post(
            config.HELIUS_RPC_HTTP,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            data = await resp.json(content_type=None)
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data.get("result", data)

    async def _get_token_balance(self, mint_str: str) -> tuple[int, float]:
        """
        Get token balance for our wallet.
        Returns (raw_amount, ui_amount) — ui_amount is human-readable (divided by 10^decimals).
        All 4 ATA derivations are checked in parallel — takes one RPC round-trip instead of up to 4.
        """
        TOKEN_PROGRAM    = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
        TOKEN_2022       = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
        ASSOC_TOKEN_PROG = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1brs")
        ASSOC_TOKEN_2022 = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
        mint = Pubkey.from_string(mint_str)

        async def _try(tok_prog, ata_prog):
            ata, _ = Pubkey.find_program_address(
                [bytes(self._pubkey), bytes(tok_prog), bytes(mint)], ata_prog
            )
            try:
                result = await self._rpc({
                    "method": "getTokenAccountBalance",
                    "params": [str(ata), {"commitment": "processed"}],
                }, timeout=3)
                val = result["value"]
                raw = int(val["amount"])
                if raw > 0:
                    ui = float(val.get("uiAmount") or raw / 10 ** val.get("decimals", 6))
                    return raw, ui
            except Exception:
                pass
            return 0, 0.0

        results = await asyncio.gather(
            _try(TOKEN_PROGRAM, ASSOC_TOKEN_PROG),
            _try(TOKEN_PROGRAM, ASSOC_TOKEN_2022),
            _try(TOKEN_2022,    ASSOC_TOKEN_PROG),
            _try(TOKEN_2022,    ASSOC_TOKEN_2022),
        )
        for raw, ui in results:
            if raw > 0:
                return raw, ui
        return 0, 0.0

    async def _send_via_sender(self, tx_b64: str) -> str:
        """Submit via Helius Sender — staked connection, priority inclusion."""
        async with self._session.post(
            config.HELIUS_SENDER_URL,
            json={
                "jsonrpc": "2.0",
                "id":      1,
                "method":  "sendTransaction",
                "params":  [tx_b64, {
                    "encoding":      "base64",
                    "skipPreflight": True,
                    "maxRetries":    0,
                }],
            },
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=2),
        ) as resp:
            data = await resp.json(content_type=None)
        if "error" in data:
            raise RuntimeError(f"Sender error: {data['error']}")
        return data["result"]

    # ── PumpPortal unsigned transaction builder ──────────────────────────────

    async def _get_pump_tx(self, payload: dict) -> bytes:
        """
        POST to PumpPortal local trade API.
        Returns raw unsigned transaction bytes.
        """
        async with self._session.post(
            PUMPPORTAL_TRADE_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=2),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"PumpPortal {resp.status}: {text[:200]}")
            return await resp.read()

    def _sign_tx(self, tx_bytes: bytes) -> bytes:
        """
        Deserialize unsigned versioned transaction (v0) from PumpPortal and sign.
        v0 messages must be signed over:  0x80 || serialized_message_bytes
        """
        tx  = VersionedTransaction.from_bytes(tx_bytes)
        msg = tx.message
        sig = self._keypair.sign_message(bytes([0x80]) + bytes(msg))
        return bytes(VersionedTransaction.populate(msg, [sig]))

    # ── Public API ────────────────────────────────────────────────────────────

    async def buy(self, mint_str: str, sol_amount: float = config.BUY_AMOUNT_SOL) -> str:
        priority_fee_sol = (
            config.COMPUTE_UNIT_PRICE * config.COMPUTE_UNIT_LIMIT / 1_000_000 / 1_000_000_000
        )

        payload = {
            "publicKey":        str(self._pubkey),
            "action":           "buy",
            "mint":             mint_str,
            "amount":           sol_amount,
            "denominatedInSol": "true",
            "slippage":         int(config.SLIPPAGE * 100),
            "priorityFee":      priority_fee_sol,
            "pool":             "pump",
        }

        log.info("BUY  %s  %.4f SOL", mint_str, sol_amount)

        unsigned_tx = await self._get_pump_tx(payload)
        tx_bytes    = self._sign_tx(unsigned_tx)
        tx_b64      = base64.b64encode(tx_bytes).decode()

        sig = await self._send_via_sender(tx_b64)
        log.info("BUY  submitted  sig=%s", sig)
        return sig

    async def sell_all(self, mint_str: str) -> str:
        raw_balance, ui_balance = await self._get_token_balance(mint_str)
        if raw_balance == 0:
            raise RuntimeError(f"No token balance for {mint_str}")

        priority_fee_sol = (
            config.COMPUTE_UNIT_PRICE * config.COMPUTE_UNIT_LIMIT / 1_000_000 / 1_000_000_000
        )

        payload = {
            "publicKey":        str(self._pubkey),
            "action":           "sell",
            "mint":             mint_str,
            "amount":           ui_balance,
            "denominatedInSol": "false",
            "slippage":         int(config.SLIPPAGE * 100),
            "priorityFee":      priority_fee_sol,
            "pool":             "pump",
        }

        log.info("SELL %s  %.4f tokens (%d raw)", mint_str, ui_balance, raw_balance)

        unsigned_tx = await self._get_pump_tx(payload)
        tx_bytes    = self._sign_tx(unsigned_tx)
        tx_b64      = base64.b64encode(tx_bytes).decode()

        sig = await self._send_via_sender(tx_b64)
        log.info("SELL submitted  sig=%s", sig)
        return sig

    async def wait_for_order(self, sig: str, label: str = "") -> dict:
        """Poll getSignatureStatuses until confirmed or timeout."""
        timeout_s = 30 if label == "BUY" else 60
        deadline  = time.time() + timeout_s
        while time.time() < deadline:
            try:
                result   = await self._rpc({
                    "method": "getSignatureStatuses",
                    "params": [[sig], {"searchTransactionHistory": False}],
                }, timeout=3)
                statuses = result.get("value", [None])
                status   = statuses[0] if statuses else None
                if status:
                    if status.get("err"):
                        raise RuntimeError(
                            f"TX {sig[:16]} {label} failed: {status['err']}"
                        )
                    conf = status.get("confirmationStatus", "")
                    if conf in ("processed", "confirmed", "finalized"):
                        log.info("TX %s… %s  (%s)", sig[:16], label, conf)
                        return {"sig": sig, "status": conf, "output_amount": 0}
            except RuntimeError:
                raise
            except Exception as exc:
                log.debug("Status poll error: %s", exc)
            await asyncio.sleep(0.1)
        raise RuntimeError(f"TX {sig[:16]} {label} timed out after {timeout_s}s")
