#!/usr/bin/env python3
"""Build SYNTHETIC v4 captures, convert them, and check the result the way
v6's restore would.

    python3 -I validate.py [--v4-src <v4 main/config_server.c>] [--keep DIR]

Scenario "typical": v4 device_config_default (read from the v4 source, so it can
be regenerated) + realistic values (sta_ssid "HomeWiFi", sleep_volt 12.2,
sleep_time 30, wakeup_interval 100, imu_threshold 8, mqtt_en enable, webhook
enabled, WireGuard 203.0.113.10:51821 / 10.200.0.2). Every secret, URL and
key in it is a SYNTHETIC placeholder.
Scenario "custom": a changed AP password, the tunnel off, a custom TCP port,
fallback networks with a home-bypass SSID, TLS MQTT.
Scenario "sparse": only load_config.json, half the files absent/empty.

Checks per emitted component, mirroring settings_manager_restore():
  1. version <= the schema's (newer = refused)
  2. omitted keys filled from schema defaults (sm_fill_missing)
  3. the merged document validated against v6_schema.json (type, range,
     enum, length, array items: settings_manager_schema.c rules)
  4. the cross-field on_validate rules this converter knows about
     (wifi_manager, vpn_manager, mqtt_manager), evaluated twice: against a
     FRESH v6 device (stored = defaults) and a CONFIGURED one (holds a
     WireGuard key and a non-factory AP password), applying the redacted
     blank-secret rule (cb_unredact) first.
The strongest check (the real C validators) is host_validate.sh.
"""
import argparse
import base64
import copy
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import convert  # noqa: E402

DEV_ID = "a0b1c2d3e4f5"   # synthetic
SYN_PEER = base64.b64encode(bytes(range(32))).decode()       # synthetic
SYN_DEVICE_PRIV = base64.b64encode(bytes(range(32, 64))).decode()  # synthetic


def v4_default_config(path):
    src = open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r'^const char device_config_default\[\] = "(.*?)";', src,
                  re.S | re.M)
    body = m.group(1)
    body = re.sub(r'\\\s*\n\s*', "", body)       # C line continuations
    body = body.replace('\\"', '"')
    body = body.replace("%s", DEV_ID)
    return json.loads(body)


def scenario_typical(v4src):
    cfg = v4_default_config(v4src)
    cfg.update({
        "wifi_mode": "APStation",
        "sta_ssid": "HomeWiFi", "sta_pass": "SYNTHETIC-sta-pass",
        "sta_security": "wpa2",
        "ap_pass": "@meatpi#",
        "sleep_status": "enable", "sleep_disable_agree": "no",
        "sleep_volt": "12.2", "sleep_time": "30",
        "periodic_wakeup": "enable", "wakeup_interval": "100",
        "imu_threshold": "8",
        "mqtt_en": "enable", "mqtt_url": "mqtt://192.168.86.9",
        "mqtt_port": "1899", "mqtt_user": "SYNTHETIC-user",
        "mqtt_pass": "SYNTHETIC-mqtt-pass",
        "mqtt_tx_en": "disable", "mqtt_rx_en": "disable",
        "protocol": "auto_pid", "webhook_en": "enable",
        "ap_auto_disable": "disable",
    })
    return {
        "load_config.json": cfg,
        "vpn_load_config.json": {
            "success": True, "vpn_type": 1, "enabled": True,
            "home_bypass_enabled": False, "home_ssids": "",
            "wireguard": {
                "peer_public_key": SYN_PEER, "preshared_key_present": False,
                "address": "10.200.0.2/32", "allowed_ips": "10.200.0.0/24",
                "endpoint": "203.0.113.10:51821", "persistent_keepalive": 25},
            "status": "connected"},
        "api_webhook.json": {
            "url": "https://example.invalid/api/webhook/SYNTHETIC",
            "manual_override": False, "enabled": True, "last_post": "",
            "status": "ok", "retries": 0, "interval": 60,
            "success_count": 1, "fail_count": 0, "last_error_time": "",
            "last_error": ""},
        "load_auto_pid.json": {
            "initialisation": "ATSP6;ATSH7E0;", "grouping": "enable",
            "webhook_data_mode": "changed", "car_model": "Generic: Vehicle",
            "standard_pids": "enable", "car_specific": "enable",
            "disable_on_sleep_voltage": "disable_pid_requests",
            "pids": [{"pid": "0105", "parameters": []}], "destinations": []},
        "load_auto_pid_car_data.json": {"init": "ATSP6;", "pids": []},
        "load_canflt.json": [],
        "load_car_config.json": {},
        "cert_manager_sets.json": [],
        "check_status.json": {"device_id": DEV_ID, "fw_version": "4.51"},
    }


