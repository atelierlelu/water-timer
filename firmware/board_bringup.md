# Board bring-up, 27 Sep 2026

Rev B boards were tested on the bench and then programmed with the plant timer. How to run the next one is in [README.md](README.md#board-bring-up).

The PPK2 fed the board through the unbridged solder bridges (Schottky diodes in series). The ST-LINK stayed connected for SWD and UART. Its 3V3 jumper stayed off, so the rail followed the PPK2. At run current the drop was about 220 mV (PPK2 at 3300 mV, chip near 3080 mV).

## What the script does

`python3 tools/board_test.py` powers the board at 3300 mV and walks one image (`make flash-boardtest`, `Core/Src/board_test.c`). That image stays in Run. The plant timer is a separate build and is flashed only after the checks pass.

1. Both LEDs blink for the 10 s programming window, then red, green, and both.
2. Operator, on a `NOW` line: hold the big button (green), the small button (red), then both (both LEDs).
3. EEPROM: write, read, and erase a test word. This wipes the stored plant interval. A new plant image therefore boots at the 3-day default.
4. RTC: 60 seconds of the LSI calendar against the host clock. The test image uses the nominal dividers, async 127 and sync 288.
5. Low battery: step the PPK2 down until the chip reports `low_batt` at or below 2400 mV, then back up. Clear counts only once the chip is again at or above 2500 mV, so a sample that settles inside the hysteresis band does not fail the board.
6. Stop current at PPK2 3200, 3000, 2800, and 2600 mV. The chip sleeps 8 s at each step. The script records the median.
7. If buttons, EEPROM, RTC, and low-battery passed, rebuild the plant image with a corrected sync prescaler and `st-flash` it while the PPK2 is still on. The row is appended either way.

RTC pass limit in the script is ±20%. The plant budget in the spec is about ±10%. Two boards were outside ±10% and still inside ±20%, and they were flashed.

## RTC correction

Drift is `(RTC seconds / wall seconds − 1) × 100`, measured with dividers 127 / 288. A positive number means the LSI ran fast.

The flashed sync prescaler is `round((1 + drift/100) × 289) − 1`. Async stays 127. That puts `ck_spre` on 1 Hz for the measured LSI. `HAL_RTC_Init` writes the prescaler only while the calendar is blank, and the backup domain keeps the old divider across reset, so `rtc_apply_prescaler()` in `main.c` writes it on every boot.

`make flash` with no extra flags still builds the nominal 288. The corrected value is compiled into that board's image only. The binary left in `build/` after a batch is the last board's image. Do not copy it onto another chip. Run the script, which rebuilds from that board's own drift.
