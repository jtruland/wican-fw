/*
 * SPDX-FileCopyrightText: 2022-2024 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: CC0-1.0
 *
 * USB CDC host for a u-blox class GPS receiver (e.g. VK-162, VID 0x1546).
 * Streams NMEA over CDC-ACM, parses GGA/RMC into a thread-safe fix that the
 * rest of the firmware reads via usb_gps_get_fix().
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>
#include <inttypes.h>

#include "esp_log.h"
#include "esp_system.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "usb/cdc_acm_host.h"
#include "usb/vcp_ch34x.hpp"
#include "usb/vcp_cp210x.hpp"
#include "usb/vcp_ftdi.hpp"
#include "usb/vcp.hpp"
#include "usb/usb_host.h"
#include "driver/gpio.h"
#include "dev_status.h"
#include "usb_host.h"

using namespace esp_usb;

// NMEA serial parameters. u-blox over native USB ignores baud, but a VCP-bridge
// GPS (CP210x/FTDI/CH34x) defaults to 9600 8N1, so set that for both paths.
#define GPS_BAUDRATE         (9600)
#define GPS_STOP_BITS        (0)      // 0: 1 stopbit, 1: 1.5 stopbits, 2: 2 stopbits
#define GPS_PARITY           (0)      // 0: None, 1: Odd, 2: Even, 3: Mark, 4: Space
#define GPS_DATA_BITS        (8)

#define USB_HOST_PRIORITY        (20)
#define USB_DEVICE_VID           (0x1546)   // u-blox AG
#define USB_DEVICE_PID           (0x01A7)   // u-blox 7 (VK-162)
#define USB_DEVICE_DUAL_PID      (0x4002)

namespace {
static const char *TAG = "usb";
static SemaphoreHandle_t device_disconnected_sem;

// ---- GPS (NMEA) decode state ----
static SemaphoreHandle_t s_gps_mtx = NULL;   // guards s_gps_fix
static usb_gps_fix_t     s_gps_fix = {};
static char              s_nmea[128];        // line-assembly buffer (RX task only)
static size_t            s_nmea_len = 0;

// Validate "$....*hh" XOR checksum and strip the "*hh" suffix in place.
static bool nmea_checksum_ok(char *s)
{
    if (s[0] != '$') return false;
    char *star = strchr(s, '*');
    if (!star) return false;
    uint8_t cs = 0;
    for (char *p = s + 1; p < star; ++p) cs ^= (uint8_t)*p;
    unsigned int given = (unsigned int)strtoul(star + 1, NULL, 16);
    *star = '\0';                            // drop checksum so it isn't a field
    return cs == (uint8_t)given;
}

// Split a comma-delimited sentence in place, preserving empty fields.
static int nmea_split(char *s, char **f, int max_f)
{
    int n = 0;
    f[n++] = s;
    for (char *p = s; *p && n < max_f; ++p) {
        if (*p == ',') { *p = '\0'; f[n++] = p + 1; }
    }
    return n;
}

// Convert NMEA ddmm.mmmm / dddmm.mmmm + hemisphere to signed decimal degrees.
static double nmea_coord(const char *val, const char *hemi)
{
    if (!val || !*val) return NAN;
    double raw = atof(val);
    double deg = floor(raw / 100.0);
    double dec = deg + (raw - deg * 100.0) / 60.0;
    if (hemi && (*hemi == 'S' || *hemi == 'W')) dec = -dec;
    return dec;
}

static void nmea_parse_line(char *line)
{
    if (!nmea_checksum_ok(line)) return;     // also strips the "*hh" suffix
    if (strlen(line) < 6) return;
    const char *type = line + 3;             // skip '$' + 2-char talker (GP/GN/...)

    char *f[20];
    int nf = nmea_split(line, f, 20);

    if (strncmp(type, "GGA", 3) == 0 && nf >= 10) {
        double lat = nmea_coord(f[2], f[3]);
        double lon = nmea_coord(f[4], f[5]);
        int    q   = (f[6] && *f[6]) ? atoi(f[6]) : 0;
        int    sat = (f[7] && *f[7]) ? atoi(f[7]) : 0;
        float  alt = (f[9] && *f[9]) ? (float)atof(f[9]) : 0.0f;
        if (q > 0 && !isnan(lat) && !isnan(lon)) {
            xSemaphoreTake(s_gps_mtx, portMAX_DELAY);
            s_gps_fix.latitude    = lat;
            s_gps_fix.longitude   = lon;
            s_gps_fix.altitude_m  = alt;
            s_gps_fix.satellites  = (uint8_t)sat;
            s_gps_fix.fix_quality = (uint8_t)q;
            s_gps_fix.valid       = true;
            s_gps_fix.updated_us  = esp_timer_get_time();
            xSemaphoreGive(s_gps_mtx);
        }
    } else if (strncmp(type, "RMC", 3) == 0 && nf >= 9) {
        bool   active = (f[2] && f[2][0] == 'A');
        double lat = nmea_coord(f[3], f[4]);
        double lon = nmea_coord(f[5], f[6]);
        float  spd = (f[7] && *f[7]) ? (float)atof(f[7]) * 0.514444f : 0.0f; // kn->m/s
        float  hdg = (f[8] && *f[8]) ? (float)atof(f[8]) : 0.0f;
        if (active && !isnan(lat) && !isnan(lon)) {
            xSemaphoreTake(s_gps_mtx, portMAX_DELAY);
            s_gps_fix.latitude    = lat;
            s_gps_fix.longitude   = lon;
            s_gps_fix.speed_mps   = spd;
            s_gps_fix.heading_deg = hdg;
            s_gps_fix.valid       = true;
            s_gps_fix.updated_us  = esp_timer_get_time();
            xSemaphoreGive(s_gps_mtx);
        }
    }
}

// CDC-ACM RX callback: reassemble lines from arbitrary chunks, parse each NMEA.
static bool handle_rx(const uint8_t *data, size_t data_len, void *arg)
{
    for (size_t i = 0; i < data_len; ++i) {
        char c = (char)data[i];
        if (c == '\n') {
            if (s_nmea_len > 0) { s_nmea[s_nmea_len] = '\0'; nmea_parse_line(s_nmea); }
            s_nmea_len = 0;
        } else if (c != '\r') {
            if (s_nmea_len < sizeof(s_nmea) - 1) s_nmea[s_nmea_len++] = c;
            else s_nmea_len = 0;             // overflow: drop the partial line
        }
    }
    return true;
}

static void handle_event(const cdc_acm_host_dev_event_data_t *event, void *user_ctx)
{
    switch (event->type) {
    case CDC_ACM_HOST_ERROR:
        ESP_LOGE(TAG, "CDC-ACM error has occurred, err_no = %d", event->data.error);
        break;
    case CDC_ACM_HOST_DEVICE_DISCONNECTED:
        ESP_LOGI(TAG, "Device suddenly disconnected");
        if (event->data.cdc_hdl) {
            ESP_ERROR_CHECK(cdc_acm_host_close(event->data.cdc_hdl));
        }
        xSemaphoreGive(device_disconnected_sem);
        break;
    case CDC_ACM_HOST_SERIAL_STATE:
        ESP_LOGI(TAG, "Serial state notif 0x%04X", event->data.serial_state.val);
        break;
    case CDC_ACM_HOST_NETWORK_CONNECTION:
    default:
        ESP_LOGW(TAG, "Unsupported CDC event: %i", event->type);
        break;
    }
}

static void usb_lib_task(void *arg)
{
    while (1) {
        uint32_t event_flags;

        dev_status_wait_for_bits(DEV_AWAKE_BIT, portMAX_DELAY);
        usb_host_lib_handle_events(portMAX_DELAY, &event_flags);
        if (event_flags & USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS) {
            ESP_ERROR_CHECK(usb_host_device_free_all());
        }
        if (event_flags & USB_HOST_LIB_EVENT_FLAGS_ALL_FREE) {
            ESP_LOGI(TAG, "USB: All devices freed");
        }
    }
}

static void configure_line_coding(std::unique_ptr<CdcAcmDevice>& vcp, cdc_acm_dev_hdl_t cdc_dev)
{
    cdc_acm_line_coding_t line_coding = {
        .dwDTERate = GPS_BAUDRATE,
        .bCharFormat = GPS_STOP_BITS,
        .bParityType = GPS_PARITY,
        .bDataBits = GPS_DATA_BITS,
    };

    if (vcp != nullptr) {
        ESP_ERROR_CHECK(vcp->line_coding_set(&line_coding));
    } else if (cdc_dev != NULL) {
        ESP_ERROR_CHECK(cdc_acm_host_line_coding_set(cdc_dev, &line_coding));
    }
}
}

extern "C" bool usb_gps_get_fix(usb_gps_fix_t *out, uint32_t max_age_ms)
{
    if (!out || s_gps_mtx == NULL) return false;
    bool ok = false;
    xSemaphoreTake(s_gps_mtx, portMAX_DELAY);
    if (s_gps_fix.valid) {
        int64_t age_us = esp_timer_get_time() - s_gps_fix.updated_us;
        if (max_age_ms == 0 || age_us <= (int64_t)max_age_ms * 1000) {
            *out = s_gps_fix;
            ok = true;
        }
    }
    xSemaphoreGive(s_gps_mtx);
    return ok;
}

extern "C" void usb_host_init(void)
{
    device_disconnected_sem = xSemaphoreCreateBinary();
    assert(device_disconnected_sem);

    s_gps_mtx = xSemaphoreCreateMutex();
    assert(s_gps_mtx);

    gpio_reset_pin(GPIO_NUM_11);
    gpio_set_direction(GPIO_NUM_11, GPIO_MODE_OUTPUT);
    gpio_set_level(GPIO_NUM_11, 1);          // enable USB-C host VBUS (5V)
    vTaskDelay(pdMS_TO_TICKS(2000));

    ESP_LOGI(TAG, "Installing USB Host");
    usb_host_config_t host_config = {
        .skip_phy_setup = false,
        .intr_flags = ESP_INTR_FLAG_LEVEL2|ESP_INTR_FLAG_SHARED
    };
    ESP_ERROR_CHECK(usb_host_install(&host_config));

    // Allocate stack memory in PSRAM for the USB library task
    static StackType_t *usb_lib_task_stack;
    static StaticTask_t usb_lib_task_buffer;

    usb_lib_task_stack = (StackType_t*)heap_caps_malloc(4096, MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);

    if (usb_lib_task_stack == NULL)
    {
        ESP_LOGE(TAG, "Failed to allocate USB library task stack memory");
        return;
    }

    // Create static task
    TaskHandle_t usb_task_handle = xTaskCreateStatic(
        usb_lib_task,
        "usb_lib",
        4096,
        NULL,
        USB_HOST_PRIORITY,
        usb_lib_task_stack,
        &usb_lib_task_buffer
    );

    if (usb_task_handle == NULL)
    {
        ESP_LOGE(TAG, "Failed to create USB library task");
        heap_caps_free(usb_lib_task_stack);
        return;
    }

    ESP_LOGI(TAG, "Installing CDC-ACM driver");
    ESP_ERROR_CHECK(cdc_acm_host_install(NULL));

    // Register VCP drivers to VCP service
    VCP::register_driver<FT23x>();
    VCP::register_driver<CP210x>();
    VCP::register_driver<CH34x>();

    while (true) {
        const cdc_acm_host_device_config_t dev_config = {
            .connection_timeout_ms = 1000,
            .out_buffer_size = 512,
            .in_buffer_size = 512,
            .event_cb = handle_event,
            .data_cb = handle_rx,
            .user_arg = NULL,
        };

        // Try generic VCP device first (CP210x/FTDI/CH34x serial-bridge GPS)
        ESP_LOGI(TAG, "Opening USB device as VCP...");
        auto vcp = std::unique_ptr<CdcAcmDevice>(VCP::open(&dev_config));
        cdc_acm_dev_hdl_t cdc_dev = NULL;

        if (vcp == nullptr) {
            ESP_LOGI(TAG, "Trying specific VID/PID device...");
            esp_err_t err = cdc_acm_host_open(USB_DEVICE_VID, USB_DEVICE_PID, 0, &dev_config, &cdc_dev);
            if (ESP_OK != err) {
                ESP_LOGI(TAG, "Trying dual PID device...");
                err = cdc_acm_host_open(USB_DEVICE_VID, USB_DEVICE_DUAL_PID, 0, &dev_config, &cdc_dev);
                if (ESP_OK != err) {
                    ESP_LOGI(TAG, "Failed to open any device");
                    continue;
                }
            }
            cdc_acm_host_desc_print(cdc_dev);
        }

        vTaskDelay(pdMS_TO_TICKS(10));

        ESP_LOGI(TAG, "Setting up line coding");
        configure_line_coding(vcp, cdc_dev);

        // GPS streams unsolicited NMEA; nothing to send. NMEA arrives via handle_rx().
        ESP_LOGI(TAG, "GPS open; streaming NMEA");
        xSemaphoreTake(device_disconnected_sem, portMAX_DELAY);
    }
}
