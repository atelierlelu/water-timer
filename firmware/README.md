# water timer firmware (STM32L011F4, rev B)

CLI firmware for the rev B **STM32L011F4P6** board (TSSOP20). CubeMX 6.17 generated the HAL project as **EWARM only**; this tree adds a GCC `Makefile` and linker script so you can build, flash, and log from the command line.

The current image is the **plant timer** (`Core/Src/app.c`): Stop + LSI RTC, watered / view / setup on the two buttons, EEPROM interval, low-battery overlay. Waiting uses the EEPROM 1/3/7 day. How to force a one-minute due on the bench is in [Bench test](#bench-test).

## Everyday use

NRST is wired to the ST-LINK. `make flash` connects under reset, so the chip can be in Stop.

```bash
make
make flash
make picocom        # live UART on /dev/ttyACM0 (Ctrl-A Ctrl-X to quit)
```

Leave the option bytes alone. Rev B pulls BOOT0 down, so a new chip boots main flash. `make optbytes` is not part of programming this board.

A plain `st-info` while the core is in Stop can show `chipid 0x000`. That is the debug clock gated, not a dead chip. Connect under reset, or wait until the core is in Run.

The core stays in Run for **10 s after reset** with both LEDs blinking (`never_remove_swd_boot_window()` in `main.c`). **Do not remove, shorten, or move it.** It is the visible sign that the new image started, and a second way to attach if reset connect fails. After the window the plant timer starts and enters Stop.

SETUP (long-press the small button, solid red) also holds Run. The 10 s idle timer leaves SETUP and enters Stop. If a write is going without reset held, tap the big button about every 5 s until flash says verified, or a half-erased image comes back as UART garbage.

If `make flash` does not attach:

```bash
make catchflash              # retry SWD for 90 s
make catchflash CATCH_SECS=30
```

Short non-interactive capture: `make serial`. SWD RTT (`make log`) still works but halts the core; prefer UART.

## First fresh board

Factory option bytes are already the right ones. Do not run `make optbytes`.

1. Programmer first: an ST-LINK appears as `0483:374b` on USB, with UART on `/dev/ttyACM0`. Cube Programmer should show about 3.2 V. Confirm the probe is the L011 (`chipid 0x457`, 16 KiB flash), not the debugger's own MCU.
2. `make` then `make flash`. **Flash written and verified** means the image is in flash.
3. Both LEDs blink ~10 s, then go dark. UART at 115200 8N1:

```
SWD window 10s — do not strip this
EEPROM invalid: default interval_enum=1 (3 days)
water_timer L011 plant
state=WAITING elapsed_s=0 interval_s=259200 interval_enum=1 (3 days) wakeup_s=900 vdda_mv=…. low_batt=0
```

`EEPROM invalid` is normal on a new chip (empty data EEPROM → default 3-day enum). After the window the core enters Stop (`chipid 0x000` again) — that is success.

Everyday flashing after this is [Everyday use](#everyday-use). Why the option bytes stay factory is in [Boot](#boot).

## User manual

Two buttons and two LEDs. After the 10 s power-on blink (both LEDs, programming window — not a plant signal), the timer is either **waiting** (dark), **due** (green double-blink), or **setup** (solid red).

### How to read the LEDs

| What you see | Meaning |
| --- | --- |
| Both LEDs blinking together for ~10 s after battery / reset | Programming window. Ignore for plant use. |
| Dark | Waiting. Interval is running. Nothing to do. |
| One short green (right after the big button) | Watered. Timer restarted. |
| One short green (after the small button) | Current interval is **1 day**. |
| Three short greens | Current interval is **3 days**. |
| One long green (~½ s) | Current interval is **1 week**. |
| Two very brief greens, repeating every 60 s | **Due — water the plant**, then press the big button. |
| Solid red | **Setup** — you are changing the interval. Green still plays the code above. |
| Short red (with a button, or with the due blink) | Battery is low. Replace the CR2032. |

Due is always **two** shorts. One short is never “due”.

### Buttons — normal use (waiting or due)

| Button | Press | What happens |
| --- | --- | --- |
| **Big** (button 1, watered) | Short or long | “I watered.” One short green. Timer starts over. Leaves due. |
| **Small** (button 2, view) | Short (tap) | Plays the green interval code (1 day / 3 days / 1 week). |
| **Small** | Long (hold ~1.5 s) | Enter setup (red stays on). |
| Both at once | — | Ignored. |

### Buttons — setup (solid red)

Stay in setup until you leave. The green code repeats about every 2 s so you can see the current choice. Solid red also means SWD can attach — tap the big button every few seconds if you are flashing, or 10 s idle will Stop mid-write.

| Button | Press | What happens |
| --- | --- | --- |
| **Big** | Short | Cycle interval: 1 day → 3 days → 7 days → 1 day. Green plays the new code. |
| **Big** | Long | Ignored (red stays on). |
| **Small** | Short or long | Save if you changed something, then exit (red off). |
| — | 10 s with no press | Same as small: save if dirty, exit. |

A battery swap also restarts the timer (elapsed time is only in RAM). The chosen interval is kept in EEPROM.

## Hardware

| Item | Detail |
| --- | --- |
| MCU | STM32L011F4P6, Cortex-M0+, chipid `0x457` |
| Flash | 16 KiB at `0x08000000` |
| RAM | **2 KiB** at `0x20000000` (see [Memory map](#memory-map)) |
| Clock | MSI 2.097 MHz |
| RED LED | PA2 (active high) |
| GREEN LED | PA5 (active high) |
| Main button (watered) | PA6 `BUTTON_1`, active-low, external 10 kΩ + 100 nF |
| Small button (view / setup) | PA7 `BUTTON_2`, same |
| USART2 | PA9 TX / PA10 RX, 115200 8N1 |
| SWD | PA13 SWDIO / PA14 SWCLK |
| BOOT0 | PB9 / U1 pin 1. Pulled down on the board, so factory bytes boot main flash |
| NRST | Wired to the ST-LINK. `make flash` connects under reset |

Programmer is an ST-LINK/V2.1 (`0483:374b`), such as a Nucleo used as a dongle. Confirm SWD is talking to the L011 (`chipid 0x457`, 16 KiB flash), not the Nucleo’s own MCU. UART on `/dev/ttyACM0` is the L011 once the virtual COM port is wired as below.

## Host packages

```bash
sudo apt install gcc-arm-none-eabi libnewlib-arm-none-eabi stlink-tools python3 python3-serial usbutils
```

Optional: `picocom` for an interactive UART terminal. Add your user to `dialout` and `plugdev` so `st-flash` and `/dev/ttyACM0` work without root. `python3-serial` is for `tools/ppk2.py`.

Cube Programmer is the flash fallback (`STM32_Programmer_CLI` under `/opt/st/stm32cubeide_*`). Distro `st-util` is the SWD server. CubeIDE OpenOCD CLI scripts recurse here and are not usable.

## Boot

Rev B pulls PB9/BOOT0 down. Factory bytes are `nBOOT_SEL=0` (boot from the pin), `nBOOT0=0`, `nBOOT1=1`. The pulldown makes that pin read as main flash, so a new chip starts the application after `make flash`. Do not run `make optbytes`.

`make optbytes` forces `nBOOT_SEL=1`, which ignores the pin. That was a workaround for an earlier board whose BOOT0 pad floated. On this board it is unused.

| Byte | Leave it | Meaning |
| --- | --- | --- |
| `nBOOT_SEL` | 0 | BOOT0 comes from PB9 |
| `nBOOT0` | 0 | Unused while `nBOOT_SEL` is 0 |
| `nBOOT1` | 1 | Keep (0 boots empty SRAM) |
| RDP | `0xAA` | Level 0, no readout protection |

**Do not** set RDP, set `nBOOT1=0`, remap PA13/PA14, or switch the app to Standby. Those cut off SWD programming.

## UART (ST-LINK VCP)

USART2 is 115200 8N1 on **PA9 TX / PA10 RX**. `/dev/ttyACM0` is that port after the Nucleo VCP is pointed at the L011.

CN3 silkscreen is the **ST-LINK** side. UART is crossed; same-name to same-name is wrong (two TXes fight):

| L011 | Nucleo |
| --- | --- |
| PA9 `USART2_TX` | CN3 **RX** |
| PA10 `USART2_RX` | CN3 **TX** |
| GND | GND |

A stock Nucleo-64 has **SB13 and SB14 ON**, so the onboard MCU already owns the virtual COM port. Lift those bridges before using that port for the L011. Do not put L011 TX on that net while they are still fitted.

```bash
make picocom                 # interactive
make serial                  # SECONDS=8 dump
make serial SECONDS=3        # shorter
```

Expected text after a power-cycle:

```
SWD window 10s — do not strip this
water_timer L011 plant
state=WAITING elapsed_s=0 interval_s=259200 interval_enum=1 (3 days) wakeup_s=900 vdda_mv=…. low_batt=0
```

Button presses print `button 1 (main / watered): short` or `button 2 (small / view): long` (or `none` on a bounce). Setup prints enter / heartbeat / cycle / exit-reason / EEPROM lines so a hang is visible.

## Bench test

Do not wait a real day to see DUE. Waiting becomes due when `elapsed_s` reaches `interval_s`. On a plant that is 86400 / 259200 / 604800. Two ways to make `interval_s` 60 so DUE happens in about a minute. UART still prints the EEPROM name (`interval_enum=2 (7 days)`); watch `interval_s`, not that string.

**Preferred: both buttons at the end of the 10 s blink.** No rebuild. After reset both LEDs blink for 10 s (`never_remove_swd_boot_window`) — the plant timer has not started yet. When that blink **ends**, `app_init()` reads EEPROM and samples the buttons once. If **both** are held at that instant:

```
boot: both buttons held, test interval until SETUP save
state=WAITING elapsed_s=0 interval_s=60 interval_enum=… wakeup_s=10
```

RTC then wakes every 10 s; DUE at ~60 s. The EEPROM enum is unchanged (RAM flag `g_test_until_save`). It stays on 60 s until a SETUP save that actually writes EEPROM (cycle the interval so dirty, then exit). Opening SETUP and leaving without a change does not clear it. Reset or a battery pull clears it unless you hold both again.

Hold through the **end** of the blink, not only the start. During normal waiting, both buttons together are ignored; this check is boot-only.

**Compile-time override.** If `APP_TEST_INTERVAL_S` in `app.c` is non-zero, **every** boot uses that many seconds and never looks at EEPROM or the both-buttons flag. That is how you get `interval_enum=2 (7 days)` with `interval_s=60` and SETUP cannot change the wait. It is **0** in the tree (real days). Set it to `60`, `make flash`, and set it back to `0` before a plant image.

## Board bring-up

Factory check for a new board, fed from the PPK2 with the solder bridges open and the ST-LINK 3V3 jumper off. What the image checks is in [board_bringup.md](board_bringup.md). The script appends a local `board_tests.csv`; that log is not part of this tree.

```bash
make flash-boardtest          # connects under reset; PPK2 on
python3 tools/board_test.py   # prompts, then flashes the plant image
```

The test image (`Core/Src/board_test.c`) stays in Run. It checks both buttons (green = big, red = small, both LEDs = both), EEPROM, 60 s of RTC drift, and the 2400/2500 mV low-battery thresholds on the chip's own VDDA. Move only on a `NOW` line. After the buttons, leave it alone.

A passing board is then flashed with the plant timer. The measured LSI error is compiled in as `APP_RTC_SYNCH_PREDIV` (nominal 288). A plain `make flash` does **not** carry that correction, and `build/firmware.bin` after a batch is only the last board. Run the script per chip.

The EEPROM check erases the stored interval, so the plant image boots at the 3-day default. Stop-current columns in the CSV are the PPK2 floor while the ST-LINK is attached. They are not a reject limit.

## RTT logs

The default image writes a small SEGGER-compatible ring buffer in SRAM (no `BKPT`). The core keeps running if nobody is listening.

```bash
make log                 # until Ctrl-C
make log LOG_SECONDS=8   # short capture
```

`tools/swd_rtt.py` starts `st-util --no-reset`, finds `"SEGGER RTT"` in SRAM, and polls the up-buffer. Same strings as UART. `st-util` does not speak RTT itself; the Python poller does. No extra host packages.

`st-util` cannot read SRAM while the core runs, so `make log` **halts every ~150 ms**. SysTick freezes during those halts. UART does not do that — prefer `make picocom` for everyday text. `make log` also needs SWD, so use it during the 10 s boot window or while SETUP is holding Run (long-press the small button).

## Other targets

```bash
make              # plant-timer ELF/BIN/HEX (RTT on, no BKPT)
make flash        # program over SWD, connecting under reset
make catchflash   # retry SWD for CATCH_SECS (default 90)
make probe        # list ST-LINK devices
make log          # attach SWD and stream RTT (no reset)
make swdlog       # optional semihosting build + ~8 s of BKPT text
make run          # same, keep SWD attached
make serial       # dump L011 UART from PORT=/dev/ttyACM0 for SECONDS
make picocom      # interactive L011 UART on that port
make clean
make help
python3 tools/ppk2.py probe          # PPK2 port check; does not power the board
python3 tools/ppk2.py capture --mv 3300 --seconds 15
```

### If SWD is wedged

Stop gates the debug clock. `st-info` may show the dongle but `flash: 0` / `chipid: 0x000`, or `st-flash` may report `Failed to enter SWD`. That is expected while sleeping. Do **not** treat it as a dead chip.

1. **Best:** `make flash`. It connects under reset. NRST is on the ST-LINK, so Stop does not block the write.
2. If reset connect fails: long-press the small button (solid red) and tap the big button so SETUP does not idle-exit, then `make flash` again. Or power-cycle and flash during the 10 s blink.
3. Or `make catchflash`.
4. If the probe itself is stuck: kill leftover `st-util`, then reset USB and retry:

```bash
pkill st-util || true
usbreset 0483:374b
st-info --probe          # expect chipid 0x457, 16 KiB flash — only if the core is in Run
```

## UART vs SWD text

The plant timer writes the same strings to USART2 and to RTT. Use UART (`make picocom`) as the normal console. RTT is the fallback when you only have SWD. Optional `make run` / `make swdlog` also emit ARM semihosting (`BKPT 0xAB`); that image hangs if no debugger is attached, so it is not the default.

## Why `main` fixes VTOR and PRIMASK

A debugger jump (or leaving the ROM loader) can leave leftover state:

- **VTOR** still points at system memory. The first SysTick then vectors into ROM. `main` sets `SCB->VTOR = FLASH_BASE`.
- **PRIMASK** can be set, so SysTick never ticks and `HAL_Delay` hangs. `main` calls `__enable_irq()`.

## Memory map

The linker script (`STM32L011F4Px_FLASH.ld`) is **16 KiB flash / 2 KiB RAM**, heap 0, stack `0x400`. `st-info` sometimes reports 8 KiB SRAM (tool table for the L0 category). Trust the datasheet and the linker, not that SRAM number. The RTT up-buffer is 128 bytes.

## Layout

| Path | Role |
| --- | --- |
| `firmware.ioc` | CubeMX 6.17 project (do not regenerate over the GCC files) |
| `Makefile` | GCC build, flash under reset, RTT log |
| `STM32L011F4Px_FLASH.ld` | 16K/2K memory map |
| `Core/Src/main.c` | Clocks, UART, Stop, **10 s SWD boot window** (do not strip) |
| `Core/Src/app.c` | Plant-timer FSM, LEDs, buttons, EEPROM, VREFINT |
| `Core/Src/rtt_log.c` | SEGGER-compatible RTT up-buffer |
| `Core/Src/swd_log.c` | ARM semihosting, compiled in only with `-DSWD_SEMIHOSTING` |
| `tools/swd_rtt.py` | `st-util` client: hotplug and stream RTT |
| `tools/swd_semihost.py` | Optional BKPT semihosting client |
| `tools/ppk2.py` | PPK2 source-mode capture |
| `tools/board_test.py` | Bring-up script: buttons, RTC, low-battery, then plant flash |
| `Core/Src/board_test.c` | Bring-up image (separate from the plant timer) |
| `board_bringup.md` | What the bring-up image checks |

Startup is the CMSIS GCC template `startup_stm32l011xx.s`.
