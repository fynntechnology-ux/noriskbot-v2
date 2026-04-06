import os
from dotenv import load_dotenv

load_dotenv()

# Wallet
WALLET_PRIVATE_KEY: str = os.environ["WALLET_PRIVATE_KEY"]
WALLET_ADDRESS:     str = os.environ["WALLET_ADDRESS"]

# Trade settings
BUY_AMOUNT_SOL:          float = float(os.getenv("BUY_AMOUNT_SOL", "0.03"))
HOLD_TIME_SECONDS:       int   = int(os.getenv("HOLD_TIME_SECONDS", "60"))
MAX_TOKEN_AGE_SECONDS:   int   = int(os.getenv("MAX_TOKEN_AGE_SECONDS", "60"))
SLIPPAGE:                float = float(os.getenv("SLIPPAGE", "0.5"))
MAX_CONCURRENT_POSITIONS:int   = int(os.getenv("MAX_CONCURRENT_POSITIONS", "3"))
TRAIL_STOP_PCT:          float = float(os.getenv("TRAIL_STOP_PCT", "5.0"))    # sell when drawdown from peak exceeds X%

# Solana transaction fees
COMPUTE_UNIT_LIMIT:          int = int(os.getenv("COMPUTE_UNIT_LIMIT",          "200000"))
COMPUTE_UNIT_PRICE:          int = int(os.getenv("COMPUTE_UNIT_PRICE",          "500000"))  # micro-lamports/CU — buys
SELL_COMPUTE_UNIT_PRICE:     int = int(os.getenv("SELL_COMPUTE_UNIT_PRICE",     "100000"))  # micro-lamports/CU — sells
SENDER_TIP_LAMPORTS:         int = int(os.getenv("SENDER_TIP_LAMPORTS",         "1000000"))  # Astralane tip lamports
HELIUS_SENDER_TIP_LAMPORTS:  int = int(os.getenv("HELIUS_SENDER_TIP_LAMPORTS",  "200000"))   # 0.0002 SOL
MIN_BUY_BUFFER_LAMPORTS:     int = int(os.getenv("MIN_BUY_BUFFER_LAMPORTS",     "20000000")) # 0.02 SOL

# sendIdeal dual-path fee config
# Tx A: high priority fee + low tip  → SWQoS / priority-fee validators
# Tx B: low priority fee  + high tip → Jito bundle validators
# Whichever path wins, the nonce is consumed and the other tx becomes invalid.
IDEAL_HIGH_FEE_CU_PRICE: int = int(os.getenv("IDEAL_HIGH_FEE_CU_PRICE", "300000"))   # µL/CU for Tx A
IDEAL_LOW_TIP_LAMPORTS:   int = int(os.getenv("IDEAL_LOW_TIP_LAMPORTS",   "10000"))    # Astralane min tip for Tx A
IDEAL_LOW_FEE_CU_PRICE:   int = int(os.getenv("IDEAL_LOW_FEE_CU_PRICE",  "1000"))     # µL/CU for Tx B
IDEAL_HIGH_TIP_LAMPORTS:  int = int(os.getenv("IDEAL_HIGH_TIP_LAMPORTS",  "1000000"))  # Jito tip for Tx B (0.001 SOL)

# RPC endpoints
HELIUS_API_KEY:      str = os.environ["HELIUS_API_KEY"]
HELIUS_RPC_HTTP:     str = f"https://beta.helius-rpc.com/?api-key={HELIUS_API_KEY}"
FALLBACK_RPC_HTTP:   str = os.getenv("FALLBACK_RPC_HTTP", "https://solana-mainnet.g.alchemy.com/v2/jpQYcpvnQ8XNbc0M6_-Vd")
HELIUS_RPC_WS:       str = f"wss://beta.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_SENDER_URL:   str = os.getenv("HELIUS_SENDER_URL",   "http://ams-sender.helius-rpc.com/fast")
HELIUS_SENDER_URL_2: str = os.getenv("HELIUS_SENDER_URL_2", "http://fra-sender.helius-rpc.com/fast")
ASTRALANE_API_KEY:   str = os.environ["ASTRALANE_API_KEY"]
ASTRALANE_URL:       str = f"https://fr.gateway.astralane.io/iris?api-key={ASTRALANE_API_KEY}"
ASTRALANE_AMS_URL:   str = f"http://ams.gateway.astralane.io/iris?api-key={ASTRALANE_API_KEY}"
ASTRALANE_IDEAL_URL: str = f"https://fr.gateway.astralane.io/sendIdeal?api-key={ASTRALANE_API_KEY}"
ASTRALANE_AMS_TIP_LAMPORTS: int = int(os.getenv("ASTRALANE_AMS_TIP_LAMPORTS", "20000"))  # 0.00002 SOL

# Durable nonce (optional) — set NONCE_ACCOUNT to enable pre-signed txs
NONCE_ACCOUNT:     str = os.getenv("NONCE_ACCOUNT", "")

# PumpPortal WebSocket for new token events
PUMPPORTAL_WS: str = "wss://pumpportal.fun/api/data"

# Solana constants
SOL_MINT:        str = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL:int = 1_000_000_000
