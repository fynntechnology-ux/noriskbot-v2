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

# Solana transaction fees
COMPUTE_UNIT_LIMIT:   int = int(os.getenv("COMPUTE_UNIT_LIMIT",   "200000"))
COMPUTE_UNIT_PRICE:   int = int(os.getenv("COMPUTE_UNIT_PRICE",   "500000"))  # micro-lamports/CU
SENDER_TIP_LAMPORTS:      int = int(os.getenv("SENDER_TIP_LAMPORTS",      "1000000"))  # 0.001 SOL
MIN_BUY_BUFFER_LAMPORTS:  int = int(os.getenv("MIN_BUY_BUFFER_LAMPORTS",  "20000000")) # 0.02 SOL — covers pump.fun ~50% curve fee + tip

# RPC endpoints
HELIUS_RPC_HTTP:   str = "https://mainnet.helius-rpc.com/?api-key=f3c50534-dbfe-4018-a722-4bc22358ca9c"
HELIUS_RPC_WS:     str = "wss://beta.helius-rpc.com/?api-key=f3c50534-dbfe-4018-a722-4bc22358ca9c"
HELIUS_SENDER_URL: str = "http://ams-sender.helius-rpc.com/fast"

# PumpPortal WebSocket for new token events
PUMPPORTAL_WS: str = "wss://pumpportal.fun/api/data"

# Solana constants
SOL_MINT:        str = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL:int = 1_000_000_000
