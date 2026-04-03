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
_PUMP_OLD_PROG    = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
_PFEE_PROG        = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

_GLOBAL           = Pubkey.from_string("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5zXDFQLa3s9Cz1g")
_FEE_RECIPIENT    = Pubkey.from_string("62qc2CNXwrYqQScmEdiZFFAnkwbGEHBKHsvCb4RRTMGU")
_EVENT_AUTHORITY  = Pubkey.from_string("Ce6TQqeH3go77A8dz3FPRp1MTEFGYZiQAzRoXH3MRBkS")
_PUMP_PDA         = Pubkey.from_string("Hq2wp8uQPApxkjFKuvYQBBqjkRqQmvBBs9dEHRfN9Cmb")
_PUMP_CONST2      = Pubkey.from_string("8Wf5TiA9KN2FLGE1PJp1UyeCpMWP4r1C12fKHxvLkFDL")
_PFEE_CONST       = Pubkey.from_string("7FeFBYb5FWAS8KwxT1CUZ3K3UyCnFbJTa2bsTq3QjYNW")

_TOKEN_PROGRAM    = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
_TOKEN_2022       = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
_ASSOC_TOKEN_PROG = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1brs")
_ASSOC_TOKEN_2022 = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
_SYSTEM_PROGRAM   = Pubkey.from_string("11111111111111111111111111111111")
_SYSVAR_RENT      = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# Helius Sender tip recipient — must be one of the addresses Sender accepts
_HELIUS_TIP       = Pubkey.from_string("4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey")

# Buy instruction discriminator (8 bytes)
_BUY_DISC = bytes([0x66, 0x32, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea])


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
        self._blockhash     = ""
        self._blockhash_ts  = 0.0
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

        await asyncio.gather(_warm_helius(), _warm_pumpportal(), _prime_blockhash())
        asyncio.create_task(self._blockhash_refresher())

    async def _blockhash_refresher(self):
        """Background task: keep blockhash ≤300ms old."""
        while True:
            await asyncio.sleep(0.3)
            try:
                self._blockhash    = await self._get_blockhash()
                self._blockhash_ts = time.time()
            except Exception as exc:
                log.warning("Blockhash refresh failed: %s", exc)

    async def _fresh_blockhash(self) -> str:
        """Return cached blockhash if ≤400ms old, else fetch synchronously."""
        if time.time() - self._blockhash_ts <= 0.4 and self._blockhash:
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
            log.warning("prefetch PumpPortal call failed for %s: %s", mint_str[:8], exc)
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
          tokens_out = sol_lamports * vtoken_raw // (vsol_lamports + sol_lamports)
          max_sol_cost = sol_lamports * (1 + slippage)
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
        tokens_out   = sol_lamports * vtoken_raw // (vsol_lamports + sol_lamports)
        max_sol_cost = int(sol_lamports * (1 + config.SLIPPAGE))
        slippage_bps = int(config.SLIPPAGE * 100)

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

        ix_buy = Instruction(
            program_id=_PUMP_NEW_PROG,
            accounts=[
                AccountMeta(_GLOBAL,          False, False),  # [0]
                AccountMeta(_FEE_RECIPIENT,   False, True),   # [1]
                AccountMeta(mint,             False, False),  # [2]
                AccountMeta(bc,               False, True),   # [3]
                AccountMeta(abc,              False, True),   # [4]
                AccountMeta(assoc_user,       False, True),   # [5]
                AccountMeta(self._pubkey,     True,  True),   # [6]
                AccountMeta(_SYSTEM_PROGRAM,  False, False),  # [7]
                AccountMeta(_TOKEN_2022,      False, False),  # [8]
                AccountMeta(cvlt,             False, True),   # [9]
                AccountMeta(_EVENT_AUTHORITY, False, False),  # [10]
                AccountMeta(_PUMP_OLD_PROG,   False, False),  # [11]
                AccountMeta(_PUMP_PDA,        False, False),  # [12]
                AccountMeta(pump_const1,      False, True),   # [13] per-token, from prefetch
                AccountMeta(_PUMP_CONST2,     False, False),  # [14]
                AccountMeta(_PFEE_PROG,       False, False),  # [15]
                AccountMeta(unk16,            False, True),   # [16]
                AccountMeta(_PFEE_CONST,      False, True),   # [17]
            ],
            data=bytes(buy_data),
        )

        msg = MessageV0.try_compile(
            payer=self._pubkey,
            instructions=[ix_cu_limit, ix_cu_price, ix_tip, ix_ata, ix_buy],
            address_lookup_table_accounts=[],
            recent_blockhash=Hash.from_string(blockhash),
        )
        sig = self._keypair.sign_message(bytes([0x80]) + bytes(msg))
        return bytes(VersionedTransaction.populate(msg, [sig]))

    # ── Public API ────────────────────────────────────────────────────────────

    async def buy(
        self,
        mint_str:      str,
        sol_amount:    float                = config.BUY_AMOUNT_SOL,
        token_accounts: "TokenAccounts | None" = None,
        vsol_lamports: int | None           = None,
        vtoken_raw:    int | None           = None,
    ) -> str:
        log.info("BUY  %s  %.4f SOL", mint_str, sol_amount)

        sol_lamports = int(sol_amount * config.LAMPORTS_PER_SOL)

        # Phase 2: build locally if we have all the data
        if (token_accounts is not None
                and vsol_lamports is not None
                and vtoken_raw is not None
                and vtoken_raw > 0
                and vsol_lamports > 0):
            try:
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
                sig    = await self._send_via_sender(tx_b64)
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
            "slippage":         int(config.SLIPPAGE * 100),
            "priorityFee":      priority_fee_sol,
            "pool":             "pump",
        }
        unsigned_tx = await self._get_pump_tx(payload)
        tx_bytes    = self._sign_tx(unsigned_tx)
        tx_b64      = base64.b64encode(tx_bytes).decode()
        # PumpPortal txs have no Helius tip — use regular RPC not Sender
        sig = await self._send_via_rpc(tx_b64)
        log.info("BUY  submitted (pumpportal)  sig=%s", sig)
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

        # PumpPortal txs have no Helius tip — use regular RPC not Sender
        sig = await self._send_via_rpc(tx_b64)
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
