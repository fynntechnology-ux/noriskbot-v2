# noriskbot

A high-speed pump.fun sniper bot that detects coordinated dumps and buys back the dip before anyone else.

---

## How it works

### The strategy

pump.fun tokens launch on a bonding curve. When a token is created, the virtual SOL in the bonding curve starts at **30 SOL**. As people buy, it rises. At **793 SOL** the token graduates to Raydium.

The bot watches for a specific pattern:
1. A new token launches and someone buys in — vSol rises above **33 SOL** (30 + 3 SOL minimum activity)
2. The buyer then sells everything, dumping vSol back down to near **30.01 SOL**
3. The bot detects this and immediately buys — catching the token at near-zero bonding curve progress

The idea: the token had genuine buy interest, then got dumped. The bot re-enters at the bottom right as it resets, before other bots react.

---

### Signal detection

Two data feeds run in parallel — whichever fires first wins:

| Feed | Source | What it does |
|------|--------|-------------|
| **Primary** | Helius Gatekeeper (`accountSubscribe`) | Subscribes directly to the bonding curve account on-chain. Gets raw account data the moment a transaction lands. |
| **Backup** | PumpPortal WebSocket (`subscribeTokenTrade`) | Receives trade events with `vSolInBondingCurve`. Slightly slower but reliable fallback. |

New token discovery happens via **PumpPortal WebSocket** (`subscribeNewToken`). When a new token is detected, both feeds subscribe immediately. Tokens on the `MAyhSmzX` program (different bonding curve) are automatically skipped.

**Signal fires when all three conditions are true:**
- `peak_vSol - 30 SOL >= 3 SOL` — token had real activity
- `vSol - 30 SOL <= 0.01 SOL` — bonding curve is back near zero
- `token age <= 60 seconds` — token is still fresh

---

### Buying

Once a signal fires the bot builds a **local buy transaction** directly against the pump.fun `6EF8` program (no PumpPortal API call in the hot path):

1. Blockhash and fresh bonding curve reserves are fetched in parallel from Helius Gatekeeper
2. AMM math computes expected tokens out from live on-chain virtual reserves
3. Token account is created with `createAccountWithSeed` + `InitializeAccount3` (cheaper than ATA, ~70k CU total)
4. Transaction is signed and submitted via **Helius Sender** (`/fast` endpoint) and Helius RPC in parallel — first response wins
5. A 1,000,000 lamport tip is included in the tx for Helius Sender priority

---

### Selling

After **30 seconds** (configurable), the position is automatically sold via a **local sell transaction** directly against `6EF8`:

1. Token balance is polled (up to 10s for settlement)
2. Sell tx is built using the seed-derived token account as the source (same account created during buy)
3. Submitted via Helius Gatekeeper RPC (no tip required — speed not critical for sells)

If a sell fails, it retries up to **3 times** with a 3-second delay.

---

## Configuration

All settings are in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `WALLET_PRIVATE_KEY` | required | Base58 wallet private key |
| `WALLET_ADDRESS` | required | Wallet public key |
| `BUY_AMOUNT_SOL` | `0.06` | SOL to spend per buy |
| `HOLD_TIME_SECONDS` | `30` | Seconds to hold before auto-sell |
| `MAX_TOKEN_AGE_SECONDS` | `60` | Ignore tokens older than this |
| `SLIPPAGE` | `0.10` | Slippage tolerance (10%) |
| `MAX_CONCURRENT_POSITIONS` | `5` | Max open positions at once |
| `COMPUTE_UNIT_PRICE` | `20000` | Priority fee micro-lamports/CU (buys) |
| `COMPUTE_UNIT_LIMIT` | `85000` | CU limit for buy transactions |
| `SELL_COMPUTE_UNIT_PRICE` | `100000` | Priority fee micro-lamports/CU (sells) |
| `SENDER_TIP_LAMPORTS` | `1000000` | Helius Sender tip per buy (0.001 SOL) |

---

## Setup

```bash
pip install -r requirements.txt

cp .env.example .env
# fill in WALLET_PRIVATE_KEY and WALLET_ADDRESS

python3 main.py
```

Dashboard available at `http://localhost:8080`

---

## Architecture

```
main.py
 ├── bot.py              — orchestrates signals → buys → position tracking
 ├── pumpfun_monitor.py  — dual-feed signal detection (PumpPortal + Helius)
 ├── helius_feed.py      — Helius Gatekeeper accountSubscribe WebSocket
 ├── solana_client.py    — local tx building/signing, Helius Sender + RPC submission
 ├── position_manager.py — tracks open positions, fires auto-sell timer
 ├── config.py           — all settings from .env
 ├── state.py            — shared bot state (positions, event log)
 └── server.py           — dashboard web server
```

---

## Key implementation details

- **Token accounts** use `createAccountWithSeed(seed=mint[:8])` instead of ATA program — saves ~18k CU per buy
- **Buy tx structure**: `[SetCULimit, SetCUPrice, createAccountWithSeed, InitializeAccount3, Buy, Tip]` — 17 accounts, ~70k CU consumed
- **Sell tx structure**: `[SetCULimit, SetCUPrice, Sell]` — 16 accounts, ~56k CU consumed
- **Fresh reserves**: bonding curve reserves are fetched on-chain at buy time (concurrent with blockhash) to avoid stale AMM calculations from PumpPortal
- **Program filter**: tokens using `MAyhSmzX` (new pump.fun wrapper) are skipped — different account layout
