#!/usr/bin/env python3
"""Run the bring-up image while the PPK2 powers the board.

Flash first, with the chip in Run (the 10 s blink, or SETUP on the plant image):

    make flash-boardtest
    python3 tools/board_test.py

The solder bridges are open, so a Schottky sits between the PPK2 and the MCU.
This script does not guess that drop. It reads vdda_mv from the chip. Leave the
ST-LINK 3V3 jumper off; the debugger stays on SWD and UART only. If that jumper
is on, the rail will not follow the PPK2 and the low-battery step fails.

The test image stays in Run, erases the plant interval in EEPROM, and then
reports VDDA until the supply is turned off.
"""

from __future__ import annotations

import csv
import glob
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ppk2  # noqa: E402

try:
    import serial
except ImportError:
    sys.stderr.write("python3-serial is not installed (apt install python3-serial)\n")
    raise SystemExit(1)

UART_GLOB = "/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_*-if02"

# Same thresholds as Core/Src/board_test.c.
TRIP_MV = 2400
CLEAR_MV = 2500
START_MV = 3300
FLOOR_MV = 2200
STEP_DOWN_MV = 100
STEP_UP_MV = 50
# Stop the sweep if the pin itself falls this low.
PIN_FLOOR_MV = 1900

# PPK2 settings spanning a coin cell, from fresh down toward the cutoff.
# The current measured here is what the cell must supply.
SLEEP_PPK_MV = (3200, 3000, 2800, 2600)
# Typical CR2032 capacity at a microamp drain.
CELL_MAH = 220.0
# Nominal RTC dividers the test image measures against. ck_spre = LSI / (128*289).
NOMINAL_ASYNCH = 127
NOMINAL_SYNCH = 288

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT, "board_tests.csv")


class UartLog:
    """Background reader for the ST-LINK VCP."""

    def __init__(self, port: str) -> None:
        """Open the VCP and start collecting lines."""
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.2)
        except serial.SerialException as exc:
            raise SystemExit(f"cannot open {port}: {exc}") from exc
        self.rows: list[tuple[float, str]] = []
        self.cv = threading.Condition()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="uart", daemon=True)
        self.thread.start()

    def close(self) -> None:
        """Stop the reader and close the port."""
        self.stop.set()
        self.thread.join(timeout=1.0)
        self.ser.close()

    def _run(self) -> None:
        """Read bytes into whole lines tagged with perf_counter."""
        buf = b""
        while not self.stop.is_set():
            try:
                chunk = self.ser.read(512)
            except serial.SerialException:
                break
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = raw.decode("utf-8", "replace").strip("\r")
                if text == "":
                    continue
                with self.cv:
                    self.rows.append((time.perf_counter(), text))
                    self.cv.notify_all()

    def write(self, payload: bytes) -> None:
        """Send bytes to the chip."""
        self.ser.write(payload)
        self.ser.flush()

    def mark(self) -> int:
        """Return an index just past the lines already received."""
        with self.cv:
            return len(self.rows)

    def wait_from(self, index: int, pred, timeout: float):
        """Return (perf_counter, line) for the next matching line at or after index."""
        deadline = time.monotonic() + timeout
        with self.cv:
            while True:
                for stamped in self.rows[index:]:
                    if pred(stamped[1]):
                        return stamped
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return None
                self.cv.wait(remain)


def find_uart() -> str:
    """Return the ST-LINK virtual COM port (by-id if02)."""
    matches = sorted(glob.glob(UART_GLOB))
    if not matches:
        raise SystemExit(
            "no ST-LINK VCP (usb STMicroelectronics STM32 STLink *-if02). "
            "Is the Nucleo plugged in?"
        )
    if len(matches) > 1:
        raise SystemExit("more than one ST-LINK is plugged in")
    return matches[0]


def countdown(action: str) -> None:
    """Tell the operator what to do, three seconds before NOW."""
    print(f"\nIN 3 SECONDS — {action}", flush=True)
    time.sleep(1.0)
    print("2...", flush=True)
    time.sleep(1.0)
    print("1...", flush=True)
    time.sleep(1.0)
    sys.stdout.write("\a")
    print(f"NOW — {action}", flush=True)


def parse_btn(line: str) -> tuple[str, str] | None:
    """Return (big, small) from a button line, or None."""
    if not line.startswith("boardtest: btn "):
        return None
    big = None
    small = None
    for part in line.split():
        if part.startswith("big="):
            big = part.split("=", 1)[1]
        elif part.startswith("small="):
            small = part.split("=", 1)[1]
    if big is None or small is None:
        return None
    return big, small


