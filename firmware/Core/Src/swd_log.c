#include "swd_log.h"

/* ARM semihosting operations used for host stdout. */
#define SEMIHOST_SYS_WRITE   0x05
#define SEMIHOST_SYS_WRITE0  0x04
#define SEMIHOST_STDOUT_FD   1U

#if defined(SWD_SEMIHOSTING)
/**
 * Invoke one ARM semihosting operation (Thumb BKPT 0xAB).
 * Only compiled for `make swdlog`; a BKPT with no debugger attached hangs.
 */
static int32_t swd_semihost(int32_t op, void *arg)
{
  register int32_t r0 asm("r0") = op;
  register void *r1 asm("r1") = arg;

  __asm volatile ("bkpt 0xAB" : "+r"(r0) : "r"(r1) : "memory");
  return r0;
}
#endif

/**
 * Write bytes to the debug host over SWD using ARM semihosting.
 */
void swd_write(const uint8_t *data, uint16_t len)
{
  uint32_t args[3];

  if ((data == 0) || (len == 0U))
  {
    return;
  }

  args[0] = SEMIHOST_STDOUT_FD;
  args[1] = (uint32_t)data;
  args[2] = (uint32_t)len;
#if defined(SWD_SEMIHOSTING)
  (void)swd_semihost(SEMIHOST_SYS_WRITE, args);
#else
  (void)args;
#endif
}

/**
 * Write a NUL-terminated string to the debug host over SWD.
 */
void swd_print(const char *s)
{
  if (s == 0)
  {
    return;
  }

#if defined(SWD_SEMIHOSTING)
  (void)swd_semihost(SEMIHOST_SYS_WRITE0, (void *)s);
#endif
}

/**
 * Write an unsigned decimal value (no leading zeros except for 0).
 */
void swd_print_u32(uint32_t value)
{
  char buf[10];
  uint32_t n = 0U;
  uint32_t v = value;
  uint32_t i;

  if (v == 0U)
  {
    buf[0] = '0';
    swd_write((const uint8_t *)buf, 1U);
    return;
  }

  while ((v > 0U) && (n < sizeof(buf)))
  {
    buf[n++] = (char)('0' + (v % 10U));
    v /= 10U;
  }

  for (i = 0U; i < n / 2U; i++)
  {
    char tmp = buf[i];
    buf[i] = buf[n - 1U - i];
    buf[n - 1U - i] = tmp;
  }
  swd_write((const uint8_t *)buf, (uint16_t)n);
}
