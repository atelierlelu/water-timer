#!/usr/bin/env python3
"""Drive a Nordic Power Profiler Kit II from the command line.

The PPK2 samples at 100 kHz and sends 4-byte words in blocks of 512. If the
USB host does not take a block, the firmware drops it. A 6-bit counter inside
each word hides that drop, because 512 is a multiple of 64. Judge a capture by
samples per second, not by a gap-free counter.

On this bench the AMD 600-series chipset USB controller (1022:43f7) only
delivers about 30k samples/s. The red CPU USB port (1022:15b9) delivers the
full 100 kHz. This script uses the kernel CDC driver, which is
fast enough on the good port. It does not enable VOUT except during capture,
and capture always turns the output off before it returns.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time

try:
    import serial
except ImportError:
    sys.stderr.write("python3-serial is not installed (apt install python3-serial)\n")
    raise SystemExit(1)

# if01 is the CDC port that accepts commands and carries the sample stream.
# if03 stays quiet.
PORT_GLOB = "/dev/serial/by-id/usb-Nordic_Semiconductor_PPK2_*-if01"

# Host controllers measured on this machine. The chipset drops sample blocks.
CHIPSET_USB = "1022:43f7"
CPU_USB = "1022:15b9"

# Firmware ADC rate. A capture below this fraction is missing blocks.
FULL_SPS = 100_000
MIN_SPS_FRACTION = 0.90

# Source-mode voltage limits from the Power Profiler app, in millivolts.
MV_MIN = 800
MV_MAX = 5000

# Sample word layout. ADC is 14 bits, then scaled by 4 to match the app.
ADC_MASK = 0x3FFF
RANGE_SHIFT = 14
COUNTER_SHIFT = 18
COUNTER_MOD = 64
ADC_MULT = 1.8 / 163840

# Command bytes. Same codes the nRF Connect Power Profiler app sends.
CMD_AVG_START = 6
CMD_AVG_STOP = 7
CMD_DEVICE_RUNNING = 12
CMD_REGULATOR = 13
CMD_POWER_MODE = 17
CMD_GET_METADATA = 25
MODE_AMPERE = 1
MODE_SOURCE = 2

# Factory modifiers, replaced by the metadata block when the device answers.
DEFAULT_MODS = {
    "r": [1031.64, 101.65, 10.15, 0.94, 0.043],
    "gs": [1.0, 1.0, 1.0, 1.0, 1.0],
    "gi": [1.0, 1.0, 1.0, 1.0, 1.0],
    "o": [0.0, 0.0, 0.0, 0.0, 0.0],
    "s": [0.0, 0.0, 0.0, 0.0, 0.0],
    "i": [0.0, 0.0, 0.0, 0.0, 0.0],
    "ug": [1.0, 1.0, 1.0, 1.0, 1.0],
}


def find_port() -> str:
    """Return the PPK2 command port (by-id if01)."""
    matches = sorted(glob.glob(PORT_GLOB))
    if not matches:
        raise SystemExit(
            "no PPK2 command port (usb Nordic PPK2 *-if01). Is it plugged in?"
        )
    if len(matches) > 1:
        raise SystemExit("more than one PPK2 is plugged in; pass --port")
    return matches[0]


def tty_name(port: str) -> str:
    """Return the ttyACM name a by-id symlink points at."""
    return os.path.basename(os.path.realpath(port))


def usb_device_dir(tty: str) -> str | None:
    """Walk from a tty up to the USB device directory that has idVendor."""
    cur = os.path.realpath(f"/sys/class/tty/{tty}/device")
    while cur != "/":
        if os.path.isfile(os.path.join(cur, "idVendor")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def pci_id(usb_dir: str) -> tuple[str, str] | None:
    """Return (slot, vendor:device) of the USB controller behind this device."""
    cur = usb_dir
    while cur != "/":
        vendor_path = os.path.join(cur, "vendor")
        device_path = os.path.join(cur, "device")
        if os.path.isfile(vendor_path) and os.path.isfile(device_path):
            vendor = read_sysfs(vendor_path)
            device = read_sysfs(device_path)
            if len(vendor) == 6 and vendor.startswith("0x"):
                ident = f"{vendor[2:]}:{device[2:]}"
                return os.path.basename(cur), ident
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def read_sysfs(path: str) -> str:
    """Read one sysfs file, or return an empty string if it is missing."""
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def port_info(port: str) -> str:
    """Describe the tty, link speed, and USB controller for a PPK2 port."""
    tty = tty_name(port)
    lines = [f"port {port} -> {tty}"]
    usb_dir = usb_device_dir(tty)
    if usb_dir is None:
        return "\n".join(lines)
    speed = read_sysfs(os.path.join(usb_dir, "speed"))
    product = read_sysfs(os.path.join(usb_dir, "product"))
    serial = read_sysfs(os.path.join(usb_dir, "serial"))
    lines.append(f"device {product} serial {serial} speed {speed} Mbps")
    pci = pci_id(usb_dir)
    if pci is None:
        return "\n".join(lines)
    slot, ident = pci
    lines.append(f"controller {slot} {ident}")
    if ident == CHIPSET_USB:
        lines.append(
            "warning: AMD 600-series chipset USB drops PPK2 sample blocks. "
            "Use the red CPU USB port instead."
        )
    elif ident == CPU_USB:
        lines.append("controller is the CPU USB port that delivered full rate.")
    return "\n".join(lines)


def parse_metadata(raw: bytes) -> dict[str, float | None]:
    """Parse the ASCII metadata block that ends with END."""
    text = raw.split(b"END", 1)[0].decode("ascii", "replace")
    meta: dict[str, float | None] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if value == "" or value.lower() == "null" or value.lower() == "-nan":
            meta[key] = None
            continue
        try:
            meta[key] = float(value)
        except ValueError:
            continue
    return meta


def modifiers_from(meta: dict[str, float | None]) -> dict[str, list[float]]:
    """Build the five-range calibration vectors, falling back to factory defaults."""
    mods: dict[str, list[float]] = {}
    for key, default in DEFAULT_MODS.items():
        row = []
        for index, fallback in enumerate(default):
            value = meta.get(f"{key}{index}")
            row.append(fallback if value is None else float(value))
        mods[key] = row
    return mods


def sample_to_ua(mods: dict[str, list[float]], rng: int, adc14: int, vdd_mv: int) -> float:
    """Convert one raw sample to microamps. Same formula as the Power Profiler app."""
    adc = adc14 * 4
    normal = (adc - mods["o"][rng]) * (ADC_MULT / mods["r"][rng])
    amps = mods["ug"][rng] * (
        normal * (mods["gs"][rng] * normal + mods["gi"][rng])
        + (mods["s"][rng] * (vdd_mv / 1000.0) + mods["i"][rng])
    )
    return amps * 1e6


def best_align(buf: bytes) -> int:
    """Pick the 4-byte alignment whose sample counter increments most often."""
    best = 0
    best_ok = -1
    for align in range(4):
        prev = None
        ok = 0
        end = min(len(buf) - 4, align + 8000)
        for offset in range(align, end, 4):
            counter = (int.from_bytes(buf[offset : offset + 4], "little") >> COUNTER_SHIFT) & (
                COUNTER_MOD - 1
            )
            if prev is not None and counter == ((prev + 1) & (COUNTER_MOD - 1)):
                ok += 1
            prev = counter
        if ok > best_ok:
            best = align
            best_ok = ok
    return best


class Ppk2:
    """One open PPK2 command port. VOUT stays off unless capture() is running."""

    def __init__(self, port: str) -> None:
        """Open the CDC port without turning the regulator on."""
        try:
            self.ser = serial.Serial(port, timeout=0)
        except serial.SerialException as exc:
            raise SystemExit(f"cannot open {port}: {exc}") from exc
        self.port = port

    def close(self) -> None:
        """Close the serial port."""
        self.ser.close()

    def write(self, payload: bytes) -> None:
        """Send one command and flush it."""
        self.ser.write(payload)
        self.ser.flush()

    def read_some(self, timeout: float) -> bytes:
        """Read whatever arrives until timeout, including a quiet port."""
        fd = self.ser.fileno()
        buf = bytearray()
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            try:
                chunk = os.read(fd, 1 << 20)
            except BlockingIOError:
                time.sleep(0.002)
                continue
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def read_metadata(self) -> dict[str, float | None]:
        """Request the calibration block and parse it."""
        self.ser.reset_input_buffer()
        self.write(bytes([CMD_GET_METADATA]))
        raw = bytearray()
        deadline = time.perf_counter() + 2.0
        fd = self.ser.fileno()
        while b"END" not in raw and time.perf_counter() < deadline:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            raw.extend(chunk)
        if b"END" not in raw:
            raise SystemExit("PPK2 did not return metadata")
        return parse_metadata(bytes(raw))

    def output_off(self) -> None:
        """Stop sampling and leave the kit in ampere mode so VOUT is not driven."""
        self.write(bytes([CMD_DEVICE_RUNNING, 0]))
        self.write(bytes([CMD_AVG_STOP]))
        self.write(bytes([CMD_POWER_MODE, MODE_AMPERE]))

    def prepare_source(self, mv: int) -> None:
        """Select source mode and the regulator voltage without enabling VOUT."""
        if mv < MV_MIN or mv > MV_MAX:
            raise SystemExit(f"voltage must be {MV_MIN}..{MV_MAX} mV")
        self.write(bytes([CMD_DEVICE_RUNNING, 0]))
        time.sleep(0.02)
        self.write(bytes([CMD_POWER_MODE, MODE_SOURCE]))
        time.sleep(0.02)
        self.write(bytes([CMD_REGULATOR, (mv >> 8) & 0xFF, mv & 0xFF]))
        time.sleep(0.02)

    def source_on(self, mv: int) -> None:
        """Enable VOUT at mv and leave it on. The caller turns it off."""
        self.prepare_source(mv)
        self.write(bytes([CMD_DEVICE_RUNNING, 1]))

    def set_source_mv(self, mv: int) -> None:
        """Change the source voltage without turning VOUT off."""
        if mv < MV_MIN or mv > MV_MAX:
            raise SystemExit(f"voltage must be {MV_MIN}..{MV_MAX} mV")
        self.write(bytes([CMD_REGULATOR, (mv >> 8) & 0xFF, mv & 0xFF]))

    def measure(self, seconds: float) -> tuple[bytes, float]:
        """Record the sample stream without changing VOUT or the voltage.

        Returns the raw byte stream and the seconds the read loop actually ran.
        Sampling is stopped before return. VOUT stays on.
        """
        self.read_some(0.02)
        self.write(bytes([CMD_AVG_START]))
        time.sleep(0.01)
        self.read_some(0.02)
        fd = self.ser.fileno()
        chunks: list[bytes] = []
        started = time.perf_counter()
        try:
            while time.perf_counter() - started < seconds:
                try:
                    chunk = os.read(fd, 1 << 20)
                except BlockingIOError:
                    time.sleep(0.0005)
                    continue
                if chunk:
                    chunks.append(chunk)
            elapsed = time.perf_counter() - started
        finally:
            self.write(bytes([CMD_AVG_STOP]))
            self.read_some(0.05)
        return b"".join(chunks), elapsed

    def capture(self, mv: int, seconds: float) -> tuple[bytes, float]:
        """Enable VOUT, read the sample stream, and always turn VOUT off again.

        Returns the raw byte stream and the seconds the read loop actually ran.
        """
        self.prepare_source(mv)
        self.read_some(0.03)
        self.write(bytes([CMD_AVG_START]))
        time.sleep(0.01)
        self.read_some(0.02)
        fd = self.ser.fileno()
        chunks: list[bytes] = []
        self.write(bytes([CMD_DEVICE_RUNNING, 1]))
        started = time.perf_counter()
        try:
            while time.perf_counter() - started < seconds:
                try:
                    chunk = os.read(fd, 1 << 20)
                except BlockingIOError:
                    time.sleep(0.0005)
                    continue
                if chunk:
                    chunks.append(chunk)
            elapsed = time.perf_counter() - started
        finally:
            self.output_off()
        return b"".join(chunks), elapsed


def summarise(raw: bytes, elapsed: float, mv: int, mods: dict[str, list[float]]) -> dict:
    """Decode the stream and return rate plus current stats in microamps."""
    align = best_align(raw) if len(raw) >= 8 else 0
    count = 0
    total = 0.0
    minimum = None
    maximum = None
    for offset in range(align, len(raw) - 3, 4):
        word = int.from_bytes(raw[offset : offset + 4], "little")
        rng = (word >> RANGE_SHIFT) & 7
        if rng > 4:
            continue
        ua = sample_to_ua(mods, rng, word & ADC_MASK, mv)
        count += 1
        total += ua
        if minimum is None or ua < minimum:
            minimum = ua
        if maximum is None or ua > maximum:
            maximum = ua
    sps = count / elapsed if elapsed > 0 else 0.0
    mean = total / count if count else 0.0
    return {
        "samples": count,
        "elapsed": elapsed,
        "sps": sps,
        "mean_ua": mean,
        "min_ua": 0.0 if minimum is None else minimum,
        "max_ua": 0.0 if maximum is None else maximum,
        "align": align,
    }


def write_bins(path: str, raw: bytes, elapsed: float, mv: int, mods: dict[str, list[float]], bin_ms: float) -> None:
    """Write one CSV row per time bin: t_ms, mean_ua, min_ua, max_ua."""
    align = best_align(raw) if len(raw) >= 8 else 0
    words = []
    for offset in range(align, len(raw) - 3, 4):
        word = int.from_bytes(raw[offset : offset + 4], "little")
        rng = (word >> RANGE_SHIFT) & 7
        if rng > 4:
            continue
        words.append(sample_to_ua(mods, rng, word & ADC_MASK, mv))
    n = len(words)
    if n == 0 or elapsed <= 0:
        raise SystemExit("no samples to bin")
    bin_s = bin_ms / 1000.0
    nbins = max(1, int(round(elapsed / bin_s)))
    with open(path, "w", encoding="ascii") as handle:
        handle.write("t_ms,mean_ua,min_ua,max_ua\n")
        for index in range(nbins):
            start = int(index * n / nbins)
            stop = max(start + 1, int((index + 1) * n / nbins))
            chunk = words[start:stop]
            mean = sum(chunk) / len(chunk)
            t_ms = (index + 0.5) * elapsed / nbins * 1000.0
            handle.write(f"{t_ms:.3f},{mean:.4f},{min(chunk):.4f},{max(chunk):.4f}\n")


def format_ua(ua: float) -> str:
    """Format a current for the summary line."""
    magnitude = abs(ua)
    if magnitude >= 1e6:
        return f"{ua / 1e6:.3f} A"
    if magnitude >= 1e3:
        return f"{ua / 1e3:.3f} mA"
    return f"{ua:.2f} µA"


def cmd_probe(ppk: Ppk2) -> int:
    """Print where the PPK2 is plugged in and its calibration header. VOUT stays off."""
    print(port_info(ppk.port))
    meta = ppk.read_metadata()
    calibrated = meta.get("calibrated")
    hw = meta.get("hw")
    print(f"metadata calibrated={calibrated} hw={hw}")
    print("VOUT was not enabled")
    return 0


def cmd_off(ppk: Ppk2) -> int:
    """Force the regulator off."""
    ppk.output_off()
    print("VOUT off, ampere mode")
    return 0


def cmd_capture(ppk: Ppk2, args: argparse.Namespace) -> int:
    """Source `args.mv` for `args.seconds`, then turn VOUT off and print the result."""
    print(port_info(ppk.port), file=sys.stderr)
    meta = ppk.read_metadata()
    mods = modifiers_from(meta)
    raw, elapsed = ppk.capture(args.mv, args.seconds)
    if args.raw:
        with open(args.raw, "wb") as handle:
            handle.write(raw)
    stats = summarise(raw, elapsed, args.mv, mods)
    if args.bins:
        write_bins(args.bins, raw, elapsed, args.mv, mods, args.bin_ms)
    sps = stats["sps"]
    print(
        f"{args.mv} mV for {stats['elapsed']:.3f} s, "
        f"{stats['samples']} samples, {sps:.0f} samples/s "
        f"({100.0 * sps / FULL_SPS:.0f}% of {FULL_SPS})"
    )
    print(
        f"mean {format_ua(stats['mean_ua'])}  "
        f"min {format_ua(stats['min_ua'])}  "
        f"max {format_ua(stats['max_ua'])}"
    )
    print("VOUT off, ampere mode")
    if sps < FULL_SPS * MIN_SPS_FRACTION:
        print(
            "warning: sample rate is well below 100 kHz, so the firmware dropped "
            "blocks. Move the PPK2 to the red CPU USB port and capture again. "
            "Do not trust this file.",
            file=sys.stderr,
        )
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        help="PPK2 command port. Default: the if01 by-id symlink.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    probe = sub.add_parser("probe", help="show the USB port and metadata; do not power the DUT")
    probe.set_defaults(func=cmd_probe)

    off = sub.add_parser("off", help="force VOUT off")
    off.set_defaults(func=cmd_off)

    capture = sub.add_parser("capture", help="source a voltage, record current, then turn VOUT off")
    capture.add_argument("--mv", type=int, default=3300, help="source voltage in millivolts (800-5000)")
    capture.add_argument("--seconds", type=float, required=True, help="how long VOUT stays on")
    capture.add_argument("--raw", help="write the raw 4-byte sample stream to this path")
    capture.add_argument("--bins", help="write a binned CSV (t_ms,mean_ua,min_ua,max_ua)")
    capture.add_argument("--bin-ms", type=float, default=10.0, help="bin width for --bins, in milliseconds")
    capture.set_defaults(func=cmd_capture)
    return parser


def main() -> int:
    """Dispatch probe, off, or capture. Capture turns VOUT off even on error."""
    args = build_parser().parse_args()
    port = args.port or find_port()
    ppk = Ppk2(port)
    try:
        if args.cmd == "capture":
            return cmd_capture(ppk, args)
        return args.func(ppk)
    finally:
        if args.cmd == "capture":
            try:
                ppk.output_off()
            except OSError:
                pass
        ppk.close()


if __name__ == "__main__":
    sys.exit(main())
