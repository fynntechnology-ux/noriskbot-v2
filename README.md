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
| **Primary** | Helius Gatekeeper RPC (`accountSubscribe`) | Subscribes directly to the bonding curve account on-chain. Gets raw account data updates the moment a transaction lands. |
| **Backup** | PumpPortal WebSocket (`subscribeTokenTrade`) | Receives trade events with `vSolInBondingCurve` field. Slightly slower but reliable fallback. |

New token discovery happens via **PumpPortal WebSocket** (`subscribeNewToken`). When a new token is detected, both feeds subscribe to it immediately.

**Signal fires when all three conditions are true:**
- `peak_vSol - 30 SOL >= 3 SOL` — token had real activity (not a dead launch)
- `vSol - 30 SOL <= 0.01 SOL` — bonding curve is back near zero (everyone sold)
- `token age <= 60 seconds` — token is still fresh (not a delayed dump)

---

### Buying

Once a signal fires:
1. The bot calls **PumpPortal's local trade API** to get an unsigned versioned transaction (v0) correctly structured for pump.fun's current on-chain program
2. The transaction is signed locally with the wallet private key
3. It is submitted simultaneously to:
   - **Helius Gatekeeper RPC** — broadcasts to all validators
   - **All 6 Jito block engine regions** (mainnet, Amsterdam, Frankfurt, NY, SLC, Tokyo) — sends a 2-transaction bundle (buy tx + SOL tip to Jito tip account) for priority inclusion

The bot holds a maximum of **3 positions** at once. If 3 are already open, new signals are ignored.

---

### Selling

After **60 seconds** (configurable), the position is automatically sold:
1. The current token balance is fetched from the wallet's associated token account
2. A sell transaction is built via PumpPortal and signed locally
3. Submitted to Helius + all Jito regions the same way as the buy

If a sell fails, it retries up to **3 times** with a 3-second delay between attempts.

---

## Configuration

All settings are in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `WALLET_PRIVATE_KEY` | required | Base58 wallet private key |
| `WALLET_ADDRESS` | required | Wallet public key |
| `BUY_AMOUNT_SOL` | `0.03` | How much SOL to spend per buy |
| `HOLD_TIME_SECONDS` | `60` | How long to hold before auto-sell |
| `MAX_TOKEN_AGE_SECONDS` | `60` | Ignore tokens older than this |
| `SLIPPAGE` | `0.5` | Slippage tolerance (50%) |
| `MAX_CONCURRENT_POSITIONS` | `3` | Max open positions at once |
| `COMPUTE_UNIT_PRICE` | `500000` | Priority fee in micro-lamports/CU |
| `JITO_TIP_LAMPORTS` | `100000` | Jito tip per transaction (0.0001 SOL) |
| `USE_JITO` | `true` | Enable/disable Jito bundle submission |

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
 ├── solana_client.py    — transaction building, signing, Helius + Jito submission
 ├── position_manager.py — tracks open positions, fires auto-sell timer
 ├── config.py           — all settings from .env
 ├── state.py            — shared bot state (positions, event log)
 └── server.py           — dashboard web server
```
