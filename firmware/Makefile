# CLI build/flash/serial for the custom STM32L011F4 CubeMX project.
# Requires: gcc-arm-none-eabi, libnewlib-arm-none-eabi, stlink-tools
# Optional serial: picocom

TARGET = firmware
BUILD_DIR = build

# Cross compiler (from apt: gcc-arm-none-eabi)
PREFIX = arm-none-eabi-
CC  = $(PREFIX)gcc
AS  = $(PREFIX)gcc -x assembler-with-cpp
CP  = $(PREFIX)objcopy
SZ  = $(PREFIX)size

# MCU: STM32L011F4Px Cortex-M0+
CPU = -mcpu=cortex-m0plus
MCU = $(CPU) -mthumb -mfloat-abi=soft

C_DEFS = -DUSE_HAL_DRIVER -DSTM32L011xx

C_INCLUDES = \
  -ICore/Inc \
  -IDrivers/STM32L0xx_HAL_Driver/Inc \
  -IDrivers/STM32L0xx_HAL_Driver/Inc/Legacy \
  -IDrivers/CMSIS/Device/ST/STM32L0xx/Include \
  -IDrivers/CMSIS/Include

# Application and the HAL modules CubeMX enabled for this project.
C_SOURCES = \
  Core/Src/main.c \
  Core/Src/app.c \
  Core/Src/rtt_log.c \
  Core/Src/swd_log.c \
  Core/Src/stm32l0xx_it.c \
  Core/Src/stm32l0xx_hal_msp.c \
  Core/Src/system_stm32l0xx.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_adc.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_adc_ex.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_cortex.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_dma.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_exti.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_flash.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_flash_ex.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_flash_ramfunc.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_gpio.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_pwr.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_pwr_ex.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_rcc.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_rcc_ex.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_rtc.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_rtc_ex.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_uart.c \
  Drivers/STM32L0xx_HAL_Driver/Src/stm32l0xx_hal_uart_ex.c

# GCC startup (vector table + reset) from CMSIS Device templates.
ASM_SOURCES = Drivers/CMSIS/Device/ST/STM32L0xx/Source/Templates/gcc/startup_stm32l011xx.s

LDSCRIPT = STM32L011F4Px_FLASH.ld

OPT = -Os
CFLAGS = $(MCU) $(C_DEFS) $(C_INCLUDES) $(OPT) -Wall -fdata-sections -ffunction-sections -g
ASFLAGS = $(MCU) $(C_DEFS) $(C_INCLUDES) $(OPT) -Wall -fdata-sections -ffunction-sections -g

# newlib-nano + nosys stubs (no host OS syscalls).
LIBS = -lc -lm -lnosys
LDFLAGS = $(MCU) -specs=nano.specs -T$(LDSCRIPT) $(LIBS) \
  -Wl,-Map=$(BUILD_DIR)/$(TARGET).map,--cref -Wl,--gc-sections

# ST-Link VCP on the Nucleo dongle is usually /dev/ttyACM0.
PORT ?= /dev/ttyACM0
BAUD ?= 115200
# How many seconds `make serial` listens (agent-friendly; not an interactive TTY).
SECONDS ?= 8

# CubeIDE programmer used as a fallback if st-flash cannot attach.
CUBEPROG ?= $(firstword $(wildcard /opt/st/stm32cubeide_*/plugins/com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.linux64_*/tools/bin/STM32_Programmer_CLI))

# st-util (stlink-tools) serves SWD + ARM semihosting; see tools/swd_semihost.py.

OBJECTS = $(addprefix $(BUILD_DIR)/,$(notdir $(C_SOURCES:.c=.o)))
vpath %.c $(sort $(dir $(C_SOURCES)))
OBJECTS += $(addprefix $(BUILD_DIR)/,$(notdir $(ASM_SOURCES:.s=.o)))
vpath %.s $(sort $(dir $(ASM_SOURCES)))

