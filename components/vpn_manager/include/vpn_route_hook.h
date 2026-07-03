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

/*
 * Injected into lwIP via ESP_IDF_LWIP_HOOK_FILENAME (see top-level
 * CMakeLists.txt), following the exact pattern Espressif's own
 * examples/network/vlan_support uses: the hook is a `static inline` function
 * defined directly in this header, compiled straight into whichever lwip
 * core .c file needs it. This sidesteps a real pitfall of doing it as a
 * separate .c file in another component: nothing else in the app references
 * that object file by name, so plain archive linking never selects it and
 * the final link fails with "undefined reference" -- lwip's own components
 * (main, in vlan_support's case) get force-linked; a random extra component
 * does not.
 *
 * This replaces (does not remove) the stock LWIP_HOOK_IP4_ROUTE_SRC wiring:
 * ip4_route_src() itself (used unconditionally by tcp_connect/udp_sendto/
 * raw_sendto whenever a PCB already has a bound, non-ANY local address) only
 * compiles at all when this macro is defined, so it must stay defined --
 * pointing at our wrapper, which tries a destination-CIDR match first and
 * falls back to the real stock hook for everything else.
 */
#pragma once

#include <stdbool.h>
#include "lwip/ip4_addr.h"
#include "lwip/netif.h"

// Implemented in vpn_manager.c.
struct netif *vpn_manager_get_netif(void);
bool vpn_manager_route_matches(const ip4_addr_t *dest);

// Real stock hook (components/lwip/port/hooks/lwip_default_hooks.c, part of
// liblwip.a) -- declared here since lwip_default_hooks.h only declares it
// AFTER including this file.
struct netif *ip4_route_src_hook(const ip4_addr_t *src, const ip4_addr_t *dest);

static inline struct netif *wican_ip4_route_hook(const ip4_addr_t *src, const ip4_addr_t *dest)
{
    if (dest != NULL && vpn_manager_route_matches(dest))
    {
        struct netif *wg_netif = vpn_manager_get_netif();
        if (wg_netif != NULL)
        {
            return wg_netif;
        }
    }
    // No destination match (or tunnel down) -- fall back to the stock
    // source-address-based behavior needed by tcp/udp/raw.
    return ip4_route_src_hook(src, dest);
}

#undef LWIP_HOOK_IP4_ROUTE_SRC
#define LWIP_HOOK_IP4_ROUTE_SRC wican_ip4_route_hook
