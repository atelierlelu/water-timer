#!/usr/bin/env python3
"""Stream SEGGER-compatible RTT text from the L011 over SWD using st-util.

Attaches without reset, finds the "SEGGER RTT" control block in SRAM, and
polls the first up-buffer. The firmware keeps running if nobody is listening.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import socket
import subprocess
import sys
import time

RAM_BASE = 0x20000000
RAM_SIZE = 0x800
RTT_ID = b"SEGGER RTT"
UP0_PBUFFER = 28
UP0_SIZE = 32
UP0_WROFF = 36
UP0_RDOFF = 40


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


def gdb_read_mem(sock: socket.socket, addr: int, length: int, chunk: int = 64) -> bytes:
    """Read `length` bytes from target memory in GDB chunks of `chunk` bytes."""
    out = bytearray()
    offset = 0
    while offset < length:
        n = min(chunk, length - offset)
        gdb_send(sock, f"m{addr + offset:x},{n:x}")
        body = gdb_read_packet(sock, 2.0) or ""
        if body.startswith("E") or (len(body) % 2) != 0:
            break
        try:
            piece = bytes.fromhex(body)
        except ValueError:
            break
        if not piece:
            break
        out.extend(piece)
        offset += len(piece)
    return bytes(out)


def gdb_write_mem(sock: socket.socket, addr: int, data: bytes) -> None:
    """Write bytes to target memory."""
    gdb_send(sock, f"M{addr:x},{len(data):x}:{data.hex()}")
    gdb_read_packet(sock, 2.0)


def gdb_read_u32(sock: socket.socket, addr: int) -> int:
    """Read one little-endian word from target memory."""
    data = gdb_read_mem(sock, addr, 4)
    if len(data) < 4:
        return 0
    return int.from_bytes(data, "little")


def find_rtt_cb(sock: socket.socket) -> int:
    """Return the SRAM address of the RTT control block, or 0 if missing."""
    step = 64
    overlap = len(RTT_ID) - 1
    prev = b""
    addr = RAM_BASE
    while addr < RAM_BASE + RAM_SIZE:
        chunk = gdb_read_mem(sock, addr, step)
        if not chunk:
            addr += step
            continue
        hay = prev + chunk
        idx = hay.find(RTT_ID)
        if idx >= 0:
            return addr - len(prev) + idx
        prev = hay[-overlap:] if len(hay) >= overlap else hay
        addr += len(chunk)
    return 0


def ring_bytes(buf: bytes, start: int, end: int) -> bytes:
    """Copy `[start, end)` from a ring buffer, wrapping if needed."""
    if start == end:
        return b""
    if end > start:
        return buf[start:end]
    return buf[start:] + buf[:end]


def poll_up0(sock: socket.socket, cb: int) -> bytes:
    """Read newly written up-buffer bytes and advance the host read offset."""
    size = gdb_read_u32(sock, cb + UP0_SIZE)
    wr = gdb_read_u32(sock, cb + UP0_WROFF)
    rd = gdb_read_u32(sock, cb + UP0_RDOFF)
    ptr = gdb_read_u32(sock, cb + UP0_PBUFFER)
    if size == 0 or size > 4096 or wr >= size or rd >= size or ptr == 0:
        return b""
    raw = gdb_read_mem(sock, ptr, size, chunk=size)
    wr2 = gdb_read_u32(sock, cb + UP0_WROFF)
    if len(raw) < size or wr2 != wr:
        return b""
    out = ring_bytes(raw, rd, wr)
    if out:
        gdb_write_mem(sock, cb + UP0_RDOFF, wr.to_bytes(4, "little"))
    return out


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


def halt_from_run(sock: socket.socket) -> None:
    """Interrupt a running core and wait for the stop reply."""
    sock.sendall(b"\x03")
    deadline = time.time() + 1.0
    while time.time() < deadline:
        body = gdb_read_packet(sock, 0.2)
        if body is None:
            continue
        if body.startswith("S") or body.startswith("T"):
            return


def serve(sock: socket.socket, seconds: float) -> None:
    """Resume the core and poll the RTT up-buffer (0 seconds = forever)."""
    deadline = float("inf") if seconds <= 0 else (time.time() + seconds)
    cb = find_rtt_cb(sock)
    tries = 0
    while cb == 0 and tries < 20:
        gdb_send(sock, "c")
        gdb_read_packet(sock, 0.05)
        time.sleep(0.2)
        halt_from_run(sock)
        cb = find_rtt_cb(sock)
        tries += 1
    if cb == 0:
        raise SystemExit(
            "no RTT control block in SRAM; is the flashed image running?"
        )
    sys.stderr.write(f"rtt cb={cb:#x}\n")
    sys.stderr.flush()
    data = poll_up0(sock, cb)
    if data:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    gdb_send(sock, "c")
    gdb_read_packet(sock, 0.05)
    while time.time() < deadline:
        time.sleep(0.15)
        halt_from_run(sock)
        data = poll_up0(sock, cb)
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        gdb_send(sock, "c")
        gdb_read_packet(sock, 0.05)


def main() -> int:
    """Start st-util without reset and stream RTT text."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="How long to stream. 0 means until Ctrl-C / SIGINT.",
    )
    parser.add_argument("--port", type=int, default=4242)
    args = parser.parse_args()

    proc = subprocess.Popen(
        ["st-util", "--no-reset", "-p", str(args.port)],
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
        serve(sock, args.seconds)
        return 0
    finally:
        if sock is not None:
            try:
                gdb_send(sock, "D")
                gdb_read_packet(sock, 0.5)
            except OSError:
                pass
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
