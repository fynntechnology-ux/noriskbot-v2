#!/usr/bin/env python3
"""
pump.fun sniper bot — entry point.

Usage:
    cp .env.example .env
    # fill in credentials
    python main.py
"""

import asyncio
import signal
import sys

from bot import PumpSnipeBot
from server import run_server
from state import BotState
from logger import get_logger

log = get_logger("main")


async def main():
    state = BotState()
    bot   = PumpSnipeBot(state)

    loop = asyncio.get_running_loop()

    def _shutdown(sig_name):
        log.info("Received %s. Shutting down…", sig_name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig.name)

    # Run web server and bot concurrently.
    # Server is a background task — if it crashes, bot keeps running.
    server_task = asyncio.create_task(run_server(state, host="0.0.0.0", port=8080))
    bot_task    = asyncio.create_task(bot.run())

    log.info("Dashboard running at http://localhost:8080")

    try:
        await bot_task
    except asyncio.CancelledError:
        log.info("Bot stopped.")
    finally:
        server_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
