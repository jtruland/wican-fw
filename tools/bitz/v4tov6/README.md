# v4 → v6 settings converter

Turns a capture of a WiCAN PRO **v4.51** (bitz fork) configuration into a **v6 settings
backup** (`wican_backup: 1`, settings only) that v6's *Backup and Restore* page accepts.
v6 has no importer of its own: a v4 → v6 OTA starts from defaults.

## Use

1. **Capture** the v4 device (it must be awake and reachable). Save each response as
   `<path with / replaced by _>.json` in one directory: `/check_status`, `/load_config`,
   `/load_auto_pid`, `/load_auto_pid_car_data`, `/load_canflt`, `/load_car_config`,
   `/vpn/load_config`, `/api/webhook`, `/cert_manager/sets`. ⚠️ `load_config` holds
   passwords in clear: keep the capture out of git.
2. **Convert:** `python3 -I convert.py <capture_dir> -o v6-backup.json --report`
   - `--target bitz` (default) also emits `sleep_manager.motion_wake/motion_mg`, which only
     exist on the bitz firmware; `--target upstream` for stock v6.
   - `--wg-route` (default `10.200.0.0/24`): v6 routes nothing through WireGuard that is not
     listed, so the tunnel's own subnet must be given. `--vpn-off` to skip the tunnel.
   - `--ap-password`: v6 refuses the factory `@meatpi#`.
3. **On the v6 device, before restoring:** generate a WireGuard key (v4 never exported the
   private key; the backup carries it blank, which means "keep the device's") and register
   its public key on the server; set a non-factory AP password, or pass `--ap-password`.
4. **Restore** the file. v6 dry-runs every component before writing anything.

Each emitted component is restored **whole**: keys the converter does not set go to the v6
schema default (the report lists them). Components it does not emit are left alone.
AutoPID PID tables are **not** converted (settings only).

## Files

- `extract_schema.py` → `v6_schema.json`: every component's settings schema, parsed from
  the v6 C source (`FIELDS[]` tables + descriptor versions). Re-run after an upstream bump.
- `convert.py`: the converter; `--report` prints mapped / dropped / left-at-default.
- `validate.py`: synthetic captures (from v4's `device_config_default`) → convert → an
  emulated restore (defaults fill, schema, cross-field rules, blank-secret rule).
- `host/host_validate.py` + `harness.c` + `stubs/`: compiles the **unmodified** v6
  `settings_manager`, `config_backup` codec and the emitted components' `*_settings.c`
  with gcc (no ESP-IDF) and runs the real restore validation on the converter's output.

## Test setup (not committed: regenerable)

```bash
cd tools/bitz/v4tov6
mkdir -p src/v4 src/v6 src/v6fork third_party
git -C ../../.. archive feature/motion-wake main components | tar -x -C src/v4
git -C <meatpi-components clone> archive e786dd5 components | tar -x -C src/v6
git -C <meatpi-components clone> archive bitz/v6-alfa04 components | tar -x -C src/v6fork
git clone -q --depth 1 -b v1.7.18 https://github.com/DaveGamble/cJSON third_party/cjson
python3 -I extract_schema.py && python3 -I validate.py && python3 -I host/host_validate.py --fork
```