# Factory test image. Separate build dir so `make flash` stays the plant timer.
BOARDTEST_SOURCES = $(filter-out Core/Src/app.c,$(C_SOURCES)) Core/Src/board_test.c
BOARDTEST_DIR = build/boardtest

.PHONY: all clean flash catchflash probe serial picocom swdlog run log optbytes size help boardtest flash-boardtest

all: $(BUILD_DIR)/$(TARGET).elf $(BUILD_DIR)/$(TARGET).hex $(BUILD_DIR)/$(TARGET).bin size

$(BUILD_DIR)/%.o: %.c Makefile | $(BUILD_DIR)
	$(CC) -c $(CFLAGS) -Wa,-a,-ad,-alms=$(BUILD_DIR)/$(notdir $(<:.c=.lst)) $< -o $@

$(BUILD_DIR)/%.o: %.s Makefile | $(BUILD_DIR)
	$(AS) -c $(ASFLAGS) $< -o $@

$(BUILD_DIR)/$(TARGET).elf: $(OBJECTS) Makefile
	$(CC) $(OBJECTS) $(LDFLAGS) -o $@

$(BUILD_DIR)/%.hex: $(BUILD_DIR)/%.elf | $(BUILD_DIR)
	$(CP) -O ihex $< $@

$(BUILD_DIR)/%.bin: $(BUILD_DIR)/%.elf | $(BUILD_DIR)
	$(CP) -O binary -S $< $@

$(BUILD_DIR):
	mkdir -p $@

# Print flash/RAM usage after a successful link.
size: $(BUILD_DIR)/$(TARGET).elf
	$(SZ) $<

# List connected ST-Link probes.
probe:
	st-info --probe

# Retry SWD. Rev B wires NRST to the ST-LINK, so connect-under-reset is tried first.
# Hot-plug is the fallback when reset is not held.
# A plain st-info while the core is in Stop can still show chipid 0x000.
CATCH_SECS ?= 90
catchflash: $(BUILD_DIR)/$(TARGET).bin
	@echo "hammering SWD for $(CATCH_SECS)s to catch a wake window..."
	@bash -c 'end=$$(($$(date +%s)+$(CATCH_SECS))); n=0; \
	while [ $$(date +%s) -lt $$end ]; do \
	  n=$$((n+1)); \
	  if st-flash --connect-under-reset --freq=100 --reset write "$<" 0x08000000; then \
	    echo "catchflash: ok under reset on try $$n"; exit 0; \
	  fi; \
	  if st-flash --hot-plug --freq=100 --reset write "$<" 0x08000000; then \
	    echo "catchflash: ok on try $$n"; exit 0; \
	  fi; \
	done; \
	echo "catchflash: no SWD in $(CATCH_SECS)s ($$n tries)"; exit 1'

# Build the bring-up image (LEDs, buttons, EEPROM, RTC, VDDA). Does not flash.
boardtest:
	$(MAKE) all BUILD_DIR=$(BOARDTEST_DIR) \
	  C_DEFS="$(C_DEFS) -DBOARD_TEST" \
	  C_SOURCES="$(BOARDTEST_SOURCES)"

# Program the bring-up image. Connect under reset; NRST is on the ST-LINK.
flash-boardtest: boardtest
	@if st-flash --connect-under-reset --reset write $(BOARDTEST_DIR)/$(TARGET).bin 0x08000000; then \
	  exit 0; \
	fi; \
	if st-flash --reset write $(BOARDTEST_DIR)/$(TARGET).bin 0x08000000; then \
	  exit 0; \
	fi; \
	if [ -n "$(CUBEPROG)" ]; then \
	  echo "st-flash failed; trying STM32_Programmer_CLI"; \
	  "$(CUBEPROG)" -c port=SWD freq=4000 -w $(BOARDTEST_DIR)/$(TARGET).bin 0x08000000 -v -rst; \
	else \
	  echo "flash-boardtest failed (st-flash attach error, no Cube Programmer)"; \
	  exit 1; \
	fi

