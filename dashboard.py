"""
Bloomberg-style terminal dashboard.
Runs as a background asyncio task — never blocks trading.
Refreshes every 0.5s by reading from BotState (read-only).
"""

import asyncio
import time
from datetime import datetime

from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from state import BotState
import config

REFRESH_RATE = 0.5   # seconds between redraws


# ── Colour palette ──────────────────────────────────────────────────────────
C_HEADER   = "bold white on #0a0a1a"
C_GREEN    = "bright_green"
C_RED      = "bright_red"
C_YELLOW   = "yellow"
C_CYAN     = "cyan"
C_DIM      = "dim white"
C_GOLD     = "bold yellow"
C_BLUE     = "steel_blue1"
C_WHITE    = "bold white"


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _age(ts: float) -> str:
    s = int(time.time() - ts)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m {s%60}s"
    return f"{s//3600}h {(s%3600)//60}m"


def _pnl_color(val: float) -> str:
    if val > 0:  return C_GREEN
    if val < 0:  return C_RED
    return C_DIM


def _bonding_bar(pct: float, width: int = 12) -> Text:
    """Tiny ASCII progress bar for bonding curve %."""
    filled = int((pct / 100) * width)
    bar = "█" * filled + "░" * (width - filled)
    color = C_GREEN if pct < 10 else C_YELLOW if pct < 50 else C_RED
    t = Text()
    t.append(bar, style=color)
    t.append(f" {pct:.2f}%", style=C_DIM)
    return t


# ── Header panel ─────────────────────────────────────────────────────────────

def build_header(state: BotState) -> Panel:
    pnl = state.pnl_sol
    pnl_str = f"{pnl:+.4f} SOL"
    pnl_col = _pnl_color(pnl)

    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(justify="left")
    grid.add_column(justify="center")
    grid.add_column(justify="right")

    grid.add_row(
        Text.assemble(
            ("⚡ PUMP.FUN SNIPER", "bold cyan"),
            "  ",
            (f"wallet: {config.WALLET_ADDRESS[:8]}…{config.WALLET_ADDRESS[-6:]}", C_DIM),
        ),
        Text.assemble(
            ("BUY ", C_DIM), (f"{config.BUY_AMOUNT_SOL} SOL", C_CYAN),
            ("  HOLD ", C_DIM), (f"{config.HOLD_TIME_SECONDS}s", C_CYAN),
            ("  SLIP ", C_DIM), (f"{int(config.SLIPPAGE*100)}%", C_CYAN),
        ),
        Text.assemble(
            ("uptime ", C_DIM), (state.uptime_str, C_WHITE),
            ("  ", ""),
            (datetime.now().strftime("%H:%M:%S"), C_DIM),
        ),
    )

    grid.add_row(
        Text.assemble(
            ("buys ", C_DIM), (str(state.total_buys), C_WHITE),
            ("  sells ", C_DIM), (str(state.total_sells), C_WHITE),
            ("  tracking ", C_DIM), (str(len(state.tracked)), C_CYAN),
            ("  open ", C_DIM), (str(len(state.open_positions)), C_GOLD),
        ),
        Text(""),
        Text.assemble(
            ("P&L  ", C_DIM), (pnl_str, f"bold {pnl_col}"),
            ("  spent ", C_DIM), (f"{state.total_sol_spent:.3f} SOL", C_DIM),
        ),
    )

    return Panel(grid, style="on #0a0a1a", border_style="cyan", padding=(0, 1))


# ── Active positions ──────────────────────────────────────────────────────────

def build_positions(state: BotState) -> Panel:
    tbl = Table(
        box=box.SIMPLE_HEAD,
        style="on #0d0d1f",
        header_style="bold cyan",
        show_footer=False,
        expand=True,
        pad_edge=False,
    )
    tbl.add_column("TOKEN",    style=C_WHITE,  width=14)
    tbl.add_column("BOUGHT",   style=C_DIM,    width=10)
    tbl.add_column("AGE",      style=C_YELLOW, width=7)
    tbl.add_column("SELL IN",  style=C_CYAN,   width=8)
    tbl.add_column("BONDING@BUY", width=18)
    tbl.add_column("PEAK",     style=C_DIM,    width=8)
    tbl.add_column("ORDER",    style=C_DIM,    width=20, no_wrap=True)

    for pos in state.open_positions:
        age_s = time.time() - pos.bought_at
        sell_in = max(0, config.HOLD_TIME_SECONDS - age_s)
        sell_color = C_GREEN if sell_in > 20 else C_YELLOW if sell_in > 5 else C_RED

        tbl.add_row(
            f"{pos.symbol[:10]}",
            _fmt_time(pos.bought_at),
            _age(pos.bought_at),
            Text(f"{sell_in:.0f}s", style=f"bold {sell_color}"),
            _bonding_bar(pos.bonding_at_buy),
            f"{pos.peak_bonding:.2f}%",
            pos.buy_order_id[:18] + "…",
        )

    if not state.open_positions:
        tbl.add_row(
            Text("no open positions", style=C_DIM),
            "", "", "", "", "", "",
        )

    return Panel(
        tbl,
        title="[bold cyan]● OPEN POSITIONS[/]",
        border_style="cyan",
        style="on #0d0d1f",
        padding=(0, 1),
    )


