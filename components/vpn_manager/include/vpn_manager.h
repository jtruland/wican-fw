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

#ifndef VPN_MANAGER_H
#define VPN_MANAGER_H

#include <esp_err.h>
#include <esp_event.h>
#include <stdint.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include "vpn_manager_http.h"

struct netif; // lwIP netif, forward-declared to avoid a full lwip include here

#ifdef __cplusplus
extern "C" {
#endif

// VPN Event Group Bits
#define VPN_CONNECTED_BIT       BIT0
#define VPN_CONNECTING_BIT      BIT1
#define VPN_DISCONNECTED_BIT    BIT2
#define VPN_ERROR_BIT           BIT3

// VPN Types
typedef enum 
{
    VPN_TYPE_DISABLED = 0,
    VPN_TYPE_WIREGUARD = 1
} vpn_type_t;

// VPN Status
typedef enum
{
    VPN_STATUS_DISABLED = 0,
    VPN_STATUS_DISCONNECTED,
    VPN_STATUS_CONNECTING,
    VPN_STATUS_CONNECTED,
    VPN_STATUS_ERROR,
    VPN_STATUS_PAUSED_HOME   // VPN intentionally held down: STA is on a trusted home SSID
} vpn_status_t;

// A single destination CIDR routed over the WG tunnel (real AllowedIPs semantics,
// distinct from the tunnel's own local address/mask below).
#define VPN_WG_MAX_ROUTES 8
typedef struct
{
    char ip[16];
    char mask[16];
} vpn_wg_route_t;

// VPN WireGuard Configuration (separate from esp_wireguard API)
typedef struct
{
    char private_key[64];
    char public_key[64];
    char preshared_key[64];
    // Optional DNS servers from WireGuard config (IPv4 only)
    char dns_main[16];
    char dns_backup[16];
    char address[32];
    // Legacy single-CIDR fields, kept for JSON back-compat. No longer used to
    // derive the tunnel's local IP (see vpn_wg_init) -- superseded by routes[].
    char allowed_ip[32];
    char allowed_ip_mask[32];
    // Parsed destination routes (from the same "AllowedIPs" UI field, now
    // genuinely comma-separated). These are what the IP4 route hook matches
    // against -- they do NOT affect the tunnel's own local address.
    vpn_wg_route_t routes[VPN_WG_MAX_ROUTES];
    uint8_t route_count;
    char endpoint[64];
    int port;
    int persistent_keepalive;
} vpn_wireguard_config_t;

// VPN Configuration
typedef struct
{
    vpn_type_t type;
    bool enabled;
    // Home-network bypass: pause the VPN while the STA is associated to a trusted SSID.
    // SSID match is authenticated by the WPA association itself (we only join networks
    // we hold credentials for), so no additional probing is required.
    bool home_bypass_enabled;
    char home_ssids[100]; // comma-separated trusted SSID list
    union
    {
        vpn_wireguard_config_t wireguard;
    } config;
} vpn_config_t;

/**
 * @brief Initialize VPN manager
 * 
 * @return esp_err_t ESP_OK on success
 */
esp_err_t vpn_manager_init(void);

/**
 * @brief Deinitialize VPN manager
 * 
 * @return esp_err_t ESP_OK on success
 */
// Deinit no longer exposed (task lifecycle is app-owned)

/**
 * @brief Start VPN connection
 * 
 * @param config VPN configuration
 * @return esp_err_t ESP_OK on success
 */
esp_err_t vpn_manager_start(const vpn_config_t *config);

/**
 * @brief Stop VPN connection
 * 
 * @return esp_err_t ESP_OK on success
 */
esp_err_t vpn_manager_stop(void);

/**
 * @brief Get VPN status
 * 
 * @return vpn_status_t Current VPN status
 */
vpn_status_t vpn_manager_get_status(void);

/**
 * @brief Get VPN event group handle
 * 
 * @return EventGroupHandle_t Event group handle
 */
/**
 * @brief Enable/disable VPN from application logic (server does not control runtime)
 */
void vpn_manager_set_enabled(bool enabled);

/**
 * @brief Request the VPN task to reload configuration from storage and reconcile state
 */
void vpn_manager_request_reload(void);

/**
 * @brief Request a one-shot VPN connectivity test (uses current config and gating)
 */
void vpn_manager_request_test(void);

/**
 * @brief Request a one-shot VPN connectivity test using the provided config (not persisted)
 *
 * Useful for "Test Connection" in the UI before the user stores changes.
 */
esp_err_t vpn_manager_request_test_with_config(const vpn_config_t *config);

/**
 * @brief Request a one-shot VPN connectivity test using hardcoded WG values (dev only)
 */
void vpn_manager_request_test_hardcoded(void);

/**
 * @brief Generate WireGuard key pair
 * 
 * @param public_key Buffer to store public key (base64 encoded)
 * @param public_key_size Size of public key buffer
 * @return esp_err_t ESP_OK on success
 */
esp_err_t vpn_manager_generate_wireguard_keys(char *public_key, size_t public_key_size);

/**
 * @brief Save VPN configuration to filesystem
 * 
 * @param config VPN configuration
 * @return esp_err_t ESP_OK on success
 */
esp_err_t vpn_manager_save_config(const vpn_config_t *config);

/**
 * @brief Load VPN configuration from filesystem
 * 
 * @param config Buffer to store loaded configuration
 * @return esp_err_t ESP_OK on success
 */
esp_err_t vpn_manager_load_config(vpn_config_t *config);

/**
 * @brief Parse WireGuard configuration from text
 * 
 * @param config_text Configuration text
 * @param config Buffer to store parsed configuration
 * @return esp_err_t ESP_OK on success
 */
// Text parse API removed from manager; use vpn_config_parse_wg if needed internally

/**
 * @brief Test VPN connection
 * 
 * @return esp_err_t ESP_OK if connection test passes
 */
// Deprecated test removed; use vpn_manager_request_test[_hardcoded]()

/**
 * @brief Get VPN IP address if connected
 * 
 * @param ip_str Buffer to store IP address string
 * @param ip_str_size Size of IP address buffer
 * @return esp_err_t ESP_OK if connected and IP retrieved
 */
esp_err_t vpn_manager_get_ip_address(char *ip_str, size_t ip_str_size);

// Returns true if a connect attempt is currently in progress.
// elapsed_ms/timeout_ms are optional outputs (set to 0 if not connecting).
bool vpn_manager_get_connect_timing(uint32_t *elapsed_ms, uint32_t *timeout_ms);

// True while the VPN is intentionally paused because the STA is on a trusted home SSID.
bool vpn_manager_home_bypass_active(void);

// Copy the currently associated STA SSID into ssid (empty string if not associated).
esp_err_t vpn_manager_get_sta_ssid(char *ssid, size_t size);

// Returns the WG lwIP netif handle when the tunnel is connected, else NULL.
// Used by the IP4 route hook (vpn_route_hook.h) to decide whether wg0 is a
// valid routing target. (Route matching itself is vpn_manager_route_matches(),
// declared in vpn_route_hook.h since it takes a concrete lwIP ip4_addr_t.)
struct netif *vpn_manager_get_netif(void);

#ifdef __cplusplus
}
#endif

#endif // VPN_MANAGER_H
