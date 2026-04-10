# noriskbot v2

High-speed pump.fun sniper bot. Detects coordinated dumps and buys the dip.

---

## How it works

pump.fun tokens launch on a bonding curve starting at **30 SOL**. At **793 SOL** they graduate to Raydium.

The bot watches for: someone buys in (vSol rises above 33), then dumps everything (vSol drops back to ~30). The bot buys the dip.

---

## Signal detection

| Feed | Source | What it does |
|------|--------|-------------|
| **Primary** | ERPC Geyser gRPC | Subscribes to all pump.fun bonding curves + transactions + slots |
| **Backup** | PumpPortal WebSocket | Trade events with vSolInBondingCurve |

**Geyser gRPC** provides:
- Account updates (bonding curve vSol/vtoken changes)
- Transaction notifications (detect sell transactions directly)
- Slot notifications (track current block)

**Signal fires when:**
- `peak_vSol - 30 SOL >= 3 SOL` — had real activity
- `vSol - 30 SOL <= 0.01 SOL` — dumped back near zero
- `token age <= 35 seconds` — still fresh

**Fast signal**: when gRPC detects a sell transaction on a tracked bonding curve, fires immediately (before account update confirms vSol drop).

---

## Buying

**Transaction:**
1. Cached blockhash (refreshed every 200ms, no durable nonce)
2. Reserves from signal feed (no extra RPC)
3. Creator vault from cached PumpPortal
4. AMM math computes expected tokens
5. Token account via `createAccountWithSeed` + `InitializeAccount3`

**Gateways (submitted in parallel):**
| Gateway | Tip | Protocol |
|---------|-----|----------|
| Astralane FR | 0.0001 SOL | HTTPS |
| Astralane AMS | 0.0001 SOL | HTTPS |
| ERPC | none (3x prio fee) | HTTPS |

**TPU direct submission (fire-and-forget):**
| Method | Target | Notes |
|--------|--------|-------|
| UDP | Leader TPU port | No handshake, instant |
| QUIC | Leader TPU port | TLS, more reliable |

TPU submits directly to the block-producing validator. Doesn't block the gather — fires in background after gateways return.

**Leader-aware submission:**
- Queries ERPC `getLeaderSlots` every 5s (cached)
- Finds the lowest-ping leader (prefers Frankfurt)
- When Frankfurt leader detected (<5ms ping): logs "FRANKFURT leader — TPU priority"
- Submits via TPU directly to the leader's IP:port

**RPC chain:**
| Use | Primary | Fallback |
|-----|---------|----------|
| HTTP RPC | Alchemy | GetBlock |
| WS feed | ERPC gRPC | — |
| Buy gateways | Astralane FR + AMS | ERPC |

---

## Selling

**Trailing stop loss** activates at **+10% gain**. Triggers sell on **3% drawdown** from peak.

**Hold timers (if trailing stop doesn't trigger):**
- Peak bonding < 2%: **40 seconds** (low market cap)
- Peak bonding >= 2%: **55 seconds** (higher market cap)

**Sell execution:**
- Both old/new layout variants submitted in parallel (handles 6024 cashback upgrade)
- Creator vault refreshed from PumpPortal at sell time
- On-chain error doesn't fail sell if SOL was received
- On-chain error doesn't fail sell if token balance is 0

---

## Configuration (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `WALLET_PRIVATE_KEY` | required | Base58 private key |
| `WALLET_ADDRESS` | required | Public key |
| `NONCE_ACCOUNT` | required | Durable nonce account (unused for buys) |
| `ERPC_API_KEY` | required | ERPC API key |
| `ASTRALANE_API_KEY` | required | Astralane API key |
| `BUY_AMOUNT_SOL` | `0.10` | SOL per buy |
| `HOLD_TIME_SECONDS` | `55` | Max hold before auto-sell |
| `TRAIL_STOP_PCT` | `3.0` | Trailing stop drawdown % |
| `TRAIL_ACTIVATE_PCT` | `10.0` | Gain % to activate trailing stop |
| `MAX_TOKEN_AGE_SECONDS` | `35` | Max token age for signal |
| `SLIPPAGE` | `0.05` | Slippage tolerance (5%) |
| `COMPUTE_UNIT_PRICE` | `100000` | Priority fee (µL/CU) |
| `COMPUTE_UNIT_LIMIT` | `94000` | CU limit |
| `SENDER_TIP_LAMPORTS` | `100000` | Astralane tip (0.0001 SOL) |

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in required keys
python3 main.py
```

Dashboard: `http://localhost:8080`

---

## Architecture

```
main.py
 ├── bot.py              — signals → buys → position tracking
 ├── pumpfun_monitor.py  — dual-feed signal detection (gRPC + PumpPortal)
 ├── geyser_feed.py      — ERPC Geyser gRPC (accounts + transactions + slots)
 ├── solana_client.py    — tx building, multi-gateway + TPU submission
 ├── position_manager.py — trailing stop + dynamic hold
 ├── config.py           — settings from .env
 ├── state.py            — positions, event log
 ├── server.py           — dashboard
 └── tpu_sender.py       — Python wrapper for Rust TPU binary
```

---

## TPU sender (Rust)

Direct UDP/QUIC submission to Solana validators. Located at `/home/ubuntu/tpu-sender/`.

```bash
# Build
cd tpu-sender && cargo build --release

# Usage
echo <base64_tx> | ./target/release/tpu-sender udp <ip> <port>
echo <base64_tx> | ./target/release/tpu-sender quic <ip> <port>
```

---

## Performance

```
Signal detection:    ~1-2ms (Geyser gRPC)
Blockhash:           ~0ms (cached)
Tx building:         ~1ms (solders)
Gateways:            ~15-25ms (AMS + ERPC parallel)
TPU direct:          ~1ms (background, fire-and-forget)
TOTAL:               ~20-30ms from dump to tx landing
```

**Buy times**: 23-30ms consistently (when nothing hangs).

---

## Key details

- **Geyser gRPC** replaces WebSocket accountSubscribe (~10x faster)
- **Transaction feed** detects sell txs directly (fast signal)
- **Slot feed** tracks current block for leader detection
- **Leader-aware** — identifies Frankfurt validators via ERPC getLeaderSlots
- **TPU direct** — submits to leader's IP:port via UDP/QUIC (fire-and-forget)
- **Dual Astralane** — FR + AMS gateways in parallel
- **3 RPC fallback** — Alchemy → GetBlock
- **Cached blockhash** — no durable nonce for buys
- **Sell 6024 handling** — checks SOL received + token balance on error
- **vtoken fallback** — rejects PumpPortal truncated floats
