#ifndef __USB_HOST_H__
#define __USB_HOST_H__

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// Latest GPS fix decoded from a USB CDC GPS receiver (e.g. u-blox VK-162).
typedef struct {
    bool    valid;        // true once a position fix has been parsed
    double  latitude;     // decimal degrees, +N / -S
    double  longitude;    // decimal degrees, +E / -W
    float   altitude_m;   // metres above MSL (from GGA)
    float   speed_mps;    // ground speed, metres/second (from RMC)
    float   heading_deg;  // course over ground, degrees (from RMC)
    uint8_t satellites;   // satellites in use (from GGA)
    uint8_t fix_quality;  // GGA fix quality: 0=none, 1=GPS, 2=DGPS
    int64_t updated_us;   // esp_timer_get_time() at last successful parse
} usb_gps_fix_t;

void usb_host_init(void);

// Copy the latest fix into *out when it is valid and was updated within
// max_age_ms (pass 0 to ignore age). Returns true on success, false otherwise.
bool usb_gps_get_fix(usb_gps_fix_t *out, uint32_t max_age_ms);

#ifdef __cplusplus
}
#endif

#endif
