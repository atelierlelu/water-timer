/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include <stdint.h>
#include "app.h"
#include "rtt_log.h"
#include "swd_log.h"
#ifdef BOARD_TEST
#include "board_test.h"
#endif
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
/**
 * LSI is ~37 kHz (LSI_VALUE). 37000 / ((127+1)*(288+1)) ≈ 1 Hz ck_spre.
 * The board test passes -DAPP_RTC_SYNCH_PREDIV when it flashes a measured board,
 * so this nominal pair stays the uncalibrated default.
 */
#ifndef APP_RTC_ASYNCH_PREDIV
#define APP_RTC_ASYNCH_PREDIV   127U
#endif
#ifndef APP_RTC_SYNCH_PREDIV
#define APP_RTC_SYNCH_PREDIV    288U
#endif
/** Dummy calendar start; the wall date does not matter. */
#define APP_RTC_YEAR            26U
#define APP_RTC_MONTH           1U
#define APP_RTC_DATE            1U
/** Blocking UART timeout; a short line at 115200 finishes well under this. */
#define UART_TIMEOUT_MS         100U
/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
UART_HandleTypeDef huart2;

/* USER CODE BEGIN PV */
RTC_HandleTypeDef hrtc;
/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_USART2_UART_Init(void);
/* USER CODE BEGIN PFP */
static void MX_RTC_Init(void);
static void rtc_apply_prescaler(void);
static void uart_write(const uint8_t *data, uint16_t len);
#ifndef BOARD_TEST
static void enter_stop(void);
#endif
static void never_remove_swd_boot_window(void);
/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */
/**
 * Send a raw buffer over USART2 (Nucleo ST-Link VCP).
 */
static void uart_write(const uint8_t *data, uint16_t len)
{
  (void)HAL_UART_Transmit(&huart2, (uint8_t *)data, len, UART_TIMEOUT_MS);
}

/**
 * Write the same buffer to USART2, RTT, and optional semihosting.
 */
void log_write(const uint8_t *data, uint16_t len)
{
  uart_write(data, len);
  rtt_write(data, len);
  swd_write(data, len);
}

/**
 * Write a NUL-terminated string to every log sink.
 */
void log_print(const char *s)
{
  const char *p = s;

  while (*p != '\0')
  {
    p++;
  }
  log_write((const uint8_t *)s, (uint16_t)(p - s));
}

/*
 * ***************************************************************************
 * DO NOT REMOVE, DISABLE, SHORTEN, OR MOVE THIS FUNCTION.
 * DO NOT CALL enter_stop() OR ANY OTHER SLEEP FROM HERE.
 *
 * After reset the core stays in Run for 10 s so SWD can attach. Stop gates
 * the debug clock; without this window a power-cycle + make flash fails.
 * Both LEDs blink so you can see the window is open.
 * ***************************************************************************
 */
static void never_remove_swd_boot_window(void)
{
  uint32_t elapsed_ms = 0U;

  /* NEVER TOUCH: 10 s Run, no Stop, no RTC arm yet. */
  log_print("SWD window 10s — do not strip this\r\n");
  while (elapsed_ms < 10000U)
  {
    HAL_GPIO_WritePin(GPIOA, RED_Pin | GREEN_Pin, GPIO_PIN_SET);
    HAL_Delay(200U);
    HAL_GPIO_WritePin(GPIOA, RED_Pin | GREEN_Pin, GPIO_PIN_RESET);
    HAL_Delay(200U);
    elapsed_ms += 400U;
  }
}

#ifndef BOARD_TEST
/**
 * Enter Stop. RTC wakeup and button EXTI bring us back.
 * Clear leftover EXTI so Stop does not return immediately.
 * Leave DBG_STOP clear so Stop can gate the debug clock.
 * ULP turns VREFINT off in Stop. HAL_PWR_EnterSTOPMode does not set it,
 * and without it this board sat at about 13 µA instead of under 1 µA.
 */
static void enter_stop(void)
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
#endif

/**
 * Write the RTC prescalers on every boot.
 * HAL_RTC_Init programs them only while the calendar is still blank, and the
 * backup domain keeps the old divider across reset. The board test measures
 * drift against 127/288, then flashes a corrected synchronous divider, so
 * that value has to be applied even when the RTC is already running.
 */
static void rtc_apply_prescaler(void)
{
  __HAL_RTC_WRITEPROTECTION_DISABLE(&hrtc);
  if (RTC_EnterInitMode(&hrtc) != HAL_OK)
  {
    Error_Handler();
  }
  hrtc.Instance->PRER = (uint32_t)hrtc.Init.SynchPrediv;
  hrtc.Instance->PRER |= (uint32_t)(hrtc.Init.AsynchPrediv << RTC_PRER_PREDIV_A_Pos);
  if (RTC_ExitInitMode(&hrtc) != HAL_OK)
  {
    Error_Handler();
  }
  __HAL_RTC_WRITEPROTECTION_ENABLE(&hrtc);
}

/**
 * Start LSI-clocked RTC and load a dummy calendar. Wakeup is armed in app_init().
 */
