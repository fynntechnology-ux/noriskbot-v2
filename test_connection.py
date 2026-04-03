"""
Connection test — no trades placed.
Checks: GMGN auth, token info endpoint, WebSocket stream.
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

async def main():
    from gmgn_client import GMGNClient
    import websockets, json, config

    print("\n=== 1. GMGN API — user info ===")
    client = GMGNClient()
    try:
        data = await client._normal_get("/v1/user/info", {})
        print("  OK:", data)
    except Exception as e:
        print("  FAIL:", e)

    print("\n=== 2. GMGN API — token info (known pump.fun token) ===")
    # Using a well-known pump.fun token for the test
    TEST_TOKEN = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # BONK
    try:
        info = await client.get_token_info(TEST_TOKEN)
        print("  OK — keys returned:", list(info.keys()) if isinstance(info, dict) else info)
    except Exception as e:
        print("  FAIL:", e)

    print("\n=== 3. GMGN API — wallet token balance ===")
    try:
        bal = await client.get_wallet_token_balance(TEST_TOKEN)
        print(f"  OK — balance: {bal}")
    except Exception as e:
        print("  FAIL:", e)

    print("\n=== 4. PumpPortal WebSocket — waiting for 1 new token event ===")
    try:
        async with websockets.connect(
            config.PUMPPORTAL_WS,
            ping_interval=20,
            open_timeout=10,
        ) as ws:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            print("  Connected. Waiting for first event (up to 30s)…")
            ws.max_size = 2**20
            msg = await asyncio.wait_for(ws.recv(), timeout=30)
            parsed = json.loads(msg)
            print("  OK — received event:", list(parsed.keys()) if isinstance(parsed, dict) else str(parsed)[:100])
    except asyncio.TimeoutError:
        print("  OK (connected but no new token in 30s — market may be quiet)")
    except Exception as e:
        print("  FAIL:", e)

    await client.close()
    print("\n=== Done ===\n")

asyncio.run(main())
