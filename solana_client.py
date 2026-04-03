"""
Direct on-chain Solana client for pump.fun trading.

Transaction flow:
  1. POST pumpportal.fun/api/trade-local  →  unsigned transaction bytes
  2. Sign locally with our keypair
  3. Send simultaneously to:
       a. Helius Gatekeeper  (broadcast to all validators)
       b. Jito block engine  (MEV-protected, priority inclusion)

PumpPortal only provides the correctly-structured transaction — it never
executes anything on our behalf. Signing and broadcasting are fully local.
"""

import asyncio
import base64
import random
import time
from typing import Optional

import aiohttp
import base58
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import transfer, TransferParams
from solders.transaction import Transaction, VersionedTransaction

import config
from logger import get_logger

log = get_logger("solana")

PUMPPORTAL_TRADE_URL = "https://pumpportal.fun/api/trade-local"
JITO_BUNDLE_URLS = [
    "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://slc.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://tokyo.mainnet.block-engine.jito.wtf/api/v1/bundles",
]
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvB8wr7FZFepBJhTVzMRBUDeGRe2LzrBFj",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zzaogfBvqQ",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]


class SolanaClient:
    def __init__(self):
        self._keypair = Keypair.from_base58_string(config.WALLET_PRIVATE_KEY)
        self._pubkey  = self._keypair.pubkey()
        connector = aiohttp.TCPConnector(
            limit=20, ttl_dns_cache=300, enable_cleanup_closed=True,
        )
        self._session = aiohttp.ClientSession(connector=connector)
        log.info("Wallet: %s", self._pubkey)

    async def _get_session(self) -> aiohttp.ClientSession:
        return self._session

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
                # HEAD-equivalent: tiny POST that will 400 but opens the TCP connection
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
        session = await self._get_session()
        async with session.post(
            config.HELIUS_RPC_HTTP,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            data = await resp.json(content_type=None)
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data.get("result", data)

    async def _get_latest_blockhash(self) -> str:
        result = await self._rpc({
            "method": "getLatestBlockhash",
            "params": [{"commitment": "processed"}],
        })
        return result["value"]["blockhash"]

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

    async def _send_raw(self, tx_b64: str) -> str:
        """Broadcast to Helius, returns signature."""
        result = await self._rpc({
            "method": "sendTransaction",
            "params": [tx_b64, {
                "encoding":            "base64",
                "skipPreflight":       True,
                "maxRetries":          0,
                "preflightCommitment": "processed",
            }],
        })
        return result

    def _build_tip_tx(self, blockhash: str) -> bytes:
        """Build a legacy SOL transfer to a random Jito tip account."""
        tip_account = Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS))
        bh = Hash.from_string(blockhash)
        ix = transfer(TransferParams(
            from_pubkey=self._pubkey,
            to_pubkey=tip_account,
            lamports=config.JITO_TIP_LAMPORTS,
        ))
        msg = Message.new_with_blockhash([ix], self._pubkey, bh)
        tx  = Transaction([self._keypair], msg, bh)
        return bytes(tx)

    async def _send_jito_bundle_one(self, session: aiohttp.ClientSession, url: str, bundle: list):
        """Send a bundle to a single Jito region."""
        region = url.split("/")[2].split(".")[0]
        try:
            async with session.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "sendBundle", "params": [bundle]},
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                data = await resp.json(content_type=None)
                log.debug("Jito %s: %s", region, data)
        except Exception as exc:
            log.debug("Jito %s (non-fatal): %s", region, exc)

    async def _send_jito(self, tx_bytes: bytes):
        """Build tip tx using the same blockhash as main tx, blast all regions in parallel.

        Bundle order: tip tx FIRST, main tx second — required by Jito.
        Blockhash extracted from the already-signed main tx so both transactions match.
        """
        try:
            # Extract the blockhash that PumpPortal baked into the main tx
            main_tx   = VersionedTransaction.from_bytes(tx_bytes)
            blockhash = str(main_tx.message.recent_blockhash())

            tip_bytes = self._build_tip_tx(blockhash)
            bundle = [
                base58.b58encode(tip_bytes).decode(),  # tip FIRST
                base58.b58encode(tx_bytes).decode(),   # main tx second
            ]
            session = await self._get_session()
            await asyncio.gather(*[
                self._send_jito_bundle_one(session, url, bundle)
                for url in JITO_BUNDLE_URLS
            ])
        except Exception as exc:
            log.debug("Jito bundle (non-fatal): %s", exc)

    # ── PumpPortal unsigned transaction builder ──────────────────────────────

    async def _get_pump_tx(self, payload: dict) -> bytes:
        """
        POST to PumpPortal local trade API.
        Returns raw unsigned transaction bytes.
        """
        session = await self._get_session()
        async with session.post(
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
        # v0 signing payload = version prefix (0x80) + raw message bytes
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

        jito_coro = self._send_jito(tx_bytes) if config.USE_JITO else asyncio.sleep(0)
        sig, _ = await asyncio.gather(self._send_raw(tx_b64), jito_coro)
        log.info("BUY  submitted  sig=%s", sig)
        return sig

    async def sell_all(self, mint_str: str) -> str:
        # Get token balance first to know how much to sell
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
            "denominatedInSol": "false",   # amount is in token units (human-readable)
            "slippage":         int(config.SLIPPAGE * 100),
            "priorityFee":      priority_fee_sol,
            "pool":             "pump",
        }

        log.info("SELL %s  %.4f tokens (%d raw)", mint_str, ui_balance, raw_balance)

        unsigned_tx = await self._get_pump_tx(payload)
        tx_bytes    = self._sign_tx(unsigned_tx)
        tx_b64      = base64.b64encode(tx_bytes).decode()

        jito_coro = self._send_jito(tx_bytes) if config.USE_JITO else asyncio.sleep(0)
        sig, _ = await asyncio.gather(self._send_raw(tx_b64), jito_coro)
        log.info("SELL submitted  sig=%s", sig)
        return sig

    async def wait_for_order(self, sig: str, label: str = "") -> dict:
        """Poll getSignatureStatuses until confirmed or timeout."""
        deadline = time.time() + 60
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
            await asyncio.sleep(0.3)
        raise RuntimeError(f"TX {sig[:16]} {label} timed out after 60s")
