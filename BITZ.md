# bitz fork of WiCAN PRO v6 (beta.4 base)

Upstream **v6.0.0p-beta.4** (wican-fw tag `v6.0.0p-beta.4` = `3fbc944`, its release commit;
meatpi-components `0536313`, the components `main` the release was cut from: beta.4's main
glue `9279c10` calls `external_storage_register_http`, which first exists there) plus the
little the fork still needs. Two repos, one branch name:

| Repo | Branch | What lives there |
|---|---|---|
| `jtruland/wican-fw` | `bitz/v6-beta4` | top-level `CMakeLists.txt` (components URL + ref + pin), `version.txt`, this file, `tools/bitz/` |
| `jtruland/meatpi-components` | `bitz/v6-beta4` | every firmware component; all changes are here |

## The components pin

Upstream's `CMakeLists.txt` clones the components' default branch, unpinned. Ours clones
`jtruland/meatpi-components@bitz/v6-beta4` into `components/meatpi/` (gitignored) and checks
out **one commit**, `_meatpi_pin` = `dbfc75959fe5fbb1a3fb0eef9a359c1e96262d61`. A configure <!-- commit SHA: secret-scan:allow -->
whose `components/meatpi` sits on any other commit **fails**; a sibling checkout
(`../meatpi-components`) only warns. Moving the components means bumping the pin in the same
commit.

Overrides: `MEATPI_COMPONENTS_GIT_URL`, `MEATPI_COMPONENTS_GIT_REF` (a different ref drops the
pin unless a SHA is given too), `MEATPI_COMPONENTS_GIT_SHA` (`none` = the ref's tip, unpinned),
`MEATPI_COMPONENTS_PATH` (use that tree as-is; no clone, no pin check).

## Version

`version.txt` reads **`6.0.0-beta.4-bitz1`**: that is the app version the device reports
(`esp_app_desc`, About page, `/api/info`). `fw_build` carries the `git describe` of the build
(`v6.0.0p-beta.4-bitz1` when built from the tag). fw_update's tolerant parser does not accept
the `-bitz1` tail, so the stock updater reports `unknown_version` and never offers a stock
release; a manual upload still works.

## On top of upstream

| Change | Component | Upstream status |
|---|---|---|
| Wake on IMU motion, and hold awake while moving (`motion_wake`, `motion_mg`) | `sleep_manager` | requested in meatpiHQ/wican-fw#974 |
| "with BitzDev" beside the header logo | `web_ui_v2` | fork-only, never upstream |

Both cherry-picked cleanly onto `0536313`. The superseded ports are branches `bitz/v6-beta2`
(tag `v6.0.0p-beta.2-bitz1`), `bitz/v6-alfa04` (tag `v6.00p_alfa-04-bitz1`) and `bitz/v6`
(tag `v6.00p_alfa-03-bitz1`).

## AutoPID byte indexing changed in beta.4 (#1014)

⚠️ beta.4 reads the OBD chip with headers on and counts expression bytes the way v4 and the
published vehicle profiles do (every byte after the CAN id, ISO-TP framing included). The
alfa/beta.1/beta.2 importer shifted every index down by one. Standard rows are re-numbered by
the device at first boot; a **custom** row written on an earlier v6 against a single-frame
reply now reads one byte too far. After flashing, open each custom AutoPID row, run *Test a
PID* and move each `B` index up by one (or re-import the published profile). v4-era rows are
already in the v4 numbering.

## Settings: additions only, never a schema bump

⚠️ In v6 a descriptor `version` bump **without** an `on_migrate` path resets the component
to defaults at boot (`settings_manager_boot.c`). The motion keys are non-breaking additions,
filled from the schema defaults; `sleep_manager` stays at upstream's version.

## Before flashing the truck

- ⚠️ v6 requires ha-wican branch **`meatpi-multi-device`** (beta.4 release notes).
- ⚠️ VPN: v6 replaces Allowed IP/Mask with `routes`. List the VPS tunnel subnet
  (`10.200.0.0/24`) in `routes` explicitly, then tick "Pause VPN" on the home network.
- ⚠️ From 4.x the new partition layout erases files, logs and the saved configuration: take a
  backup and note the WireGuard keys first. `tools/bitz/v4tov6` converts a v4 capture into a
  v6 backup (`--target bitz` also turns off fw_update's scheduled stock check).
- ⚠️ Custom AutoPID rows: re-check each with *Test a PID* (see #1014 above).

## Build (forge, CT 110)

ESP-IDF **6.0.2** (`espressif/idf:v6.0.2`). `tools/bitz/forge-build.sh` is installed as
`/opt/builds/wican-v6-build.sh`; it takes the branch from `BITZ_BRANCH` (default
`bitz/v6-beta4`), the build tree from `BITZ_REPO` and the log from `BITZ_LOG`. The beta.4
build used `BITZ_REPO=/opt/builds/wican-fw-v6-beta4` and
`BITZ_LOG=/tmp/wican-v6-beta4-build.log`. It clones the components and checks out the pin
before any `docker run -w` into them (a missing `-w` path is created empty and CMake then
skips its clone), runs the `sleep_manager` + `vpn_manager` host tests under `timeout` (the
linux-target ELF never exits on its own), then `idf.py build`.