# Write the binary over SWD. Hold NRST during connect so Stop does not hide the core.
# Fall back to a normal connect, then to Cube Programmer.
flash: $(BUILD_DIR)/$(TARGET).bin
	@if st-flash --connect-under-reset --reset write $< 0x08000000; then \
	  exit 0; \
	fi; \
	if st-flash --reset write $< 0x08000000; then \
	  exit 0; \
	fi; \
	if [ -n "$(CUBEPROG)" ]; then \
	  echo "st-flash failed; trying STM32_Programmer_CLI"; \
	  "$(CUBEPROG)" -c port=SWD freq=4000 -w $< 0x08000000 -v -rst; \
	else \
	  echo "flash failed (st-flash attach error, no Cube Programmer)"; \
	  exit 1; \
	fi

# Agent-friendly UART capture: configure 8N1 and dump $(SECONDS) of output.
serial:
	@test -e $(PORT) || (echo "Serial port $(PORT) not found. Is the Nucleo plugged in?"; exit 1)
	stty -F $(PORT) $(BAUD) cs8 -cstopb -parenb raw -echo
	timeout $(SECONDS) cat $(PORT) || true

# Interactive serial terminal for a human at the keyboard.
picocom:
	picocom -b $(BAUD) --imap lfcrlf $(PORT)

# Rebuild with BKPT semihosting, flash, and print SWD text for $(SECONDS).
# Default `make flash` omits BKPT so the blinky does not hang without a debugger.
swdlog:
	$(MAKE) clean all C_DEFS="$(C_DEFS) -DSWD_SEMIHOSTING"
	$(MAKE) flash
	python3 tools/swd_semihost.py --seconds $(SECONDS)

# Same as swdlog but keep SWD attached. The semihosting image hangs if no
# debugger is attached, so this target is only for live text.
run:
	$(MAKE) clean all C_DEFS="$(C_DEFS) -DSWD_SEMIHOSTING"
	$(MAKE) flash
	python3 tools/swd_semihost.py --seconds 0

# Attach over SWD without reset and stream RTT (app keeps running standalone).
# LOG_SECONDS=0 means until Ctrl-C; use LOG_SECONDS=8 for a short capture.
LOG_SECONDS ?= 0
log:
	python3 tools/swd_rtt.py --seconds $(LOG_SECONDS)

# Force nBOOT_SEL=1 so the chip ignores PB9. Rev B pulls BOOT0 down and boots
# main flash with the factory bytes, so this target is not used on that board.
# Does not change RDP or WRP. nBOOT0=1 + nBOOT1=1: flash, if the pin is ignored.
optbytes:
	@if [ -z "$(CUBEPROG)" ]; then echo "STM32_Programmer_CLI not found"; exit 1; fi
	"$(CUBEPROG)" -c port=SWD freq=4000 -ob nBOOT_SEL=1 nBOOT0=1 nBOOT1=1
	"$(CUBEPROG)" -c port=SWD freq=4000 -ob displ

clean:
	rm -rf $(BUILD_DIR)

help:
	@echo "make           - build firmware.elf/.bin/.hex"
	@echo "make flash     - program over SWD, connecting under reset"
	@echo "make catchflash - retry SWD for CATCH_SECS=$(CATCH_SECS)"
	@echo "make probe     - list ST-Link devices"
	@echo "make serial   - print UART for SECONDS=$(SECONDS) from PORT=$(PORT)"
	@echo "make picocom  - interactive serial terminal"
	@echo "make log      - attach SWD and stream RTT (no reset; LOG_SECONDS=0)"
	@echo "make swdlog   - flash semihosting build and print SWD text"
	@echo "make run      - same, but keep SWD attached (live LEDs + text)"
	@echo "make optbytes - unused on rev B; forces the chip to ignore BOOT0"
	@echo "make boardtest - build the bring-up image in $(BOARDTEST_DIR)/"
	@echo "make flash-boardtest - program that image under reset"
	@echo "python3 tools/board_test.py - bring-up, then plant image with RTC correction"
	@echo "make clean    - remove $(BUILD_DIR)/"