def btn_is(line: str, big: str, small: str) -> bool:
    """True when the line is that exact button combination."""
    parsed = parse_btn(line)
    return parsed == (big, small)


def parse_vdda(line: str) -> tuple[int, int] | None:
    """Return (millivolts, low_batt) from a vdda line, or None."""
    if not line.startswith("boardtest: vdda_mv="):
        return None
    try:
        head, flag = line.split(" low_batt=", 1)
        mv = int(head.split("=", 1)[1])
        low = int(flag)
    except (ValueError, IndexError):
        return None
    return mv, low


def parse_rtc(line: str) -> int | None:
    """Return the RTC second count from a rtc line, or None."""
    if not line.startswith("boardtest: rtc s="):
        return None
    try:
        return int(line.split("=", 1)[1])
    except ValueError:
        return None


def wait_line(uart: UartLog, index: int, prefix: str, timeout: float) -> tuple[float, str] | None:
    """Wait for a line that starts with prefix."""
    return uart.wait_from(index, lambda line: line.startswith(prefix), timeout)


def ask_hold(uart: UartLog, big: str, small: str, action: str) -> int | None:
    """Countdown, then wait until that button combination is reported.

    Returns the log index from before the countdown, so a quick tap that
    already released is still visible to the release wait.
    """
    mark = uart.mark()
    countdown(action)
    got = uart.wait_from(mark, lambda line: btn_is(line, big, small), 25.0)
    if got is None:
        print("did not see that press on the UART", flush=True)
        return None
    print(f"chip: {got[1]}", flush=True)
    return mark


def ask_release(uart: UartLog, since: int) -> bool:
    """Tell the operator to let go, and wait for both buttons up."""
    if uart.wait_from(since, lambda line: btn_is(line, "0", "0"), 0.3) is not None:
        print("chip: boardtest: btn big=0 small=0", flush=True)
        return True
    print("Release.", flush=True)
    got = uart.wait_from(since, lambda line: btn_is(line, "0", "0"), 20.0)
    if got is None:
        print("buttons did not release", flush=True)
        return False
    print(f"chip: {got[1]}", flush=True)
    return True


def run_buttons(uart: UartLog) -> bool:
    """Prompt for big, small, then both. LEDs echo the presses."""
    print(
        "\nButton check. Green = big button. Red = small button. Both LEDs = both buttons.",
        flush=True,
    )
    held = ask_hold(uart, "1", "0", "press and hold the BIG button. Green should light.")
    if held is None or not ask_release(uart, held):
        return False
    held = ask_hold(uart, "0", "1", "press and hold the SMALL button. Red should light.")
    if held is None or not ask_release(uart, held):
        return False
    if ask_hold(
        uart,
        "1",
        "1",
        "press and hold BOTH buttons. Both LEDs should light, then they go out on their own.",
    ) is None:
        return False
    print("Release if you are still holding them.", flush=True)
    got = wait_line(uart, uart.mark(), "boardtest: buttons pass", 5.0)
    if got is None:
        # The pass line is printed as both go down, so it may already be in the log.
        earlier = uart.wait_from(0, lambda line: line.startswith("boardtest: buttons pass"), 0.1)
        if earlier is None:
            print("button check did not pass", flush=True)
            return False
    print("buttons pass", flush=True)
    return True


def run_eeprom(uart: UartLog) -> bool:
    """Wait for the EEPROM write / read / erase result."""
    print("\nEEPROM check (this erases the stored plant interval).", flush=True)
    got = uart.wait_from(
        0,
        lambda line: line.startswith("boardtest: eeprom "),
        10.0,
    )
    if got is None:
        print("no EEPROM result", flush=True)
        return False
    print(f"chip: {got[1]}", flush=True)
    return got[1] == "boardtest: eeprom pass"


