"""
GMGN OpenAPI client  —  https://openapi.gmgn.ai

Auth modes
----------
Normal  (token info, balance queries):
    Headers: X-APIKEY
    Query:   timestamp, client_id

Critical (swap, order query):
    Headers: X-APIKEY, X-Signature
    Query:   timestamp, client_id
    Signature message: "{sub_path}:{sorted_query_string}:{body}:{timestamp}"
    Signed with Ed25519 private key, base64-encoded.

Swap flow
---------
1. POST /v1/trade/swap  →  returns { order_id }
2. Poll GET /v1/trade/query_order?order_id=...&chain=sol  until filled/failed
"""

import asyncio
import base64
import uuid
import time
import aiohttp

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import config
from logger import get_logger

log = get_logger("gmgn")

# How long to poll for order confirmation
ORDER_POLL_TIMEOUT  = 30   # seconds
ORDER_POLL_INTERVAL = 0.3  # seconds — faster polling


class GMGNClient:
    def __init__(self):
        self._api_key = config.GMGN_API_KEY
        self._host = config.GMGN_HOST.rstrip("/")
        self._private_key: Ed25519PrivateKey = load_pem_private_key(
            config.GMGN_PRIVATE_KEY_PEM.encode(), password=None
        )
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Session — persistent connector with DNS cache + keepalive
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=20,                  # max concurrent connections
                ttl_dns_cache=300,         # cache DNS for 5 min
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                connector_owner=True,
            )
        return self._session

    async def warmup(self):
        """Pre-establish TCP connection to GMGN so first trade has no cold-start."""
        try:
            await self._normal_get("/v1/user/info", {})
            log.info("GMGN connection warmed up")
        except Exception as exc:
            log.warning("Warmup failed (non-fatal): %s", exc)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _auth_params(self) -> dict:
        return {
            "timestamp": str(int(time.time())),
            "client_id": str(uuid.uuid4()),
        }

    def _sign(self, sub_path: str, query: dict, body: str) -> str:
        """
        Build the signature message and sign it with Ed25519.
        Format: {sub_path}:{sorted_qs}:{body}:{timestamp}
        """
        timestamp = query["timestamp"]
        sorted_qs = "&".join(f"{k}={query[k]}" for k in sorted(query))
        message = f"{sub_path}:{sorted_qs}:{body}:{timestamp}"
        sig_bytes = self._private_key.sign(message.encode("utf-8"))
        return base64.b64encode(sig_bytes).decode()

    # ------------------------------------------------------------------
    # Request builders
    # ------------------------------------------------------------------

    async def _normal_get(self, sub_path: str, params: dict) -> dict:
        auth = self._auth_params()
        query = {**params, **auth}
        url = f"{self._host}{sub_path}"
        headers = {"X-APIKEY": self._api_key, "Content-Type": "application/json"}

        session = await self._get_session()
        async with session.get(
            url, params=query, headers=headers,
            timeout=aiohttp.ClientTimeout(total=3)
        ) as resp:
            data = await resp.json()

        self._check(sub_path, data)
        return data.get("data", data)

    async def _critical_get(self, sub_path: str, params: dict) -> dict:
        auth = self._auth_params()
        query = {**params, **auth}
        body = ""
        sig = self._sign(sub_path, query, body)
        url = f"{self._host}{sub_path}"
        headers = {
            "X-APIKEY": self._api_key,
            "X-Signature": sig,
            "Content-Type": "application/json",
        }

        session = await self._get_session()
        async with session.get(
            url, params=query, headers=headers,
            timeout=aiohttp.ClientTimeout(total=3)
        ) as resp:
            data = await resp.json()

        self._check(sub_path, data)
        return data.get("data", data)

    async def _critical_post(self, sub_path: str, query_params: dict, body: dict) -> dict:
        auth = self._auth_params()
        query = {**query_params, **auth}
        body_str = __import__("json").dumps(body, separators=(",", ":"))
        sig = self._sign(sub_path, query, body_str)
        url = f"{self._host}{sub_path}"
        headers = {
            "X-APIKEY": self._api_key,
            "X-Signature": sig,
            "Content-Type": "application/json",
        }

        session = await self._get_session()
        async with session.post(
            url, params=query, data=body_str, headers=headers,
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            data = await resp.json()

        self._check(sub_path, data)
        return data.get("data", data)

    def _check(self, path: str, data: dict):
        if data.get("code") != 0:
            raise RuntimeError(
                f"GMGN error on {path}: code={data.get('code')} "
                f"error={data.get('error')} message={data.get('message')}"
            )

    # ------------------------------------------------------------------
    # Token / wallet info
    # ------------------------------------------------------------------

    async def get_token_info(self, mint: str) -> dict:
        """GET /v1/token/info — price, market cap, bonding curve, etc."""
        return await self._normal_get("/v1/token/info", {"chain": "sol", "address": mint})

    async def get_wallet_token_balance(self, mint: str) -> int:
        """GET /v1/user/wallet_token_balance — raw SPL balance."""
        data = await self._normal_get("/v1/user/wallet_token_balance", {
            "chain": "sol",
            "wallet_address": config.WALLET_ADDRESS,
            "token_address": mint,
        })
        return int(data.get("balance", 0))

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    async def buy(self, token_mint: str, sol_amount: float = config.BUY_AMOUNT_SOL) -> str:
        """
        Buy `sol_amount` SOL worth of `token_mint`.
        Returns order_id.
        """
        lamports = str(int(sol_amount * config.LAMPORTS_PER_SOL))
        log.info("BUY  %s  %.4f SOL (%s lamports)", token_mint, sol_amount, lamports)

        result = await self._critical_post("/v1/trade/swap", {}, {
            "chain": "sol",
            "from_address": config.WALLET_ADDRESS,
            "input_token": config.SOL_MINT,
            "output_token": token_mint,
            "input_amount": lamports,
            "slippage": config.SLIPPAGE,
            "is_anti_mev": True,
            "priority_fee": config.PRIORITY_FEE,
            "tip_fee": config.TIP_FEE,
        })

        order_id = result.get("order_id") or result.get("id")
        if not order_id:
            raise RuntimeError(f"No order_id in swap response: {result}")

        log.info("BUY  submitted  order_id=%s", order_id)
        return order_id

    async def sell_all(self, token_mint: str) -> str:
        """
        Sell 100% of our position in `token_mint`.
        Uses input_amount_bps=10000 (100%).
        Returns order_id.
        """
        log.info("SELL %s  100%%", token_mint)

        result = await self._critical_post("/v1/trade/swap", {}, {
            "chain": "sol",
            "from_address": config.WALLET_ADDRESS,
            "input_token": token_mint,
            "output_token": config.SOL_MINT,
            "input_amount": "0",
            "input_amount_bps": "10000",   # 100% of balance
            "slippage": config.SLIPPAGE,
            "is_anti_mev": True,
            "priority_fee": config.PRIORITY_FEE,
            "tip_fee": config.TIP_FEE,
        })

        order_id = result.get("order_id") or result.get("id")
        if not order_id:
            raise RuntimeError(f"No order_id in sell response: {result}")

        log.info("SELL submitted  order_id=%s", order_id)
        return order_id

    async def wait_for_order(self, order_id: str, label: str = "") -> dict:
        """
        Poll /v1/trade/query_order until the order is filled or failed.
        Returns the final order data dict.
        """
        deadline = time.time() + ORDER_POLL_TIMEOUT
        while time.time() < deadline:
            try:
                data = await self._critical_get("/v1/trade/query_order", {
                    "order_id": order_id,
                    "chain": "sol",
                })
                status = data.get("status", "")
                log.debug("Order %s %s status=%s", label, order_id, status)

                if status in ("filled", "success", "completed"):
                    log.info("Order %s FILLED  hash=%s", order_id, data.get("hash", "?"))
                    return data
                if status in ("failed", "cancelled", "expired"):
                    raise RuntimeError(f"Order {order_id} {label} ended with status={status}")

            except RuntimeError:
                raise
            except Exception as exc:
                log.warning("Order poll error: %s", exc)

            await asyncio.sleep(ORDER_POLL_INTERVAL)

        raise RuntimeError(f"Order {order_id} {label} timed out after {ORDER_POLL_TIMEOUT}s")
