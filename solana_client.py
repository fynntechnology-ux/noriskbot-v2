"""
Direct on-chain Solana client for pump.fun trading.

Transaction flow (Phase 3 — dual-path via /iris):
  1. POST pumpportal.fun/api/trade-local once at token discovery → cache per-token accounts
  2. At signal time: build TWO tx variants with same nonce
       Tx A: high CU price + low Astralane tip  → SWQoS / priority-fee validators
       Tx B: low  CU price + high Jito tip      → Jito bundle validators
  3. Sign both locally; submit both via Astralane /iris in parallel
     Both share the same durable nonce — whichever lands first consumes it,
     making the other automatically invalid.

Fallback (if prefetch unavailable): use PumpPortal tx as before.
"""

import asyncio
import base64
import struct
import time
from dataclasses import dataclass

import aiohttp
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message, MessageV0
from solders.pubkey import Pubkey
from solders.transaction import Transaction, VersionedTransaction

import config
from logger import get_logger

log = get_logger("solana")

PUMPPORTAL_TRADE_URL = "https://pumpportal.fun/api/trade-local"

# ── pump.fun constant addresses ───────────────────────────────────────────────

_PUMP_NEW_PROG    = Pubkey.from_string("FAdo9NCw1ssek6Z6yeWzWjhLVsr8uiCwcWNUnKgzTnHe")
_TOKEN_PROG       = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
_TOKEN_2022       = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
_ASSOC_TOKEN_PROG = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1brs")
_ASSOC_TOKEN_2022 = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
_SYSTEM_PROGRAM   = Pubkey.from_string("11111111111111111111111111111111")

# Tip recipients
_ASTRALANE_TIP     = Pubkey.from_string("AStrAJv2RN2hKCHxwUMtqmSxgdcNZbihCwc1mCSnG83W")
_HELIUS_FAST_TIP   = Pubkey.from_string("9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta")

# Nonce account sysvar required by AdvanceNonceAccount instruction
_SYSVAR_RECENT_BLOCKHASHES = Pubkey.from_string("SysvarRecentB1ockHashes11111111111111111111")

# Buy / Sell instruction discriminators (sha256("global:buy/sell")[:8])
_BUY_DISC  = bytes([0x66, 0x06, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea])
_SELL_DISC = bytes([0x33, 0xe6, 0x85, 0xa4, 0x01, 0x7f, 0x83, 0xad])

# pump.fun Address Lookup Table — contains all global program/account constants.
# Fetched once at startup; passed to MessageV0.try_compile so the compiler can
# replace full 32-byte pubkeys with 1-byte ALT references in the compiled tx.
#
# Relevant entries (verified by decoding a live PumpPortal buy tx):
#   [1]  11111111111111111111111111111111               SYSTEM_PROGRAM    buy[7]  (readonly)
#   [2]  6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P  PUMP_OLD_PROG     buy[11] (readonly)
#   [5]  Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1 CONST_10          buy[10] (readonly)
#   [11] 4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf  GLOBAL            buy[0]  (readonly)
#   [22] 62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV  FEE_RECIPIENT     buy[1]  (writable)
#   [24] Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y  CONST_12          buy[12] (readonly)
#   [31] 7FeFBYbewCqXG7LP6gC8Fnqzk7hmQFMtntXVRqnXi4a6  FEE_CONFIG        buy[17] (writable) — added Sep 2025
#   [28] pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ   PFEE_PROG         buy[15] (readonly)
#   [29] 8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt  CONST_14          buy[14] (readonly)
_PUMP_ALT_ADDR  = Pubkey.from_string("84gxtAAWToZ6xep3wrWsx8TEoLB7EBS9VrKkV9CtMdJi")
_PUMP_OLD_PROG  = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")


class WrongProgramError(Exception):
    """Raised when a token's bonding curve is owned by an unsupported program."""


@dataclass
class TokenAccounts:
    """Per-token on-chain accounts needed to build the buy instruction locally."""
    assoc_user:          str  # static[1] → buy slot[ 5]
    bonding_curve:       str  # static[2] → buy slot[ 3]
    assoc_bonding_curve: str  # static[3] → buy slot[ 4]
    creator_vault:       str  # static[4] → buy slot[ 9]
    pump_const1:         str  # static[5] → buy slot[13] — per-token, NOT a global constant
    unk16:               str  # static[6] → buy slot[16]


