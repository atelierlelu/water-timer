#include "app.h"

#include <stdint.h>
#include "main.h"

/** Non-zero: due after this many LSI-seconds (overrides 1/3/7 day). 0 = EEPROM. */
#ifndef APP_TEST_INTERVAL_S
#define APP_TEST_INTERVAL_S       0U
#endif

#define APP_WAITING_PERIOD_S      900U
#define APP_DUE_PERIOD_S          60U
#define APP_TEST_QUANTUM_S        10U
#define APP_BOOT_TEST_S           60U

#define APP_LED_SHORT_MS          100U
#define APP_LED_LONG_MS           600U
#define APP_LED_GAP_MS            200U
/** Due LED on-time. Short GPIO pulses, not PWM (TIM HAL would blow the 16 KiB budget). */
#define APP_DUE_LED_MS            8U
#define APP_DUE_GAP_MS            80U
#define APP_DEBOUNCE_MS           10U
#define APP_LONG_PRESS_MS         1500U
#define APP_SETUP_IDLE_MS         10000U
#define APP_SETUP_REPLAY_MS       2000U

#define APP_BATT_LOW_MV           2400U
#define APP_BATT_OK_MV            2500U
#define APP_ADC_EVERY_N_WAKES     2U

#define APP_EE_BASE               DATA_EEPROM_BASE
#define APP_EE_MAGIC              0xA5U
#define APP_EE_VERSION            1U

#define APP_BTN_DOWN(port, pin)   (HAL_GPIO_ReadPin((port), (pin)) == GPIO_PIN_RESET)

/** VREFINT_CAL at VDDA = 3.0 V (STM32L0). */
#define APP_VREFINT_CAL_ADDR      ((const uint16_t *)0x1FF80078U)
#define APP_VREFINT_CAL_MV        3000U

typedef enum
{
  APP_ST_WAITING = 0,
  APP_ST_DUE,
  APP_ST_SETUP
} app_state_t;

typedef enum
{
  APP_PRESS_NONE = 0,
  APP_PRESS_SHORT,
  APP_PRESS_LONG
} app_press_t;

static const uint32_t app_day_s[3] = {86400U, 259200U, 604800U};

static volatile uint8_t g_rtc_wake;
static volatile uint8_t g_btn1_wake;
static volatile uint8_t g_btn2_wake;

static uint32_t g_elapsed_s;
static uint32_t g_interval_s;
static uint32_t g_wakeup_s;
static uint32_t g_vdda_mv;
static uint8_t g_enum;
static uint8_t g_state;
static uint8_t g_low_batt;
static uint8_t g_dirty;
static uint8_t g_test_until_save;
static uint8_t g_wakes_since_adc;

static ADC_HandleTypeDef hadc;

/**
 * Append a NUL-terminated string and return the new end.
 */
static char *app_append_str(char *p, const char *s)
{
  while (*s != '\0')
  {
    *p++ = *s++;
  }
  return p;
}

/**
 * Append a decimal uint32_t (no leading zeros except 0).
 */
static char *app_append_u32(char *p, uint32_t value)
{
  char tmp[10];
  uint8_t n = 0U;

  if (value == 0U)
  {
    *p++ = '0';
    return p;
  }
  while ((value > 0U) && (n < 10U))
  {
    tmp[n++] = (char)('0' + (value % 10U));
    value /= 10U;
  }
  while (n > 0U)
  {
    *p++ = tmp[--n];
  }
  return p;
}

/**
 * Print a prefix and a decimal value, then CRLF.
 */
static void app_log_u32(const char *prefix, uint32_t value)
{
  char num[12];
  char *p = num;

  log_print(prefix);
  p = app_append_u32(num, value);
  *p++ = '\r';
  *p++ = '\n';
  *p = '\0';
  log_print(num);
}

/**
 * Human-readable state name for UART.
 */
static const char *app_state_name(uint8_t state)
{
  if (state == APP_ST_WAITING)
  {
    return "WAITING";
  }
  if (state == APP_ST_DUE)
  {
    return "DUE";
  }
  if (state == APP_ST_SETUP)
  {
    return "SETUP";
  }
  return "UNKNOWN";
}

