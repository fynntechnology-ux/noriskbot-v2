"""
FastAPI dashboard server.
Runs in the same asyncio loop as the bot — shares BotState directly.
Trading is never blocked: the server only reads state and pushes JSON.
"""

import asyncio
import json
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

import config
from state import BotState

DASHBOARD_HTML = Path(__file__).parent / "templates" / "index.html"
PUSH_INTERVAL  = 0.5   # seconds between WebSocket pushes


def create_app(state: BotState) -> FastAPI:
    app = FastAPI()

    # ── Serve dashboard ───────────────────────────────────────────────
    @app.get("/")
    async def index():
        return HTMLResponse(DASHBOARD_HTML.read_text())

    # ── WebSocket: push state every PUSH_INTERVAL ─────────────────────
    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.send_text(json.dumps(serialize(state)))
                await asyncio.sleep(PUSH_INTERVAL)
        except (WebSocketDisconnect, Exception):
            pass

    return app


def serialize(state: BotState) -> dict:
    now = time.time()
    return {
        "uptime":            state.uptime_str,
        "total_buys":        state.total_buys,
        "total_sells":       state.total_sells,
        "pnl_sol":           round(state.pnl_sol, 6),
        "sol_spent":         round(state.total_sol_spent, 4),
        "sol_returned":      round(state.total_sol_returned, 4),
        "sell_failures":     state.sell_failures,
        "open_count":        len(state.open_positions),
        "tracking_count":    len(state.tracked),
        "gateway_hits":      state.gateway_hits,

        "open_positions": [
            {
                "mint":          p.mint,
                "symbol":        p.symbol,
                "name":          p.name,
                "bought_at":     p.bought_at,
                "age_s":         round(now - p.bought_at, 1),
                "sell_in_s":     max(0, round(config.HOLD_TIME_SECONDS - (now - p.bought_at), 1)),
                "sol_spent":     p.sol_spent,
                "bonding_at_buy": round(p.bonding_at_buy, 4),
                "peak_bonding":  round(p.peak_bonding, 4),
                "order_id":      p.buy_order_id,
            }
            for p in state.open_positions
        ],

        "closed_positions": [
            {
                "mint":          p.mint,
                "symbol":        p.symbol,
                "name":          p.name,
                "bought_at":     p.bought_at,
                "age_s":         round(now - p.bought_at, 1),
                "sol_spent":     p.sol_spent,
                "bonding_at_buy": round(p.bonding_at_buy, 4),
                "peak_bonding":  round(p.peak_bonding, 4),
                "sell_result":   p.sell_result,
            }
            for p in state.closed_positions[:30]
        ],

        "tracking": [
            {
                "mint":            t.mint,
                "symbol":          t.symbol,
                "name":            t.name,
                "age_s":           round(now - t.created_at, 1),
                "current_bonding": round(t.current_bonding * 100, 4),
                "peak_bonding":    round(t.peak_bonding * 100, 4),
            }
            for t in sorted(state.tracked.values(),
                            key=lambda x: x.last_update, reverse=True)[:30]
        ],

        "events": [
            {
                "ts":     round(ev.ts, 3),
                "kind":   ev.kind,
                "symbol": ev.symbol,
                "mint":   ev.mint,
                "detail": ev.detail,
            }
            for ev in list(state.events)[:60]
        ],
    }


async def run_server(state: BotState, host: str = "0.0.0.0", port: int = 8080):
    """Run uvicorn in the existing asyncio event loop (non-blocking)."""
    app = create_app(state)
    cfg = uvicorn.Config(app, host=host, port=port,
                         log_level="warning", loop="none")
    server = uvicorn.Server(cfg)
    await server.serve()
