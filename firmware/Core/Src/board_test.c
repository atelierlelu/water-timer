#include "board_test.h"

#include <stdint.h>
#include "main.h"

/**
 * Bring-up image for a new board. The plant timer is not linked in.
 *
 * Stays in Run for the whole test. Stop would drop the debug clock and the
 * ST-LINK would lose the chip. Sleep current is measured with the plant image,
 * not here.
 *
 * Low-battery thresholds match the product: trip at 2.4 V, clear at 2.5 V.
 * The sample is taken with both LEDs off. On a board whose solder bridges are
 * open, the PPK2 is upstream of the Schottky, so the PPK2 voltage is higher
 * than this reading by the diode drop.
 */

#define BT_LOW_MV             2400U
#define BT_OK_MV              2500U
#define BT_LED_MS             700U
#define BT_DEBOUNCE_MS        10U
#define BT_VREF_SETTLE_MS     3U
#define BT_LED_OFF_SETTLE_MS  50U
#define BT_VDDA_PERIOD_MS     200U
#define BT_RTC_SECONDS        60U
/** Stop window. Longer than the host's current capture so the wake is not included. */
#define BT_SLEEP_S            8U

#define BT_EE_BASE            DATA_EEPROM_BASE
#define BT_EE_TEST_WORD       0x54534554U /* 'TEST' little-endian; not plant magic */

#define BT_VREFINT_CAL_ADDR   ((const uint16_t *)0x1FF80078U)
#define BT_VREFINT_CAL_MV     3000U

#define BT_DOWN(port, pin)    (HAL_GPIO_ReadPin((port), (pin)) == GPIO_PIN_RESET)

static ADC_HandleTypeDef bt_adc;
static uint8_t bt_low_batt;

extern UART_HandleTypeDef huart2;
void SystemClock_Config(void);

/**
 * Append a decimal uint32_t. No leading zeros except for zero itself.
 */
static char *bt_put_u32(char *p, uint32_t value)
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
 * Print a prefix, a decimal, and a suffix (the suffix includes CRLF).
 */
static void bt_log_u32(const char *prefix, uint32_t value, const char *suffix)
{
  char line[80];
  char *p = line;

  while (*prefix != '\0')
  {
    *p++ = *prefix++;
  }
  p = bt_put_u32(p, value);
  while (*suffix != '\0')
  {
    *p++ = *suffix++;
  }
  *p = '\0';
  log_print(line);
}

/**
 * Drive the two LEDs. Non-zero is on.
 */
