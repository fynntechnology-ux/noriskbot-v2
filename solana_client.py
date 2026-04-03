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
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

import config
from logger import get_logger

log = get_logger("solana")

PUMPPORTAL_TRADE_URL = "https://pumpportal.fun/api/trade-local"

# ── pump.fun constant addresses ───────────────────────────────────────────────

_PUMP_NEW_PROG    = Pubkey.from_string("FAdo9NCw1ssek6Z6yeWzWjhLVsr8uiCwcWNUnKgzTnHe")
_TOKEN_2022       = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
_ASSOC_TOKEN_PROG = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1brs")
_ASSOC_TOKEN_2022 = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
_SYSTEM_PROGRAM   = Pubkey.from_string("11111111111111111111111111111111")

# Helius Sender tip recipient — must be one of the addresses Sender accepts
_HELIUS_TIP       = Pubkey.from_string("4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey")

# Buy instruction discriminator (8 bytes)
_BUY_DISC = bytes([0x66, 0x32, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea])

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
#   [26] FgX1cdFq7khWeivEfHCULBA6ovtSr9djdAfJ9r3LvNST  CONST_17          buy[17] (writable)
#   [28] pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ   PFEE_PROG         buy[15] (readonly)
#   [29] 8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt  CONST_14          buy[14] (readonly)
_PUMP_ALT_ADDR  = Pubkey.from_string("84gxtAAWToZ6xep3wrWsx8TEoLB7EBS9VrKkV9CtMdJi")
_PUMP_OLD_PROG  = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")


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
        log.debug("Sender response (status=%d): %s", resp.status, data)
        if "error" in data:
            raise RuntimeError(f"Sender error: {data['error']}")
        if "result" not in data:
            raise RuntimeError(f"Sender unexpected response: {data}")
        return data["result"]

    async def _send_fast(self, tx_b64: str) -> str:
        """Race Helius Sender and RPC in parallel — first successful response wins."""
        tasks = [
            asyncio.create_task(self._send_via_sender(tx_b64)),
            asyncio.create_task(self._send_via_rpc(tx_b64)),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        # Return first SUCCESS — if first completion was an exception, try others
        for t in done:
            if not t.exception():
                return t.result()
        # All completed tasks failed — re-raise the first exception
        raise done.pop().exception()

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
            "slippage":         int(config.SLIPPAGE * 10000),
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
            return TokenAccounts(
                assoc_user          = str(static[1]),
                bonding_curve       = str(static[2]),
                assoc_bonding_curve = str(static[3]),
                creator_vault       = str(static[4]),
                pump_const1         = str(static[5]),
                unk16               = str(static[6]),
            )
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
            msg = MessageV0.try_compile(
                payer=self._pubkey,
                instructions=[ix_cu_limit, ix_cu_price, ix_ata],
                address_lookup_table_accounts=[],
                recent_blockhash=Hash.from_string(blockhash),
            )
            sig_obj = self._keypair.sign_message(bytes([0x80]) + bytes(msg))
            tx_bytes = bytes(VersionedTransaction.populate(msg, [sig_obj]))
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
        mint_str:     str,
        accounts:     TokenAccounts,
        sol_lamports: int,
        vtoken_raw:   int,
        vsol_lamports: int,
        blockhash:    str,
    ) -> bytes:
        """
        Build and sign a pump.fun buy transaction locally.

        Instructions:
          1. SetComputeUnitLimit
          2. SetComputeUnitPrice
          3. SystemProgram::transfer  (tip to Helius Sender)
          4. AssociatedTokenAccount create-if-needed  (user's token ATA)
          5. pump.fun buy

        AMM pricing:
          tokens_out = expected_tokens  — full AMM estimate, sets the buy size
          max_sol_cost = sol_lamports * (1 + slippage)  — SOL ceiling, absorbs price drift
        """
        from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
        from solders.system_program import transfer, TransferParams

        mint = Pubkey.from_string(mint_str)

        # ── 1. Compute budget ──────────────────────────────────────────────────
        ix_cu_limit = set_compute_unit_limit(config.COMPUTE_UNIT_LIMIT)
        ix_cu_price = set_compute_unit_price(config.COMPUTE_UNIT_PRICE)

        # ── 2. Tip to Helius Sender ────────────────────────────────────────────
        ix_tip = transfer(TransferParams(
            from_pubkey=self._pubkey,
            to_pubkey=_HELIUS_TIP,
            lamports=config.SENDER_TIP_LAMPORTS,
        ))

        # ── 3. ATA create-if-needed ────────────────────────────────────────────
        assoc_user = Pubkey.from_string(accounts.assoc_user)
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
            data=b'\x01',  # idempotent create
        )

        # ── 4. pump.fun buy ────────────────────────────────────────────────────
        # tokens_out: full AMM estimate — sets the buy size; max_sol_cost is the slippage guard
        # minimum acceptable amount — handles vSol drift between signal and execution
        tokens_out   = int(sol_lamports * vtoken_raw // (vsol_lamports + sol_lamports))
        max_sol_cost = int(sol_lamports * (1 + config.SLIPPAGE))
        slippage_bps = int(config.SLIPPAGE * 10000)

        buy_data = (
            _BUY_DISC
            + struct.pack("<Q", tokens_out)
            + struct.pack("<Q", max_sol_cost)
            + struct.pack("<H", slippage_bps)  # required by pump.fun on-chain struct
        )

        bc          = Pubkey.from_string(accounts.bonding_curve)
        abc         = Pubkey.from_string(accounts.assoc_bonding_curve)
        cvlt        = Pubkey.from_string(accounts.creator_vault)
        pump_const1 = Pubkey.from_string(accounts.pump_const1)
        unk16       = Pubkey.from_string(accounts.unk16)

        # ALT-sourced constants — indices verified by decoding a live PumpPortal buy tx
        alt = self._pump_alt.addresses
        _global    = alt[11]  # buy[0]  GLOBAL (4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf)
        _fee_recip = alt[22]  # buy[1]  FEE_RECIPIENT (62qc2CNX...fafNgV) writable
        _sys_prog  = alt[1]   # buy[7]  SYSTEM_PROGRAM (11111...)
        _const10   = alt[5]   # buy[10] Ce6TQqeHC9p8...
        _pump_old  = alt[2]   # buy[11] PUMP_OLD_PROG (6EF8...)
        _const12   = alt[24]  # buy[12] Hq2wp8uJ9...
        _const14   = alt[29]  # buy[14] 8Wf5TiAheL...
        _pfee_prog = alt[28]  # buy[15] pfeeUxB6... (PFEE_PROG)
        _const17   = alt[26]  # buy[17] FgX1cdFq7... writable

        ix_buy = Instruction(
            program_id=_PUMP_NEW_PROG,
            accounts=[
                AccountMeta(_global,      False, False),  # [0]  GLOBAL
                AccountMeta(_fee_recip,   False, True),   # [1]  FEE_RECIPIENT (writable)
                AccountMeta(mint,         False, False),  # [2]
                AccountMeta(bc,           False, True),   # [3]
                AccountMeta(abc,          False, True),   # [4]
                AccountMeta(assoc_user,   False, True),   # [5]
                AccountMeta(self._pubkey, True,  True),   # [6]
                AccountMeta(_sys_prog,    False, False),  # [7]  SYSTEM_PROGRAM
                AccountMeta(_TOKEN_2022,  False, False),  # [8]
                AccountMeta(cvlt,         False, True),   # [9]
                AccountMeta(_const10,     False, False),  # [10]
                AccountMeta(_pump_old,    False, False),  # [11] PUMP_OLD_PROG
                AccountMeta(_const12,     False, False),  # [12]
                AccountMeta(pump_const1,  False, True),   # [13] per-token (prefetch static[5])
                AccountMeta(_const14,     False, False),  # [14]
                AccountMeta(_pfee_prog,   False, False),  # [15] PFEE_PROG
                AccountMeta(unk16,        False, True),   # [16]
                AccountMeta(_const17,     False, True),   # [17] writable
            ],
            data=bytes(buy_data),
        )

        msg = MessageV0.try_compile(
            payer=self._pubkey,
            instructions=[ix_cu_limit, ix_cu_price, ix_tip, ix_ata, ix_buy],
            address_lookup_table_accounts=[self._pump_alt],
            recent_blockhash=Hash.from_string(blockhash),
        )
        sig = self._keypair.sign_message(bytes([0x80]) + bytes(msg))
        return bytes(VersionedTransaction.populate(msg, [sig]))

    # ── Public API ────────────────────────────────────────────────────────────

    async def buy(
        self,
        mint_str:        str,
        sol_amount:      float                  = config.BUY_AMOUNT_SOL,
        token_accounts:  "TokenAccounts | None" = None,
        vsol_lamports:   int | None             = None,
        vtoken_raw:      int | None             = None,
        creator_pubkey:  str | None             = None,
    ) -> str:
        log.info("BUY  %s  %.4f SOL", mint_str, sol_amount)

        sol_lamports = int(sol_amount * config.LAMPORTS_PER_SOL)

        if vtoken_raw is None:
            log.warning("vtoken_raw is None at buy time for %s — will use PumpPortal fallback",
                        mint_str[:8])

        # Phase 2: build locally if we have all the data
        if (self._pump_alt is not None
                and token_accounts is not None
                and vsol_lamports is not None
                and vtoken_raw is not None
                and vtoken_raw > 0
                and vsol_lamports > 0):
            try:
                # Derive creator_vault PDA from seeds ["vault", creator_pubkey, mint]
                # to avoid relying on static[4] from prefetch tx (wrong since pump.fun update)
                if creator_pubkey:
                    try:
                        creator_pk = Pubkey.from_string(creator_pubkey)
                        mint_pk    = Pubkey.from_string(mint_str)
                        vault_pda, _ = Pubkey.find_program_address(
                            [b"vault", bytes(creator_pk), bytes(mint_pk)],
                            _PUMP_OLD_PROG,
                        )
                        token_accounts = TokenAccounts(
                            assoc_user          = token_accounts.assoc_user,
                            bonding_curve       = token_accounts.bonding_curve,
                            assoc_bonding_curve = token_accounts.assoc_bonding_curve,
                            creator_vault       = str(vault_pda),
                            pump_const1         = token_accounts.pump_const1,
                            unk16               = token_accounts.unk16,
                        )
                        log.debug("creator_vault derived: %s", str(vault_pda))
                    except Exception as exc:
                        log.warning("creator_vault PDA derivation failed for %s: %s",
                                    mint_str[:8], exc)

                blockhash = await self._fresh_blockhash()
                tx_bytes  = self._build_local_buy_tx(
                    mint_str      = mint_str,
                    accounts      = token_accounts,
                    sol_lamports  = sol_lamports,
                    vtoken_raw    = vtoken_raw,
                    vsol_lamports = vsol_lamports,
                    blockhash     = blockhash,
                )
                tx_b64 = base64.b64encode(tx_bytes).decode()
                sig    = await self._send_fast(tx_b64)
                log.info("BUY  submitted (local)  sig=%s", sig)
                return sig
            except Exception as exc:
                log.warning("Local tx build failed, falling back to PumpPortal: %s", exc)

        # Fallback: PumpPortal unsigned tx
        priority_fee_sol = (
            config.COMPUTE_UNIT_PRICE * config.COMPUTE_UNIT_LIMIT / 1_000_000 / 1_000_000_000
        )
        payload = {
            "publicKey":        str(self._pubkey),
            "action":           "buy",
            "mint":             mint_str,
            "amount":           sol_amount,
            "denominatedInSol": "true",
            "slippage":         int(config.SLIPPAGE * 10000),
            "priorityFee":      priority_fee_sol,
            "pool":             "pump",
        }
        unsigned_tx = await self._get_pump_tx(payload)
        tx_bytes    = self._sign_tx(unsigned_tx)
        tx_b64      = base64.b64encode(tx_bytes).decode()
        sig = await self._send_via_rpc(tx_b64)
        log.info("BUY  submitted via FALLBACK (PumpPortal tx, RPC)  sig=%s", sig)
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
            "amount":           raw_balance,
            "denominatedInSol": "false",
            "slippage":         int(config.SLIPPAGE * 10000),
            "priorityFee":      priority_fee_sol,
            "pool":             "pump",
        }

        log.info("SELL %s  %.4f tokens (%d raw)", mint_str, ui_balance, raw_balance)

        unsigned_tx = await self._get_pump_tx(payload)
        tx_bytes    = self._sign_tx(unsigned_tx)
        tx_b64      = base64.b64encode(tx_bytes).decode()

        sig = await self._send_via_rpc(tx_b64)
        log.info("SELL submitted via RPC  sig=%s", sig)
        return sig

    async def wait_for_order(self, sig: str, label: str = "") -> dict:
        """Poll getSignatureStatuses until confirmed or timeout.

        For SELL transactions, also fetches the confirmed tx to read the
        wallet's SOL delta (postBalance - preBalance for account[0]) so
        position_manager can record accurate P&L.
        """
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
                        output_amount = 0
                        sol_spent     = 0
                        if label == "SELL":
                            output_amount = await self._get_sol_received(sig)
                        elif label == "BUY":
                            sol_spent = await self._get_sol_spent(sig)
                        return {"sig": sig, "status": conf, "output_amount": output_amount, "sol_spent": sol_spent}
            except RuntimeError:
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