/**
 * Human-readable plant interval for an EEPROM enum.
 */
static const char *app_enum_name(uint8_t e)
{
  if (e == 0U)
  {
    return "1 day";
  }
  if (e == 1U)
  {
    return "3 days";
  }
  if (e == 2U)
  {
    return "7 days";
  }
  return "invalid";
}

/**
 * Print spelled-out state and named variables to UART/RTT.
 */
static void app_log_status(void)
{
  char line[160];
  char *p = line;

  p = app_append_str(p, "state=");
  p = app_append_str(p, app_state_name(g_state));
  p = app_append_str(p, " elapsed_s=");
  p = app_append_u32(p, g_elapsed_s);
  p = app_append_str(p, " interval_s=");
  p = app_append_u32(p, g_interval_s);
  p = app_append_str(p, " interval_enum=");
  p = app_append_u32(p, (uint32_t)g_enum);
  p = app_append_str(p, " (");
  p = app_append_str(p, app_enum_name(g_enum));
  p = app_append_str(p, ") wakeup_s=");
  p = app_append_u32(p, g_wakeup_s);
  p = app_append_str(p, " vdda_mv=");
  p = app_append_u32(p, g_vdda_mv);
  p = app_append_str(p, " low_batt=");
  *p++ = (char)('0' + (g_low_batt != 0U));
  *p++ = '\r';
  *p++ = '\n';
  log_write((const uint8_t *)line, (uint16_t)(p - line));
}

/**
 * Log which button and whether the press was short, long, or a bounce.
 */
static void app_log_button(uint16_t pin, app_press_t press)
{
  log_print("button ");
  if (pin == BUTTON_1_Pin)
  {
    log_print("1 (main / watered)");
  }
  else
  {
    log_print("2 (small / view)");
  }
  log_print(": ");
  if (press == APP_PRESS_SHORT)
  {
    log_print("short\r\n");
  }
  else if (press == APP_PRESS_LONG)
  {
    log_print("long\r\n");
  }
  else
  {
    log_print("none (bounce or released too fast)\r\n");
  }
}

/**
 * Plant interval for the stored enum, unless a test override is active.
 */
static uint32_t app_interval_from_enum(void)
{
#if APP_TEST_INTERVAL_S != 0U
  (void)app_day_s;
  return APP_TEST_INTERVAL_S;
#else
  if (g_test_until_save != 0U)
  {
    return APP_BOOT_TEST_S;
  }
  if (g_enum > 2U)
  {
    return app_day_s[1];
  }
  return app_day_s[g_enum];
#endif
}

/**
 * WAITING RTC quantum: 15 min, or 10 s while the interval is a test value.
 */
static uint32_t app_waiting_period_s(void)
{
  if (g_interval_s <= 120U)
  {
    return APP_TEST_QUANTUM_S;
  }
  return APP_WAITING_PERIOD_S;
}

/**
 * Recompute WAITING vs DUE from elapsed vs interval.
 */
static void app_reevaluate(void)
{
  g_interval_s = app_interval_from_enum();
  if (g_elapsed_s >= g_interval_s)
  {
    g_state = APP_ST_DUE;
  }
  else
  {
    g_state = APP_ST_WAITING;
  }
}

/**
 * Arm the RTC wakeup for period_s of ck_spre (1 Hz).
 */
