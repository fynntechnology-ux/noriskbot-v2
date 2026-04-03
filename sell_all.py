"""
One-shot: sell every token in the wallet (except SOL/USDC).
Run while bot is stopped or alongside it.
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

SKIP_SYMBOLS = {"SOL", "USDC", "USDT"}
SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",
    "So11111111111111111111111111111111111111111",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}

async def main():
    from gmgn_client import GMGNClient
    import config

    client = GMGNClient()

    print(f"\nFetching holdings for {config.WALLET_ADDRESS}…")

    data = await client._normal_get("/v1/user/wallet_holdings", {
        "chain": "sol",
        "wallet_address": config.WALLET_ADDRESS,
    })

    holdings = data if isinstance(data, list) else data.get("holdings", [])

    to_sell = []
    for h in holdings:
        token   = h.get("token", {})
        mint    = token.get("address", "")
        symbol  = token.get("symbol", "?")
        balance = float(h.get("balance") or 0)
        usd_val = float(h.get("usd_value") or 0)

        if mint in SKIP_MINTS or symbol in SKIP_SYMBOLS:
            continue
        if balance <= 0:
            continue

        to_sell.append((mint, symbol, balance, usd_val))

    if not to_sell:
        print("No positions to sell.\n")
        await client.close()
        return

    print(f"\nFound {len(to_sell)} position(s) to sell:\n")
    for mint, symbol, balance, usd in to_sell:
        print(f"  {symbol:10s}  balance={balance:.0f}  ~${usd:.2f}  {mint}")

    print()
    for mint, symbol, balance, usd in to_sell:
        try:
            print(f"Selling {symbol} ({mint[:12]}…)…", end=" ", flush=True)
            order_id = await client.sell_all(mint)
            result   = await client.wait_for_order(order_id, label="SELL")
            print(f"✓  sig={result.get('hash','?')[:16]}…")
        except Exception as e:
            print(f"✗  FAILED: {e}")

    print("\nDone.\n")
    await client.close()

asyncio.run(main())
