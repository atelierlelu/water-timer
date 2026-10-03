#include "rtt_log.h"

/*
 * Minimal SEGGER-RTT-compatible control block (1 up, 1 unused down).
 * Host tools look for the "SEGGER RTT" id in SRAM and poll aUp[0].
 */

#define RTT_UP_SIZE 128U

/** One RTT channel (up = target to host, down = host to target). */
typedef struct
{
  const char *name;
  char *buffer;
  uint32_t size;
  volatile uint32_t wr_off;
  volatile uint32_t rd_off;
  uint32_t flags;
} rtt_buf_t;

/** Layout matches SEGGER_RTT_CB so OpenOCD / J-Link / our poller can find it. */
typedef struct
{
  char id[16];
  int32_t max_up;
  int32_t max_down;
  rtt_buf_t up[1];
  rtt_buf_t down[1];
} rtt_cb_t;

static char rtt_up_mem[RTT_UP_SIZE];
static rtt_cb_t rtt_cb;
static uint8_t rtt_ready;

/**
 * Publish the control block. The id string is written last so a host never
 * sees a half-initialized block.
 */
static void rtt_init(void)
{
  if (rtt_ready != 0U)
  {
    return;
  }

  rtt_cb.max_up = 1;
  rtt_cb.max_down = 1;
  rtt_cb.up[0].name = "Terminal";
  rtt_cb.up[0].buffer = rtt_up_mem;
  rtt_cb.up[0].size = RTT_UP_SIZE;
  rtt_cb.up[0].wr_off = 0U;
  rtt_cb.up[0].rd_off = 0U;
  rtt_cb.up[0].flags = 0U;
  rtt_cb.down[0].name = "Terminal";
  rtt_cb.down[0].buffer = 0;
  rtt_cb.down[0].size = 0U;
  rtt_cb.down[0].wr_off = 0U;
  rtt_cb.down[0].rd_off = 0U;
  rtt_cb.down[0].flags = 0U;
  /* Compiler barrier: channel fields must be visible before the signature. */
  __asm volatile ("" ::: "memory");
  rtt_cb.id[0] = 'S';
  rtt_cb.id[1] = 'E';
  rtt_cb.id[2] = 'G';
  rtt_cb.id[3] = 'G';
  rtt_cb.id[4] = 'E';
  rtt_cb.id[5] = 'R';
  rtt_cb.id[6] = ' ';
  rtt_cb.id[7] = 'R';
  rtt_cb.id[8] = 'T';
  rtt_cb.id[9] = 'T';
  rtt_ready = 1U;
}

/**
 * Write bytes into the RTT up-buffer for the SWD host to poll.
 */
void rtt_write(const uint8_t *data, uint16_t len)
{
  uint16_t i;
  uint32_t wr;
  uint32_t rd;
  uint32_t next;

  if ((data == 0) || (len == 0U))
  {
    return;
  }

  rtt_init();
  wr = rtt_cb.up[0].wr_off;
  rd = rtt_cb.up[0].rd_off;

  for (i = 0U; i < len; i++)
  {
    next = wr + 1U;
    if (next >= RTT_UP_SIZE)
    {
      next = 0U;
    }
    /* Leave one byte empty so full and empty are distinguishable. */
    if (next == rd)
    {
      break;
    }
    rtt_up_mem[wr] = (char)data[i];
    wr = next;
  }

  __asm volatile ("" ::: "memory");
  rtt_cb.up[0].wr_off = wr;
}

/**
 * Write a NUL-terminated string into the RTT up-buffer.
 */
void rtt_print(const char *s)
{
  const char *p;

  if (s == 0)
  {
    return;
  }

  p = s;
  while (*p != '\0')
  {
    p++;
  }
  rtt_write((const uint8_t *)s, (uint16_t)(p - s));
}

/**
 * Write an unsigned decimal value (no leading zeros except for 0).
 */
void rtt_print_u32(uint32_t value)
{
  char buf[10];
  uint32_t n = 0U;
  uint32_t v = value;
  uint32_t i;

  if (v == 0U)
  {
    buf[0] = '0';
    rtt_write((const uint8_t *)buf, 1U);
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
  rtt_write((const uint8_t *)buf, (uint16_t)n);
}