static void bt_leds(uint8_t red, uint8_t green)
{
  HAL_GPIO_WritePin(RED_GPIO_Port, RED_Pin, red ? GPIO_PIN_SET : GPIO_PIN_RESET);
  HAL_GPIO_WritePin(GREEN_GPIO_Port, GREEN_Pin, green ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

/**
 * Append eight uppercase hex digits.
 */
static char *bt_put_hex32(char *p, uint32_t value)
{
  static const char hex[] = "0123456789ABCDEF";
  int8_t shift;

  for (shift = 28; shift >= 0; shift = (int8_t)(shift - 4))
  {
    *p++ = hex[(value >> (uint8_t)shift) & 0xFU];
  }
  return p;
}

/**
 * Print the 96-bit factory UID at UID_BASE as 24 hex digits.
 */
static void bt_log_uid(void)
{
  char line[40];
  char *p = line;
  const char *prefix = "boardtest: uid=";

  while (*prefix != '\0')
  {
    *p++ = *prefix++;
  }
  p = bt_put_hex32(p, *(const volatile uint32_t *)UID_BASE);
  p = bt_put_hex32(p, *(const volatile uint32_t *)(UID_BASE + 4U));
  p = bt_put_hex32(p, *(const volatile uint32_t *)(UID_BASE + 8U));
  *p++ = '\r';
  *p++ = '\n';
  *p = '\0';
  log_print(line);
}

/**
 * Red alone, green alone, then both. A dead LED is obvious before any button.
 */
static void bt_led_walk(void)
{
  log_print("boardtest: phase leds\r\n");
  bt_leds(1U, 0U);
  HAL_Delay(BT_LED_MS);
  bt_leds(0U, 0U);
  HAL_Delay(200U);
  bt_leds(0U, 1U);
  HAL_Delay(BT_LED_MS);
  bt_leds(0U, 0U);
  HAL_Delay(200U);
  bt_leds(1U, 1U);
  HAL_Delay(BT_LED_MS);
  bt_leds(0U, 0U);
}

/**
 * Read both buttons twice. Returns 1 only when the two reads agree.
 * Big is BUTTON_1, small is BUTTON_2, active low.
 */
static uint8_t bt_read_stable(uint8_t *big, uint8_t *small)
{
  uint8_t big1;
  uint8_t small1;
  uint8_t big2;
  uint8_t small2;

  big1 = (uint8_t)BT_DOWN(BUTTON_1_GPIO_Port, BUTTON_1_Pin);
  small1 = (uint8_t)BT_DOWN(BUTTON_2_GPIO_Port, BUTTON_2_Pin);
  HAL_Delay(BT_DEBOUNCE_MS);
  big2 = (uint8_t)BT_DOWN(BUTTON_1_GPIO_Port, BUTTON_1_Pin);
  small2 = (uint8_t)BT_DOWN(BUTTON_2_GPIO_Port, BUTTON_2_Pin);
  if ((big1 != big2) || (small1 != small2))
  {
    return 0U;
  }
  *big = big2;
  *small = small2;
  return 1U;
}

/**
 * Mirror the buttons: big lights green, small lights red, both lights both.
 */
static void bt_show_buttons(uint8_t big, uint8_t small)
{
  bt_leds(small, big);
}

/**
 * Log one stable button sample for the host script.
 */
static void bt_log_buttons(uint8_t big, uint8_t small)
{
  log_print("boardtest: btn big=");
  log_print(big ? "1" : "0");
  log_print(" small=");
  log_print(small ? "1\r\n" : "0\r\n");
}

/**
 * Wait until the operator has held big alone, small alone, and both.
 */
static void bt_buttons(void)
{
  uint8_t prev_big = 0xFFU;
  uint8_t prev_small = 0xFFU;
  uint8_t seen_big = 0U;
  uint8_t seen_small = 0U;
  uint8_t seen_both = 0U;

  log_print("boardtest: phase buttons\r\n");
  while ((seen_big == 0U) || (seen_small == 0U) || (seen_both == 0U))
  {
    uint8_t big = 0U;
    uint8_t small = 0U;

    if (bt_read_stable(&big, &small) == 0U)
    {
      continue;
    }
    bt_show_buttons(big, small);
    if ((big == prev_big) && (small == prev_small))
    {
      continue;
    }
    prev_big = big;
    prev_small = small;
    bt_log_buttons(big, small);
    if ((big != 0U) && (small == 0U))
    {
      seen_big = 1U;
    }
    if ((big == 0U) && (small != 0U))
    {
      seen_small = 1U;
    }
    if ((big != 0U) && (small != 0U))
    {
      seen_both = 1U;
    }
  }
  bt_leds(0U, 0U);
  log_print("boardtest: buttons pass\r\n");
}

/**
 * Erase or program one aligned EEPROM word. Returns 0 on success.
 */
static uint8_t bt_ee_store(uint32_t word, uint8_t do_program)
{
  uint32_t primask;
  HAL_StatusTypeDef st = HAL_ERROR;

  if (HAL_FLASHEx_DATAEEPROM_Unlock() != HAL_OK)
  {
    return 1U;
  }
  primask = __get_PRIMASK();
  __disable_irq();
  st = HAL_FLASHEx_DATAEEPROM_Erase(BT_EE_BASE);
  if ((st == HAL_OK) && (do_program != 0U))
  {
    st = HAL_FLASHEx_DATAEEPROM_Program(FLASH_TYPEPROGRAMDATA_WORD, BT_EE_BASE, word);
  }
  __set_PRIMASK(primask);
  (void)HAL_FLASHEx_DATAEEPROM_Lock();
  return (st == HAL_OK) ? 0U : 1U;
}

/**
 * Write a test word, read it back, then erase it.
 * Erase leaves 0, which the plant image rejects, so the interval stays at the default.
 */
static void bt_eeprom(void)
{
  uint32_t got;

  log_print("boardtest: phase eeprom\r\n");
  if (bt_ee_store(BT_EE_TEST_WORD, 1U) != 0U)
  {
    log_print("boardtest: eeprom fail write\r\n");
    return;
  }
  got = *(volatile uint32_t *)BT_EE_BASE;
  if (got != BT_EE_TEST_WORD)
  {
    log_print("boardtest: eeprom fail read\r\n");
    (void)bt_ee_store(0U, 0U);
    return;
  }
  if (bt_ee_store(0U, 0U) != 0U)
  {
    log_print("boardtest: eeprom fail erase\r\n");
    return;
  }
  got = *(volatile uint32_t *)BT_EE_BASE;
  if (got != 0U)
  {
    log_print("boardtest: eeprom fail blank\r\n");
    return;
  }
  log_print("boardtest: eeprom pass\r\n");
}

/**
 * Set the RTC calendar back to zero so the accuracy window starts clean.
 */
static void bt_rtc_zero(void)
{
  RTC_TimeTypeDef time = {0};
  RTC_DateTypeDef date = {0};

  time.Hours = 0U;
  time.Minutes = 0U;
  time.Seconds = 0U;
  time.TimeFormat = RTC_HOURFORMAT12_AM;
  time.DayLightSaving = RTC_DAYLIGHTSAVING_NONE;
  time.StoreOperation = RTC_STOREOPERATION_RESET;
  date.WeekDay = RTC_WEEKDAY_THURSDAY;
  date.Month = 1U;
  date.Date = 1U;
  date.Year = 26U;
  (void)HAL_RTC_SetTime(&hrtc, &time, RTC_FORMAT_BIN);
  (void)HAL_RTC_SetDate(&hrtc, &date, RTC_FORMAT_BIN);
}

/**
 * Seconds since midnight from the RTC. Date must be read to unlock the shadow registers.
 */
static uint32_t bt_rtc_sod(void)
{
  RTC_TimeTypeDef time = {0};
  RTC_DateTypeDef date = {0};

  (void)HAL_RTC_GetTime(&hrtc, &time, RTC_FORMAT_BIN);
  (void)HAL_RTC_GetDate(&hrtc, &date, RTC_FORMAT_BIN);
  return ((uint32_t)time.Hours * 3600U) +
         ((uint32_t)time.Minutes * 60U) +
         (uint32_t)time.Seconds;
}

/**
 * Print s=0 through s=60 as the RTC counts. The host compares that to wall time.
 */
static void bt_rtc(void)
{
  uint32_t last = 0xFFFFFFFFU;
  uint32_t now;

  log_print("boardtest: phase rtc\r\n");
  bt_rtc_zero();
  while (last != BT_RTC_SECONDS)
  {
    now = bt_rtc_sod();
    if (now != last)
    {
      last = now;
      bt_log_u32("boardtest: rtc s=", now, "\r\n");
    }
    HAL_Delay(20U);
  }
}

/**
 * ADC1, one VREFINT channel. PCLK is the 2.1 MHz MSI, divided by 2.
 */
static void bt_adc_init(void)
{
  __HAL_RCC_ADC1_CLK_ENABLE();
  bt_adc.Instance = ADC1;
  bt_adc.Init.OversamplingMode = DISABLE;
  bt_adc.Init.ClockPrescaler = ADC_CLOCK_SYNC_PCLK_DIV2;
  bt_adc.Init.Resolution = ADC_RESOLUTION_12B;
  bt_adc.Init.SamplingTime = ADC_SAMPLETIME_160CYCLES_5;
  bt_adc.Init.ScanConvMode = ADC_SCAN_DIRECTION_FORWARD;
  bt_adc.Init.DataAlign = ADC_DATAALIGN_RIGHT;
  bt_adc.Init.ContinuousConvMode = DISABLE;
  bt_adc.Init.DiscontinuousConvMode = DISABLE;
  bt_adc.Init.ExternalTrigConvEdge = ADC_EXTERNALTRIGCONVEDGE_NONE;
  bt_adc.Init.ExternalTrigConv = ADC_SOFTWARE_START;
  bt_adc.Init.DMAContinuousRequests = DISABLE;
  bt_adc.Init.EOCSelection = ADC_EOC_SINGLE_CONV;
  bt_adc.Init.Overrun = ADC_OVR_DATA_PRESERVED;
  bt_adc.Init.LowPowerAutoWait = DISABLE;
  bt_adc.Init.LowPowerFrequencyMode = ENABLE;
  bt_adc.Init.LowPowerAutoPowerOff = DISABLE;
  if (HAL_ADC_Init(&bt_adc) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
 * Measure VDDA from VREFINT. LEDs must already be off.
 * Returns 1 and stores millivolts on success.
 */
static uint8_t bt_sample_vdd(uint32_t *mv_out)
{
  ADC_ChannelConfTypeDef ch = {0};
  uint32_t raw;
  uint16_t cal;

  (void)HAL_ADCEx_EnableVREFINT();
  HAL_Delay(BT_VREF_SETTLE_MS);
  ch.Channel = ADC_CHANNEL_VREFINT;
  ch.Rank = ADC_RANK_CHANNEL_NUMBER;
  if (HAL_ADC_ConfigChannel(&bt_adc, &ch) != HAL_OK)
  {
    HAL_ADCEx_DisableVREFINT();
    return 0U;
  }
  if (HAL_ADC_Start(&bt_adc) != HAL_OK)
  {
    HAL_ADCEx_DisableVREFINT();
    return 0U;
  }
  if (HAL_ADC_PollForConversion(&bt_adc, 50U) != HAL_OK)
  {
    (void)HAL_ADC_Stop(&bt_adc);
    HAL_ADCEx_DisableVREFINT();
    return 0U;
  }
  raw = HAL_ADC_GetValue(&bt_adc);
  (void)HAL_ADC_Stop(&bt_adc);
  HAL_ADCEx_DisableVREFINT();

  cal = *BT_VREFINT_CAL_ADDR;
  if ((raw == 0U) || (cal == 0U))
  {
    return 0U;
  }
  *mv_out = (BT_VREFINT_CAL_MV * (uint32_t)cal) / raw;
  return 1U;
}

/**
 * Apply the product hysteresis to one LED-off sample.
 */
static void bt_update_low(uint32_t mv)
{
  if (mv <= BT_LOW_MV)
  {
    bt_low_batt = 1U;
  }
  else if (mv >= BT_OK_MV)
  {
    bt_low_batt = 0U;
  }
}

/**
 * One byte from the ST-LINK VCP, or 0 if the receiver is empty.
 */
static uint8_t bt_rx_byte(uint8_t *out)
{
  if (__HAL_UART_GET_FLAG(&huart2, UART_FLAG_RXNE) == RESET)
  {
    return 0U;
  }
  *out = (uint8_t)(huart2.Instance->RDR & 0xFFU);
  return 1U;
}

/**
 * Enter Stop and wake on the RTC. Same ULP setup as the plant image.
 */
static void bt_enter_stop(void)
{
  __HAL_PWR_CLEAR_FLAG(PWR_FLAG_WU);
  __HAL_GPIO_EXTI_CLEAR_IT(BUTTON_1_Pin | BUTTON_2_Pin);
  HAL_DBGMCU_DisableDBGStopMode();
  HAL_PWREx_EnableUltraLowPower();
  HAL_SuspendTick();
  HAL_PWR_EnterSTOPMode(PWR_LOWPOWERREGULATOR_ON, PWR_STOPENTRY_WFI);
  HAL_ResumeTick();
  SystemClock_Config();
}

/**
 * Sleep for BT_SLEEP_S so the host can measure Stop current, then come back.
 */
static void bt_sleep_once(void)
{
  bt_leds(0U, 0U);
  log_print("boardtest: sleeping\r\n");
  (void)HAL_RTCEx_DeactivateWakeUpTimer(&hrtc);
  (void)HAL_RTCEx_SetWakeUpTimer_IT(&hrtc, BT_SLEEP_S - 1U,
                                    RTC_WAKEUPCLOCK_CK_SPRE_16BITS);
  bt_enter_stop();
  (void)HAL_RTCEx_DeactivateWakeUpTimer(&hrtc);
  log_print("boardtest: awake\r\n");
}

/**
 * Report VDDA until the host sends 'S'. Red follows low_batt; the sample is taken dark.
 * 'S' enters Stop for one timed window, then this loop resumes.
 */
static void bt_vdda_forever(void)
{
  log_print("boardtest: phase vdda\r\n");
  bt_log_u32("boardtest: low_batt trip_mv=", BT_LOW_MV, "\r\n");
  bt_log_u32("boardtest: low_batt clear_mv=", BT_OK_MV, "\r\n");
  for (;;)
  {
    uint32_t mv = 0U;
    uint8_t cmd = 0U;

    if ((bt_rx_byte(&cmd) != 0U) && ((cmd == 'S') || (cmd == 's')))
    {
      bt_sleep_once();
    }

    bt_leds(0U, 0U);
    HAL_Delay(BT_LED_OFF_SETTLE_MS);
    if (bt_sample_vdd(&mv) == 0U)
    {
      log_print("boardtest: vdda fail\r\n");
    }
    else
    {
      bt_update_low(mv);
      bt_log_u32("boardtest: vdda_mv=", mv, bt_low_batt ? " low_batt=1\r\n" : " low_batt=0\r\n");
    }
    bt_leds(bt_low_batt, 0U);
    HAL_Delay(BT_VDDA_PERIOD_MS);
  }
}

/**
 * Run every hardware check, then sit in the voltage report.
 */
void board_test_run(void)
{
  log_print("boardtest: start\r\n");
  bt_log_uid();
  bt_led_walk();
  bt_buttons();
  bt_eeprom();
  bt_rtc();
  bt_adc_init();
  bt_low_batt = 0U;
  bt_vdda_forever();
}
