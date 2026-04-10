"""
TPU sender — direct UDP/QUIC submission to Solana validator.
Calls the Rust tpu-sender binary via subprocess.
"""

import asyncio
import base64
import logging

log = logging.getLogger("tpu")

TPU_SENDER_BIN = "/home/ubuntu/tpu-sender/target/release/tpu-sender"


async def send_via_tpu(tx_bytes: bytes, tpu_ip: str, tpu_port: int, mode: str = "udp") -> str | None:
    """Submit tx directly to validator TPU via UDP or QUIC."""
    try:
        tx_b64 = base64.b64encode(tx_bytes).decode()
        proc = await asyncio.create_subprocess_exec(
            TPU_SENDER_BIN, mode, tpu_ip, str(tpu_port),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=tx_b64.encode()),
            timeout=1.0,
        )
        if proc.returncode == 0:
            result = stdout.decode().strip()
            log.debug("TPU %s sent to %s:%d — %s", mode, tpu_ip, tpu_port, result)
            return result
        else:
            err = stderr.decode().strip()
            log.debug("TPU %s failed: %s", mode, err)
            return None
    except asyncio.TimeoutError:
        log.debug("TPU %s timeout for %s:%d", mode, tpu_ip, tpu_port)
        return None
    except Exception as exc:
        log.debug("TPU error: %s", exc)
        return None
