#!/usr/bin/env python3
"""Dump ARM semihosting text from the L011 over SWD using st-util.

st-util --semihosting stops on BKPT 0xAB and reports SIGTRAP. This client
emulates SYS_WRITE / SYS_WRITE0, prints the bytes, skips the breakpoint, and
resumes. No gdb-multiarch and no Nucleo UART bridges.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time

SEMIHOST_SYS_WRITE = 0x05
SEMIHOST_SYS_WRITE0 = 0x04
BKPT_AB = 0xBEAB


def gdb_checksum(body: bytes) -> int:
    """Return the GDB remote-protocol checksum for one packet body."""
    return sum(body) % 256


def gdb_send(sock: socket.socket, body: str) -> None:
    """Send one GDB RSP packet."""
    raw = body.encode("ascii")
    sock.sendall(b"$" + raw + b"#" + f"{gdb_checksum(raw):02x}".encode("ascii"))


def gdb_read_packet(sock: socket.socket, timeout: float) -> str | None:
    """Read the next GDB packet body, ACK it, and return the payload string."""
    sock.settimeout(timeout)
    buf = bytearray()
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf.extend(chunk)
            start = buf.find(b"$")
            hash_at = buf.find(b"#", start + 1) if start >= 0 else -1
            if start >= 0 and hash_at >= 0 and len(buf) >= hash_at + 3:
                sock.sendall(b"+")
                return buf[start + 1 : hash_at].decode("ascii", errors="replace")
    except socket.timeout:
        return None


def le_u32_from_hex(word: str) -> int:
    """Decode one little-endian 32-bit register from GDB hex."""
    raw = bytes.fromhex(word)
    return int.from_bytes(raw, "little")


def le_u32_to_hex(value: int) -> str:
    """Encode one little-endian 32-bit register as GDB hex."""
    return (value & 0xFFFFFFFF).to_bytes(4, "little").hex()


def gdb_read_regs(sock: socket.socket) -> list[int]:
    """Read r0-r15 via the GDB `g` packet."""
    gdb_send(sock, "g")
    body = gdb_read_packet(sock, 2.0) or ""
    regs = []
    for i in range(0, 16 * 8, 8):
        regs.append(le_u32_from_hex(body[i : i + 8]))
    return regs


def gdb_write_reg(sock: socket.socket, regno: int, value: int) -> None:
    """Write one core register (`P` packet, regno in hex)."""
    gdb_send(sock, f"P{regno:x}={le_u32_to_hex(value)}")
    gdb_read_packet(sock, 2.0)


def gdb_read_mem(sock: socket.socket, addr: int, length: int) -> bytes:
    """Read `length` bytes from target memory."""
    gdb_send(sock, f"m{addr:x},{length:x}")
    body = gdb_read_packet(sock, 2.0) or ""
    if body.startswith("E"):
        return b""
    return bytes.fromhex(body)


def gdb_read_u32(sock: socket.socket, addr: int) -> int:
    """Read one little-endian word from target memory."""
    data = gdb_read_mem(sock, addr, 4)
    if len(data) < 4:
        return 0
    return int.from_bytes(data, "little")


def gdb_read_string(sock: socket.socket, addr: int, maxlen: int = 256) -> bytes:
    """Read a NUL-terminated C string from target memory."""
    out = bytearray()
    while len(out) < maxlen:
        chunk = gdb_read_mem(sock, addr + len(out), 32)
        if not chunk:
            break
        z = chunk.find(b"\x00")
        if z >= 0:
            out.extend(chunk[:z])
            break
        out.extend(chunk)
    return bytes(out)


def handle_semihost(sock: socket.socket) -> bool:
    """If PC is BKPT 0xAB, print SYS_WRITE* payload, skip the trap, return True."""
    regs = gdb_read_regs(sock)
    r0, r1, pc = regs[0], regs[1], regs[15]
    pc_addr = pc & ~1
    insn_bytes = gdb_read_mem(sock, pc_addr, 2)
    if len(insn_bytes) < 2:
        return False
    insn = int.from_bytes(insn_bytes, "little")
    if insn != BKPT_AB:
        return False

    if r0 == SEMIHOST_SYS_WRITE0:
        sys.stdout.buffer.write(gdb_read_string(sock, r1))
        sys.stdout.buffer.flush()
        gdb_write_reg(sock, 0, 0)
    elif r0 == SEMIHOST_SYS_WRITE:
        _fd = gdb_read_u32(sock, r1)
        ptr = gdb_read_u32(sock, r1 + 4)
        length = gdb_read_u32(sock, r1 + 8)
        sys.stdout.buffer.write(gdb_read_mem(sock, ptr, length))
        sys.stdout.buffer.flush()
        gdb_write_reg(sock, 0, 0)
    else:
        gdb_write_reg(sock, 0, 0xFFFFFFFF)

    # Thumb BKPT is 2 bytes; advance PC so the core does not re-hit it.
    gdb_write_reg(sock, 15, (pc_addr + 2) | 1)
    return True


def jump_to_flash_reset(sock: socket.socket) -> None:
    """Load SP/PC from the flash vector table.

    This L011 often comes up in system memory (BOOT0 / nBOOT1). Forcing the
    flash entry point lets us log without changing option bytes.
    """
    sp = gdb_read_u32(sock, 0x08000000)
    reset = gdb_read_u32(sock, 0x08000004)
    gdb_write_reg(sock, 13, sp)
    gdb_write_reg(sock, 15, reset | 1)
    sys.stderr.write(f"jump flash sp={sp:#x} pc={reset:#x}\n")
    sys.stderr.flush()


def pump_stutil(proc: subprocess.Popen[bytes], stop: threading.Event) -> None:
    """Copy st-util stdout (it may print SYS_WRITE itself) until stopped."""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    while not stop.is_set():
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()


def serve(sock: socket.socket, seconds: float) -> None:
    """Resume the core and service semihosting traps.

    `seconds <= 0` means run until the process is killed (live LED log).
    """
    jump_to_flash_reset(sock)
    deadline = float("inf") if seconds <= 0 else (time.time() + seconds)
    gdb_send(sock, "c")
    while time.time() < deadline:
        body = gdb_read_packet(sock, 0.3)
        if body is None:
            continue
        if body.startswith("S") or body.startswith("T"):
            handle_semihost(sock)
            gdb_send(sock, "c")


def gdb_connect(host: str, port: int, retries: int) -> socket.socket:
    """Connect to the st-util GDB server."""
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            return socket.create_connection((host, port), timeout=1.0)
        except OSError as exc:
            last_err = exc
            time.sleep(0.1)
    raise SystemExit(f"could not connect to {host}:{port}: {last_err}")


def wait_for_listen(proc: subprocess.Popen[bytes], timeout: float) -> None:
    """Block until st-util prints that it is listening."""
    deadline = time.time() + timeout
    buf = b""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    while time.time() < deadline:
        if proc.poll() is not None:
            rest = os.read(fd, 65536)
            raise SystemExit(
                "st-util exited before listening:\n"
                + (buf + rest).decode("utf-8", errors="replace")
            )
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            continue
        sys.stderr.buffer.write(chunk)
        sys.stderr.buffer.flush()
        buf += chunk
        if b"Listening at" in buf:
            return
    raise SystemExit("timed out waiting for st-util to listen")


def main() -> int:
    """Start st-util and print semihosting text for SECONDS."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds",
        type=float,
        default=8.0,
        help="How long to stream. 0 means until Ctrl-C / SIGINT.",
    )
    parser.add_argument("--port", type=int, default=4242)
    args = parser.parse_args()

    proc = subprocess.Popen(
        ["st-util", "--semihosting", "-p", str(args.port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    sock = None
    try:
        wait_for_listen(proc, timeout=5.0)
        sock = gdb_connect("127.0.0.1", args.port, retries=30)
        gdb_send(sock, "qSupported:swbreak+;hwbreak+")
        gdb_read_packet(sock, 2.0)
        gdb_send(sock, "Hc0")
        gdb_read_packet(sock, 2.0)
        gdb_send(sock, "?")
        gdb_read_packet(sock, 2.0)
        stop = threading.Event()
        pump = threading.Thread(target=pump_stutil, args=(proc, stop), daemon=True)
        pump.start()
        serve(sock, args.seconds)
        stop.set()
        pump.join(timeout=1)
        return 0
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