def run_rtc(uart: UartLog) -> tuple[bool, float | None]:
    """Compare 60 RTC seconds with the host clock. Returns (ok, error_percent).

    s=0 is often already in the log by the time EEPROM handling returns, so the
    search starts at the beginning of the capture.
    """
    print("\nRTC check. Hands off for about a minute.", flush=True)
    first = uart.wait_from(0, lambda line: parse_rtc(line) == 0, 20.0)
    if first is None:
        print("RTC did not start", flush=True)
        return False, None
    print(f"chip: {first[1]}", flush=True)
    last = first
    while True:
        prev = parse_rtc(last[1])
        if prev is None:
            prev = -1
        nxt = uart.wait_from(
            0,
            lambda line, floor=prev: (parse_rtc(line) is not None and parse_rtc(line) > floor),
            5.0,
        )
        if nxt is None:
            print("RTC stopped counting", flush=True)
            return False, None
        sec = parse_rtc(nxt[1])
        if sec is not None and sec % 10 == 0:
            print(f"chip: {nxt[1]}", flush=True)
        last = nxt
        if sec == 60:
            break
    host_s = last[0] - first[0]
    rtc_span = (parse_rtc(last[1]) or 0) - (parse_rtc(first[1]) or 0)
    if host_s <= 0 or rtc_span <= 0:
        print("RTC host interval was empty", flush=True)
        return False, None
    # Positive: the RTC ran fast (its seconds finished in less wall time).
    error_pct = (rtc_span / host_s - 1.0) * 100.0
    print(
        f"RTC: {rtc_span} RTC-seconds took {host_s:.2f} s of wall time "
        f"({error_pct:+.1f}%). Plant budget is about ±10%.",
        flush=True,
    )
    ok = abs(error_pct) <= 20.0
    if not ok:
        print("RTC error is outside ±20%. The LSI is not usable.", flush=True)
    return ok, error_pct


def read_vdda(uart: UartLog, after: int, timeout: float) -> tuple[float, int, int] | None:
    """Wait for a fresh VDDA report. Returns (host_time, mv, low_batt)."""
    got = uart.wait_from(after, lambda line: parse_vdda(line) is not None, timeout)
    if got is None:
        return None
    parsed = parse_vdda(got[1])
    if parsed is None:
        return None
    print(f"chip: {got[1]}", flush=True)
    return got[0], parsed[0], parsed[1]


def settle_vdda(ppk: ppk2.Ppk2, uart: UartLog, mv: int) -> tuple[int, int] | None:
    """Set the PPK2 voltage, let the pin settle, and return the next chip reading."""
    ppk.set_source_mv(mv)
    time.sleep(0.45)
    got = read_vdda(uart, uart.mark(), 3.0)
    if got is None:
        return None
    return got[1], got[2]


def run_vdda(
    ppk: ppk2.Ppk2, uart: UartLog
) -> tuple[bool, tuple[int, int] | None, tuple[int, int] | None]:
    """Step the PPK2 down through the diode until the chip reports low battery, then back up.

    Returns (ok, trip (ppk_mv, chip_mv), clear (ppk_mv, chip_mv)).
    Clear counts only once the chip is back at or above 2.5 V, so a sample
    that lands inside the hysteresis band does not fail the board.
    """
    print(
        "\nLow-battery check. Hands off. Red means the chip measured at or below "
        f"{TRIP_MV} mV. It should go out again above {CLEAR_MV} mV.",
        flush=True,
    )
    phase = wait_line(uart, 0, "boardtest: phase vdda", 30.0)
    if phase is None:
        print("voltage phase did not start", flush=True)
        return False, None, None
    first = read_vdda(uart, uart.mark(), 3.0)
    if first is None:
        print("no VDDA reading", flush=True)
        return False, None, None
    start_mv = first[1]
    start_low = first[2]
    drop = START_MV - start_mv
    print(
        f"PPK2 {START_MV} mV, chip {start_mv} mV, diode drop {drop} mV at run current.",
        flush=True,
    )
    if start_low != 0 or start_mv < CLEAR_MV:
        print("chip already reports low battery at the starting voltage", flush=True)
        return False, None, None
    if drop < -50:
        print(
            "chip voltage is above the PPK2. The ST-LINK 3V3 jumper is probably on. "
            "Turn that jumper off and run this again.",
            flush=True,
        )
        return False, None, None

    mv = START_MV
    pin = start_mv
    low = 0
    tripped_at: tuple[int, int] | None = None
    while low == 0 and mv > FLOOR_MV:
        mv -= STEP_DOWN_MV
        got = settle_vdda(ppk, uart, mv)
        if got is None:
            print("lost VDDA reports during the sweep", flush=True)
            return False, tripped_at, None
        pin, low = got
        print(f"PPK2 {mv} mV -> chip {pin} mV  low_batt={low}", flush=True)
        if pin < PIN_FLOOR_MV:
            print("pin fell below 1.9 V before the detector tripped. Stopping.", flush=True)
            ppk.set_source_mv(START_MV)
            return False, tripped_at, None
        if (START_MV - mv) >= 400 and (start_mv - pin) < 150:
            print(
                "the pin is not following the PPK2. "
                "The ST-LINK 3V3 jumper is probably feeding the rail.",
                flush=True,
            )
            ppk.set_source_mv(START_MV)
            return False, tripped_at, None
        if low == 1:
            tripped_at = (mv, pin)

    if tripped_at is None:
        print(
            f"low_batt did not trip. Last chip reading was {pin} mV "
            f"with the PPK2 at {mv} mV.",
            flush=True,
        )
        ppk.set_source_mv(START_MV)
        return False, None, None
    if tripped_at[1] > TRIP_MV:
        print(f"flag went low_batt with the chip still at {tripped_at[1]} mV", flush=True)
        ppk.set_source_mv(START_MV)
        return False, tripped_at, None
    print(f"tripped: PPK2 {tripped_at[0]} mV, chip {tripped_at[1]} mV, red should be on", flush=True)

    cleared_at: tuple[int, int] | None = None
    while cleared_at is None and mv < START_MV:
        mv = min(START_MV, mv + STEP_UP_MV)
        got = settle_vdda(ppk, uart, mv)
        if got is None:
            print("lost VDDA reports on the way back up", flush=True)
            ppk.set_source_mv(START_MV)
            return False, tripped_at, None
        pin, low = got
        print(f"PPK2 {mv} mV -> chip {pin} mV  low_batt={low}", flush=True)
        if low == 0 and pin >= CLEAR_MV:
            cleared_at = (mv, pin)
            break
        if low == 0:
            print(
                f"flag is clear at {pin} mV, still inside the {TRIP_MV}..{CLEAR_MV} band",
                flush=True,
            )
    ppk.set_source_mv(START_MV)
    if cleared_at is None:
        print("low_batt did not clear with the chip back at or above 2.5 V", flush=True)
        return False, tripped_at, None
    print(
        f"cleared: PPK2 {cleared_at[0]} mV, chip {cleared_at[1]} mV, red should be off",
        flush=True,
    )
    return True, tripped_at, cleared_at