def scenario_custom(v4src):
    cap = scenario_typical(v4src)
    cfg = cap["load_config.json"]
    cfg.update({
        "ap_pass": "SYNTHETIC-ap-pw", "ap_ssid_en": "enable",
        "ap_ssid": "WiCAN-Truck", "ap_ch": "11",
        "protocol": "elm327", "port_type": "tcp", "port": "3500",
        "sta_fallbacks": [{"ssid": "HomeNet", "pass": "SYNTHETIC-home",
                           "security": "wpa3"},
                          {"ssid": "Phone", "pass": "", "security": "wpa2"}],
        "mqtt_security": "tls", "mqtt_cert_set": "broker",
        "mqtt_tx_topic": "cars/truck/can/tx",
        "mqtt_rx_topic": "cars/truck/can/rx",
        "mqtt_status_topic": "cars/truck/can/status",
        "mqtt_rx_en": "enable", "ble_status": "enable", "ble_pass": "654321",
        "ble_power": "3", "can_datarate": "250K", "sleep_volt": "15.5",
    })
    cap["vpn_load_config.json"]["enabled"] = False
    cap["vpn_load_config.json"]["home_bypass_enabled"] = True
    cap["vpn_load_config.json"]["home_ssids"] = "HomeNet, Elsewhere"
    cap["api_webhook.json"]["urls"] = [
        "https://example.invalid/a", "https://example.invalid/b"]
    cap["load_auto_pid.json"]["disable_on_sleep_voltage"] = "automate_threshold"
    cap["load_auto_pid.json"]["pid_polling_min_voltage"] = 13.0
    return cap


def scenario_sparse(v4src):
    cap = scenario_typical(v4src)
    return {"load_config.json": cap["load_config.json"],
            "vpn_load_config.json": "", "api_webhook.json": None}


# ---- the restore emulation ----------------------------------------------------
def defaults(schema_comp):
    return {f["key"]: copy.deepcopy(f["default"]) for f in schema_comp["fields"]}


def check_schema(comp, sc, data, target):
    errs = []
    for f in sc["fields"]:
        k = f["key"]
        if k not in data:
            errs.append("missing %s" % k)
            continue
        v = data[k]
        t = f["type"]
        if t == "bool" and not isinstance(v, bool):
            errs.append("%s not bool" % k)
        elif t == "int" and (isinstance(v, bool) or not isinstance(v, int)
                             or not f["min"] <= v <= f["max"]):
            errs.append("%s=%r not int in %d..%d" % (k, v, f["min"], f["max"]))
        elif t == "string":
            if not isinstance(v, str):
                errs.append("%s not string" % k)
                continue
            n = len(v.encode())
            if "enum" in f and v not in f["enum"]:
                errs.append("%s=%r not in enum" % (k, v))
            if f.get("maxlen") and n > f["maxlen"]:
                errs.append("%s longer than %d" % (k, f["maxlen"]))
            if f.get("minlen") and n < f["minlen"]:
                errs.append("%s shorter than %d" % (k, f["minlen"]))
        elif t == "array":
            if not isinstance(v, list) or len(v) > f["max_items"]:
                errs.append("%s bad array" % k)
            else:
                for it in v:
                    for sub in f["items"] or []:
                        if sub.get("required") and sub["key"] not in it:
                            errs.append("%s item misses %s" % (k, sub["key"]))
    return errs


def wg_key(k):
    return bool(convert.WG_KEY_RE.match(k or ""))


