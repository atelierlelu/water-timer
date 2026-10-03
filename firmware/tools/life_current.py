#!/usr/bin/env python3
"""Cue a life-cycle current capture and record it on the PPK2.

Powers the board at 3.3 V (t = 0 is VOUT on) and prints each thing you must
do three seconds before the moment to do it. Setup is not part of this run.
Do not long-press the small button.

The both-button hold at the end of the 10 s blink is the bench latch in
app_init(): interval 60 s, RTC wake every 10 s, due flash on the sixth wake.
Without that hold the RTC stays at 15 minutes and this capture will not see
a wake. Times below are seconds after power-on and assume you act on NOW.
LSI can slide the automatic wakes by a couple of seconds.

    python3 tools/life_current.py
    python3 tools/life_current.py --practice   # same cues, PPK2 stays off
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ppk2  # noqa: E402

# Source voltage used for the other bench captures.
MV = 3300

# Hold covers the button sample at the end of the 10 s blink (~10.2 s).
HOLD_AT_S = 9.0
RELEASE_AT_S = 14.0

# Sixth 10 s wake after release. Two 8 ms green pulses. Just watch.
DUE_AT_S = RELEASE_AT_S + 60.0

# Short taps, after the due flash, with gaps so the LED code finishes first.
VIEW_AT_S = 92.0
WATERED_AT_S = 110.0

# Long enough to include one 10 s wake after the watered ack.
CAPTURE_S = 132.0

# How far ahead of NOW the instruction is printed.
LEAD_S = 3.0

# Bin width for the CSV. 1 ms keeps the 8 ms due pulses visible.
BIN_MS = 1.0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Clock:
    """Capture start time, shared with the cue loop."""

    def __init__(self) -> None:
        """Create a clock that is not running yet."""
        self._ready = threading.Event()
        self.t0 = 0.0

    def start(self) -> None:
        """Mark t = 0. A second call does nothing."""
        if self._ready.is_set():
            return
        self.t0 = time.perf_counter()
        self._ready.set()

    def wait(self, timeout: float) -> bool:
        """Block until start(), or until timeout seconds have passed."""
        return self._ready.wait(timeout)

    def now(self) -> float:
        """Seconds since start(). Zero if start() has not run."""
        if not self._ready.is_set():
            return 0.0
        return time.perf_counter() - self.t0


def build_events() -> list[tuple[float, str, str]]:
    """Return (t_seconds, kind, text) sorted by time.

    kind is "warn" (LEAD_S before an action), "now" (do it), or "note".
    """
    actions = [
        (
            HOLD_AT_S,
            "Press and hold BOTH buttons (big and small). Keep holding.",
        ),
        (
            RELEASE_AT_S,
            "Release both buttons.",
        ),
        (
            VIEW_AT_S,
            "Tap the SMALL button once and let go immediately. Do not hold it.",
        ),
        (
            WATERED_AT_S,
            "Tap the BIG button once and let go.",
        ),
    ]
    notes = [
        (
            0.0,
            "Power is on. Both LEDs blink for 10 seconds. Hands off until the countdown.",
        ),
        (
            16.0,
            "Asleep. Hands off. A double green flash (easy to miss) comes about a minute after the release.",
        ),
        (
            45.0,
            "Still recording. Hands off.",
        ),
        (
            DUE_AT_S - LEAD_S,
            "Watch the LEDs. In 3 seconds: two very short green flashes. Do not press anything.",
        ),
        (
            DUE_AT_S,
            "Due flashes should be about now. Still hands off.",
        ),
        (
            96.0,
            "View is a green code: one blink, three blinks, or one longer blink. Hands off.",
        ),
        (
            114.0,
            "Watered ack is one short green blink, then sleep. Hands off until the end.",
        ),
        (
            122.0,
            "A wake with no LED may show up around now. Hands off. Capture ends in a few seconds.",
        ),
    ]
    events: list[tuple[float, str, str]] = []
    for t_s, text in actions:
        events.append((t_s - LEAD_S, "warn", text))
        events.append((t_s, "now", text))
    for t_s, text in notes:
        events.append((t_s, "note", text))
    events.sort(key=lambda item: (item[0], item[1] != "now"))
    return events


def sleep_until(clock: Clock, t_s: float, stop: threading.Event) -> bool:
    """Sleep until t_s on the capture clock. False if stop was set."""
    while not stop.is_set():
        remain = t_s - clock.now()
        if remain <= 0:
            return True
        time.sleep(min(remain, 0.05))
    return False


def emit(clock: Clock, kind: str, text: str, log: list[tuple[float, str, str]]) -> None:
    """Print one cue and remember the actual print time."""
    t_s = clock.now()
    log.append((t_s, kind, text))
    if kind == "warn":
        prefix = "IN 3 SECONDS"
    elif kind == "now":
        prefix = "NOW"
        sys.stdout.write("\a")
    else:
        prefix = "note"
    print(f"[{t_s:6.1f}s]  {prefix} — {text}", flush=True)


def run_cues(clock: Clock, stop: threading.Event) -> list[tuple[float, str, str]]:
    """Print the schedule. Returns the lines that were actually printed."""
    log: list[tuple[float, str, str]] = []
    for t_s, kind, text in build_events():
        if not sleep_until(clock, t_s, stop):
            break
        emit(clock, kind, text, log)
    return log


def capture_worker(
    ppk: ppk2.Ppk2,
    seconds: float,
    clock: Clock,
    chunks: list[bytes],
    stop: threading.Event,
    error: list[BaseException],
) -> None:
    """Enable VOUT, mark t = 0, read samples, and always turn VOUT off."""
    try:
        ppk.prepare_source(MV)
        ppk.read_some(0.03)
        ppk.write(bytes([ppk2.CMD_AVG_START]))
        time.sleep(0.01)
        ppk.read_some(0.02)
        fd = ppk.ser.fileno()
        ppk.write(bytes([ppk2.CMD_DEVICE_RUNNING, 1]))
        clock.start()
        started = time.perf_counter()
        while time.perf_counter() - started < seconds and not stop.is_set():
            try:
                chunk = os.read(fd, 1 << 20)
            except BlockingIOError:
                time.sleep(0.0005)
                continue
            if chunk:
                chunks.append(chunk)
    except Exception as exc:
        error.append(exc)
    finally:
        clock.start()
        try:
            ppk.output_off()
        except OSError:
            pass


def print_plan(practice: bool) -> None:
    """Print the run the operator is about to do, before power is applied."""
    if practice:
        closing = "Practice only: the PPK2 stays off. Press Enter to start the countdown."
    else:
        closing = (
            "PPK2 on the red USB port. Close the Power Profiler app if it is open.\n"
            "Fingers free. Press Enter to apply power."
        )
    print(
        "\n".join(
            [
                f"Life-cycle current capture at {MV} mV, about {CAPTURE_S:.0f} seconds.",
                "Only move on a NOW line. Each one is announced 3 seconds earlier",
                "(the terminal also beeps on NOW).",
                "",
                f"  {HOLD_AT_S:5.0f} s   Hold BOTH buttons through the end of the LED blink.",
                "               That selects the 1-minute bench timer. It is not setup.",
                f"  {RELEASE_AT_S:5.0f} s   Release both.",
                "               Then hands off: sleep, a wake about every 10 s,",
                f"               and two tiny green flashes near {DUE_AT_S:.0f} s.",
                f"  {VIEW_AT_S:5.0f} s   Tap the SMALL button once. Do not hold it",
                "               (holding it enters setup, which this run skips).",
                f"  {WATERED_AT_S:5.0f} s   Tap the BIG button once.",
                "",
                closing,
            ]
        ),
        flush=True,
    )


def save_run(
    out_dir: str,
    raw: bytes,
    elapsed: float,
    mods: dict[str, list[float]],
    log: list[tuple[float, str, str]],
) -> int:
    """Write raw samples, 1 ms bins, the cue log, and print the summary."""
    os.makedirs(out_dir, exist_ok=True)
    raw_path = os.path.join(out_dir, "raw.bin")
    bins_path = os.path.join(out_dir, "bins.csv")
    cues_path = os.path.join(out_dir, "cues.csv")
    with open(raw_path, "wb") as handle:
        handle.write(raw)
    stats = ppk2.summarise(raw, elapsed, MV, mods)
    if stats["samples"]:
        ppk2.write_bins(bins_path, raw, elapsed, MV, mods, BIN_MS)
    with open(cues_path, "w", encoding="ascii") as handle:
        handle.write("t_s,kind,text\n")
        for t_s, kind, text in log:
            escaped = text.replace('"', "'")
            handle.write(f"{t_s:.3f},{kind},\"{escaped}\"\n")
    sps = stats["sps"]
    print(
        f"\nSaved {out_dir}\n"
        f"{MV} mV for {stats['elapsed']:.3f} s, "
        f"{stats['samples']} samples, {sps:.0f} samples/s "
        f"({100.0 * sps / ppk2.FULL_SPS:.0f}% of {ppk2.FULL_SPS})\n"
        f"mean {ppk2.format_ua(stats['mean_ua'])}  "
        f"min {ppk2.format_ua(stats['min_ua'])}  "
        f"max {ppk2.format_ua(stats['max_ua'])}\n"
        "VOUT off.",
        flush=True,
    )
    if sps < ppk2.FULL_SPS * ppk2.MIN_SPS_FRACTION:
        print(
            "warning: sample rate is well below 100 kHz, so blocks were dropped. "
            "Move the PPK2 to the red CPU USB port and run this again. "
            "Do not trust this capture.",
            file=sys.stderr,
        )
        return 2
    return 0


def run_practice(stop: threading.Event) -> int:
    """Print the cue timeline with the PPK2 left off."""
    print("Practice only. The PPK2 stays off and nothing is recorded.", flush=True)
    clock = Clock()
    clock.start()
    run_cues(clock, stop)
    print("Practice finished.", flush=True)
    return 0


def run_capture(stop: threading.Event) -> int:
    """Power the board, cue the operator, and save the sample stream."""
    port = ppk2.find_port()
    print(ppk2.port_info(port), flush=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(ROOT, "captures", f"life-{stamp}")
    ppk = ppk2.Ppk2(port)
    chunks: list[bytes] = []
    error: list[BaseException] = []
    clock = Clock()
    log: list[tuple[float, str, str]] = []
    try:
        meta = ppk.read_metadata()
        mods = ppk2.modifiers_from(meta)
        worker = threading.Thread(
            target=capture_worker,
            args=(ppk, CAPTURE_S, clock, chunks, stop, error),
            name="ppk2-capture",
        )
        worker.start()
        if not clock.wait(5.0):
            stop.set()
            worker.join(timeout=2.0)
            raise SystemExit("PPK2 did not start the capture")
        if error:
            stop.set()
            worker.join(timeout=2.0)
            raise error[0]
        try:
            log = run_cues(clock, stop)
        except KeyboardInterrupt:
            stop.set()
            print("\nstopped early; saving what was recorded.", flush=True)
        finally:
            stop.set()
            worker.join()
        raw = b"".join(chunks)
        elapsed = clock.now()
        code = save_run(out_dir, raw, elapsed, mods, log)
        if error:
            raise error[0]
        return code
    finally:
        try:
            ppk.output_off()
        except OSError:
            pass
        ppk.close()


def main() -> int:
    """Prompt, then either practice the cues or record a capture."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--practice",
        action="store_true",
        help="print the cues on the real timeline without powering the board",
    )
    args = parser.parse_args()
    print_plan(args.practice)
    try:
        input()
    except EOFError:
        print("no terminal input; not starting", file=sys.stderr)
        return 1
    stop = threading.Event()
    try:
        if args.practice:
            return run_practice(stop)
        return run_capture(stop)
    except KeyboardInterrupt:
        stop.set()
        print("\nstopped.", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
