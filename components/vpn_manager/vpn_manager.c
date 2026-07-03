/*
 * This file is part of the WiCAN project.
 *
 * Copyright (C) 2022  Meatpi Electronics.
 * Written by Ali Slim <ali@meatpi.com>
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

#include "vpn_manager.h"
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <esp_log.h>
#include <esp_wifi.h>
#include <esp_netif.h>
#include <freertos/task.h>
#include <freertos/timers.h>
#include <freertos/queue.h>
#include <esp_wireguard.h>
#include <esp_heap_caps.h>
#include <cJSON.h>
#include <esp_timer.h>
#include "filesystem.h"
#include "vpn_wireguard.h"
#include "vpn_config.h"
#include "vpn_route_hook.h"
#include <time.h>
#include "dev_status.h"
#include "esp_system.h"
#include "lwip/ip_addr.h"
#include "lwip/ip4_addr.h"

static const char *TAG = "VPN_MANAGER";

// Global variables
static EventGroupHandle_t vpn_event_group = NULL;
static StaticEventGroup_t s_vpn_event_group_bss;
static vpn_config_t current_config = {0};
static vpn_status_t current_status = VPN_STATUS_DISABLED;
static esp_netif_t *vpn_netif = NULL;

// DNS override while VPN is connected (IPv4 only)
static bool s_dns_saved = false;
static esp_netif_dns_info_t s_prev_dns_main = {0};
static esp_netif_dns_info_t s_prev_dns_backup = {0};

static void vpn_manager_restore_dns(void)
{
    if (!s_dns_saved)
    {
        return;
    }
    esp_netif_t *sta_netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (sta_netif)
    {
        (void)esp_netif_set_dns_info(sta_netif, ESP_NETIF_DNS_MAIN, &s_prev_dns_main);
        (void)esp_netif_set_dns_info(sta_netif, ESP_NETIF_DNS_BACKUP, &s_prev_dns_backup);
    }
    s_dns_saved = false;
}

static void vpn_manager_apply_dns_from_wg(const vpn_wireguard_config_t *wg)
{
    if (!wg || wg->dns_main[0] == '\0')
    {
        return;
    }

    esp_netif_t *sta_netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (!sta_netif)
    {
        return;
    }

    // Save existing DNS once so we can restore on disconnect.
    if (!s_dns_saved)
    {
        if (esp_netif_get_dns_info(sta_netif, ESP_NETIF_DNS_MAIN, &s_prev_dns_main) == ESP_OK)
        {
            (void)esp_netif_get_dns_info(sta_netif, ESP_NETIF_DNS_BACKUP, &s_prev_dns_backup);
            s_dns_saved = true;
        }
    }

    esp_netif_dns_info_t dns_main = {0};
    dns_main.ip.type = IPADDR_TYPE_V4;
    if (esp_netif_str_to_ip4(wg->dns_main, &dns_main.ip.u_addr.ip4) == ESP_OK)
    {
        (void)esp_netif_set_dns_info(sta_netif, ESP_NETIF_DNS_MAIN, &dns_main);
    }

    // If backup is not provided, use main as backup to avoid leaking to previous DNS.
    const char *backup = (wg->dns_backup[0] != '\0') ? wg->dns_backup : wg->dns_main;
    esp_netif_dns_info_t dns_backup = {0};
    dns_backup.ip.type = IPADDR_TYPE_V4;
    if (esp_netif_str_to_ip4(backup, &dns_backup.ip.u_addr.ip4) == ESP_OK)
    {
        (void)esp_netif_set_dns_info(sta_netif, ESP_NETIF_DNS_BACKUP, &dns_backup);
    }
}

// VPN task/command infrastructure
typedef enum
{
    VPN_CMD_ENABLE,
    VPN_CMD_RELOAD,
    VPN_CMD_TEST,
    VPN_CMD_TEST_CONFIG
} vpn_cmd_type_t;

typedef struct
{
    vpn_cmd_type_t type;
    bool enabled; // for VPN_CMD_ENABLE
    bool has_config; // for VPN_CMD_TEST_CONFIG
    vpn_config_t config; // for VPN_CMD_TEST_CONFIG
} vpn_cmd_msg_t;

static TaskHandle_t s_vpn_task = NULL;
static QueueHandle_t s_vpn_cmd_q = NULL;
static StaticQueue_t s_vpn_cmd_q_struct;
static uint8_t s_vpn_cmd_q_storage[8 * sizeof(vpn_cmd_msg_t)];
static uint32_t s_backoff_ms = 0;
static uint32_t s_backoff_cap_ms = 15000; // 15s cap
static uint32_t s_backoff_base_ms = 2000; // 2s start
static int64_t s_connect_started_us = 0;
static int64_t s_connect_timeout_us = 15000000; // 15s

// Use PSRAM for VPN task stack when available
#define VPN_TASK_STACK_WORDS  (4096)
#define VPN_TASK_PRIORITY     (5)
static StackType_t *s_vpn_task_stack = NULL;   // allocated from PSRAM
static StaticTask_t s_vpn_task_tcb;            // kept internal (BSS)

static void vpn_task_fn(void *arg);
static void vpn_backoff_reset(void)
{
    s_backoff_ms = 0;
}
static void vpn_backoff_bump(void)
{
    if (s_backoff_ms == 0)
    {
        s_backoff_ms = s_backoff_base_ms;
    }
    else
    {
        s_backoff_ms = s_backoff_ms * 2;
    }
    if (s_backoff_ms > s_backoff_cap_ms)
    {
        s_backoff_ms = s_backoff_cap_ms;
    }
    // add +-15% jitter
    uint32_t jitter = (s_backoff_ms * 15) / 100;
    s_backoff_ms = s_backoff_ms - (jitter / 2) + (esp_random() % (jitter + 1));
}

// Home-network bypass state (reported via vpn_manager_home_bypass_active)
static volatile bool s_home_paused = false;
// After a one-shot test succeeds at home, hold the tunnel this long before re-pausing
// so the UI polling /vpn/debug can observe the result.
static int64_t s_home_test_grace_until_us = 0;

// Private function declarations
static void vpn_manager_update_status(void);
static esp_err_t vpn_manager_validate_wireguard_config(const vpn_wireguard_config_t *cfg);
static bool vpn_manager_home_ssid_matched(const char *ssid_list);

esp_err_t vpn_manager_init(void)
{
    esp_err_t ret = ESP_OK;

    ESP_LOGI(TAG, "Initializing VPN Manager");

    // Create event group
    if (vpn_event_group == NULL)
    {
        vpn_event_group = xEventGroupCreateStatic(&s_vpn_event_group_bss);
        if (vpn_event_group == NULL)
        {
            ESP_LOGE(TAG, "Failed to create VPN event group");
            return ESP_ERR_NO_MEM;
        }
    }

    // Create command queue and VPN task
    if (!s_vpn_cmd_q)
    {
        s_vpn_cmd_q = xQueueCreateStatic(8, sizeof(vpn_cmd_msg_t), s_vpn_cmd_q_storage, &s_vpn_cmd_q_struct);
        if (!s_vpn_cmd_q)
        {
            ESP_LOGE(TAG, "Failed to create VPN command queue");
            return ESP_ERR_NO_MEM;
        }
    }
    if (!s_vpn_task)
    {
        // Try to allocate the task stack from PSRAM
        size_t stack_bytes = VPN_TASK_STACK_WORDS * sizeof(StackType_t);
        s_vpn_task_stack = (StackType_t *)heap_caps_malloc(stack_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (s_vpn_task_stack)
        {
            ESP_LOGI(TAG, "Creating VPN task with PSRAM stack (%u bytes)", (unsigned)stack_bytes);
            s_vpn_task = xTaskCreateStatic(
                vpn_task_fn,
                "vpn_task",
                VPN_TASK_STACK_WORDS,
                NULL,
                VPN_TASK_PRIORITY,
                s_vpn_task_stack,
                &s_vpn_task_tcb);
            if (s_vpn_task == NULL)
            {
                ESP_LOGE(TAG, "xTaskCreateStatic failed even with PSRAM stack");
                // Fallback to normal dynamic create (internal heap)
                free(s_vpn_task_stack);
                s_vpn_task_stack = NULL;
            }
        }

        if (!s_vpn_task)
        {
            ESP_LOGE(TAG, "No PSRAM or failed to allocate PSRAM stack, creating VPN task with internal heap");
            return ESP_ERR_NO_MEM;
        }
    }

        // Load config at startup
    vpn_config_t loaded = {0};
    if (vpn_manager_load_config(&loaded) == ESP_OK)
    {
        memcpy(&current_config, &loaded, sizeof(current_config));
        ESP_LOGI(TAG, "VPN config loaded at task start");
    }
    // Initialize current status
    current_status = VPN_STATUS_DISABLED;
    xEventGroupSetBits(vpn_event_group, VPN_DISCONNECTED_BIT);

    // Preload JSON config into PSRAM once (avoids reopening file repeatedly)
    esp_err_t pre = vpn_config_preload();
    if (pre != ESP_OK)
    {
        ESP_LOGW(TAG, "vpn_config_preload failed: %s", esp_err_to_name(pre));
    }

    // Load configuration from (cached) JSON
    vpn_config_t loaded_config = {0};
    if (vpn_manager_load_config(&loaded_config) == ESP_OK)
    {
        memcpy(&current_config, &loaded_config, sizeof(vpn_config_t));
        ESP_LOGI(TAG, "Loaded VPN configuration");

        // Auto-start if enabled
        if (current_config.enabled && current_config.type == VPN_TYPE_WIREGUARD)
        {
            // ESP_LOGI(TAG, "Auto-starting VPN");
            // ret = vpn_manager_start(&current_config);
        }
    }

    ESP_LOGI(TAG, "VPN Manager initialized");
    return ret;
}

// Note: explicit deinit removed; app lifetime manages the task

esp_err_t vpn_manager_start(const vpn_config_t *config)
{
    if (config == NULL)
    {
        ESP_LOGE(TAG, "VPN config is NULL");
        return ESP_ERR_INVALID_ARG;
    }

    ESP_LOGI(TAG, "Starting VPN connection, type: %d", config->type);

    // Stop any existing connection
    vpn_manager_stop();

    // Copy configuration
    memcpy(&current_config, config, sizeof(vpn_config_t));

    s_connect_started_us = esp_timer_get_time();

    current_status = VPN_STATUS_CONNECTING;
    xEventGroupClearBits(vpn_event_group, VPN_CONNECTED_BIT | VPN_DISCONNECTED_BIT | VPN_ERROR_BIT);
    xEventGroupSetBits(vpn_event_group, VPN_CONNECTING_BIT);

    esp_err_t ret = ESP_OK;
    switch (config->type)
    {
        case VPN_TYPE_WIREGUARD:
        {
            // Validate config before attempting to init/connect to avoid NULL strings
            esp_err_t vret = vpn_manager_validate_wireguard_config(&config->config.wireguard);
            if (vret != ESP_OK)
            {
                ESP_LOGE(TAG, "WireGuard config invalid; aborting start");
                ret = vret;
                break;
            }
            ret = vpn_wg_init(&config->config.wireguard);
            if (ret == ESP_OK)
            {
                ret = vpn_wg_start();
            }
            break;
        }
        case VPN_TYPE_DISABLED:
        default:
            ESP_LOGW(TAG, "Invalid VPN type: %d", config->type);
            ret = ESP_ERR_NOT_SUPPORTED;
            break;
    }

    if (ret != ESP_OK)
    {
        current_status = VPN_STATUS_ERROR;
        xEventGroupClearBits(vpn_event_group, VPN_CONNECTING_BIT);
        xEventGroupSetBits(vpn_event_group, VPN_ERROR_BIT);
        s_connect_started_us = 0;
        ESP_LOGE(TAG, "Failed to start VPN: %s", esp_err_to_name(ret));
    }

    return ret;
}

// Ensure all mandatory fields are present/non-empty before starting WireGuard
static esp_err_t vpn_manager_validate_wireguard_config(const vpn_wireguard_config_t *cfg)
{
    if (cfg == NULL)
    {
        ESP_LOGE(TAG, "WG config is NULL");
        return ESP_ERR_INVALID_ARG;
    }

    // Helper to check empty strings safely
    #define EMPTY_OR_NULL(s) ((s)==NULL || (s)[0]=='\0')

    bool ok = true;
    if (EMPTY_OR_NULL(cfg->private_key))
    {
        ESP_LOGE(TAG, "WG private_key is empty");
        ok = false;
    }
    if (EMPTY_OR_NULL(cfg->public_key))
    {
        ESP_LOGE(TAG, "WG public_key is empty");
        ok = false;
    }
    if (EMPTY_OR_NULL(cfg->address))
    {
        ESP_LOGE(TAG, "WG address is empty");
        ok = false;
    }
    // The tunnel's local IP is always derived from address now (see vpn_wg_init).
    // What actually needs validating is that at least one destination route is
    // configured -- otherwise the tunnel would connect but carry no traffic.
    if (cfg->route_count == 0)
    {
        ESP_LOGE(TAG, "WG has no routes configured (AllowedIPs) -- tunnel would carry no traffic");
        ok = false;
    }
    if (EMPTY_OR_NULL(cfg->endpoint))
    {
        ESP_LOGE(TAG, "WG endpoint is empty");
        ok = false;
    }
    if (cfg->port <= 0)
    {
        ESP_LOGE(TAG, "WG port is invalid: %d", cfg->port);
        ok = false;
    }

    if (!ok)
    {
        ESP_LOGE(TAG, "WireGuard configuration is incomplete. Will not start.");
        return ESP_ERR_INVALID_ARG;
    }

    return ESP_OK;
}

esp_err_t vpn_manager_stop(void)
{
    ESP_LOGI(TAG, "Stopping VPN connection");

    // Restore DNS before tearing down VPN (best-effort)
    vpn_manager_restore_dns();

    esp_err_t ret = ESP_OK;
    switch (current_config.type)
    {
        case VPN_TYPE_WIREGUARD:
            ret = vpn_wg_stop();
            vpn_wg_deinit();
            break;

        case VPN_TYPE_DISABLED:
        default:
            break;
    }

    current_status = VPN_STATUS_DISCONNECTED;
    s_connect_started_us = 0;
    xEventGroupClearBits(vpn_event_group, VPN_CONNECTED_BIT | VPN_CONNECTING_BIT | VPN_ERROR_BIT);
    xEventGroupSetBits(vpn_event_group, VPN_DISCONNECTED_BIT);

    return ret;
}

vpn_status_t vpn_manager_get_status(void)
{
    // Update status by checking WireGuard connection
    vpn_manager_update_status();
    return current_status;
}

bool vpn_manager_get_connect_timing(uint32_t *elapsed_ms, uint32_t *timeout_ms)
{
    if (timeout_ms)
    {
        *timeout_ms = (uint32_t)(s_connect_timeout_us / 1000);
    }

    if (current_status != VPN_STATUS_CONNECTING || s_connect_started_us == 0)
    {
        if (elapsed_ms)
        {
            *elapsed_ms = 0;
        }
        return false;
    }

    int64_t now_us = esp_timer_get_time();
    int64_t delta_us = now_us - s_connect_started_us;
    if (delta_us < 0)
    {
        delta_us = 0;
    }
    if (elapsed_ms)
    {
        *elapsed_ms = (uint32_t)(delta_us / 1000);
    }
    return true;
}

// Event group handle is now private to manager

bool vpn_manager_home_bypass_active(void)
{
    return s_home_paused;
}

esp_err_t vpn_manager_get_sta_ssid(char *ssid, size_t size)
{
    if (ssid == NULL || size == 0)
    {
        return ESP_ERR_INVALID_ARG;
    }
    ssid[0] = '\0';
    wifi_ap_record_t ap = {0};
    esp_err_t ret = esp_wifi_sta_get_ap_info(&ap);
    if (ret == ESP_OK)
    {
        strlcpy(ssid, (const char *)ap.ssid, size);
    }
    return ret;
}

struct netif *vpn_manager_get_netif(void)
{
    return vpn_wg_get_netif();
}

bool vpn_manager_route_matches(const ip4_addr_t *dest)
{
    if (dest == NULL || current_config.type != VPN_TYPE_WIREGUARD)
    {
        return false;
    }
    const vpn_wireguard_config_t *wg = &current_config.config.wireguard;
    for (uint8_t i = 0; i < wg->route_count; i++)
    {
        ip4_addr_t route_ip, route_mask;
        if (ip4addr_aton(wg->routes[i].ip, &route_ip) && ip4addr_aton(wg->routes[i].mask, &route_mask) &&
            ip4_addr_netcmp(dest, &route_ip, &route_mask))
        {
            return true;
        }
    }
    return false;
}

// Match the currently associated STA SSID against a comma-separated trusted list.
// Case-sensitive exact match (SSIDs are case-sensitive); entries are whitespace-trimmed.
static bool vpn_manager_home_ssid_matched(const char *ssid_list)
{
    if (ssid_list == NULL || ssid_list[0] == '\0')
    {
        return false;
    }
    wifi_ap_record_t ap = {0};
    if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK || ap.ssid[0] == '\0')
    {
        return false;
    }

    char buf[100];
    strlcpy(buf, ssid_list, sizeof(buf));
    char *saveptr = NULL;
    for (char *tok = strtok_r(buf, ",", &saveptr); tok; tok = strtok_r(NULL, ",", &saveptr))
    {
        // Trim leading/trailing whitespace
        while (*tok == ' ' || *tok == '\t')
        {
            tok++;
        }
        char *end = tok + strlen(tok);
        while (end > tok && (end[-1] == ' ' || end[-1] == '\t' || end[-1] == '\r' || end[-1] == '\n'))
        {
            *--end = '\0';
        }
        if (*tok != '\0' && strcmp(tok, (const char *)ap.ssid) == 0)
        {
            return true;
        }
    }
    return false;
}

esp_err_t vpn_manager_generate_wireguard_keys(char *public_key, size_t public_key_size)
{
    if (public_key == NULL || public_key_size < 64)
    {
        ESP_LOGE(TAG, "Invalid parameters for key generation");
        return ESP_ERR_INVALID_ARG;
    }

    ESP_LOGI(TAG, "Generating WireGuard key pair (delegated)");
    return vpn_config_generate_wg_keys(public_key, public_key_size);
}

esp_err_t vpn_manager_save_config(const vpn_config_t *config)
{
    if (config == NULL)
    {
        ESP_LOGE(TAG, "Config is NULL");
        return ESP_ERR_INVALID_ARG;
    }

    return vpn_config_save(config);
}

esp_err_t vpn_manager_load_config(vpn_config_t *config)
{
    if (config == NULL)
    {
        ESP_LOGE(TAG, "Config buffer is NULL");
        return ESP_ERR_INVALID_ARG;
    }

    return vpn_config_load(config);
}

// Text parse helper removed from manager (use vpn_config_parse_wg where needed)

// Old blocking test removed; replaced with async command below

esp_err_t vpn_manager_get_ip_address(char *ip_str, size_t ip_str_size)
{
    if (ip_str == NULL || ip_str_size == 0)
    {
        return ESP_ERR_INVALID_ARG;
    }

    if (vpn_netif == NULL || current_status != VPN_STATUS_CONNECTED)
    {
        return ESP_ERR_INVALID_STATE;
    }

    esp_netif_ip_info_t ip_info;
    esp_err_t ret = esp_netif_get_ip_info(vpn_netif, &ip_info);
    if (ret == ESP_OK)
    {
        snprintf(ip_str, ip_str_size, IPSTR, IP2STR(&ip_info.ip));
    }

    return ret;
}

// Private function implementations

// True if ip_str falls within any of wg->routes[]. Used only for the DNS
// coverage sanity check below (not for actual packet routing -- that's
// vpn_route_hook.c, driven directly by the same routes[]).
static bool ipv4_covered_by_routes(const char *ip_str, const vpn_wireguard_config_t *wg)
{
    if (!ip_str || !ip_str[0])
    {
        return true; // nothing to check
    }
    ip4_addr_t target;
    if (!ip4addr_aton(ip_str, &target))
    {
        return true; // unparseable, don't false-alarm
    }
    for (uint8_t i = 0; i < wg->route_count; i++)
    {
        ip4_addr_t route_ip, route_mask;
        if (ip4addr_aton(wg->routes[i].ip, &route_ip) && ip4addr_aton(wg->routes[i].mask, &route_mask) &&
            ip4_addr_netcmp(&target, &route_ip, &route_mask))
        {
            return true;
        }
    }
    return false;
}

// wg0 is intentionally never made the default route (see vpn_route_hook.c) --
// only the destination CIDRs in routes[] go over the tunnel. If the
// configured WG DNS server isn't covered by any route, DNS-over-tunnel will
// silently fail once away from home (the query has nowhere to go). Surface
// that misconfiguration instead of leaving it silent.
static void vpn_manager_warn_if_dns_unrouted(const vpn_wireguard_config_t *wg)
{
    if (wg->dns_main[0] && !ipv4_covered_by_routes(wg->dns_main, wg))
    {
        ESP_LOGW(TAG, "WG DNS server %s is not covered by any configured route -- "
                      "DNS over the tunnel will silently fail while away from home",
                 wg->dns_main);
    }
}

// Helper function to update VPN status
static void vpn_manager_update_status(void)
{
    if (vpn_wg_is_peer_up())
    {
        if (current_status != VPN_STATUS_CONNECTED)
        {
            current_status = VPN_STATUS_CONNECTED;
            xEventGroupClearBits(vpn_event_group, VPN_CONNECTING_BIT | VPN_DISCONNECTED_BIT | VPN_ERROR_BIT);
            xEventGroupSetBits(vpn_event_group, VPN_CONNECTED_BIT);
            s_connect_started_us = 0;
            // wg0 stays a non-default netif; STA/cellular remains the default
            // route unconditionally. Only destination CIDRs configured in
            // routes[] are routed over the tunnel (vpn_route_hook.c) -- nothing
            // else (webhook, NTP, ...) is ever affected by tunnel state.
            vpn_manager_warn_if_dns_unrouted(&current_config.config.wireguard);
            // Apply DNS override (from WG config) on successful connect.
            vpn_manager_apply_dns_from_wg(&current_config.config.wireguard);
            ESP_LOGI(TAG, "VPN connected successfully");
        }
    }
    else
    {
        // If we were connected and the peer went down, report disconnected.
        // Do not force CONNECTING -> DISCONNECTED here; CONNECTING is resolved by timeout logic in the VPN task.
        if (current_status == VPN_STATUS_CONNECTED)
        {
            current_status = VPN_STATUS_DISCONNECTED;
            xEventGroupClearBits(vpn_event_group, VPN_CONNECTED_BIT | VPN_CONNECTING_BIT);
            xEventGroupSetBits(vpn_event_group, VPN_DISCONNECTED_BIT);
            // Restore previous DNS when the VPN drops.
            vpn_manager_restore_dns();
            ESP_LOGI(TAG, "VPN disconnected");
        }
    }
}

// Public control API implemented via command queue and dev_status bits
void vpn_manager_set_enabled(bool enabled)
{
    if (enabled)
    {
        dev_status_set_vpn_enabled();
    }
    else
    {
        dev_status_clear_vpn_enabled();
    }
    if (s_vpn_cmd_q)
    {
        vpn_cmd_msg_t msg = { .type = VPN_CMD_ENABLE, .enabled = enabled };
        xQueueSend(s_vpn_cmd_q, &msg, 0);
    }
}

void vpn_manager_request_reload(void)
{
    if (s_vpn_cmd_q)
    {
        vpn_cmd_msg_t msg = { .type = VPN_CMD_RELOAD };
        xQueueSend(s_vpn_cmd_q, &msg, 0);
    }
}

void vpn_manager_request_test(void)
{
    if (s_vpn_cmd_q)
    {
        vpn_cmd_msg_t msg = { .type = VPN_CMD_TEST };
        xQueueSend(s_vpn_cmd_q, &msg, 0);
    }
}

esp_err_t vpn_manager_request_test_with_config(const vpn_config_t *config)
{
    if (!config)
    {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_vpn_cmd_q)
    {
        return ESP_ERR_INVALID_STATE;
    }
    vpn_cmd_msg_t msg = {0};
    msg.type = VPN_CMD_TEST_CONFIG;
    msg.has_config = true;
    msg.config = *config;
    BaseType_t ok = xQueueSend(s_vpn_cmd_q, &msg, 0);
    return ok == pdTRUE ? ESP_OK : ESP_ERR_TIMEOUT;
}

void vpn_manager_request_test_hardcoded(void)
{
    // Push a RELOAD with temporary hardcoded config then TEST
    // For safety, only if type is WG
    vpn_config_t cfg = {0};
    if (vpn_manager_load_config(&cfg) != ESP_OK)
    {
        cfg.type = VPN_TYPE_WIREGUARD;
        cfg.enabled = true;
    }
    if (cfg.type != VPN_TYPE_WIREGUARD)
    {
        cfg.type = VPN_TYPE_WIREGUARD;
    }
    cfg.enabled = true;
    // Hardcoded values (developer bench). Replace as needed.
    strlcpy(cfg.config.wireguard.private_key, "KEY_HERE", sizeof(cfg.config.wireguard.private_key));
    strlcpy(cfg.config.wireguard.public_key,  "KEY_HERE", sizeof(cfg.config.wireguard.public_key));
    strlcpy(cfg.config.wireguard.address,      "0.0.0.0", sizeof(cfg.config.wireguard.address));
    // Dev-bench default: route everything over the tunnel (replace with real routes as needed).
    vpn_config_parse_routes("0.0.0.0/0", cfg.config.wireguard.routes, VPN_WG_MAX_ROUTES,
                             &cfg.config.wireguard.route_count);
    strlcpy(cfg.config.wireguard.endpoint,     "0.0.0.0", sizeof(cfg.config.wireguard.endpoint));
    cfg.config.wireguard.port = 51820;
    cfg.config.wireguard.persistent_keepalive = 25;

    vpn_manager_save_config(&cfg);
    vpn_manager_request_reload();
    vpn_manager_set_enabled(true);
    vpn_manager_request_test();
}

// Core VPN task: owns WG ctx; gates on dev_status; backoff on failures
static void vpn_task_fn(void *arg)
{
    (void)arg;
    bool test_once = false;

    const TickType_t tick = pdMS_TO_TICKS(200);
    for (;;)
    {
        if(!dev_status_is_sta_connected())
        {
            dev_status_wait_for_bits(DEV_STA_CONNECTED_BIT, portMAX_DELAY);
            vTaskDelay(pdMS_TO_TICKS(3000));
        }
        // Drain commands
        vpn_cmd_msg_t msg;
        while (s_vpn_cmd_q && xQueueReceive(s_vpn_cmd_q, &msg, 0) == pdTRUE)
        {
            switch (msg.type)
            {
                case VPN_CMD_ENABLE:
                    ESP_LOGI(TAG, "CMD ENABLE: %s", msg.enabled ? "on" : "off");
                    // Nothing else; bit already set/cleared by caller
                    // If disabling, stop immediately
                    if (!msg.enabled)
                    {
                        vpn_manager_stop();
                        vpn_backoff_reset();
                    }
                    break;
                case VPN_CMD_RELOAD:
                {
                    vpn_config_t cfg = {0};
                    if (vpn_manager_load_config(&cfg) == ESP_OK)
                    {
                        current_config = cfg;
                        ESP_LOGI(TAG, "Config reloaded (type=%d, enabled=%d)", cfg.type, (int)cfg.enabled);
                        // If connected and type/params changed, restart
                        if (current_status == VPN_STATUS_CONNECTED || current_status == VPN_STATUS_CONNECTING)
                        {
                            vpn_manager_stop();
                            vpn_backoff_reset();
                        }
                    }
                    else
                    {
                        ESP_LOGW(TAG, "Config reload failed");
                    }
                }
                break;
                case VPN_CMD_TEST:
                    ESP_LOGI(TAG, "CMD TEST: will attempt one-shot connect when gated");
                    test_once = true;
                    vpn_backoff_reset();
                    if (current_status == VPN_STATUS_CONNECTED)
                    {
                        // Force re-handshake
                        vpn_manager_stop();
                    }
                    break;

                case VPN_CMD_TEST_CONFIG:
                    if (msg.has_config)
                    {
                        ESP_LOGI(TAG, "CMD TEST_CONFIG: applying provided config for one-shot test (type=%d, enabled=%d)",
                                msg.config.type, (int)msg.config.enabled);
                        current_config = msg.config;
                        test_once = true;
                        vpn_backoff_reset();
                        if (current_status == VPN_STATUS_CONNECTED || current_status == VPN_STATUS_CONNECTING)
                        {
                            vpn_manager_stop();
                        }
                    }
                    break;
            }
        }

        // Test resolved successfully (status may be flipped by any thread via
        // vpn_manager_update_status): clear the override and grant a grace window
        // before the home-bypass gate may re-pause, so the UI can observe success.
        if (test_once && current_status == VPN_STATUS_CONNECTED)
        {
            test_once = false;
            s_home_test_grace_until_us = esp_timer_get_time() + 60000000LL; // 60s
        }

        // Gating conditions
        bool prereqs = dev_status_are_bits_set(DEV_VPN_ENABLED_BIT | DEV_STA_CONNECTED_BIT);
        bool blockers = dev_status_is_any_bit_set(DEV_AP_ENABLED_BIT | DEV_SLEEP_BIT);

        if (!prereqs || blockers)
        {
            if (current_status == VPN_STATUS_CONNECTED || current_status == VPN_STATUS_CONNECTING)
            {
                ESP_LOGI(TAG, "Gating lost or blocker set; stopping VPN");
                vpn_manager_stop();
            }
            // Idle state while waiting
            vTaskDelay(tick);
            continue;
        }

        // Home-network bypass: hold the VPN down while the STA is associated to a
        // trusted home SSID. A pending one-shot test (test_once) overrides the pause
        // so a deliberate tunnel test from home is still possible.
        bool home_match = current_config.home_bypass_enabled &&
                          vpn_manager_home_ssid_matched(current_config.home_ssids);
        if (home_match && !test_once && esp_timer_get_time() >= s_home_test_grace_until_us)
        {
            if (!s_home_paused)
            {
                ESP_LOGI(TAG, "Trusted home SSID detected; pausing VPN");
            }
            s_home_paused = true;
            if (current_status == VPN_STATUS_CONNECTED || current_status == VPN_STATUS_CONNECTING)
            {
                vpn_manager_stop();
                vpn_backoff_reset();
            }
            current_status = VPN_STATUS_PAUSED_HOME;
            vTaskDelay(tick);
            continue;
        }
        if (s_home_paused)
        {
            // Left the home network (or bypass disabled/test requested); resume normal flow.
            ESP_LOGI(TAG, "Home bypass cleared; resuming VPN connect flow");
            s_home_paused = false;
            if (current_status == VPN_STATUS_PAUSED_HOME)
            {
                current_status = VPN_STATUS_DISCONNECTED;
            }
            vpn_backoff_reset();
        }

        // Attempt connect if needed
        if (current_status != VPN_STATUS_CONNECTED && current_status != VPN_STATUS_CONNECTING)
        {
            if (s_backoff_ms == 0 || test_once)
            {
                if (current_config.type == VPN_TYPE_WIREGUARD)
                {
                    esp_err_t vret = vpn_manager_validate_wireguard_config(&current_config.config.wireguard);
                    if (vret != ESP_OK)
                    {
                        // Invalid config: do not retry until reload
                        ESP_LOGE(TAG, "Invalid WG config; waiting for reload");
                        // Sleep a bit to avoid tight loop
                        vTaskDelay(pdMS_TO_TICKS(2000));
                    }
                    else
                    {
                        ESP_LOGI(TAG, "Connecting VPN...");
                        esp_err_t sret = vpn_manager_start(&current_config);
                        if (sret != ESP_OK)
                        {
                            ESP_LOGW(TAG, "Connect failed: %s", esp_err_to_name(sret));
                            if (!test_once)
                            {
                                vpn_backoff_bump();
                            }
                            else
                            {
                                test_once = false; // single attempt
                            }
                        }
                        else
                        {
                            vpn_backoff_reset();
                            // Keep test_once set through CONNECTING so the home-bypass
                            // gate doesn't kill an in-flight test; it clears when the
                            // test resolves (connected below, or connect timeout).
                        }
                    }
                }
            }
        }
        else if (current_status == VPN_STATUS_CONNECTING)
        {
            // Drive state transitions without relying on UI polling.
            vpn_manager_update_status();

            // If still connecting, enforce a timeout so a bad profile doesn't hijack routing forever.
            if (current_status == VPN_STATUS_CONNECTING && s_connect_started_us != 0)
            {
                int64_t now_us = esp_timer_get_time();
                if ((now_us - s_connect_started_us) > s_connect_timeout_us)
                {
                    ESP_LOGW(TAG, "VPN connect timeout; stopping and backing off");
                    vpn_manager_stop();
                    vpn_backoff_bump();
                    test_once = false; // test resolved as failure
                }
            }
        }
        else if (current_status == VPN_STATUS_CONNECTED)
        {
            // Monitor link; if peer down, trigger reconnect with backoff
            if (!vpn_wg_is_peer_up())
            {
                ESP_LOGW(TAG, "Peer down; restarting VPN with backoff");
                vpn_manager_stop();
                vpn_backoff_bump();
            }
        }

        // Handle backoff countdown while still responsive to commands
        if (s_backoff_ms > 0)
        {
            uint32_t step = 200;
            if (s_backoff_ms < step)
            {
                step = s_backoff_ms;
            }
            vTaskDelay(pdMS_TO_TICKS(step));
            s_backoff_ms -= step;
        }
        else
        {
            vTaskDelay(tick);
        }
    }
}
