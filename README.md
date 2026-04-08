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

---

### Signal detection

Two data feeds run in parallel — whichever fires first wins:

| Feed | Source | What it does |
|------|--------|-------------|
| **Primary** | Helius Gatekeeper (`accountSubscribe`) | Subscribes directly to the bonding curve account on-chain. Gets raw account data the moment a transaction lands. |
| **Backup** | PumpPortal WebSocket (`subscribeTokenTrade`) | Receives trade events with `vSolInBondingCurve`. Slightly slower but reliable fallback. |

**Signal fires when all three conditions are true:**
- `peak_vSol - 30 SOL >= 3 SOL` — token had real activity
- `vSol - 30 SOL <= 0.01 SOL` — bonding curve is back near zero
- `token age <= 58 seconds` — token is still fresh

---

### Buying

Once a signal fires the bot builds **multiple local buy transaction variants** and submits to **6 gateways in parallel**:

**Transaction Building:**
1. Durable nonce is acquired atomically (ERPC for lowest latency)
2. Reserves come from the signal feed (no extra RPC)
3. Creator vault comes from cached prefetch (no extra RPC)
4. AMM math computes expected tokens out from on-chain virtual reserves
5. Token account is created with `createAccountWithSeed` + `InitializeAccount3`
6. Multiple tx variants built with different tip recipients

**Multi-Gateway Submission (6 paths):**
| Gateway | Tip | Protocol |
|---------|-----|----------|
| Astralane AMS | 0.001 SOL | HTTPS |
| Helius AMS fast | 0.0006 SOL | HTTP |
| GetBlock | 0.0006 SOL | HTTPS |
| 0xslot DE1 (OVH) | 0.0015 SOL | HTTP |
| 0xslot DE2 (TSW) | 0.0015 SOL | HTTP |
| ERPC | 0.0006 SOL | HTTPS |

All 6 gateways fire in parallel. Whichever lands first wins. The nonce is consumed by the first successful tx — the others become automatically invalid.

**Creator Vault Derivation:**
- Cached from PumpPortal `static[4]` during prefetch (fast, correct)
- Verified via on-chain PDA derivation as fallback

---

### Selling

Positions are managed with a **trailing stop loss** that activates after the position reaches **+10% gain**:

1. Once activated, the bot tracks the peak value of the position
2. If the position drops **2% from its peak** (drawdown), it triggers an automatic sell
3. If the trailing stop doesn't trigger, the position auto-sells after the **hold timer** expires (default 40 seconds)

**Sell execution:**
1. Token balance is polled (up to 10s for settlement)
2. Both old/new layout variants submitted in parallel (handles cashback upgrade)
3. On-chain error doesn't fail the sell if SOL was received
4. Retries up to 3 times

---

### On-chain vault verification

The bot uses `_derive_creator_vault` via Alchemy RPC to verify the creator vault at prefetch time. Reads creator pubkey from bonding curve account at bytes [49:81], derives PDA locally.

---

## Configuration

All settings are in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `WALLET_PRIVATE_KEY` | required | Base58 wallet private key |
| `WALLET_ADDRESS` | required | Wallet public key |
| `NONCE_ACCOUNT` | required | Durable nonce account address |
| `HELIUS_API_KEY` | required | Helius API key |
| `ASTRALANE_API_KEY` | required | Astralane API key |
| `BUY_AMOUNT_SOL` | `0.15` | SOL to spend per buy |
| `HOLD_TIME_SECONDS` | `40` | Seconds to hold before auto-sell |
| `TRAIL_STOP_PCT` | `2.0` | Trailing stop loss percentage (from peak) |
| `TRAIL_ACTIVATE_PCT` | `10.0` | Gain % required to activate trailing stop |
| `MAX_TOKEN_AGE_SECONDS` | `58` | Ignore tokens older than this |
| `SLIPPAGE` | `0.05` | Slippage tolerance (5%) |
| `MAX_CONCURRENT_POSITIONS` | `9999` | Max open positions at once |
| `COMPUTE_UNIT_PRICE` | `30000` | Priority fee (µL/CU) |
| `COMPUTE_UNIT_LIMIT` | `94000` | CU limit for buy transactions |
| `SENDER_TIP_LAMPORTS` | `1000000` | Astralane AMS tip (0.001 SOL) |

---

## Setup

```bash
pip install -r requirements.txt

cp .env.example .env
# fill in WALLET_PRIVATE_KEY, WALLET_ADDRESS, NONCE_ACCOUNT, HELIUS_API_KEY, ASTRALANE_API_KEY

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
 ├── solana_client.py    — local tx building, multi-gateway submission
 ├── position_manager.py — trailing stop loss + hold timer
 ├── config.py           — all settings from .env
 ├── state.py            — shared bot state (positions, event log, gateway stats)
 └── server.py           — dashboard web server
```

---

## Key implementation details

- **Durable nonce accounts**: All transactions use a durable nonce instead of recent blockhash, eliminating blockhash expiry issues
- **Multi-gateway submission**: Each buy is submitted to 6 gateways in parallel — Astralane AMS, Helius AMS, GetBlock, 0xslot DE1, 0xslot DE2, ERPC
- **Atomic nonce allocation**: Nonce is consumed on read — same nonce can never be handed to two buys
- **Token accounts**: Use `createAccountWithSeed(seed=mint[:8])` instead of ATA program — saves ~18k CU per buy
- **Signal reserves**: Reserves come from the signal feed (Helius/PumpPortal) — no extra RPC at buy time
- **Cached vault**: Creator vault cached from prefetch — no extra RPC at buy time
- **vtoken fallback**: When Helius is dead, uses known pump.fun initial vtoken (10^15) to prevent truncated float issues from PumpPortal
- **Sell layout retry**: Both old/new layout variants submitted in parallel — handles Feb 2026 cashback upgrade
- **Sell error recovery**: On-chain error doesn't fail the sell if SOL was received back
- **WebSocket watchdogs**: Both PumpPortal and Helius feeds monitored — auto-reconnect if silent for 60s
- **Hard timeouts**: Nonce (3s), buy gather (3s), PumpPortal calls (3s) — prevents infinite hangs
- **Gateway tracking**: Dashboard shows per-gateway landing counts
