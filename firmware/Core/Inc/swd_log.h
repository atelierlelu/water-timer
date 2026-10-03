#ifndef SWD_LOG_H
#define SWD_LOG_H

#include <stdint.h>

/**
 * Write bytes to the debug host over SWD using ARM semihosting.
 * No-ops when no debugger is attached, so the MCU does not hang on BKPT.
 */
void swd_write(const uint8_t *data, uint16_t len);

/**
 * Write a NUL-terminated string to the debug host over SWD.
 */
void swd_print(const char *s);

/**
 * Write an unsigned decimal value (no leading zeros except for 0).
 */
void swd_print_u32(uint32_t value);

#endif /* SWD_LOG_H */
