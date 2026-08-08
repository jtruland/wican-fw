#ifndef IMU_H
#define IMU_H

#include <esp_err.h>
#include "icm42670.h"

typedef enum {
    ACTIVITY_STATE_STATIONARY = 0,
    ACTIVITY_STATE_ACTIVE = 1,
    ACTIVITY_STATE_INVALID
} activity_state_t;

esp_err_t imu_init(i2c_port_t i2c_num, gpio_num_t sda_gpio, gpio_num_t scl_gpio, gpio_num_t int_gpio, uint8_t threshold);
esp_err_t imu_config_wom(uint8_t threshold);
esp_err_t imu_enable_wom(bool enable);
esp_err_t imu_read_accel(float *ax, float *ay, float *az);
esp_err_t imu_read_gyro(float *gx, float *gy, float *gz);
esp_err_t imu_read_temp(float *temp);
esp_err_t imu_get_device_id(uint8_t *id);
esp_err_t imu_set_accel_fsr(icm42670_accel_fsr_t fsr);
esp_err_t imu_set_gyro_fsr(icm42670_gyro_fsr_t fsr);
activity_state_t imu_get_activity_state(void);

/*
 * Motion check usable while the device is asleep.
 *
 * imu_motion_task() blocks on DEV_AWAKE_BIT, so the WoM interrupt path is inert
 * during sleep and cannot be used as a wake source. This polls the accelerometer
 * directly and compares against the reference captured by imu_motion_ref_reset(),
 * which works from the sleep task's existing 2 s wake cycle without depending on
 * interrupt delivery across light sleep.
 *
 * Returns true when the acceleration delta exceeds the configured WoM threshold
 * (the same imu_threshold setting, 1 LSB = 3.9 mg). Returns false on I2C error,
 * so an unreadable IMU can never spuriously wake the device.
 */
bool imu_motion_since_ref(void);
void imu_motion_ref_reset(void);

#endif