static void MX_RTC_Init(void)
{
  RTC_TimeTypeDef time = {0};
  RTC_DateTypeDef date = {0};

  hrtc.Instance = RTC;
  hrtc.Init.HourFormat = RTC_HOURFORMAT_24;
  hrtc.Init.AsynchPrediv = APP_RTC_ASYNCH_PREDIV;
  hrtc.Init.SynchPrediv = APP_RTC_SYNCH_PREDIV;
  hrtc.Init.OutPut = RTC_OUTPUT_DISABLE;
  hrtc.Init.OutPutRemap = RTC_OUTPUT_REMAP_NONE;
  hrtc.Init.OutPutPolarity = RTC_OUTPUT_POLARITY_HIGH;
  hrtc.Init.OutPutType = RTC_OUTPUT_TYPE_OPENDRAIN;
  if (HAL_RTC_Init(&hrtc) != HAL_OK)
  {
    Error_Handler();
  }
  rtc_apply_prescaler();

  /* Reload a known start so each reset shows the calendar advancing. */
  time.Hours = 0U;
  time.Minutes = 0U;
  time.Seconds = 0U;
  time.TimeFormat = RTC_HOURFORMAT12_AM;
  time.DayLightSaving = RTC_DAYLIGHTSAVING_NONE;
  time.StoreOperation = RTC_STOREOPERATION_RESET;
  if (HAL_RTC_SetTime(&hrtc, &time, RTC_FORMAT_BIN) != HAL_OK)
  {
    Error_Handler();
  }

  date.WeekDay = RTC_WEEKDAY_THURSDAY;
  date.Month = APP_RTC_MONTH;
  date.Date = APP_RTC_DATE;
  date.Year = APP_RTC_YEAR;
  if (HAL_RTC_SetDate(&hrtc, &date, RTC_FORMAT_BIN) != HAL_OK)
  {
    Error_Handler();
  }

}

/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */
  /* Bootloader leave VTOR at system memory; SysTick must use the flash table. */
  SCB->VTOR = FLASH_BASE;
  /* Debugger attach can leave PRIMASK set; SysTick must run for HAL_Delay. */
  __enable_irq();
  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_USART2_UART_Init();
  /* USER CODE BEGIN 2 */
  /* DBG_STOP is left clear. The 10 s window below is what makes SWD attach work. */
  /* NEVER TOUCH: must run after GPIO, before RTC/Stop. See function banner. */
  never_remove_swd_boot_window();
  MX_RTC_Init();
#ifdef BOARD_TEST
  /* Stays in Run. The plant image is what enters Stop. */
  board_test_run();
#else
  app_init();
#endif
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
#ifndef BOARD_TEST
    app_process();
    enter_stop();
#endif
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};
  RCC_PeriphCLKInitTypeDef PeriphClkInit = {0};

  /** Configure the main internal regulator output voltage
  */
  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_MSI | RCC_OSCILLATORTYPE_LSI;
  RCC_OscInitStruct.LSIState = RCC_LSI_ON;
  RCC_OscInitStruct.MSIState = RCC_MSI_ON;
  RCC_OscInitStruct.MSICalibrationValue = 0;
  RCC_OscInitStruct.MSIClockRange = RCC_MSIRANGE_5;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_NONE;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_MSI;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_0) != HAL_OK)
  {
    Error_Handler();
  }
  PeriphClkInit.PeriphClockSelection = RCC_PERIPHCLK_USART2 | RCC_PERIPHCLK_RTC;
  PeriphClkInit.Usart2ClockSelection = RCC_USART2CLKSOURCE_PCLK1;
  PeriphClkInit.RTCClockSelection = RCC_RTCCLKSOURCE_LSI;
  if (HAL_RCCEx_PeriphCLKConfig(&PeriphClkInit) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief USART2 Initialization Function
  * @param None
  * @retval None
  */
static void MX_USART2_UART_Init(void)
{

  /* USER CODE BEGIN USART2_Init 0 */

  /* USER CODE END USART2_Init 0 */

  /* USER CODE BEGIN USART2_Init 1 */

  /* USER CODE END USART2_Init 1 */
  huart2.Instance = USART2;
  huart2.Init.BaudRate = 115200;
  huart2.Init.WordLength = UART_WORDLENGTH_8B;
  huart2.Init.StopBits = UART_STOPBITS_1;
  huart2.Init.Parity = UART_PARITY_NONE;
  huart2.Init.Mode = UART_MODE_TX_RX;
  huart2.Init.HwFlowCtl = UART_HWCONTROL_NONE;
  huart2.Init.OverSampling = UART_OVERSAMPLING_16;
  huart2.Init.OneBitSampling = UART_ONE_BIT_SAMPLE_DISABLE;
  huart2.AdvancedInit.AdvFeatureInit = UART_ADVFEATURE_NO_INIT;
  if (HAL_UART_Init(&huart2) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN USART2_Init 2 */

  /* USER CODE END USART2_Init 2 */

}

/**
  * @brief GPIO Initialization Function
  * @param None
  * @retval None
  */
static void MX_GPIO_Init(void)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};
  /* USER CODE BEGIN MX_GPIO_Init_1 */

  /* USER CODE END MX_GPIO_Init_1 */

  /* GPIO Ports Clock Enable */
  __HAL_RCC_GPIOA_CLK_ENABLE();

  /*Configure GPIO pin Output Level */
  HAL_GPIO_WritePin(GPIOA, RED_Pin|GREEN_Pin, GPIO_PIN_RESET);

  /*Configure GPIO pins : RED_Pin GREEN_Pin */
  GPIO_InitStruct.Pin = RED_Pin|GREEN_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);

  /*Configure GPIO pins : BUTTON_1_Pin BUTTON_2_Pin */
  /* External 10 kΩ pull-ups; falling EXTI wakes from Stop. */
  GPIO_InitStruct.Pin = BUTTON_1_Pin|BUTTON_2_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_IT_FALLING;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);

  /* USER CODE BEGIN MX_GPIO_Init_2 */
  HAL_NVIC_SetPriority(EXTI4_15_IRQn, 1, 0);
  HAL_NVIC_EnableIRQ(EXTI4_15_IRQn);
  /* USER CODE END MX_GPIO_Init_2 */
}

/* USER CODE BEGIN 4 */

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
