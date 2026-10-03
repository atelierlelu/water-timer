#ifndef RTT_LOG_H
#define RTT_LOG_H

#include <stdint.h>

/**
 * Write bytes into the RTT up-buffer for the SWD host to poll.
 * Safe with no debugger attached: extra bytes are dropped if the buffer is full.
 */
void rtt_write(const uint8_t *data, uint16_t len);

/**
 * Write a NUL-terminated string into the RTT up-buffer.
 */
void rtt_print(const char *s);

/**
 * Write an unsigned decimal value (no leading zeros except for 0).
 */
void rtt_print_u32(uint32_t value);

#endif /* RTT_LOG_H */
