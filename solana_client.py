"""
Direct on-chain Solana client for pump.fun trading.

Transaction flow (Phase 2 — local build):
  1. POST pumpportal.fun/api/trade-local once at token discovery → cache per-token accounts
  2. At signal time: build tx locally with tip + priority fee + ATA init + buy instruction
  3. Sign locally with our keypair
  4. Submit via Helius Sender (staked connection, priority inclusion)

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
_HELIUS_SENDER_TIP = Pubkey.from_string("9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta")

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
        self._pump_alt               = None   # AddressLookupTableAccount, set by warmup()
        self._wallet_balance_lamports: int = 0
        log.info("Wallet: %s", self._pubkey)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def warmup(self):
        """Pre-warm TCP connections and prime the blockhash cache."""
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

        await asyncio.gather(_warm_helius(), _warm_pumpportal(), _prime_blockhash(),
                             self._fetch_pump_alt(), self.refresh_balance())
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

    async def _send_via_helius_sender(self, tx_b64: str, url: str | None = None) -> str:
        """Submit via Helius Sender /fast endpoint (staked connection)."""
        async with self._session.post(
            url or config.HELIUS_SENDER_URL,
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
        log.debug("Helius Sender response (status=%d): %s", resp.status, data)
        if "error" in data:
            raise RuntimeError(f"Helius Sender error: {data['error']}")
        if "result" not in data:
            raise RuntimeError(f"Helius Sender unexpected response: {data}")
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
        if "error" in data:
            raise RuntimeError(f"Astralane error: {data['error']}")
        result = data.get("result")
        if not result:
            raise RuntimeError(f"Astralane unexpected response: {data}")
        return result

    async def _send_fast(self, tx_b64: str, tx_bytes: bytes | None = None) -> str:
        """Race all available endpoints in parallel — first successful response wins.

        When nonce is enabled, blasts to Astralane, Helius Sender, and Helius RPC.
        Without nonce, races Astralane sender and Helius RPC.
        """
        tasks = [
            asyncio.create_task(self._send_via_sender(tx_b64, config.ASTRALANE_URL)),
            asyncio.create_task(self._send_via_sender(tx_b64, config.ASTRALANE_AMS_URL)),
            asyncio.create_task(self._send_via_helius_sender(tx_b64, config.HELIUS_SENDER_URL)),
            asyncio.create_task(self._send_via_helius_sender(tx_b64, config.HELIUS_SENDER_URL_2)),
            asyncio.create_task(self._send_via_rpc(tx_b64)),
        ]
        if tx_bytes is not None and config.NONCE_ACCOUNT:
            tasks.append(asyncio.create_task(self._send_via_astralane(tx_bytes)))

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        # Return first SUCCESS — if first completion was an exception, try others
        for t in done:
            if not t.exception():
                return t.result()
        # All completed tasks failed — re-raise the first exception
        raise done.pop().exception()

    async def _fetch_nonce(self) -> str:
        """Read the durable nonce value from the nonce account.

        Nonce account data layout (80 bytes after 4-byte version):
          [0:4]   version       (u32 LE)
          [4:8]   state         (u32 LE) — 1 = initialized
          [8:40]  authority     (32-byte pubkey)
          [40:72] nonce         (32-byte hash — use as blockhash in tx)
          [72:80] fee_calculator (u64 LE lamports_per_signature)
        """
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
        nonce_bytes = data[40:72]
        return str(Hash.from_bytes(nonce_bytes))

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

    # ── Local transaction building ─────────────────────────────────────────────

    async def prefetch_token_accounts(self, mint_str: str) -> TokenAccounts | None:
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
            # Fetch bonding curve account: verify ownership (6EF8) and derive
            # creator_vault = PDA(["creator-vault", creator], 6EF8) from bc data.
            # BondingCurve layout: [0:8] disc, [8:16] vToken, [16:24] vSol,
            #   [24:32] realToken, [32:40] realSol, [40:48] totalSupply,
            #   [48:49] complete (bool), [49:81] creator (Pubkey)
            creator_vault_str = str(static[4])  # fallback to static slot
            try:
                bc_info = await self._rpc({
                    "method": "getAccountInfo",
                    "params": [str(static[2]), {"encoding": "base64"}]
                })
                bc_val = (bc_info or {}).get("value") or {}
                owner  = bc_val.get("owner", "")
                if owner and owner != str(_PUMP_OLD_PROG):
                    log.debug("prefetch: bc owned by %s (not 6EF8) for %s — skipping",
                              owner[:8], mint_str[:8])
                    raise WrongProgramError(owner)
                bc_data_b64 = (bc_val.get("data") or [None])[0]
                if bc_data_b64:
                    import base64 as _b64
                    bc_bytes = _b64.b64decode(bc_data_b64)
                    if len(bc_bytes) >= 81:
                        creator = Pubkey.from_bytes(bc_bytes[49:81])
                        vault, _ = Pubkey.find_program_address(
                            [b"creator-vault", bytes(creator)], _PUMP_OLD_PROG
                        )
                        creator_vault_str = str(vault)
                        log.debug("prefetch: derived creator_vault=%s for %s",
                                  creator_vault_str[:8], mint_str[:8])
            except WrongProgramError:
                raise
            except Exception as exc:
                log.debug("prefetch: bc check failed for %s: %s", mint_str[:8], exc)

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
    ) -> bytes:
        """
        Build and sign a pump.fun buy transaction locally using Legacy format.

        Legacy (not V0) — no ALT, no index drift if pump.fun updates the table.
        All global constants are hardcoded by address.

        Instructions:
          1. SetComputeUnitLimit
          2. SetComputeUnitPrice
          3. SystemProgram::createAccountWithSeed  (create user token account)
          4. Token-2022::InitializeAccount3        (init token account)
          5. pump.fun buy  (17 accounts)
          6. SystemProgram::transfer  (tip to Helius Sender)
        """
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
        from solders.system_program import transfer, TransferParams, create_account_with_seed, CreateAccountWithSeedParams

        mint        = Pubkey.from_string(mint_str)
        # Token account derived via seed — same method as competitor, no ATA program needed.
        # seed = first 8 chars of mint string; address = createWithSeed(wallet, seed, Token-2022)
        token_seed  = mint_str[:8]
        user_token  = Pubkey.create_with_seed(self._pubkey, token_seed, _TOKEN_2022)
        bc          = Pubkey.from_string(accounts.bonding_curve)
        abc         = Pubkey.from_string(accounts.assoc_bonding_curve)
        cvlt        = Pubkey.from_string(accounts.creator_vault)
        pump_const1 = Pubkey.from_string(accounts.pump_const1)
        unk16       = Pubkey.from_string(accounts.unk16)

        # Global constants — hardcoded by address, immune to ALT index drift
        _GLOBAL    = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
        _FEE_RECIP = Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV")
        _CONST10   = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
        _CONST12   = Pubkey.from_string("Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y")
        _CONST14   = Pubkey.from_string("8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt")
        _PFEE_PROG = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

        # ── Compute budget ─────────────────────────────────────────────────────
        ix_cu_limit = set_compute_unit_limit(config.COMPUTE_UNIT_LIMIT)
        ix_cu_price = set_compute_unit_price(config.COMPUTE_UNIT_PRICE)

        # ── Create token account via seed (cheaper than ATA program) ──────────
        # Min rent for a 165-byte standard token account
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

        # ── Initialize token account (Token-2022 InitializeAccount3) ──────────
        # Discriminator 18, then 32-byte owner pubkey in data; no ATA overhead
        ix_init = Instruction(
            program_id=_TOKEN_2022,
            accounts=[
                AccountMeta(pubkey=user_token, is_signer=False, is_writable=True),
                AccountMeta(pubkey=mint,       is_signer=False, is_writable=False),
            ],
            data=bytes([18]) + bytes(self._pubkey),
        )

        # ── AMM calculation ────────────────────────────────────────────────────
        tokens_out   = int(vtoken_raw * sol_lamports // (vsol_lamports + sol_lamports))
        max_sol_cost = sol_lamports * 2  # generous ceiling — actual spend determined by AMM

        buy_data = (
            _BUY_DISC
            + struct.pack("<Q", tokens_out)
            + struct.pack("<Q", max_sol_cost)
        )

        # ── Buy instruction — 17 accounts, no _FEE_CONFIG, unk16 readonly ─────
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

        # ── Tips — all senders paid; whichever lands it collects its tip ────────
        # Both Astralane endpoints share the same tip address — one combined transfer
        ix_tip_astralane = transfer(TransferParams(
            from_pubkey=self._pubkey,
            to_pubkey=_ASTRALANE_TIP,
            lamports=config.SENDER_TIP_LAMPORTS + config.ASTRALANE_AMS_TIP_LAMPORTS,
        ))
        ix_tip_helius = transfer(TransferParams(
            from_pubkey=self._pubkey,
            to_pubkey=_HELIUS_SENDER_TIP,
            lamports=config.HELIUS_SENDER_TIP_LAMPORTS,
        ))

        # ── AdvanceNonceAccount (prepended when durable nonce is configured) ────
        # Nonce ix must be first; the nonce value is used as the message blockhash.
        instructions = []
        if config.NONCE_ACCOUNT:
            nonce_acct = Pubkey.from_string(config.NONCE_ACCOUNT)
            ix_advance = Instruction(
                program_id=_SYSTEM_PROGRAM,
                accounts=[
                    AccountMeta(nonce_acct,   False, True),   # nonce account (writable)
                    AccountMeta(_SYSVAR_RECENT_BLOCKHASHES, False, False),  # recent blockhashes sysvar
                    AccountMeta(self._pubkey, True,  False),  # nonce authority (signer)
                ],
                data=bytes([4, 0, 0, 0]),  # SystemInstruction::AdvanceNonceAccount
            )
            instructions.append(ix_advance)

        instructions.extend([ix_cu_limit, ix_cu_price, ix_create, ix_init, ix_buy,
                             ix_tip_astralane, ix_tip_helius])

        # ── Legacy message — no ALT, immune to index drift ────────────────────
        msg = Message.new_with_blockhash(
            instructions,
            self._pubkey,
            Hash.from_string(blockhash),
        )
        sig = self._keypair.sign_message(bytes(msg))
        return bytes(Transaction.populate(msg, [sig]))

    def _build_local_sell_tx(
        self,
        mint_str:   str,
        accounts:   TokenAccounts,
        raw_amount: int,
        blockhash:  str,
    ) -> bytes:
        """
        Build and sign a pump.fun sell transaction locally using Legacy format.

        6EF8 sell CPI account order (16 accounts, verified from on-chain inner ix):
          [0]  global           readonly
          [1]  feeRecipient     writable
          [2]  mint             readonly
          [3]  bondingCurve     writable
          [4]  assocBondingCurve writable
          [5]  userToken        writable  ← seed-derived account (source)
          [6]  user             writable  signer
          [7]  systemProgram    readonly
          [8]  creatorVault     writable  (swapped vs buy — [9] in buy)
          [9]  tokenProgram     readonly  (swapped vs buy — [8] in buy)
          [10] eventAuthority   readonly
          [11] program          readonly
          [12] CONST14          readonly
          [13] pfeeProgram      readonly
          [14] pump_const1      writable  (per-user account = buy[13])
          [15] unk16            readonly  (per-token account = buy[16])

        Instructions: SetCULimit, SetCUPrice, sell, tip
        """
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

        mint        = Pubkey.from_string(mint_str)
        user_token  = Pubkey.create_with_seed(self._pubkey, mint_str[:8], _TOKEN_2022)
        bc          = Pubkey.from_string(accounts.bonding_curve)
        abc         = Pubkey.from_string(accounts.assoc_bonding_curve)
        cvlt        = Pubkey.from_string(accounts.creator_vault)
        pump_const1 = Pubkey.from_string(accounts.pump_const1)
        unk16       = Pubkey.from_string(accounts.unk16)

        _GLOBAL    = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf")
        _FEE_RECIP = Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV")
        _CONST10   = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
        _CONST14   = Pubkey.from_string("8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt")
        _PFEE_PROG = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

        ix_cu_limit = set_compute_unit_limit(100_000)
        ix_cu_price = set_compute_unit_price(config.SELL_COMPUTE_UNIT_PRICE)

        sell_data = (
            _SELL_DISC
            + struct.pack("<Q", raw_amount)  # tokens to sell
            + struct.pack("<Q", 0)           # min_sol_output — accept any
        )

        ix_sell = Instruction(
            program_id=_PUMP_OLD_PROG,
            accounts=[
                AccountMeta(_GLOBAL,         False, False),  # [0]  global
                AccountMeta(_FEE_RECIP,      False, True),   # [1]  fee recipient (writable)
                AccountMeta(mint,            False, False),  # [2]  mint
                AccountMeta(bc,              False, True),   # [3]  bonding curve (writable)
                AccountMeta(abc,             False, True),   # [4]  assoc bonding curve (writable)
                AccountMeta(user_token,      False, True),   # [5]  user token account (writable, source)
                AccountMeta(self._pubkey,    True,  True),   # [6]  user (signer, writable)
                AccountMeta(_SYSTEM_PROGRAM, False, False),  # [7]  system program
                AccountMeta(cvlt,            False, True),   # [8]  creator vault (writable)
                AccountMeta(_TOKEN_2022,     False, False),  # [9]  token program
                AccountMeta(_CONST10,        False, False),  # [10] event authority
                AccountMeta(_PUMP_OLD_PROG,  False, False),  # [11] program self-ref
                AccountMeta(_CONST14,        False, False),  # [12]
                AccountMeta(_PFEE_PROG,      False, False),  # [13] pfee program
                AccountMeta(pump_const1,     False, True),   # [14] per-user account (writable) = buy[13]
                AccountMeta(unk16,           False, False),  # [15] per-token (readonly)         = buy[16]
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
        log.info("BUY  %s  %.4f SOL", mint_str, sol_amount)

        sol_lamports = int(sol_amount * config.LAMPORTS_PER_SOL)

        if token_accounts is None:
            raise RuntimeError("missing prefetch data (token_accounts) — skipping")

        # Fetch blockhash (or nonce) and fresh on-chain reserves in parallel
        bh_coro = self._fetch_nonce() if config.NONCE_ACCOUNT else self._fresh_blockhash()
        blockhash, fresh_reserves = await asyncio.gather(
            bh_coro,
            self._fetch_bc_reserves(token_accounts.bonding_curve),
        )

        # Always prefer fresh on-chain reserves — signal values may be stale
        # or in wrong units if sourced from PumpPortal (UI units vs raw u64)
        if fresh_reserves:
            vsol_lamports, vtoken_raw = fresh_reserves
            log.debug("BUY using fresh reserves: vsol=%.4f vtoken=%d", vsol_lamports/1e9, vtoken_raw)
        elif vsol_lamports is None or not vtoken_raw:
            raise RuntimeError("missing reserves and fresh fetch failed — skipping")

        tx_bytes = self._build_local_buy_tx(
            mint_str      = mint_str,
            accounts      = token_accounts,
            sol_lamports  = sol_lamports,
            vtoken_raw    = vtoken_raw,
            vsol_lamports = vsol_lamports,
            blockhash     = blockhash,
        )
        tx_b64 = base64.b64encode(tx_bytes).decode()
        sig    = await self._send_fast(tx_b64, tx_bytes)
        log.info("BUY  submitted (local)  sig=%s", sig)
        return sig

    async def sell_all(self, mint_str: str, token_accounts=None) -> tuple[str, str]:
        # Poll up to 10s for balance to settle after buy confirmation lag
        raw_balance, ui_balance = 0, 0.0
        for _ in range(10):
            raw_balance, ui_balance = await self._get_token_balance(mint_str)
            if raw_balance > 0:
                break
            log.debug("Waiting for token balance to settle for %s…", mint_str[:8])
            await asyncio.sleep(1)

        if raw_balance == 0:
            raise RuntimeError(f"No token balance for {mint_str}")

        log.info("SELL %s  %.4f tokens (%d raw)", mint_str, ui_balance, raw_balance)

        if token_accounts is not None:
            blockhash = await self._fresh_blockhash()
            tx_bytes  = self._build_local_sell_tx(mint_str, token_accounts, raw_balance, blockhash)
            tx_b64    = base64.b64encode(tx_bytes).decode()
            sig       = await self._send_via_rpc(tx_b64)
            log.info("SELL submitted (local)  sig=%s", sig)
            return sig, tx_b64

        raise RuntimeError(f"no token_accounts for sell of {mint_str}")

    async def wait_for_order(self, sig: str, label: str = "", tx_b64: str = "") -> dict:
        """Poll getSignatureStatuses until confirmed or timeout.

        Rebroadcasts the tx every 2s when tx_b64 is provided so missed slots
        don't cause a timeout. For SELL transactions, also fetches the confirmed
        tx to read the wallet's SOL delta for P&L tracking.
        """
        timeout_s      = 30 if label == "BUY" else 60
        deadline       = time.time() + timeout_s
        last_broadcast = time.time()

        while time.time() < deadline:
            # Rebroadcast every 2s if we have the tx bytes
            if tx_b64 and (time.time() - last_broadcast) >= 2.0:
                try:
                    await self._send_via_rpc(tx_b64)
                    last_broadcast = time.time()
                    log.debug("Rebroadcast %s %s", label, sig[:16])
                except Exception:
                    pass

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
                        output_amount = 0
                        sol_spent     = 0
                        if label == "SELL":
                            output_amount = await self._get_sol_received(sig)
                        elif label == "BUY":
                            sol_spent = await self._get_sol_spent(sig)
                        return {"sig": sig, "status": conf, "output_amount": output_amount, "sol_spent": sol_spent}
            except RuntimeError as exc:
                if "-32429" in str(exc):
                    log.debug("Status poll rate limited — waiting 5s")
                    await asyncio.sleep(5)
                    continue
                raise
            except Exception as exc:
                log.debug("Status poll error: %s", exc)
            await asyncio.sleep(0.25)
        raise RuntimeError(f"TX {sig[:16]} {label} timed out after {timeout_s}s")

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
                delta = pre[0] - post[0]   # positive = SOL left wallet
                return max(delta, 0) / config.LAMPORTS_PER_SOL
        except Exception as exc:
            log.debug("_get_sol_spent failed for %s: %s", sig[:16], exc)
        return config.BUY_AMOUNT_SOL  # fallback to intended amount

    async def _get_sol_received(self, sig: str) -> int:
        """
        Fetch the confirmed tx and return the lamport increase for our wallet
        (postBalance - preBalance for account index 0, the payer/signer).
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
                delta = post[0] - pre[0]   # account[0] is always the fee-payer (our wallet)
                return max(delta, 0)       # negative delta (e.g. failed tx) → report 0
        except Exception as exc:
            log.debug("_get_sol_received failed for %s: %s", sig[:16], exc)
        return 0