class SolanaClient:
    def __init__(self):
        self._keypair = Keypair.from_base58_string(config.WALLET_PRIVATE_KEY)
        self._pubkey  = self._keypair.pubkey()
        connector = aiohttp.TCPConnector(
            limit=20, ttl_dns_cache=300, enable_cleanup_closed=True,
        )
        self._session       = aiohttp.ClientSession(connector=connector)
        self._blockhash              = ""
        self._blockhash_ts           = 0.0
        self._nonce                  = ""   # cached nonce value
        self._nonce_lock             = None # asyncio.Lock, created in warmup
        self._nonce_refresh_task     = None # background refill task
        self._last_consumed_nonce    = ""   # track to detect reuse
        self._pump_alt               = None   # AddressLookupTableAccount, set by warmup()
        self._wallet_balance_lamports: int = 0
        log.info("Wallet: %s", self._pubkey)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def warmup(self):
        """Pre-warm TCP connections and prime the blockhash/nonce cache."""
        self._nonce_lock = asyncio.Lock()

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

        async def _prime_blockhash():
            try:
                self._blockhash    = await self._get_blockhash()
                self._blockhash_ts = time.time()
                log.info("Blockhash primed: %s…", self._blockhash[:12])
            except Exception as exc:
                log.warning("Blockhash prime failed: %s", exc)

        async def _prime_nonce():
            if not config.NONCE_ACCOUNT:
                return
            try:
                self._nonce = await self._fetch_nonce_from_chain()
                log.info("Nonce primed: %s…", self._nonce[:12])
            except Exception as exc:
                log.warning("Nonce prime failed: %s", exc)

        await asyncio.gather(_warm_helius(), _warm_pumpportal(), _prime_blockhash(),
                             _prime_nonce(), self._fetch_pump_alt(), self.refresh_balance())
        asyncio.create_task(self._blockhash_refresher())

    async def _fetch_pump_alt(self):
        """Fetch and parse the pump.fun Address Lookup Table once at startup."""
        from solders.address_lookup_table_account import AddressLookupTableAccount
        try:
            result = await self._rpc({
                "method": "getAccountInfo",
                "params": [str(_PUMP_ALT_ADDR), {"encoding": "base64"}],
            }, timeout=5)
            data = base64.b64decode(result["value"]["data"][0])
            # ALT on-chain format: 56-byte header, then N × 32-byte pubkeys
            header_size = 56
            n = (len(data) - header_size) // 32
            addresses = [
                Pubkey.from_bytes(data[header_size + i * 32 : header_size + (i + 1) * 32])
                for i in range(n)
            ]
            self._pump_alt = AddressLookupTableAccount(key=_PUMP_ALT_ADDR, addresses=addresses)
            log.info("Pump ALT loaded: %d entries", n)
        except Exception as exc:
            log.error("Failed to load pump ALT — local buy disabled: %s", exc)

    async def refresh_balance(self) -> int:
        """Fetch current wallet SOL balance and cache it."""
        try:
            result = await self._rpc({
                "method": "getBalance",
                "params": [str(self._pubkey), {"commitment": "processed"}],
            }, timeout=3)
            self._wallet_balance_lamports = result["value"]
            log.debug("Wallet balance: %d lamports (%.4f SOL)",
                      self._wallet_balance_lamports,
                      self._wallet_balance_lamports / 1_000_000_000)
        except Exception as exc:
            log.warning("Balance refresh failed: %s", exc)
        return self._wallet_balance_lamports

    async def _blockhash_refresher(self):
        """Background task: keep blockhash ≤300ms old."""
        while True:
            await asyncio.sleep(0.2)
            try:
                self._blockhash    = await self._get_blockhash()
                self._blockhash_ts = time.time()
            except Exception as exc:
                log.warning("Blockhash refresh failed: %s", exc)

    async def _fresh_blockhash(self) -> str:
        """Return cached blockhash if ≤400ms old, else fetch synchronously."""
        if time.time() - self._blockhash_ts <= 0.6 and self._blockhash:
            return self._blockhash
        log.warning("Blockhash cache stale — fetching now")
        bh = await self._get_blockhash()
        self._blockhash    = bh
        self._blockhash_ts = time.time()
        return bh

    # ── RPC ──────────────────────────────────────────────────────────────────

    async def _rpc(self, body: dict, timeout: float = 5.0) -> dict:
        body.setdefault("jsonrpc", "2.0")
        body.setdefault("id", 1)
        urls = [config.HELIUS_RPC_HTTP, config.FALLBACK_RPC_HTTP] if hasattr(config, "FALLBACK_RPC_HTTP") else [config.HELIUS_RPC_HTTP]
        last_exc = None
        for url in urls:
            try:
                async with self._session.post(
                    url,
                    json=body,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    raise RuntimeError(f"RPC returned non-dict: {type(data)}")
                if "error" in data:
                    raise RuntimeError(f"RPC error: {data['error']}")
                return data.get("result", data)
            except Exception as exc:
                last_exc = exc
                if url != urls[-1]:
                    log.debug("_rpc failed on %s: %s — retrying on fallback", url.split("/")[2][:20], exc)
        raise last_exc

    async def _get_token_balance(self, mint_str: str) -> tuple[int, float]:
        """
        Get token balance for our wallet.
        Returns (raw_amount, ui_amount) — ui_amount is human-readable (divided by 10^decimals).
        Uses getTokenAccountsByOwner to find the balance regardless of how the token
        account was created (ATA or seed-derived).
        """
        try:
            result = await self._rpc({
                "method": "getTokenAccountsByOwner",
                "params": [
                    str(self._pubkey),
                    {"mint": mint_str},
                    {"encoding": "jsonParsed", "commitment": "processed"},
                ],
            }, timeout=5)
            for acct in (result or {}).get("value", []):
                info = (acct.get("account", {})
                            .get("data", {})
                            .get("parsed", {})
                            .get("info", {}))
                tok_amt = info.get("tokenAmount", {})
                raw = int(tok_amt.get("amount", 0))
                if raw > 0:
                    ui = float(tok_amt.get("uiAmount") or
                               raw / 10 ** tok_amt.get("decimals", 6))
                    return raw, ui
        except Exception:
            pass
        return 0, 0.0

    async def _send_via_rpc(self, tx_b64: str) -> str:
        """Submit via regular Helius RPC (no tip requirement, lower priority)."""
        result = await self._rpc({
            "method": "sendTransaction",
            "params": [tx_b64, {
                "encoding":      "base64",
                "skipPreflight": True,
                "maxRetries":    0,
            }],
        }, timeout=5)
        return result

    async def _send_via_sender(self, tx_b64: str, url: str | None = None) -> str:
        """Submit via Astralane sendTransaction (base64 encoded)."""
        async with self._session.post(
            url or config.ASTRALANE_URL,
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
        log.debug("Astralane response (status=%d): %s", resp.status, data)
        if "error" in data:
            raise RuntimeError(f"Astralane error: {data['error']}")
        if "result" not in data:
            raise RuntimeError(f"Astralane unexpected response: {data}")
        return data["result"]

    async def _send_via_astralane(self, tx_bytes: bytes) -> str:
        """Submit raw tx bytes to Astralane with Bearer auth (used for nonce txs)."""
        encoded = base64.b64encode(tx_bytes).decode()
        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {config.ASTRALANE_API_KEY}",
        }
        async with self._session.post(
            config.ASTRALANE_URL,
            json={
                "jsonrpc": "2.0",
                "id":      1,
                "method":  "sendTransaction",
                "params":  [encoded, {"encoding": "base64", "skipPreflight": True}],
            },
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=2),
        ) as resp:
            data = await resp.json(content_type=None)
        log.debug("Astralane response: %s", data)
        if "error" in data:
            raise RuntimeError(f"Astralane error: {data['error']}")
        result = data.get("result")
        if not result:
            raise RuntimeError(f"Astralane unexpected response: {data}")
        return result

    async def _fetch_nonce_from_chain(self) -> str:
        """Read the durable nonce value directly from the nonce account on-chain."""
        result = await self._rpc({
            "method": "getAccountInfo",
            "params": [config.NONCE_ACCOUNT, {"encoding": "base64", "commitment": "processed"}],
        }, timeout=3)
        data_b64 = (result.get("value") or {}).get("data", [None])[0]
        if not data_b64:
            raise RuntimeError("Nonce account not found or empty")
        data = base64.b64decode(data_b64)
        if len(data) < 80:
            raise RuntimeError(f"Nonce account data too short: {len(data)} bytes")
        return str(Hash.from_bytes(data[40:72]))

    async def _derive_creator_vault(self, mint_str: str, bc_addr: str) -> str | None:
        """Derive creator_vault PDA directly from bonding curve data (no PumpPortal round-trip).
        Uses GetBlock endpoint to spread load off Helius."""
        try:
            async with self._session.post(
                "https://go.getblock.io/8c82122595b643aab1fdfc6de55060d6",
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getAccountInfo",
                    "params": [bc_addr, {"encoding": "base64", "commitment": "processed"}],
                },
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=2),
            ) as resp:
                data = await resp.json(content_type=None)
            data_b64 = ((data or {}).get("result") or {}).get("value", {}).get("data", [None])[0]
            if not data_b64:
                return None
            data = base64.b64decode(data_b64)
            if len(data) < 81:
                return None
            creator = Pubkey.from_bytes(data[49:81])
            if str(creator) == "11111111111111111111111111111111":
                return None
            vault, _ = Pubkey.find_program_address(
                [b"creator-vault", bytes(creator)],
                _PUMP_OLD_PROG,
            )
            return str(vault)
        except Exception as exc:
            log.warning("_derive_creator_vault failed for %s: %s", mint_str[:8], exc)
            return None

    async def _acquire_nonce(self) -> str:
        """Atomically acquire a nonce for exactly one transaction.

        Guarantees the same nonce is never handed out twice:
          1. If a background replenish is running, wait up to 2s for it
          2. If cache is still empty, fetch from chain
          3. Return the nonce and clear the cache under the same lock
        """
        async with self._nonce_lock:
            # If replenish task is still running, wait briefly for it
            if self._nonce_refresh_task and not self._nonce_refresh_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(self._nonce_refresh_task), timeout=2.0)
                except asyncio.TimeoutError:
                    log.warning("Nonce replenish taking >2s — fetching fresh instead")
            if not self._nonce:
                self._nonce = await self._fetch_nonce_from_chain()
            nonce = self._nonce
            self._last_consumed_nonce = nonce
            self._nonce = ""
            return nonce

    async def _replenish_nonce(self):
        """Fetch a fresh nonce from chain. Hard-capped at 3s."""
        try:
            fresh = await asyncio.wait_for(self._fetch_nonce_from_chain(), timeout=3.0)
            if fresh == self._last_consumed_nonce:
                log.debug("Nonce stale (same as consumed) — retrying after 0.5s")
                await asyncio.sleep(0.5)
                fresh = await asyncio.wait_for(self._fetch_nonce_from_chain(), timeout=3.0)
            async with self._nonce_lock:
                if not self._nonce:
                    self._nonce = fresh
                    log.debug("Nonce replenished: %s…", self._nonce[:12])
        except asyncio.TimeoutError:
            log.warning("Nonce replenish timed out after 3s")
        except Exception as exc:
            log.warning("Nonce replenish failed: %s", exc)
        finally:
            self._nonce_refresh_task = None

    def _ensure_nonce_replenish(self):
        """Start a single background nonce refill task if none is running."""
        if self._nonce_refresh_task and not self._nonce_refresh_task.done():
            return
        self._nonce_refresh_task = asyncio.create_task(self._replenish_nonce())

    async def create_nonce_account(self) -> str:
        """One-time utility: create and fund a durable nonce account (0.0015 SOL).

        The nonce account is a new randomly-generated keypair; the bot wallet is
        the authority. Prints the new address and returns it. Call via:
            python3 main.py --create-nonce
        """
        from solders.system_program import (
            create_account, CreateAccountParams,
            initialize_nonce_account, InitializeNonceAccountParams,
        )

        nonce_keypair = Keypair()
        nonce_pubkey  = nonce_keypair.pubkey()
        NONCE_RENT    = 1_447_680  # min-rent for 80-byte nonce account

        blockhash = await self._fresh_blockhash()

        ix_create = create_account(CreateAccountParams(
            from_pubkey=self._pubkey,
            to_pubkey=nonce_pubkey,
            lamports=NONCE_RENT,
            space=80,
            owner=_SYSTEM_PROGRAM,
        ))
        ix_init = initialize_nonce_account(InitializeNonceAccountParams(
            nonce_pubkey=nonce_pubkey,
            authority=self._pubkey,
        ))

        msg = Message.new_with_blockhash(
            [ix_create, ix_init],
            self._pubkey,
            Hash.from_string(blockhash),
        )
        sig_wallet = self._keypair.sign_message(bytes(msg))
        sig_nonce  = nonce_keypair.sign_message(bytes(msg))
        tx = bytes(Transaction.populate(msg, [sig_wallet, sig_nonce]))
        tx_b64 = base64.b64encode(tx).decode()

        sig = await self._send_via_rpc(tx_b64)
        print(f"\nNonce account created: {nonce_pubkey}")
        print(f"Add to .env: NONCE_ACCOUNT={nonce_pubkey}")
        print(f"Creation tx: {sig}\n")
        return str(nonce_pubkey)

    # ── PumpPortal unsigned transaction builder ──────────────────────────────

    async def _get_pump_tx(self, payload: dict) -> bytes:
        """
        POST to PumpPortal local trade API.
        Returns raw unsigned transaction bytes.
        Hard-capped at 2.5s total to prevent hangs.
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

    async def _fetch_fresh_creator_vault(self, mint_str: str) -> str | None:
        """Re-fetch creator_vault (static[4]) from PumpPortal at buy time.
        Hard-capped at 3s to prevent blocking the buy pipeline."""
        priority_fee_sol = (
            config.COMPUTE_UNIT_PRICE * config.COMPUTE_UNIT_LIMIT / 1_000_000 / 1_000_000_000
        )
        payload = {
            "publicKey":        str(self._pubkey),
            "action":           "buy",
            "mint":             mint_str,
            "amount":           config.BUY_AMOUNT_SOL,
            "denominatedInSol": "true",
            "slippage":         int(config.SLIPPAGE * 100),
            "priorityFee":      priority_fee_sol,
            "pool":             "pump",
        }
        try:
            tx_bytes = await asyncio.wait_for(self._get_pump_tx(payload), timeout=3.0)
            tx       = VersionedTransaction.from_bytes(tx_bytes)
            static   = tx.message.account_keys
            if len(static) < 5:
                return None
            vault = str(static[4])
            log.debug("Fresh creator_vault for %s: %s", mint_str[:8], vault[:12])
            return vault
        except asyncio.TimeoutError:
            log.warning("_fetch_fresh_creator_vault TIMEOUT for %s after 3s", mint_str[:8])
            return None
        except Exception as exc:
            log.warning("_fetch_fresh_creator_vault failed for %s: %s", mint_str[:8], exc)
            return None

    # ── Local transaction building ─────────────────────────────────────────────

    async def prefetch_token_accounts(self, mint_str: str, **_kw) -> TokenAccounts | None:
        """
        Call PumpPortal trade-local for a dummy buy to obtain the per-token
        static account list, then extract the 5 accounts we need.

        Static account indices (after the ALT is resolved):
          [1] assoc_user
          [2] bonding_curve
          [3] assoc_bonding_curve
          [4] creator_vault
          [6] unk16
        """
        priority_fee_sol = (
            config.COMPUTE_UNIT_PRICE * config.COMPUTE_UNIT_LIMIT / 1_000_000 / 1_000_000_000
        )
        payload = {
            "publicKey":        str(self._pubkey),
            "action":           "buy",
            "mint":             mint_str,
            "amount":           config.BUY_AMOUNT_SOL,
            "denominatedInSol": "true",
            "slippage":         int(config.SLIPPAGE * 100),
            "priorityFee":      priority_fee_sol,
            "pool":             "pump",
        }
        try:
            tx_bytes = await self._get_pump_tx(payload)
        except Exception as exc:
            body = f" (status={exc.status})" if hasattr(exc, "status") else ""
            log.warning("prefetch FAILED for %s%s: %s — will use PumpPortal fallback",
                        mint_str, body, exc)
            return None

        try:
            tx      = VersionedTransaction.from_bytes(tx_bytes)
            static  = tx.message.account_keys  # list of Pubkey
            if len(static) < 7:
                log.warning("prefetch: unexpected account count %d for %s",
                            len(static), mint_str[:8])
                return None
            # Verify BC ownership
            try:
                bc_info = await self._rpc({
                    "method": "getAccountInfo",
                    "params": [str(static[2]), {"encoding": "base64", "commitment": "processed"}]
                })
                bc_val = (bc_info or {}).get("value") or {}
                owner  = bc_val.get("owner", "")
                if owner and owner != str(_PUMP_OLD_PROG):
                    log.debug("prefetch: bc owned by %s (not 6EF8) for %s — skipping",
                              owner[:8], mint_str[:8])
                    raise WrongProgramError(owner)
            except WrongProgramError:
                raise
            except Exception as exc:
                log.warning("prefetch: BC ownership check failed for %s: %s", mint_str[:8], exc)

            # Use PumpPortal's static[4] — authoritative creator_vault
            creator_vault_str = str(static[4])
            log.info("prefetch %s  creator_vault=static[4]=%s", mint_str[:8], creator_vault_str[:12])

            return TokenAccounts(
                assoc_user          = str(static[1]),
                bonding_curve       = str(static[2]),
                assoc_bonding_curve = str(static[3]),
                creator_vault       = creator_vault_str,
                pump_const1         = str(static[5]),
                unk16               = str(static[6]),
            )
        except WrongProgramError:
            raise
        except Exception as exc:
            log.warning("prefetch: failed to parse tx for %s: %s", mint_str[:8], exc)
            return None

    async def create_ata(self, mint_str: str, assoc_user_str: str) -> bool:
        """Send a standalone ATA create-if-needed tx. Returns True on success."""
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
        try:
            mint       = Pubkey.from_string(mint_str)
            assoc_user = Pubkey.from_string(assoc_user_str)
            ix_cu_limit = set_compute_unit_limit(25_000)
            ix_cu_price = set_compute_unit_price(config.COMPUTE_UNIT_PRICE)
            ix_ata = Instruction(
                program_id=_ASSOC_TOKEN_2022,
                accounts=[
                    AccountMeta(pubkey=self._pubkey, is_signer=True,  is_writable=True),
                    AccountMeta(pubkey=assoc_user,   is_signer=False, is_writable=True),
                    AccountMeta(pubkey=self._pubkey, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=mint,         is_signer=False, is_writable=False),
                    AccountMeta(pubkey=_SYSTEM_PROGRAM,  is_signer=False, is_writable=False),
                    AccountMeta(pubkey=_TOKEN_2022,      is_signer=False, is_writable=False),
                ],
                data=b'\x01',
            )
            blockhash = await self._fresh_blockhash()
            msg = Message.new_with_blockhash(
                [ix_cu_limit, ix_cu_price, ix_ata],
                self._pubkey,
                Hash.from_string(blockhash),
            )
            sig_obj  = self._keypair.sign_message(bytes(msg))
            tx_bytes = bytes(Transaction.populate(msg, [sig_obj]))
            tx_b64   = base64.b64encode(tx_bytes).decode()
            await self._send_via_rpc(tx_b64)
            log.debug("ATA pre-created for %s", mint_str[:8])
            return True
        except Exception as exc:
            log.debug("ATA pre-create failed for %s: %s", mint_str[:8], exc)
            return False

    async def _fetch_cashback_flag(self, bc_addr: str) -> bool:
        """Return needs_bc_v2 based on byte[82] of bonding curve data.

        byte[82]==0 → old layout → [14] bc_v2 only             (15 accounts)
        byte[82]==1 → new layout → [14] pump_const1 + [15] bc_v2 (16 accounts)
        On RPC failure default to new layout (16 accounts) — safer since it's growing.
        """
        try:
            result = await self._rpc({
                "method": "getAccountInfo",
                "params": [bc_addr, {"encoding": "base64", "commitment": "processed"}],
            }, timeout=3)
            data_b64 = (result.get("value") or {}).get("data", [None])[0]
            if not data_b64:
                return False  # default new layout
            data = base64.b64decode(data_b64)
            if len(data) <= 82:
                return False
            return data[82] == 0  # True = old layout (needs bc_v2 only)
        except Exception as exc:
            log.debug("_fetch_cashback_flag failed for %s: %s — defaulting to new layout", bc_addr[:8], exc)
            return False  # default new layout on failure

    async def _fetch_bc_reserves(self, bc_addr: str) -> tuple[int, int] | None:
        """Fetch vsol_lamports and vtoken_raw directly from the bonding curve account."""
        import struct as _struct
        try:
            result = await self._rpc({
                "method": "getAccountInfo",
                "params": [bc_addr, {"encoding": "base64", "commitment": "processed"}],
            }, timeout=3)
            data_b64 = (result.get("value") or {}).get("data", [None])[0]
            if not data_b64:
                return None
            import base64 as _b64
            data = _b64.b64decode(data_b64)
            if len(data) < 24:
                return None
            vtoken_raw    = _struct.unpack_from("<Q", data, 8)[0]
            vsol_lamports = _struct.unpack_from("<Q", data, 16)[0]
            log.debug("BC reserves fetched: vsol=%.4f vtoken=%d", vsol_lamports/1e9, vtoken_raw)
            return vsol_lamports, vtoken_raw
        except Exception as exc:
            log.debug("_fetch_bc_reserves failed for %s: %s", bc_addr[:8], exc)
            return None

    async def _get_blockhash(self) -> str:
        result = await self._rpc({
            "method": "getLatestBlockhash",
            "params": [{"commitment": "processed"}],
        }, timeout=3)
        return result["value"]["blockhash"]

    def _build_local_buy_tx(
        self,
        mint_str:      str,
        accounts:      TokenAccounts,
        sol_lamports:  int,
        vtoken_raw:    int,
        vsol_lamports: int,
        blockhash:     str,
        cu_price:      int | None = None,
        tip_lamports:  int | None = None,
        tip_recipient: Pubkey | None = None,
    ) -> bytes:
        """
        Build and sign a pump.fun buy transaction locally using Legacy format.

        Legacy (not V0) — no ALT, no index drift if pump.fun updates the table.
        All global constants are hardcoded by address.

        cu_price and tip_lamports override config defaults when provided,
        allowing _build_local_buy_tx to be called twice with different fee
        profiles for dual-path /iris submission.

        Instructions:
          1. SetComputeUnitLimit
          2. SetComputeUnitPrice
          3. SystemProgram::createAccountWithSeed  (create user token account)
          4. Token-2022::InitializeAccount3        (init token account)
          5. pump.fun buy  (17 accounts)
          6. SystemProgram::transfer  (Astralane tip)
        """
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
        from solders.system_program import transfer, TransferParams, create_account_with_seed, CreateAccountWithSeedParams

        effective_cu_price   = cu_price      if cu_price      is not None else config.COMPUTE_UNIT_PRICE
        effective_tip        = tip_lamports  if tip_lamports  is not None else config.SENDER_TIP_LAMPORTS
        effective_tip_recip  = tip_recipient if tip_recipient is not None else _ASTRALANE_TIP

        mint        = Pubkey.from_string(mint_str)
        token_seed  = mint_str[:8]
        user_token  = Pubkey.create_with_seed(self._pubkey, token_seed, _TOKEN_2022)
        bc          = Pubkey.from_string(accounts.bonding_curve)
        abc         = Pubkey.from_string(accounts.assoc_bonding_curve)
        cvlt        = Pubkey.from_string(accounts.creator_vault)
        pump_const1 = Pubkey.from_string(accounts.pump_const1)
        unk16       = Pubkey.from_string(accounts.unk16)

        _GLOBAL    = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
        _FEE_RECIP = Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV")
        _CONST10   = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
        _CONST12   = Pubkey.from_string("Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y")
        _CONST14   = Pubkey.from_string("8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt")
        _PFEE_PROG = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

        ix_cu_limit = set_compute_unit_limit(config.COMPUTE_UNIT_LIMIT)
        ix_cu_price = set_compute_unit_price(effective_cu_price)

        TOKEN_ACCT_RENT = 2039280
        ix_create = create_account_with_seed(CreateAccountWithSeedParams(
            from_pubkey=self._pubkey,
            to_pubkey=user_token,
            base=self._pubkey,
            seed=token_seed,
            lamports=TOKEN_ACCT_RENT,
            space=165,
            owner=_TOKEN_2022,
        ))

        ix_init = Instruction(
            program_id=_TOKEN_2022,
            accounts=[
                AccountMeta(pubkey=user_token, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint,       is_signer=False, is_writable=False),
            ],
            data=bytes([18]) + bytes(self._pubkey),
        )

        tokens_out   = int(vtoken_raw * sol_lamports // (vsol_lamports + sol_lamports))
        max_sol_cost = sol_lamports * 2

        buy_data = (
            _BUY_DISC
            + struct.pack("<Q", tokens_out)
            + struct.pack("<Q", max_sol_cost)
        )

        ix_buy = Instruction(
            program_id=_PUMP_OLD_PROG,
            accounts=[
                AccountMeta(_GLOBAL,         False, False),  # [0]  global
                AccountMeta(_FEE_RECIP,      False, True),   # [1]  fee recipient (writable)
                AccountMeta(mint,            False, False),  # [2]  mint
                AccountMeta(bc,              False, True),   # [3]  bonding curve (writable)
                AccountMeta(abc,             False, True),   # [4]  assoc bonding curve (writable)
                AccountMeta(user_token,      False, True),   # [5]  user token account (writable)
                AccountMeta(self._pubkey,    True,  True),   # [6]  user (signer, writable)
                AccountMeta(_SYSTEM_PROGRAM, False, False),  # [7]  system program
                AccountMeta(_TOKEN_2022,     False, False),  # [8]  token program (Token-2022)
                AccountMeta(cvlt,            False, True),   # [9]  creator vault (writable)
                AccountMeta(_CONST10,        False, False),  # [10]
                AccountMeta(_PUMP_OLD_PROG,  False, False),  # [11] pump program
                AccountMeta(_CONST12,        False, False),  # [12]
                AccountMeta(pump_const1,     False, True),   # [13] per-token (writable)
                AccountMeta(_CONST14,        False, False),  # [14]
                AccountMeta(_PFEE_PROG,      False, False),  # [15] pfee program
                AccountMeta(unk16,           False, False),  # [16] per-token (readonly)
            ],
            data=bytes(buy_data),
        )

        # Single Astralane tip — amount varies by variant (low for Tx A, high for Tx B)
        ix_tip = transfer(TransferParams(
            from_pubkey=self._pubkey,
            to_pubkey=effective_tip_recip,
            lamports=effective_tip,
        ))

        instructions = []
        if config.NONCE_ACCOUNT:
            nonce_acct = Pubkey.from_string(config.NONCE_ACCOUNT)
            ix_advance = Instruction(
                program_id=_SYSTEM_PROGRAM,
                accounts=[
                    AccountMeta(nonce_acct,   False, True),
                    AccountMeta(_SYSVAR_RECENT_BLOCKHASHES, False, False),
                    AccountMeta(self._pubkey, True,  False),
                ],
                data=bytes([4, 0, 0, 0]),
            )
            instructions.append(ix_advance)

        instructions.extend([ix_cu_limit, ix_cu_price, ix_create, ix_init, ix_buy, ix_tip])

        msg = Message.new_with_blockhash(
            instructions,
            self._pubkey,
            Hash.from_string(blockhash),
        )
        sig = self._keypair.sign_message(bytes(msg))
        return bytes(Transaction.populate(msg, [sig]))

    def _build_local_sell_tx(
        self,
        mint_str:    str,
        accounts:    TokenAccounts,
        raw_amount:  int,
        blockhash:   str,
        needs_bc_v2: bool = True,
    ) -> bytes:
        """
        Build and sign a pump.fun sell transaction locally using Legacy format.

        Feb 2026 cashback upgrade: sell now requires remaining accounts appended:
          - if cashback: [user_volume_accumulator]  (writable)
          - always:      [bonding_curve_v2]          (writable)

        Without these, the program reads wrong account indices → Custom:6024 Overflow.
        """
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

        mint        = Pubkey.from_string(mint_str)
        user_token  = Pubkey.create_with_seed(self._pubkey, mint_str[:8], _TOKEN_2022)
        bc          = Pubkey.from_string(accounts.bonding_curve)
        abc         = Pubkey.from_string(accounts.assoc_bonding_curve)
        cvlt        = Pubkey.from_string(accounts.creator_vault)

        _GLOBAL     = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
        _FEE_RECIP = Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV")
        _CONST10   = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
        _CONST14   = Pubkey.from_string("8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt")
        _PFEE_PROG = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

        _PUMP_CONST1_GLOBAL = Pubkey.from_string("63EDqM8TH3kcQAkT3P5fVExUnSMPizEheZX6GmiiutN5")
        bc_v2, _ = Pubkey.find_program_address(
            [b"bonding-curve-v2", bytes(mint)], _PUMP_OLD_PROG
        )
        if needs_bc_v2:
            remaining = [AccountMeta(bc_v2, False, True)]
        else:
            remaining = [
                AccountMeta(_PUMP_CONST1_GLOBAL, False, True),
                AccountMeta(bc_v2,               False, True),
            ]

        ix_cu_limit = set_compute_unit_limit(100_000)
        ix_cu_price = set_compute_unit_price(config.SELL_COMPUTE_UNIT_PRICE)

        sell_data = (
            _SELL_DISC
            + struct.pack("<Q", raw_amount)
            + struct.pack("<Q", 0)
        )

        ix_sell = Instruction(
            program_id=_PUMP_OLD_PROG,
            accounts=[
                AccountMeta(_GLOBAL,         False, False),  # [0]  global
                AccountMeta(_FEE_RECIP,      False, True),   # [1]  fee recipient (writable)
                AccountMeta(mint,            False, False),  # [2]  mint
                AccountMeta(bc,              False, True),   # [3]  bonding curve (writable)
                AccountMeta(abc,             False, True),   # [4]  assoc bonding curve (writable)
                AccountMeta(user_token,      False, True),   # [5]  user token account (writable)
                AccountMeta(self._pubkey,    True,  True),   # [6]  user (signer, writable)
                AccountMeta(_SYSTEM_PROGRAM, False, False),  # [7]  system program
                AccountMeta(cvlt,            False, True),   # [8]  creator vault (writable)
                AccountMeta(_TOKEN_2022,     False, False),  # [9]  token program
                AccountMeta(_CONST10,        False, False),  # [10] event authority
                AccountMeta(_PUMP_OLD_PROG,  False, False),  # [11] program self-ref
                AccountMeta(_CONST14,        False, False),  # [12]
                AccountMeta(_PFEE_PROG,      False, False),  # [13] pfee program
                *remaining,
            ],
            data=bytes(sell_data),
        )

        msg = Message.new_with_blockhash(
            [ix_cu_limit, ix_cu_price, ix_sell],
            self._pubkey,
            Hash.from_string(blockhash),
        )
        sig = self._keypair.sign_message(bytes(msg))
        return bytes(Transaction.populate(msg, [sig]))

    # ── Public API ────────────────────────────────────────────────────────────

    async def buy(
        self,
        mint_str:       str,
        sol_amount:     float                  = config.BUY_AMOUNT_SOL,
        token_accounts: "TokenAccounts | None" = None,
        vsol_lamports:  int | None             = None,
        vtoken_raw:     int | None             = None,
    ) -> str:
        t0 = time.perf_counter()
        log.info("BUY  %s  %.4f SOL", mint_str, sol_amount)

        sol_lamports = int(sol_amount * config.LAMPORTS_PER_SOL)

        if token_accounts is None:
            raise RuntimeError("missing prefetch data (token_accounts) — skipping")

        # Fetch nonce (atomic — consumed immediately), fresh reserves, creator vault
        t_fetch_start = time.perf_counter()
        if not config.NONCE_ACCOUNT:
            raise RuntimeError("buy requires NONCE_ACCOUNT — set it in .env")
        try:
            blockhash, fresh_reserves, fresh_vault = await asyncio.wait_for(
                asyncio.gather(
                    self._acquire_nonce(),
                    self._fetch_bc_reserves(token_accounts.bonding_curve),
                    self._derive_creator_vault(mint_str, token_accounts.bonding_curve),
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            raise RuntimeError("buy fetch timed out after 5s — skipping")
        t_fetch = time.perf_counter() - t_fetch_start
        log.debug("Fetch latency: %.3fs (nonce+reserves+vault)", t_fetch)

        if fresh_reserves:
            vsol_lamports, vtoken_raw = fresh_reserves
            log.debug("BUY using fresh reserves: vsol=%.4f vtoken=%d", vsol_lamports/1e9, vtoken_raw)
        elif vsol_lamports is None or not vtoken_raw:
            raise RuntimeError("missing reserves and fresh fetch failed — skipping")

        # Apply fresh creator vault to eliminate drift
        if fresh_vault:
            token_accounts = TokenAccounts(
                assoc_user          = token_accounts.assoc_user,
                bonding_curve       = token_accounts.bonding_curve,
                assoc_bonding_curve = token_accounts.assoc_bonding_curve,
                creator_vault       = fresh_vault,
                pump_const1         = token_accounts.pump_const1,
                unk16               = token_accounts.unk16,
            )
        else:
            log.warning("BUY %s using cached creator_vault — drift risk", mint_str[:8])

        build_kwargs = dict(
            mint_str      = mint_str,
            accounts      = token_accounts,
            sol_lamports  = sol_lamports,
            vtoken_raw    = vtoken_raw,
            vsol_lamports = vsol_lamports,
            blockhash     = blockhash,
        )

        # Build Astralane tx (standard tip)
        tx_astralane = self._build_local_buy_tx(
            **build_kwargs,
            cu_price     = config.IDEAL_HIGH_FEE_CU_PRICE,
            tip_lamports = config.SENDER_TIP_LAMPORTS,
        )

        # Build Helius fast tx (higher tip to different recipient)
        tx_helius = self._build_local_buy_tx(
            **build_kwargs,
            cu_price     = config.IDEAL_HIGH_FEE_CU_PRICE,
            tip_lamports = 300000,  # 0.0003 SOL
            tip_recipient = _HELIUS_FAST_TIP,
        )
        tx_helius_b64 = base64.b64encode(tx_helius).decode()

        log.info(
            "BUY  tri-path  AMS(tip=%d) helius_FR(tip=300000) helius_AMS(tip=300000)",
            config.SENDER_TIP_LAMPORTS,
        )

        # Submit to all gateways in parallel
        ams_result, helius_fr_result, helius_ams_result = await asyncio.gather(
            self._send_via_sender(base64.b64encode(tx_astralane).decode(), config.ASTRALANE_AMS_URL),
            self._send_via_sender(tx_helius_b64, "http://fra-sender.helius-rpc.com/fast"),
            self._send_via_sender(tx_helius_b64, "http://ams-sender.helius-rpc.com/fast"),
            return_exceptions=True,
        )

        # Collect all successful signatures with gateway labels
        sigs = {}
        if not isinstance(ams_result, Exception):
            sigs[ams_result] = "AMS"
        if not isinstance(helius_fr_result, Exception):
            sigs[helius_fr_result] = "helius_FR"
        if not isinstance(helius_ams_result, Exception):
            sigs[helius_ams_result] = "helius_AMS"

        if not sigs:
            raise RuntimeError(f"All paths failed: AMS={ams_result} helius_FR={helius_fr_result} helius_AMS={helius_ams_result}")

        all_sigs = list(sigs.keys())
        log.info("BUY  submitted  sigs=%s  gateways=%s  total_time=%.3fs",
                 [s[:16] for s in all_sigs], list(sigs.values()),
                 time.perf_counter() - t0)

        # Poll all signatures — whichever lands first wins
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                result = await self._rpc({
                    "method": "getSignatureStatuses",
                    "params": [all_sigs, {"searchTransactionHistory": True}],
                }, timeout=3)
                statuses = result.get("value", [])
                for idx, status in enumerate(statuses):
                    if not status:
                        continue
                    winning_sig = all_sigs[idx]
                    gateway = sigs[winning_sig]
                    if status.get("err"):
                        log.warning("BUY  sig %s (%s) failed on-chain: %s",
                                    winning_sig[:16], gateway, status["err"])
                        continue
                    conf = status.get("confirmationStatus", "")
                    if conf in ("processed", "confirmed", "finalized"):
                        log.info("BUY  Landed via %s  sig=%s  (%s)", gateway, winning_sig[:16], conf)
                        sig = winning_sig
                        break
                else:
                    await asyncio.sleep(0.25)
                    continue
                break
            except Exception:
                await asyncio.sleep(0.25)
                continue
        else:
            # Timeout — return first sig and let wait_for_order handle it
            sig = all_sigs[0]
            log.warning("BUY  No sig confirmed in 15s — returning %s (%s)", sig[:16], sigs[sig])

        log.info("BUY  nonce_at_submit=%s", blockhash[:16])

        # This nonce was already atomically consumed by _acquire_nonce().
        # Start background refill for the next buy, but never allow reuse.
        self._ensure_nonce_replenish()
        return sig

    async def _fetch_ata_balance(self, mint_str: str) -> int:
        """Fetch raw token balance directly from the seed-derived ATA we created.

        Uses getTokenAccountBalance on the exact account address rather than
        searching by owner — avoids stale/wrong amounts from account search.
        Returns 0 if account not found or not yet settled.
        """
        ata = Pubkey.create_with_seed(self._pubkey, mint_str[:8], _TOKEN_2022)
        try:
            result = await self._rpc({
                "method": "getTokenAccountBalance",
                "params": [str(ata), {"commitment": "processed"}],
            }, timeout=3)
            return int(result["value"]["amount"])
        except Exception:
            return 0

    async def sell_all(self, mint_str: str, token_accounts=None) -> tuple[str, str]:
        # Poll up to 10s for balance to settle — fetch directly from seed-derived ATA
        raw_balance = 0
        for _ in range(10):
            raw_balance = await self._fetch_ata_balance(mint_str)
            if raw_balance > 0:
                break
            log.debug("Waiting for token balance to settle for %s…", mint_str[:8])
            await asyncio.sleep(1)

        if raw_balance == 0:
            raise RuntimeError(f"No token balance for {mint_str}")

        log.info("SELL %s  %d raw tokens", mint_str, raw_balance)

        if token_accounts is not None:
            blockhash, needs_bc_v2, fresh_vault = await asyncio.gather(
                self._fresh_blockhash(),
                self._fetch_cashback_flag(token_accounts.bonding_curve),
                self._fetch_fresh_creator_vault(mint_str),
            )

            # Apply fresh creator vault to eliminate drift
            if fresh_vault:
                token_accounts = TokenAccounts(
                    assoc_user          = token_accounts.assoc_user,
                    bonding_curve       = token_accounts.bonding_curve,
                    assoc_bonding_curve = token_accounts.assoc_bonding_curve,
                    creator_vault       = fresh_vault,
                    pump_const1         = token_accounts.pump_const1,
                    unk16               = token_accounts.unk16,
                )
            else:
                log.warning("SELL %s using cached creator_vault — drift risk", mint_str[:8])

            # Build both layout variants — only one can land (same token balance)
            tx_old = self._build_local_sell_tx(
                mint_str, token_accounts, raw_balance, blockhash, True
            )
            tx_new = self._build_local_sell_tx(
                mint_str, token_accounts, raw_balance, blockhash, False
            )

            # Submit both layouts in parallel — whichever lands wins
            sig_old, sig_new = await asyncio.gather(
                self._send_via_rpc(base64.b64encode(tx_old).decode()),
                self._send_via_rpc(base64.b64encode(tx_new).decode()),
                return_exceptions=True,
            )

            # Return whichever succeeded
            if not isinstance(sig_old, Exception):
                log.info("SELL submitted (old layout)  sig=%s", sig_old[:16])
                return sig_old, base64.b64encode(tx_old).decode(), True
            if not isinstance(sig_new, Exception):
                log.info("SELL submitted (new layout)  sig=%s", sig_new[:16])
                return sig_new, base64.b64encode(tx_new).decode(), False
            raise RuntimeError(f"Both sell layouts failed: old={sig_old} new={sig_new}")

        raise RuntimeError(f"no token_accounts for sell of {mint_str}")

    async def wait_for_order(self, sig: str, label: str = "", tx_b64: str = "",
                              needs_bc_v2: bool | None = None) -> dict:
        """Poll getSignatureStatuses until confirmed or timeout.

        sig may contain multiple signatures separated by "|" (primary|all_csv).
        Polls all signatures — whichever confirms first wins.
        """
        # Parse multi-sig format: "primary|sig1,sig2"
        if "|" in sig:
            primary, all_csv = sig.split("|", 1)
            all_sigs = [s for s in all_csv.split(",") if s]
        else:
            primary = sig
            all_sigs = [sig]

        timeout_s      = 30 if label == "BUY" else 30
        deadline       = time.time() + timeout_s
        last_broadcast = time.time()

        while time.time() < deadline:
            if tx_b64 and (time.time() - last_broadcast) >= 2.0:
                try:
                    await self._send_via_rpc(tx_b64)
                    last_broadcast = time.time()
                    log.debug("Rebroadcast %s %s", label, primary[:16])
                except Exception:
                    pass

            try:
                result   = await self._rpc({
                    "method": "getSignatureStatuses",
                    "params": [all_sigs, {"searchTransactionHistory": True}],
                }, timeout=3)
                statuses = result.get("value", [])
                for idx, status in enumerate(statuses):
                    if not status:
                        continue
                    found_sig = all_sigs[idx]
                    if status.get("err"):
                        raise RuntimeError(
                            f"TX {found_sig[:16]} {label} failed: {status['err']}"
                        )
                    conf = status.get("confirmationStatus", "")
                    if conf in ("processed", "confirmed", "finalized"):
                        log.info("TX %s… %s  (%s)", found_sig[:16], label, conf)
                        output_amount = 0
                        sol_spent     = 0
                        if label == "SELL":
                            if needs_bc_v2 is not None:
                                log.info("SELL confirmed | sig=%s layout=%s accounts=%d",
                                         found_sig[:16], "old" if needs_bc_v2 else "new",
                                         15 if needs_bc_v2 else 16)
                            try:
                                output_amount = await self._get_sol_received(found_sig)
                            except Exception as e:
                                log.warning("Failed to fetch sol_received for %s: %s", found_sig[:16], e)
                        elif label == "BUY":
                            try:
                                sol_spent = await self._get_sol_spent(found_sig)
                            except Exception as e:
                                log.warning("Failed to fetch sol_spent for %s: %s", found_sig[:16], e)
                        return {"sig": found_sig, "status": conf, "output_amount": output_amount, "sol_spent": sol_spent}
            except RuntimeError as exc:
                if "-32429" in str(exc):
                    log.debug("Status poll rate limited — waiting 5s")
                    await asyncio.sleep(5)
                    continue
                raise
            except Exception as exc:
                log.debug("Status poll error: %s", exc)
            await asyncio.sleep(0.25)
        # Diagnostic: check if any tx actually landed but polling missed it
        for check_sig in all_sigs:
            try:
                tx_check = await self._rpc({
                    "method": "getTransaction",
                    "params": [check_sig, {"encoding": "json", "commitment": "confirmed",
                                           "maxSupportedTransactionVersion": 0}],
                }, timeout=5)
                if tx_check and tx_check.get("value"):
                    log.error("TIMEOUT but tx found on-chain: sig=%s", check_sig[:16])
                    return {"sig": check_sig, "status": "confirmed", "output_amount": 0, "sol_spent": 0}
            except Exception:
                pass
        log.error("TIMEOUT — none of %d sigs landed on-chain", len(all_sigs))
        raise RuntimeError(f"TX {primary[:16]} {label} timed out after {timeout_s}s")

    async def _get_sol_spent(self, sig: str) -> float:
        """Return actual SOL debited from wallet for a buy tx (in SOL, not lamports)."""
        try:
            tx = await self._rpc({
                "method": "getTransaction",
                "params": [sig, {
                    "encoding":                      "json",
                    "commitment":                    "confirmed",
                    "maxSupportedTransactionVersion": 0,
                }],
            }, timeout=5)
            meta = tx.get("meta", {})
            pre  = meta.get("preBalances",  [])
            post = meta.get("postBalances", [])
            if pre and post:
                delta = pre[0] - post[0]
                return max(delta, 0) / config.LAMPORTS_PER_SOL
        except Exception as exc:
            log.debug("_get_sol_spent failed for %s: %s", sig[:16], exc)
        return config.BUY_AMOUNT_SOL

    async def _get_sol_received(self, sig: str) -> int:
        """
        Fetch the confirmed tx and return the lamport increase for our wallet.
        Returns 0 on any error rather than crashing P&L accounting.
        """
        try:
            tx = await self._rpc({
                "method": "getTransaction",
                "params": [sig, {
                    "encoding":                      "json",
                    "commitment":                    "confirmed",
                    "maxSupportedTransactionVersion": 0,
                }],
            }, timeout=5)
            meta = tx.get("meta", {})
            pre  = meta.get("preBalances",  [])
            post = meta.get("postBalances", [])
            if pre and post:
                delta = post[0] - pre[0]
                log.info("SELL P&L  sig=%s  delta=%d lamports (%.4f SOL)",
                         sig[:16], delta, delta / 1e9)
                _EXPECTED_CONST1 = "63EDqM8TH3kcQAkT3P5fVExUnSMPizEheZX6GmiiutN5"
                try:
                    keys = tx["transaction"]["message"]["accountKeys"]
                    sell_ix = next(
                        ix for ix in tx["transaction"]["message"]["instructions"]
                        if keys[ix["programIdIndex"]].startswith("6EF8")
                    )
                    accs = [keys[a] for a in sell_ix["accounts"]]
                    if len(accs) >= 16 and accs[14] != _EXPECTED_CONST1:
                        log.warning("pump_const1 DRIFT detected! expected=%s got=%s sig=%s",
                                    _EXPECTED_CONST1[:16], accs[14][:16], sig[:16])
                except Exception:
                    pass
                return max(delta, 0)
            else:
                log.warning("SELL P&L  sig=%s  no balance data in tx", sig[:16])
        except Exception as exc:
            log.warning("_get_sol_received failed for %s: %s", sig[:16], exc)
        return 0
