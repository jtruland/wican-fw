# bitz fork of WiCAN PRO v6

Upstream **v6.00p_alfa-03** (wican-fw `93752d6` + meatpi-components `a72359f`) with the
v4.51 fork's features that v6 still lacks, ported on top. Two repos, one branch name:

| Repo | Branch | What lives there |
|---|---|---|
| `jtruland/wican-fw` | `bitz/v6` | `main/`, top-level `CMakeLists.txt` (components URL + lwIP hook), this file |
| `jtruland/meatpi-components` | `bitz/v6` | every firmware component; all ports are here |

The top-level `CMakeLists.txt` auto-clones `jtruland/meatpi-components@bitz/v6` into
`components/meatpi/` (gitignored) when no checkout is found. Override with
`MEATPI_COMPONENTS_GIT_URL` / `MEATPI_COMPONENTS_GIT_REF`.

## Ported (v4 commit → v6 component)

| v4 commit | Feature | v6 location |
|---|---|---|
| `d01d388` | Wake on IMU motion; **new:** also hold awake while moving | `sleep_manager` (`motion_wake`, `motion_mg`) |
| `a8ac72e` | WireGuard destination-CIDR split tunnel (lwIP route hook) | `vpn_manager` (`routes`), `include/vpn_route_hook.h`, top `CMakeLists.txt` |
| `5cc581d` | Pause the VPN on a trusted home SSID | `vpn_manager` (`home_bypass`, `home_ssids`, state `paused_home`) |
| `33baf1c` | `sta_ip` in the HA webhook status | `ha_webhooks` |
| `475e8bd` | USB GPS: v6 has the parser; **default flipped on** | `usb_acm_cli.enabled` = true |
| `d354c2b` | Backup includes AutoPID files + `_meta.not_included` | `api_http` `/api/settings/backup` |
| `d63da9e` | "with BitzDev" beside the header logo | `web_ui_v2` |
| `a8ac72e` | CLI prints stray `esp_console_run` errors | `cmdline_manager` |

**Already in v6, not ported:** keygen key loss (`e29e921`), byte-reversed public key
(`5180054`), Generate-keys button (`28f1e94`), rejected config rebooting past (`3211b5e`),
WG DNS field (`4a9bfa7`), password masking, firmware version in status, the git-describe
version string (`5f0a099`), `dependencies.lock` pin (`be00870`). `63942de` (one-shot test
vs home pause) has no v6 counterpart: v6 has no VPN test button.

## Settings: additions only, never a schema bump

⚠️ In v6 a descriptor `version` bump **without** an `on_migrate` path resets the
component to defaults at boot (`settings_manager_boot.c`). For `vpn_manager` that would
wipe the WireGuard keys. Every key added here is a non-breaking addition: boot fills it
from the schema default (`sm_fill_missing`), and no version was bumped.

## Build (forge, CT 110)

ESP-IDF **6.0.2** is required (`espressif/idf:v6.0.2`); the old 5.5.2 image only builds
the v4.51 tree. Script: `/opt/builds/wican-v6-build.sh` → log `/tmp/wican-v6-build.log`.
It clones the components *before* any `docker run -w` into them (a missing `-w` path is
created empty, and CMake then accepts the empty dir and skips its clone), runs the
`sleep_manager` + `vpn_manager` host tests under `timeout` (the linux-target ELF never
exits on its own), then `idf.py build`.

⚠️ Upstream's release notes say v6 **deletes all settings** when flashed over v4.x
("fixed setting persist issue when upgrading from v4.51" in Alfa-03 — untested here).
Take a v4 backup and note the WireGuard keys before flashing.
