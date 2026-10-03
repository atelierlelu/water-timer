#ifndef APP_H
#define APP_H

/**
 * Plant-timer state machine.
 */

/**
 * Load EEPROM, arm the RTC, and log the boot state.
 */
void app_init(void);

/**
 * Handle RTC / button wakes. Runs SETUP to completion.
 * Returns when the caller should enter Stop.
 */
void app_process(void);

#endif /* APP_H */