def check_cross(comp, d, booted=True):
    if comp == "wifi_manager":
        if d["sta_password"] and not d["sta_ssid"]:
            return "sta_password set but sta_ssid is empty"
        for b in ("sta", "fallback1", "fallback2", "fallback3", "fallback4",
                  "fallback5"):
            if d[b + "_ip_mode"] == "static":
                return "static addressing not expected from a v4 import"
        if d["ap_auth"] != "auto" and not d["ap_password"]:
            return "ap_auth needs an ap_password"
        if booted and d["ap_password"] == convert.WM_AP_PASSWORD_DEFAULT:
            return "the access point still has the factory password"
    if comp == "vpn_manager":
        if convert.Converter.routes_error(d["routes"]):
            return "routes: bad list"
        if d["enabled"] and d["type"] == "wireguard":
            if not wg_key(d["private_key"]):
                return "private_key must be a 44-char base64 WireGuard key"
            if not wg_key(d["peer_public_key"]):
                return "peer_public_key must be a 44-char base64 WireGuard key"
            if d["preshared_key"] and not wg_key(d["preshared_key"]):
                return "preshared_key must be empty or a 44-char base64 key"
            if not convert.ENDPOINT_RE.match(d["endpoint"] or ""):
                return "endpoint must be a hostname or IP"
            if not d["address"]:
                return "address (local tunnel IP) is required"
    if comp == "mqtt_manager" and d["enabled"]:
        u = d["url"]
        host = u[7:] if u.startswith("mqtt://") else u[8:] if u.startswith("mqtts://") else None
        if not host or host[0] in ":/":
            return "enabled needs url"
    return None


def unredact(comp, data, stored):
    for k, v in data.items():
        if v == "" and convert.is_secret(comp, k) and stored.get(k):
            data[k] = stored[k]


def emulate_restore(doc, schema, device):
    """device: comp -> stored data. Returns {comp: error or None}."""
    res = {}
    for comp, entry in doc["components"].items():
        sc = schema["components"].get(comp)
        if sc is None:
            res[comp] = "skipped (unknown component)"
            continue
        if entry["version"] > sc["version"]:
            res[comp] = "backup newer than firmware"
            continue
        data = copy.deepcopy(entry["data"])
        stored = device.get(comp, defaults(sc))
        if doc.get("redacted"):
            unredact(comp, data, stored)
        merged = defaults(sc)
        merged.update(data)
        errs = check_schema(comp, sc, merged, None)
        res[comp] = "; ".join(errs) if errs else check_cross(comp, merged)
    return res


def configured_device(schema):
    dev = {c: defaults(s) for c, s in schema["components"].items()}
    dev["vpn_manager"]["private_key"] = SYN_DEVICE_PRIV
    dev["wifi_manager"]["ap_password"] = "SYNTHETIC-dev-ap"
    return dev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--v4-src", default=os.path.join(
        HERE, "src/v4/main/config_server.c"))
    ap.add_argument("--keep", help="keep captures/outputs in this dir")
    a = ap.parse_args()

    schema = json.load(open(os.path.join(HERE, "v6_schema.json")))
    work = a.keep or tempfile.mkdtemp(prefix="v4tov6-")
    failures = 0
    for name, fn in (("typical", scenario_typical), ("custom", scenario_custom),
                     ("sparse", scenario_sparse)):
        cdir = os.path.join(work, "capture-" + name)
        os.makedirs(cdir, exist_ok=True)
        for fname, content in fn(a.v4_src).items():
            with open(os.path.join(cdir, fname), "w") as f:
                if content not in (None, ""):
                    json.dump(content, f, indent=1)
        out = os.path.join(work, "v6-backup-%s.json" % name)
        rep = os.path.join(work, "report-%s.txt" % name)
        p = subprocess.run([sys.executable, "-I", os.path.join(HERE, "convert.py"),
                            cdir, "-o", out, "--report", rep],
                           capture_output=True, text=True)
        print("== scenario %s: convert.py exit %d" % (name, p.returncode))
        if p.returncode != 0:
            print(p.stdout, p.stderr)
            failures += 1
            continue
        doc = json.load(open(out))
        assert doc["wican_backup"] == 1 and isinstance(doc["components"], dict)
        for label, device in (("fresh v6 device", {}),
                              ("configured v6 device", configured_device(schema))):
            res = emulate_restore(doc, schema, device)
            bad = {c: e for c, e in res.items() if e}
            verdict = "ACCEPTED" if not bad else "REFUSED"
            print("   %-22s %s (%d components)%s" % (
                label, verdict, len(res),
                "".join("\n      %s: %s" % kv for kv in sorted(bad.items()))))
            if label.startswith("configured") and bad:
                failures += 1
    print("outputs in", work)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