def median_ua(raw: bytes, mv: int, mods: dict[str, list[float]]) -> float | None:
    """Median current of one PPK2 capture, in microamps."""
    if len(raw) < 8:
        return None
    align = ppk2.best_align(raw)
    values: list[float] = []
    for offset in range(align, len(raw) - 3, 4):
        word = int.from_bytes(raw[offset : offset + 4], "little")
        rng = (word >> ppk2.RANGE_SHIFT) & 7
        if rng > 4:
            continue
        values.append(ppk2.sample_to_ua(mods, rng, word & ppk2.ADC_MASK, mv))
    if not values:
        return None
    values.sort()
    return values[len(values) // 2]


def run_sleep(
    ppk: ppk2.Ppk2, uart: UartLog, mods: dict[str, list[float]]
) -> dict[int, tuple[int, float]]:
    """Measure Stop current at each coin-cell voltage. Keys are PPK2 millivolts."""
    print(
        "\nSleep current, hands off. The chip stops for 8 seconds at each voltage. "
        "This is the current a coin cell has to supply.",
        flush=True,
    )
    found: dict[int, tuple[int, float]] = {}
    for mv in SLEEP_PPK_MV:
        got = settle_vdda(ppk, uart, mv)
        if got is None:
            print(f"no voltage reading at PPK2 {mv} mV", flush=True)
            continue
        vdda, _low = got
        mark = uart.mark()
        uart.write(b"S")
        if uart.wait_from(mark, lambda line: line.startswith("boardtest: sleeping"), 3.0) is None:
            print(f"chip did not enter Stop at PPK2 {mv} mV", flush=True)
            continue
        time.sleep(1.0)
        raw, elapsed = ppk.measure(3.5)
        ua = median_ua(raw, mv, mods)
        stats = ppk2.summarise(raw, elapsed, mv, mods)
        if stats["sps"] < ppk2.FULL_SPS * ppk2.MIN_SPS_FRACTION:
            print(
                f"warning: sleep capture at {mv} mV was {stats['sps']:.0f} samples/s",
                flush=True,
            )
        if uart.wait_from(mark, lambda line: line.startswith("boardtest: awake"), 12.0) is None:
            print(f"chip did not wake at PPK2 {mv} mV", flush=True)
        if ua is None:
            print(f"no current samples at PPK2 {mv} mV", flush=True)
            continue
        print(
            f"Stop at PPK2 {mv} mV (chip awake {vdda} mV): {ua:.2f} µA",
            flush=True,
        )
        found[mv] = (vdda, ua)
    return found


def corrected_synch(drift_pct: float) -> int:
    """Synchronous prescaler that puts ck_spre on 1 Hz for this measured drift.

    drift_pct is (RTC seconds / wall seconds - 1) * 100, taken with the
    nominal 127/288 dividers. A slow RTC (negative drift) gets a smaller divider.
    """
    ratio = 1.0 + drift_pct / 100.0
    sync_plus = int(round(ratio * (NOMINAL_SYNCH + 1)))
    if sync_plus < 1:
        sync_plus = 1
    if sync_plus > 32768:
        sync_plus = 32768
    return sync_plus - 1


def flash_plant(synch: int) -> bool:
    """Build and flash the plant timer with this RTC synchronous prescaler.

    The chip must already be powered and in Run. Only main.c carries the
    divider, so its object is removed first and the rest of the build is reused.
    """
    defs = f"-DUSE_HAL_DRIVER -DSTM32L011xx -DAPP_RTC_SYNCH_PREDIV={synch}"
    print(
        f"\nFlashing the plant timer. RTC synch prescaler {synch} "
        f"(nominal {NOMINAL_SYNCH}).",
        flush=True,
    )
    obj = os.path.join(ROOT, "build", "main.o")
    if os.path.exists(obj):
        os.remove(obj)
    result = subprocess.run(
        ["make", "flash", f"C_DEFS={defs}"],
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        print("plant firmware flash failed", flush=True)
        return False
    print("plant firmware flashed", flush=True)
    return True


def years_from_ua(ua: float) -> float | None:
    """Sleep-only life of a 220 mAh coin cell at this current, in years."""
    if ua <= 0.05:
        return None
    hours = (CELL_MAH * 1000.0) / ua
    return hours / (24.0 * 365.0)


def write_csv(
    uid: str,
    buttons: bool,
    eeprom: bool,
    rtc: bool,
    low_batt: bool,
    drift_pct: float | None,
    trip: tuple[int, int] | None,
    clear: tuple[int, int] | None,
    sleep: dict[int, tuple[int, float]],
    synch: int | None,
    flashed: bool,
) -> None:
    """Append one row to board_tests.csv, rewriting the header if columns grew."""
    uas = [ua for _vdda, ua in sleep.values()]
    mean_ua = sum(uas) / len(uas) if uas else None
    years = years_from_ua(mean_ua) if mean_ua is not None else None
    row: dict[str, str] = {
        "uid": uid,
        "tested_at": datetime.now().isoformat(timespec="seconds"),
        "overall": "pass" if buttons and eeprom and rtc and low_batt else "fail",
        "buttons": "pass" if buttons else "fail",
        "eeprom": "pass" if eeprom else "fail",
        "rtc": "pass" if rtc else "fail",
        "low_batt": "pass" if low_batt else "fail",
        "rtc_drift_pct": "" if drift_pct is None else f"{drift_pct:.2f}",
        "rtc_synch": "" if synch is None else str(synch),
        "plant_flashed": "yes" if flashed else "no",
        "trip_ppk_mv": "" if trip is None else str(trip[0]),
        "trip_chip_mv": "" if trip is None else str(trip[1]),
        "clear_ppk_mv": "" if clear is None else str(clear[0]),
        "clear_chip_mv": "" if clear is None else str(clear[1]),
        "sleep_ua_mean": "" if mean_ua is None else f"{mean_ua:.3f}",
        "projected_years_220mAh": "" if years is None else f"{years:.2f}",
    }
    for mv in SLEEP_PPK_MV:
        point = sleep.get(mv)
        row[f"vdda_mv_at_{mv}"] = "" if point is None else str(point[0])
        row[f"sleep_ua_at_{mv}"] = "" if point is None else f"{point[1]:.3f}"
    fieldnames = list(row.keys())
    existing: list[dict[str, str]] = []
    if os.path.exists(CSV_PATH) and os.path.getsize(CSV_PATH) > 0:
        with open(CSV_PATH, encoding="utf-8", newline="") as handle:
            existing = list(csv.DictReader(handle))
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for old in existing:
            writer.writerow(old)
        writer.writerow(row)
    print(f"wrote {CSV_PATH}", flush=True)


def parse_uid(line: str) -> str | None:
    """Return the 24-hex UID from a uid line, or None."""
    if not line.startswith("boardtest: uid="):
        return None
    uid = line.split("=", 1)[1].strip()
    if len(uid) != 24:
        return None
    return uid


def print_plan() -> None:
    """Print the bench rules and the operator's part, before power is applied."""
    print(
        "\n".join(
            [
                "Board test. The PPK2 powers the board through the diodes (bridges open).",
                "ST-LINK stays connected for SWD and UART. Its 3V3 jumper stays off.",
                "",
                "You will be told 3 seconds ahead. Only move on a NOW line.",
                "  big button   -> green LED",
                "  small button -> red LED",
                "  both         -> both LEDs",
                "",
                "Then hands off: EEPROM, about 60 s of RTC, the low-battery sweep,",
                "and Stop-current measurements at several cell voltages.",
                "A passing board is then flashed with the plant timer,",
                "its RTC divider corrected from the drift just measured.",
                "",
                "Press Enter to apply power.",
            ]
        ),
        flush=True,
    )


def main() -> int:
    """Power the board from the PPK2 and walk the bring-up sequence."""
    uart_port = find_uart()
    ppk_port = ppk2.find_port()
    print(f"UART {uart_port}", flush=True)
    print(ppk2.port_info(ppk_port), flush=True)
    print_plan()
    try:
        input()
    except EOFError:
        print("no terminal input; not starting", file=sys.stderr)
        return 1

    uart = UartLog(uart_port)
    ppk = ppk2.Ppk2(ppk_port)
    mods = ppk2.modifiers_from(ppk.read_metadata())
    ok_btn = False
    ok_ee = False
    ok_rtc = False
    ok_v = False
    drift: float | None = None
    trip: tuple[int, int] | None = None
    clear: tuple[int, int] | None = None
    sleep: dict[int, tuple[int, float]] = {}
    try:
        ppk.source_on(START_MV)
        print(
            "\nPower is on. Both LEDs blink for 10 seconds, "
            "then red alone, green alone, then both.",
            flush=True,
        )
        ready = wait_line(uart, 0, "boardtest: phase buttons", 25.0)
        if ready is None:
            print(
                "the test image did not start. Flash it with `make flash-boardtest` "
                "while the chip is in Run, then run this again.",
                flush=True,
            )
            return 1
        leds = uart.wait_from(0, lambda line: line.startswith("boardtest: phase leds"), 0.1)
        if leds is not None:
            print("LED walk finished (red, green, both).", flush=True)
        ok_btn = run_buttons(uart)
        if ok_btn:
            ok_ee = run_eeprom(uart)
            ok_rtc, drift = run_rtc(uart)
            ok_v, trip, clear = run_vdda(ppk, uart)
            if ok_v or trip is not None:
                sleep = run_sleep(ppk, uart, mods)
        uid_line = uart.wait_from(0, lambda line: parse_uid(line) is not None, 0.1)
        uid = "" if uid_line is None else (parse_uid(uid_line[1]) or "")
        mean_ua = None
        if sleep:
            mean_ua = sum(ua for _vdda, ua in sleep.values()) / len(sleep)
        years = years_from_ua(mean_ua) if mean_ua is not None else None
        print(
            "\n"
            f"uid {uid or 'unknown'}\n"
            f"buttons {'pass' if ok_btn else 'fail'}  "
            f"eeprom {'pass' if ok_ee else 'fail'}  "
            f"rtc {'pass' if ok_rtc else 'fail'}  "
            f"low_batt {'pass' if ok_v else 'fail'}",
            flush=True,
        )
        if mean_ua is not None and years is not None:
            print(
                f"mean Stop current {mean_ua:.2f} µA, "
                f"about {years:.1f} years from a 220 mAh cell if it only slept",
                flush=True,
            )
        passed = ok_btn and ok_ee and ok_rtc and ok_v
        synch = corrected_synch(drift) if passed and drift is not None else None
        flashed = flash_plant(synch) if synch is not None else False
        write_csv(uid, ok_btn, ok_ee, ok_rtc, ok_v, drift, trip, clear, sleep, synch, flashed)
        if passed and flashed:
            print("DONE", flush=True)
            return 0
        return 1
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)
        return 1
    finally:
        try:
            ppk.output_off()
        except OSError:
            pass
        ppk.close()
        uart.close()
        print("VOUT off.", flush=True)


if __name__ == "__main__":
    sys.exit(main())
