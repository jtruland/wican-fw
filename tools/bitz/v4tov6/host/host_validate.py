#!/usr/bin/env python3
"""Compile v6's real settings validation on the host and dry-run backups.

    python3 -I host/host_validate.py <backup.json> [...] [--fork]

Builds host/build/harness from UNMODIFIED v6 sources (src/v6 = meatpi
components @0536313 = beta.4; with --fork, sleep_manager from src/v6fork =
bitz/v6-beta4) plus three stub headers (esp_err/esp_log/esp_attr) and a
FreeRTOS type stub. For every component the harness needs, a wrapper TU
#includes that component's *_settings.c and calls its own register
function, so the descriptor (FIELDS, version, on_validate, on_migrate) is
the firmware's. mqtt_can registers inside mqtt_can.c (radio/queue code), so
its FIELDS table is copied verbatim into a table-only TU instead (it has no
on_validate).

Each backup is run twice: against a FRESH device (stored = defaults, after
boot) and a CONFIGURED device (stored WireGuard private key + non-factory
AP password, all synthetic).
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
V6 = os.path.join(ROOT, "src/v6/components")
FORK = os.path.join(ROOT, "src/v6fork/components")
BUILD = os.path.join(HERE, "build")

# component -> (source relative to components/, register function)
COMPONENTS = {
    "wifi_manager": ("wifi_manager/wifi_manager_settings.c", "wm_settings_register"),
    "vpn_manager": ("vpn_manager/vpn_manager_settings.c", "vpn_settings_register"),
    "mqtt_manager": ("mqtt_manager/mqtt_manager_settings.c", "mm_settings_register"),
    "socket_manager": ("socket_manager/socket_manager_settings.c", "sm_settings_register"),
    "sleep_manager": ("sleep_manager/sleep_manager_settings.c", "sleep_settings_register"),
    "can_manager": ("can_manager/can_manager_settings.c", "canm_settings_register"),
    "ble_manager": ("ble_manager/ble_manager_settings.c", "blm_settings_register"),
    "ha_webhooks": ("ha_webhooks/ha_webhooks_settings.c", "hw_settings_register"),
    "autopid": ("autopid/autopid_settings.c", "ap_settings_register"),
    "fw_update": ("fw_update/fw_update_settings.c", "fwu_settings_register"),
}
# pure helpers the on_validate hooks call
HELPERS = [
    "wifi_manager/wifi_manager_select.c", "vpn_manager/vpn_manager_check.c",
    "ip4_routes/ip4_routes_core.c", "mqtt_manager/mqtt_manager_match.c",
    "socket_manager/socket_manager_policy.c",
    "settings_manager/settings_manager_schema.c",
    "settings_manager/settings_manager_fields.c",
    "config_backup/config_backup_core.c", "config_backup/config_backup_codec.c",
    "fw_update/fw_update_version.c",
]

SYN_KEY = "ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8="  # synthetic WG key (base64 of bytes 0x20..0x3f) secret-scan:allow


def includes():
    inc = ["-I" + os.path.join(HERE, "stubs"),
           "-I" + os.path.join(ROOT, "third_party/cjson"),
           "-I" + os.path.join(V6, "settings_manager"),
           "-I" + os.path.join(V6, "config_backup")]
    inc += ["-I" + d for d in sorted(glob.glob(os.path.join(V6, "*/include")))]
    return inc


def gcc(args):
    r = subprocess.run(["gcc", "-std=gnu11", "-O1", "-g", "-w"] + args,
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit("gcc failed: %s" % " ".join(args[-3:]))


def mqtt_can_tu():
    src = open(os.path.join(V6, "mqtt_can/mqtt_can.c")).read()
    defs = "\n".join(re.findall(r"^#define MC_TOPIC_LEN .*$", src, re.M))
    table = re.search(r"static const settings_field_t FIELDS\[\] =\s*\{.*?\n\};",
                      src, re.S).group(0)
    ver = re.search(r'\.name = "mqtt_can",\s*\.version = (\d+)', src).group(1)
    return ('/* GENERATED from mqtt_can.c: its FIELDS table, verbatim */\n'
            '#include "settings_manager.h"\n#include "mqtt_can_codec.h"\n'
            '%s\n%s\n'
            'static const settings_descriptor_t DESC = { .name = "mqtt_can", '
            '.version = %s, .fields = FIELDS, .field_count = sizeof(FIELDS) / '
            'sizeof(FIELDS[0]) };\n'
            'const settings_descriptor_t *hv_get_mqtt_can(void) '
            '{ return &DESC; }\n' % (defs, table, ver))


def build(fork):
    os.makedirs(BUILD, exist_ok=True)
    inc = includes()
    objs = []
    regs = []
    for comp, (rel, regfn) in COMPONENTS.items():
        base = FORK if (fork and os.path.exists(os.path.join(FORK, rel))) else V6
        path = os.path.join(base, rel)
        tu = os.path.join(BUILD, "reg_%s.c" % comp)
        extra = ""
        if comp == "wifi_manager":
            extra = ("void hv_wifi_set_booted(bool b) { s_boot_applied = b; }\n")
        with open(tu, "w") as f:
            f.write('/* GENERATED: the real %s descriptor */\n#include "%s"\n'
                    'extern const settings_descriptor_t *hv_take(void);\n'
                    'const settings_descriptor_t *hv_get_%s(void)\n'
                    '{ static const settings_descriptor_t *d; '
                    'if (d == NULL) { (void)%s(); d = hv_take(); } return d; }\n%s'
                    % (comp, path, comp, regfn, extra))
        obj = tu[:-2] + ".o"
        gcc(["-c"] + inc + ["-I" + os.path.dirname(path),
                            "-I" + os.path.join(V6, comp), tu, "-o", obj])
        objs.append(obj)
        regs.append(comp)
    tu = os.path.join(BUILD, "reg_mqtt_can.c")
    open(tu, "w").write(mqtt_can_tu())
    gcc(["-c"] + inc + [tu, "-o", tu[:-2] + ".o"])
    objs.append(tu[:-2] + ".o")
    regs.append("mqtt_can")

    gen = os.path.join(BUILD, "gen_regs.c")
    with open(gen, "w") as f:
        f.write('#include "settings_manager.h"\n')
        for c in regs:
            f.write("const settings_descriptor_t *hv_get_%s(void);\n" % c)
        f.write("typedef struct { const char *name; const settings_descriptor_t "
                "*(*get)(void); } hv_reg_t;\nconst hv_reg_t HV_REGS[] = {\n")
        for c in regs:
            f.write('    { "%s", hv_get_%s },\n' % (c, c))
        f.write("    { 0, 0 },\n};\n")
    for rel in HELPERS:
        p = os.path.join(V6, rel)
        obj = os.path.join(BUILD, os.path.basename(p)[:-2] + ".o")
        gcc(["-c"] + inc + ["-I" + os.path.dirname(p), p, "-o", obj])
        objs.append(obj)
    exe = os.path.join(BUILD, "harness")
    gcc(inc + [os.path.join(HERE, "harness.c"), gen,
               os.path.join(ROOT, "third_party/cjson/cJSON.c")] + objs +
        ["-lm", "-Wl,--unresolved-symbols=ignore-all", "-o", exe])
    return exe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("backups", nargs="+")
    ap.add_argument("--fork", action="store_true",
                    help="sleep_manager from the bitz fork (motion_mg)")
    a = ap.parse_args()
    exe = build(a.fork)
    configured = os.path.join(BUILD, "stored-configured.json")
    json.dump({"vpn_manager": {"private_key": SYN_KEY},
               "wifi_manager": {"ap_password": "SYNTHETIC-dev-ap"}},
              open(configured, "w"))
    rc = 0
    for b in a.backups:
        for label, stored in (("fresh device", "-"),
                              ("configured device", configured)):
            r = subprocess.run([exe, b, stored, "1"], capture_output=True,
                               text=True)
            print("== %s / %s (exit %d)" % (os.path.basename(b), label,
                                            r.returncode))
            sys.stdout.write(r.stdout)
            if r.returncode < 0:
                print("   CRASH: signal %d" % -r.returncode)
                rc = 1
            if label == "configured device" and r.returncode != 0:
                rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
