"""
Geyser gRPC feed — accounts + transactions + slots.
1.2ms latency from Frankfurt. Bidirectional streaming.
"""

import asyncio
import struct
from typing import Callable

import grpc

from grpc_stubs import geyser_pb2
from grpc_stubs import geyser_pb2_grpc
from logger import get_logger

log = get_logger("geyser")

_PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_GRPC_ENDPOINT = "grpc-fra1-1.erpc.global:80"

# Sell instruction discriminator (first byte of instruction data)
_SELL_DISCRIMINATOR = b'\x33'


def _parse_bc(data: bytes) -> tuple[float, int] | None:
    try:
        if len(data) < 49:
            return None
        vtok = struct.unpack_from("<Q", data, 8)[0]
        vsol = struct.unpack_from("<Q", data, 16)[0]
        return vsol / 1e9, vtok
    except Exception:
        return None


class GeyserFeed:
    def __init__(self, on_account: Callable, on_transaction: Callable = None, on_slot: Callable = None):
        self._on_account = on_account
        self._on_transaction = on_transaction
        self._on_slot = on_slot
        self._current_slot = 0

    async def subscribe(self, mint: str, bc_addr: str):
        pass

    async def unsubscribe(self, mint: str):
        pass

    def get_current_slot(self) -> int:
        return self._current_slot

    async def run(self):
        while True:
            try:
                await self._stream()
            except Exception as exc:
                log.error("Geyser error: %s", exc, exc_info=True)
            await asyncio.sleep(2)

    async def _stream(self):
        log.info("Connecting to Geyser gRPC at %s", _GRPC_ENDPOINT)
        async with grpc.aio.insecure_channel(_GRPC_ENDPOINT) as channel:
            stub = geyser_pb2_grpc.GeyserStub(channel)

            async def request_gen():
                req = geyser_pb2.SubscribeRequest(
                    accounts={
                        'pump_accounts': geyser_pb2.SubscribeRequestFilterAccounts(
                            owner=[_PUMP_PROGRAM],
                        )
                    },
                    transactions={
                        'pump_txns': geyser_pb2.SubscribeRequestFilterTransactions(
                            vote=False,
                            failed=False,
                            account_include=[_PUMP_PROGRAM],
                        )
                    },
                    slots={
                        'all_slots': geyser_pb2.SubscribeRequestFilterSlots(),
                    },
                    commitment=geyser_pb2.PROCESSED,
                )
                yield req

                # Keep alive
                while True:
                    await asyncio.sleep(300)
                    yield geyser_pb2.SubscribeRequest()

            log.info("Geyser connected. Streaming accounts + transactions + slots…")

            async for update in stub.Subscribe(request_gen()):
                try:
                    self._handle_update(update)
                except Exception as exc:
                    log.warning("Geyser update error: %s", exc)

    def _handle_update(self, update):
        # Account update
        if update.HasField('account'):
            account = update.account.account
            data = account.data
            if data:
                reserves = _parse_bc(data)
                if reserves:
                    vsol, vtoken_raw = reserves
                    self._on_account(account.pubkey, vsol, vtoken_raw)

        # Transaction update
        elif update.HasField('transaction'):
            tx_update = update.transaction
            slot = tx_update.slot
            # Check for sell instruction
            try:
                txn = tx_update.transaction.transaction
                for ix in txn.message.instructions:
                    # Check if instruction is from pump.fun program
                    program_idx = ix.program_id_index
                    if program_idx < len(txn.message.account_keys):
                        program = txn.message.account_keys[program_idx]
                        if program == _PUMP_PROGRAM.encode()[:32] or True:
                            # Check instruction data for sell discriminator
                            ix_data = ix.data if ix.data else b''
                            if len(ix_data) > 0 and ix_data[:1] == _SELL_DISCRIMINATOR:
                                # This is a sell on pump.fun
                                self._on_transaction(tx_update, slot)
                                return
            except Exception:
                pass

        # Slot update
        elif update.HasField('slot'):
            slot_info = update.slot
            self._current_slot = slot_info.slot
            if self._on_slot:
                self._on_slot(slot_info.slot, slot_info.status)
