# bitz fork of WiCAN PRO v6 (alfa-04 base)

Upstream **v6.00p_alfa-04** (wican-fw `a628a98` + meatpi-components `e786dd5`) plus the
little the fork still needs. Two repos, one branch name:

| Repo | Branch | What lives there |
|---|---|---|
| `jtruland/wican-fw` | `bitz/v6-alfa04` | top-level `CMakeLists.txt` (components URL + ref), this file, `tools/bitz/` |
| `jtruland/meatpi-components` | `bitz/v6-alfa04` | every firmware component; all changes are here |

The top-level `CMakeLists.txt` auto-clones `jtruland/meatpi-components@bitz/v6-alfa04` into
`components/meatpi/` (gitignored) when no checkout is found. Override with
`MEATPI_COMPONENTS_GIT_URL` / `MEATPI_COMPONENTS_GIT_REF`.

## On top of upstream

| Change | Component | Upstream status |
|---|---|---|
| Wake on IMU motion, and hold awake while moving (`motion_wake`, `motion_mg`) | `sleep_manager` | requested in meatpiHQ/wican-fw#974, no reply yet |
| "with BitzDev" beside the header logo | `web_ui_v2` | fork-only, never upstream |
| `vpn_status` table keyed by enum; no out-of-bounds read while connected | `ha_webhooks` | local fix for meatpiHQ/wican-fw#985 |
| Restore learns `redacted` from the decrypted document | `config_backup` | local fix for meatpiHQ/wican-fw#986 |

Drop the two local fixes when upstream ships its own. Everything else the v4 fork carried
(split tunnel, home-Wi-Fi VPN pause, `sta_ip`, USB GPS default, full backup, CLI errors) is
upstream in alfa-04 (#975–#980). The superseded alfa-03 port is branch `bitz/v6`, tag
`v6.00p_alfa-03-bitz1`.

## Settings: additions only, never a schema bump

⚠️ In v6 a descriptor `version` bump **without** an `on_migrate` path resets the component
to defaults at boot (`settings_manager_boot.c`). The motion keys are non-breaking additions,
filled from the schema defaults; `sleep_manager` stays at upstream's version.

## Before flashing the truck

- ⚠️ v6 requires ha-wican branch **`meatpi-multi-device`** (alfa-04 release notes).
- ⚠️ VPN: alfa-04 replaces Allowed IP/Mask with `routes`. List the VPS tunnel subnet
  (`10.200.0.0/24`) in `routes` explicitly, then tick "Pause VPN" on the home network.
- ⚠️ Take a backup and note the WireGuard keys first; the release still says v6 deletes all
  settings.

## Build (forge, CT 110)

ESP-IDF **6.0.2** (`espressif/idf:v6.0.2`). `tools/bitz/forge-build.sh` is installed as
`/opt/builds/wican-v6-build.sh`; it takes the branch from `BITZ_BRANCH` (default
`bitz/v6-alfa04`) and logs to `/tmp/wican-v6-build.log`. It clones the components before any
`docker run -w` into them (a missing `-w` path is created empty and CMake then skips its
clone), runs the `sleep_manager` + `vpn_manager` host tests under `timeout` (the
linux-target ELF never exits on its own), then `idf.py build`.