# ── Closed positions ──────────────────────────────────────────────────────────

def build_closed(state: BotState) -> Panel:
    tbl = Table(
        box=box.SIMPLE_HEAD,
        style="on #0d0d1f",
        header_style="bold cyan",
        show_footer=False,
        expand=True,
        pad_edge=False,
    )
    tbl.add_column("TOKEN",   style=C_WHITE,  width=10)
    tbl.add_column("BOUGHT",  style=C_DIM,    width=10)
    tbl.add_column("HELD",    style=C_DIM,    width=7)
    tbl.add_column("BONDING", width=18)
    tbl.add_column("STATUS",  width=8)

    for pos in state.closed_positions[:12]:
        status = Text("✓ OK", style=C_GREEN) if pos.sell_result == "ok" else Text("✗ FAIL", style=C_RED)
        tbl.add_row(
            pos.symbol[:8],
            _fmt_time(pos.bought_at),
            _age(pos.bought_at),
            _bonding_bar(pos.bonding_at_buy),
            status,
        )

    if not state.closed_positions:
        tbl.add_row(Text("no closed trades yet", style=C_DIM), "", "", "", "")

    return Panel(
        tbl,
        title="[bold cyan]◎ RECENT TRADES[/]",
        border_style="steel_blue1",
        style="on #0d0d1f",
        padding=(0, 1),
    )


# ── Token tracker ─────────────────────────────────────────────────────────────

def build_tracker(state: BotState) -> Panel:
    tbl = Table(
        box=box.SIMPLE_HEAD,
        style="on #0d0d1f",
        header_style="bold cyan",
        show_footer=False,
        expand=True,
        pad_edge=False,
    )
    tbl.add_column("TOKEN",   style=C_WHITE,  width=10)
    tbl.add_column("AGE",     style=C_DIM,    width=7)
    tbl.add_column("BONDING", width=22)
    tbl.add_column("PEAK",    style=C_DIM,    width=8)

    # Sort by most recently updated
    tokens = sorted(state.tracked.values(), key=lambda t: t.last_update, reverse=True)
    for t in tokens[:14]:
        tbl.add_row(
            t.symbol[:8] or t.mint[:8],
            _age(t.created_at),
            _bonding_bar(t.current_bonding * 100),
            f"{t.peak_bonding * 100:.3f}%",
        )

    if not tokens:
        tbl.add_row(Text("waiting for launches…", style=C_DIM), "", "", "")

    return Panel(
        tbl,
        title="[bold cyan]◈ TRACKING[/]",
        border_style="steel_blue1",
        style="on #0d0d1f",
        padding=(0, 1),
    )


# ── Event log ────────────────────────────────────────────────────────────────

KIND_STYLE = {
    "launch":  ("🚀", "cyan"),
    "signal":  ("⚡", "bold yellow"),
    "buy":     ("💰", "bold green"),
    "sell":    ("💸", "bright_green"),
    "skip":    ("–",  "dim white"),
    "error":   ("✗",  "bold red"),
    "warn":    ("⚠",  "yellow"),
}

def build_log(state: BotState) -> Panel:
    tbl = Table(
        box=None,
        style="on #0d0d1f",
        show_header=False,
        expand=True,
        pad_edge=False,
        padding=(0, 1),
    )
    tbl.add_column(width=8,  style=C_DIM)
    tbl.add_column(width=2)
    tbl.add_column(width=8,  style=C_WHITE)
    tbl.add_column(style=C_DIM)

    for ev in list(state.events)[:24]:
        icon, col = KIND_STYLE.get(ev.kind, ("·", "white"))
        tbl.add_row(
            _fmt_time(ev.ts),
            Text(icon),
            Text(ev.symbol[:8] or ev.mint[:8], style=col),
            ev.detail,
        )

    if not state.events:
        tbl.add_row("", "", "", Text("no events yet", style=C_DIM))

    return Panel(
        tbl,
        title="[bold cyan]≡ EVENT LOG[/]",
        border_style="steel_blue1",
        style="on #0d0d1f",
        padding=(0, 0),
    )


# ── Layout assembly ───────────────────────────────────────────────────────────

def build_layout(state: BotState) -> Layout:
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=5),
        Layout(name="main"),
        Layout(name="bottom"),
    )

    layout["main"].split_row(
        Layout(name="left",  ratio=3),
        Layout(name="right", ratio=2),
    )

    layout["bottom"].split_row(
        Layout(name="tracker", ratio=2),
        Layout(name="log",     ratio=3),
    )

    layout["header"].update(build_header(state))
    layout["left"].update(build_positions(state))
    layout["right"].update(build_closed(state))
    layout["tracker"].update(build_tracker(state))
    layout["log"].update(build_log(state))

    return layout


# ── Dashboard task ────────────────────────────────────────────────────────────

async def run_dashboard(state: BotState):
    """
    Background task: renders the dashboard every REFRESH_RATE seconds.
    Never blocks — uses asyncio.sleep between renders.
    """
    console = Console()
    with Live(
        build_layout(state),
        console=console,
        refresh_per_second=1 / REFRESH_RATE,
        screen=True,
    ) as live:
        while True:
            await asyncio.sleep(REFRESH_RATE)
            try:
                live.update(build_layout(state))
            except Exception:
                pass  # never let dashboard errors affect trading