static void app_rtc_arm(uint32_t period_s)
{
  if (period_s < 1U)
  {
    period_s = 1U;
  }
  g_wakeup_s = period_s;
  (void)HAL_RTCEx_DeactivateWakeUpTimer(&hrtc);
  if (HAL_RTCEx_SetWakeUpTimer_IT(&hrtc, period_s - 1U,
                                  RTC_WAKEUPCLOCK_CK_SPRE_16BITS) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
 * Arm the RTC for the current state.
 */
static void app_rtc_arm_state(void)
{
  if (g_state == APP_ST_DUE)
  {
    app_rtc_arm(APP_DUE_PERIOD_S);
  }
  else
  {
    app_rtc_arm(app_waiting_period_s());
  }
}

/**
 * Both LEDs off.
 */
static void app_leds_off(void)
{
  HAL_GPIO_WritePin(GREEN_GPIO_Port, GREEN_Pin, GPIO_PIN_RESET);
  HAL_GPIO_WritePin(RED_GPIO_Port, RED_Pin, GPIO_PIN_RESET);
}

/**
 * Pulse green for ms, leave red as-is.
 */
static void app_green_ms(uint32_t ms)
{
  HAL_GPIO_WritePin(GREEN_GPIO_Port, GREEN_Pin, GPIO_PIN_SET);
  HAL_Delay(ms);
  HAL_GPIO_WritePin(GREEN_GPIO_Port, GREEN_Pin, GPIO_PIN_RESET);
}

/**
 * Pulse red for ms, leave green as-is.
 */
static void app_red_ms(uint32_t ms)
{
  HAL_GPIO_WritePin(RED_GPIO_Port, RED_Pin, GPIO_PIN_SET);
  HAL_Delay(ms);
  HAL_GPIO_WritePin(RED_GPIO_Port, RED_Pin, GPIO_PIN_RESET);
}

/**
 * Optional 100 ms red on a button event when the cell is low.
 */
static void app_low_batt_on_button(void)
{
  if (g_low_batt != 0U)
  {
    app_red_ms(APP_LED_SHORT_MS);
  }
}

/**
 * Green interval code: 1 short / 3 shorts / 1 long. Does not touch red.
 */
static void app_play_interval_code(uint8_t e)
{
  if (e == 0U)
  {
    app_green_ms(APP_LED_SHORT_MS);
  }
  else if (e == 1U)
  {
    app_green_ms(APP_LED_SHORT_MS);
    HAL_Delay(APP_LED_GAP_MS);
    app_green_ms(APP_LED_SHORT_MS);
    HAL_Delay(APP_LED_GAP_MS);
    app_green_ms(APP_LED_SHORT_MS);
  }
  else
  {
    app_green_ms(APP_LED_LONG_MS);
  }
}

/**
 * Due pattern: two very brief greens, once per RTC minute.
 * Red follows green if low_batt. Keep two ticks so this is not a 1-day code.
 */
static void app_play_due(void)
{
  HAL_GPIO_WritePin(GREEN_GPIO_Port, GREEN_Pin, GPIO_PIN_SET);
  if (g_low_batt != 0U)
  {
    HAL_GPIO_WritePin(RED_GPIO_Port, RED_Pin, GPIO_PIN_SET);
  }
  HAL_Delay(APP_DUE_LED_MS);
  app_leds_off();
  HAL_Delay(APP_DUE_GAP_MS);
  HAL_GPIO_WritePin(GREEN_GPIO_Port, GREEN_Pin, GPIO_PIN_SET);
  if (g_low_batt != 0U)
  {
    HAL_GPIO_WritePin(RED_GPIO_Port, RED_Pin, GPIO_PIN_SET);
  }
  HAL_Delay(APP_DUE_LED_MS);
  app_leds_off();
}

/**
 * Wait until the pin is high again, then clear its EXTI flag.
 * Blocks while the switch is held (or stuck low).
 */
static void app_wait_release(GPIO_TypeDef *port, uint16_t pin)
{
  log_print("button: waiting for release\r\n");
  while (APP_BTN_DOWN(port, pin) != 0)
  {
  }
  HAL_Delay(APP_DEBOUNCE_MS);
  __HAL_GPIO_EXTI_CLEAR_IT(pin);
  log_print("button: released\r\n");
}

/**
 * Debounce and classify one active-low button. Waits for release.
 */
static app_press_t app_classify(GPIO_TypeDef *port, uint16_t pin)
{
  uint32_t t0;
  app_press_t press = APP_PRESS_NONE;

  log_print("button: classifying");
  if (pin == BUTTON_1_Pin)
  {
    log_print(" button 1\r\n");
  }
  else
  {
    log_print(" button 2\r\n");
  }

  HAL_Delay(APP_DEBOUNCE_MS);
  if (APP_BTN_DOWN(port, pin) == 0)
  {
    __HAL_GPIO_EXTI_CLEAR_IT(pin);
    app_log_button(pin, APP_PRESS_NONE);
    return APP_PRESS_NONE;
  }

  t0 = HAL_GetTick();
  while (APP_BTN_DOWN(port, pin) != 0)
  {
    if ((HAL_GetTick() - t0) >= APP_LONG_PRESS_MS)
    {
      press = APP_PRESS_LONG;
      log_print("button: long threshold reached, still held\r\n");
      break;
    }
  }
  if (press == APP_PRESS_NONE)
  {
    press = APP_PRESS_SHORT;
  }
  app_wait_release(port, pin);
  app_log_button(pin, press);
  return press;
}

/**
 * XOR checksum of magic, version, and interval enum.
 */
static uint8_t app_ee_csum(uint8_t magic, uint8_t ver, uint8_t e)
{
  return (uint8_t)(magic ^ ver ^ e);
}

/**
 * Read the EEPROM record. Returns 0 on success.
 */
static uint8_t app_ee_read(uint8_t *e_out)
{
  const uint8_t magic = *(const uint8_t *)(APP_EE_BASE + 0U);
  const uint8_t ver = *(const uint8_t *)(APP_EE_BASE + 1U);
  const uint8_t e = *(const uint8_t *)(APP_EE_BASE + 2U);
  const uint8_t sum = *(const uint8_t *)(APP_EE_BASE + 3U);

  if ((magic != APP_EE_MAGIC) || (ver != APP_EE_VERSION) || (e > 2U) ||
      (sum != app_ee_csum(magic, ver, e)))
  {
    return 1U;
  }
  *e_out = e;
  return 0U;
}

/**
 * Store the interval enum as one little-endian word at 0x08080000.
 * Returns 0 on success.
 *
 * Erase is a 32-bit store. Cortex-M0+ HardFaults on an unaligned word, so
 * never erase/program at base+1/2/3. Pack magic, version, enum, checksum
 * and write the aligned word once.
 */
static uint8_t app_ee_write(uint8_t e)
{
  const uint8_t csum = app_ee_csum(APP_EE_MAGIC, APP_EE_VERSION, e);
  const uint32_t word = (uint32_t)APP_EE_MAGIC |
                        ((uint32_t)APP_EE_VERSION << 8) |
                        ((uint32_t)e << 16) |
                        ((uint32_t)csum << 24);
  uint32_t primask;
  HAL_StatusTypeDef st;

  log_print("setup: EEPROM unlock\r\n");
  if (HAL_FLASHEx_DATAEEPROM_Unlock() != HAL_OK)
  {
    log_print("setup: EEPROM unlock failed\r\n");
    return 1U;
  }

  log_print("setup: EEPROM erase+program one aligned word\r\n");
  primask = __get_PRIMASK();
  __disable_irq();
  st = HAL_FLASHEx_DATAEEPROM_Erase(APP_EE_BASE);
  if (st == HAL_OK)
  {
    st = HAL_FLASHEx_DATAEEPROM_Program(FLASH_TYPEPROGRAMDATA_WORD, APP_EE_BASE,
                                        word);
  }
  __set_PRIMASK(primask);
  (void)HAL_FLASHEx_DATAEEPROM_Lock();

  if (st != HAL_OK)
  {
    log_print("setup: EEPROM erase/program HAL error\r\n");
    return 1U;
  }
  return 0U;
}

/**
 * Configure ADC1 for a single VREFINT conversion (PCLK < 2.8 MHz).
 */
static void app_adc_init(void)
{
  __HAL_RCC_ADC1_CLK_ENABLE();

  hadc.Instance = ADC1;
  hadc.Init.OversamplingMode = DISABLE;
  hadc.Init.ClockPrescaler = ADC_CLOCK_SYNC_PCLK_DIV2;
  hadc.Init.Resolution = ADC_RESOLUTION_12B;
  hadc.Init.SamplingTime = ADC_SAMPLETIME_160CYCLES_5;
  hadc.Init.ScanConvMode = ADC_SCAN_DIRECTION_FORWARD;
  hadc.Init.DataAlign = ADC_DATAALIGN_RIGHT;
  hadc.Init.ContinuousConvMode = DISABLE;
  hadc.Init.DiscontinuousConvMode = DISABLE;
  hadc.Init.ExternalTrigConvEdge = ADC_EXTERNALTRIGCONVEDGE_NONE;
  hadc.Init.ExternalTrigConv = ADC_SOFTWARE_START;
  hadc.Init.DMAContinuousRequests = DISABLE;
  hadc.Init.EOCSelection = ADC_EOC_SINGLE_CONV;
  hadc.Init.Overrun = ADC_OVR_DATA_PRESERVED;
  hadc.Init.LowPowerAutoWait = DISABLE;
  hadc.Init.LowPowerFrequencyMode = ENABLE;
  hadc.Init.LowPowerAutoPowerOff = DISABLE;
  if (HAL_ADC_Init(&hadc) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
 * Sample VDDA via VREFINT. LEDs must be off. Updates g_low_batt with hysteresis.
 */
static void app_sample_vdd(void)
{
  ADC_ChannelConfTypeDef ch = {0};
  uint32_t raw;
  uint16_t cal;
  uint32_t mv;

  (void)HAL_ADCEx_EnableVREFINT();
  HAL_Delay(3);
  ch.Channel = ADC_CHANNEL_VREFINT;
  ch.Rank = ADC_RANK_CHANNEL_NUMBER;
  if (HAL_ADC_ConfigChannel(&hadc, &ch) != HAL_OK)
  {
    HAL_ADCEx_DisableVREFINT();
    return;
  }
  if (HAL_ADC_Start(&hadc) != HAL_OK)
  {
    HAL_ADCEx_DisableVREFINT();
    return;
  }
  if (HAL_ADC_PollForConversion(&hadc, 50U) != HAL_OK)
  {
    (void)HAL_ADC_Stop(&hadc);
    HAL_ADCEx_DisableVREFINT();
    return;
  }
  raw = HAL_ADC_GetValue(&hadc);
  (void)HAL_ADC_Stop(&hadc);
  HAL_ADCEx_DisableVREFINT();

  cal = *APP_VREFINT_CAL_ADDR;
  if ((raw == 0U) || (cal == 0U))
  {
    return;
  }
  mv = (APP_VREFINT_CAL_MV * (uint32_t)cal) / raw;
  g_vdda_mv = mv;
  if (mv <= APP_BATT_LOW_MV)
  {
    g_low_batt = 1U;
  }
  else if (mv >= APP_BATT_OK_MV)
  {
    g_low_batt = 0U;
  }
}

/**
 * Watered: reset elapsed, ack, return to WAITING.
 */
static void app_watered(void)
{
  g_elapsed_s = 0U;
  g_state = APP_ST_WAITING;
  app_low_batt_on_button();
  app_green_ms(APP_LED_SHORT_MS);
  app_rtc_arm_state();
  log_print("action: watered (elapsed_s reset, WAITING)\r\n");
  app_log_status();
}

/**
 * Small-button view: play the EEPROM/enum code.
 */
static void app_view(void)
{
  app_low_batt_on_button();
  log_print("action: view interval code\r\n");
  app_play_interval_code(g_enum);
  app_log_status();
}

/**
 * SETUP: solid red, cycle interval on main short, exit on small or 10 s idle.
 * Logs every path so a hang or unexpected exit is visible on UART.
 */
static void app_setup(void)
{
  uint32_t last_input = HAL_GetTick();
  uint32_t last_replay = HAL_GetTick();
  app_press_t press;

  g_state = APP_ST_SETUP;
  (void)HAL_RTCEx_DeactivateWakeUpTimer(&hrtc);
  HAL_GPIO_WritePin(RED_GPIO_Port, RED_Pin, GPIO_PIN_SET);
  g_btn1_wake = 0U;
  g_btn2_wake = 0U;
  log_print("setup: enter (solid red, RTC off, stay in Run)\r\n");
  app_log_status();
  log_print("setup: play current interval code\r\n");
  app_play_interval_code(g_enum);
  log_print("setup: interval code done, looping\r\n");

  while (g_state == APP_ST_SETUP)
  {
    if ((g_btn1_wake != 0U) || APP_BTN_DOWN(BUTTON_1_GPIO_Port, BUTTON_1_Pin))
    {
      g_btn1_wake = 0U;
      log_print("setup: button 1 activity\r\n");
      press = app_classify(BUTTON_1_GPIO_Port, BUTTON_1_Pin);
      if (press == APP_PRESS_SHORT)
      {
        g_enum = (uint8_t)((g_enum + 1U) % 3U);
        g_dirty = 1U;
        last_input = HAL_GetTick();
        last_replay = last_input;
        app_log_u32("setup: cycle interval_enum to ", (uint32_t)g_enum);
        log_print("setup: new interval is ");
        log_print(app_enum_name(g_enum));
        log_print("\r\n");
        log_print("setup: play new interval code\r\n");
        app_play_interval_code(g_enum);
        log_print("setup: interval code done\r\n");
      }
      else if (press == APP_PRESS_LONG)
      {
        last_input = HAL_GetTick();
        log_print("setup: button 1 long ignored\r\n");
      }
      else
      {
        log_print("setup: button 1 ignored (none)\r\n");
      }
    }

    if ((g_btn2_wake != 0U) || APP_BTN_DOWN(BUTTON_2_GPIO_Port, BUTTON_2_Pin))
    {
      g_btn2_wake = 0U;
      log_print("setup: button 2 activity, will exit after classify\r\n");
      (void)app_classify(BUTTON_2_GPIO_Port, BUTTON_2_Pin);
      log_print("setup: exit reason=button 2\r\n");
      break;
    }

    if ((HAL_GetTick() - last_input) >= APP_SETUP_IDLE_MS)
    {
      log_print("setup: exit reason=idle timeout 10 s\r\n");
      break;
    }

    if ((HAL_GetTick() - last_replay) >= APP_SETUP_REPLAY_MS)
    {
      last_replay = HAL_GetTick();
      app_log_u32("setup: heartbeat still in loop, idle_ms=",
                  HAL_GetTick() - last_input);
      log_print("setup: replay interval code\r\n");
      app_play_interval_code(g_enum);
      log_print("setup: replay done\r\n");
    }
  }

  log_print("setup: left loop, dirty=");
  log_print((g_dirty != 0U) ? "1\r\n" : "0\r\n");

  if (g_dirty != 0U)
  {
    log_print("setup: EEPROM write start\r\n");
    if (app_ee_write(g_enum) == 0U)
    {
      g_dirty = 0U;
      g_test_until_save = 0U;
      log_print("setup: EEPROM write ok\r\n");
    }
    else
    {
      log_print("setup: EEPROM write failed\r\n");
    }
  }
  else
  {
    log_print("setup: EEPROM unchanged\r\n");
  }

  HAL_GPIO_WritePin(RED_GPIO_Port, RED_Pin, GPIO_PIN_RESET);
  log_print("setup: red off, re-evaluate WAITING/DUE\r\n");
  app_reevaluate();
  app_rtc_arm_state();
  log_print("setup: exit complete\r\n");
  app_log_status();
}

/**
 * RTC wakeup: add the armed period only here, then due-check or pulse.
 */
static void app_on_rtc(void)
{
  if (g_state == APP_ST_SETUP)
  {
    log_print("RTC wakeup ignored (still in SETUP)\r\n");
    return;
  }

  log_print("RTC wakeup\r\n");
  g_elapsed_s += g_wakeup_s;

  if (g_state == APP_ST_WAITING)
  {
    app_reevaluate();
    if (g_state == APP_ST_DUE)
    {
      app_rtc_arm_state();
      app_play_due();
    }
    else
    {
      g_wakes_since_adc++;
      if (g_wakes_since_adc >= APP_ADC_EVERY_N_WAKES)
      {
        g_wakes_since_adc = 0U;
        app_sample_vdd();
      }
      if (g_low_batt != 0U)
      {
        app_red_ms(APP_LED_SHORT_MS);
      }
      app_rtc_arm_state();
    }
  }
  else
  {
    app_play_due();
    app_rtc_arm_state();
  }
  app_log_status();
}

/**
 * Main or small button while WAITING/DUE. Both together are ignored.
 */
static void app_on_buttons(void)
{
  const uint8_t both = (uint8_t)((g_btn1_wake != 0U) && (g_btn2_wake != 0U));

  if (both != 0U)
  {
    g_btn1_wake = 0U;
    g_btn2_wake = 0U;
    app_wait_release(BUTTON_1_GPIO_Port, BUTTON_1_Pin);
    app_wait_release(BUTTON_2_GPIO_Port, BUTTON_2_Pin);
    log_print("both buttons together: ignored\r\n");
    return;
  }

  if (g_btn1_wake != 0U)
  {
    g_btn1_wake = 0U;
    if (app_classify(BUTTON_1_GPIO_Port, BUTTON_1_Pin) != APP_PRESS_NONE)
    {
      app_watered();
    }
    return;
  }

  if (g_btn2_wake != 0U)
  {
    g_btn2_wake = 0U;
    if (app_classify(BUTTON_2_GPIO_Port, BUTTON_2_Pin) == APP_PRESS_LONG)
    {
      app_setup();
    }
    else
    {
      app_view();
    }
  }
}

/**
 * RTC wakeup callback: set a flag only. Elapsed is added in app_on_rtc().
 */
void HAL_RTCEx_WakeUpTimerEventCallback(RTC_HandleTypeDef *hrtc)
{
  (void)hrtc;
  g_rtc_wake = 1U;
}

/**
 * Button EXTI: falling edge, active-low.
 */
void HAL_GPIO_EXTI_Callback(uint16_t pin)
{
  if (pin == BUTTON_1_Pin)
  {
    g_btn1_wake = 1U;
  }
  else if (pin == BUTTON_2_Pin)
  {
    g_btn2_wake = 1U;
  }
}

/**
 * Load EEPROM, optional boot test-hold, first Vdd sample, arm RTC.
 */
void app_init(void)
{
  g_enum = 1U;
  if (app_ee_read(&g_enum) != 0U)
  {
    g_enum = 1U;
    log_print("EEPROM invalid: default interval_enum=1 (3 days)\r\n");
  }

  HAL_Delay(APP_DEBOUNCE_MS);
  if (APP_BTN_DOWN(BUTTON_1_GPIO_Port, BUTTON_1_Pin) &&
      APP_BTN_DOWN(BUTTON_2_GPIO_Port, BUTTON_2_Pin))
  {
    g_test_until_save = 1U;
    log_print("boot: both buttons held, test interval until SETUP save\r\n");
    app_wait_release(BUTTON_1_GPIO_Port, BUTTON_1_Pin);
    app_wait_release(BUTTON_2_GPIO_Port, BUTTON_2_Pin);
  }

  g_elapsed_s = 0U;
  g_state = APP_ST_WAITING;
  g_interval_s = app_interval_from_enum();
  app_adc_init();
  app_sample_vdd();
  app_rtc_arm_state();
  log_print("water_timer L011 plant\r\n");
  app_log_status();
}

/**
 * Drain wake flags. SETUP runs until it exits.
 */
void app_process(void)
{
  if (g_rtc_wake != 0U)
  {
    g_rtc_wake = 0U;
    app_on_rtc();
  }
  if ((g_btn1_wake != 0U) || (g_btn2_wake != 0U))
  {
    app_on_buttons();
  }
}
