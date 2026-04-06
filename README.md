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

Once a signal fires the bot builds **two local buy transaction variants** directly against the pump.fun `6EF8` program using a **dual-path submission strategy**:

**Transaction Building:**
1. Durable nonce and fresh bonding curve reserves are fetched in parallel from Helius RPC
2. AMM math computes expected tokens out from live on-chain virtual reserves
3. Token account is created with `createAccountWithSeed` + `InitializeAccount3` (cheaper than ATA, ~70k CU total)
4. Two transaction variants are built with the **same nonce**:
   - **Tx A**: High priority fee (300,000 µL/CU) + low Astralane tip (10,000 lamports) → SWQoS validators
   - **Tx B**: Low priority fee (1,000 µL/CU) + high Jito tip (1,000,000 lamports) → Jito bundle validators

**Dual-Path Submission:**
5. Both transactions are signed locally and submitted to Astralane `/iris` endpoint in parallel
6. Astralane routes Tx A through the priority-fee pipeline and Tx B through Jito simultaneously
7. Whichever transaction lands first consumes the durable nonce — the other becomes automatically invalid
8. Only one transaction can execute (we pay only once)

**Creator Vault Derivation:**
- The bot extracts the creator wallet from PumpPortal's `subscribeNewToken` event
- Creator vault PDA is derived directly from the creator wallet: `find_program_address([b"creator-vault", creator_pubkey], PUMP_PROGRAM)`
- This eliminates the need for on-chain account scanning and works reliably across all bonding curve layouts

---

### Selling

Positions are managed with a **trailing stop loss** that activates after the position reaches **+10% gain**:

1. Once activated, the bot tracks the peak value of the position
2. If the position drops **5% from its peak** (drawdown), it triggers an automatic sell
3. If the trailing stop doesn't trigger, the position auto-sells after the **hold timer** expires (default 50 seconds)

**Sell execution:**
1. Token balance is polled (up to 10s for settlement)
2. Sell tx is built using the seed-derived token account as the source (same account created during buy)
3. Submitted via Astralane `/iris` endpoint

If a sell fails, it retries up to **3 times** with a 3-second delay.

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
| `BUY_AMOUNT_SOL` | `0.18` | SOL to spend per buy |
| `HOLD_TIME_SECONDS` | `50` | Seconds to hold before auto-sell |
| `TRAIL_STOP_PCT` | `5.0` | Trailing stop loss percentage (from peak) |
| `TRAIL_ACTIVATE_PCT` | `10.0` | Gain % required to activate trailing stop |
| `MAX_TOKEN_AGE_SECONDS` | `58` | Ignore tokens older than this |
| `SLIPPAGE` | `0.25` | Slippage tolerance (25%) |
| `MAX_CONCURRENT_POSITIONS` | `10` | Max open positions at once |
| `IDEAL_HIGH_FEE_CU_PRICE` | `300000` | Tx A priority fee (µL/CU) |
| `IDEAL_LOW_TIP_LAMPORTS` | `10000` | Tx A Astralane tip |
| `IDEAL_LOW_FEE_CU_PRICE` | `1000` | Tx B priority fee (µL/CU) |
| `IDEAL_HIGH_TIP_LAMPORTS` | `1000000` | Tx B Jito tip (0.001 SOL) |
| `COMPUTE_UNIT_LIMIT` | `85000` | CU limit for buy transactions |
| `SELL_COMPUTE_UNIT_PRICE` | `100000` | Priority fee for sells (µL/CU) |
| `ASTRALANE_AMS_TIP_LAMPORTS` | `100000` | Astralane tip for sells |

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
 ├── solana_client.py    — local tx building/signing, dual-path submission via Astralane /iris
 ├── position_manager.py — tracks open positions, trailing stop loss + hold timer
 ├── config.py           — all settings from .env
 ├── state.py            — shared bot state (positions, event log)
 └── server.py           — dashboard web server
```

---

## Key implementation details

- **Durable nonce accounts**: All transactions use a durable nonce instead of recent blockhash, allowing pre-signed transactions and eliminating blockhash expiry issues
- **Dual-path submission**: Each buy submits two transaction variants simultaneously — one optimized for SWQoS validators (high priority fee), one for Jito (high tip). Whichever lands first wins.
- **Token accounts** use `createAccountWithSeed(seed=mint[:8])` instead of ATA program — saves ~18k CU per buy
- **Buy tx structure**: `[AdvanceNonceAccount, SetCULimit, SetCUPrice, createAccountWithSeed, InitializeAccount3, Buy, Tip]` — 17 accounts, ~70k CU consumed
- **Sell tx structure**: `[SetCULimit, SetCUPrice, Sell, Tip]` — 16 accounts, ~56k CU consumed
- **Fresh reserves**: bonding curve reserves are fetched on-chain at buy time (concurrent with nonce) to avoid stale AMM calculations
- **Creator vault derivation**: Extracted from PumpPortal create events and derived as PDA — no on-chain scanning required
- **Trailing stop loss**: Activates only after position reaches +10% gain, then sells on 5% drawdown from peak
- **Program filter**: tokens using `MAyhSmzX` (new pump.fun wrapper) are skipped — different account layout
