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

/* Internal WireGuard backend for VPN manager */
#pragma once

#include <esp_err.h>
#include <stdbool.h>
#include "include/vpn_manager.h"

struct netif; // lwIP netif, forward-declared to avoid a full lwip include here

#ifdef __cplusplus
extern "C" {
#endif

// Init/Deinit backend (creates wg context)
esp_err_t vpn_wg_init(const vpn_wireguard_config_t *cfg);
esp_err_t vpn_wg_deinit(void);

// Start/Stop connection
esp_err_t vpn_wg_start(void);
esp_err_t vpn_wg_stop(void);

// Helpers
bool vpn_wg_is_peer_up(void);

// Returns the WG lwIP netif when the peer is up, else NULL. Used by
// vpn_route_hook.c to decide whether wg0 is a valid routing target.
struct netif *vpn_wg_get_netif(void);

#ifdef __cplusplus
}
#endif
