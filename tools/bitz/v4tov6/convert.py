#!/usr/bin/env python3
"""Convert a WiCAN PRO v4 (jtruland fork, v4.51) configuration capture into a
v6 settings backup (config_backup document version 1) that v6's restore
accepts.

    python3 -I convert.py <capture_dir> -o v6-backup.json
            [--device-id ID] [--wg-route 10.200.0.0/24]
            [--target bitz|upstream] [--ap-password PW] [--vpn-off]
            [--schema v6_schema.json] [--report report.txt]

<capture_dir> holds the raw JSON responses of the v4 device (any may be
missing or empty): load_config.json, load_auto_pid.json,
load_auto_pid_car_data.json, load_canflt.json, load_car_config.json,
vpn_load_config.json, api_webhook.json, cert_manager_sets.json,
check_status.json.

Rules
- Only keys with a confident v4 -> v6 meaning are emitted. Every value is
  type/range/enum/length-checked against v6_schema.json (extracted from the
  v6 source by extract_schema.py) and the cross-field rules the v6
  on_validate hooks enforce; anything that would fail is dropped with a
  reason, never emitted.
- A v6 restore is a FULL REPLACE per component: keys a component omits are
  filled from schema defaults (not from what the device holds). So a
  component is emitted only when something maps into it, and the report
  lists every key that will be reset to its default.
- Secrets are never invented. The document is marked "redacted": true, so a
  secret key emitted as "" keeps the value the v6 device already stores
  (config_backup cb_unredact). That is how the WireGuard private key (v4
  never exports it) and an unknown AP password are carried.
"""
import argparse
import datetime
import ipaddress
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

CAPTURE_FILES = [
    "load_config.json", "load_auto_pid.json", "load_auto_pid_car_data.json",
    "load_canflt.json", "load_car_config.json", "vpn_load_config.json",
    "api_webhook.json", "cert_manager_sets.json", "check_status.json",
]

# config_backup_codec.c: the redaction rule (secret keys)
SECRET_SUFFIXES = ("_password", "private_key", "preshared_key", "auth_key",
                   "_token", "api_key")
SECRET_EXTRA = {("mqtt_manager", "username"), ("espnetlink", "apn_user"),
                ("ha_webhooks", "url"), ("ha_webhooks", "url2"),
                ("data_destinations", "url"),
                ("data_destinations", "basic_username")}
V4_SECRET_KEYS = {"sta_pass", "ap_pass", "mqtt_pass", "mqtt_user", "ble_pass",
                  "home_password", "drive_password", "batt_alert_pass",
                  "batt_mqtt_pass", "batt_mqtt_user", "url", "urls",
                  "group_api_token", "api_token"}

WM_AP_PASSWORD_DEFAULT = "@meatpi#"     # wifi_manager_private.h
V4_FACTORY_STA = ("MeatPi", "TomatoSauce")  # v4 device_config_default placeholders
WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")  # vpn_check_wg_key
ENDPOINT_RE = re.compile(r"^[A-Za-z0-9.\-:_]{1,63}$")  # vpn_check_endpoint
VPN_MAX_ROUTES = 8


def is_secret(comp, key):
    return key.endswith(SECRET_SUFFIXES) or (comp, key) in SECRET_EXTRA


def mask(value):
    if isinstance(value, str):
        return '""' if value == "" else "***(%d chars)" % len(value)
    return "***"


def fmt_val(comp, key, value, secret=None):
    if secret if secret is not None else is_secret(comp, key):
        return mask(value)
    return json.dumps(value)


class Report:
    def __init__(self):
        self.mapped = []      # (v4 source, comp, key, shown value)
        self.dropped = []     # (v4 source, reason)
        self.notes = []       # review items
        self.errors = []

    def note(self, msg):
        self.notes.append(msg)

    def drop(self, src, reason):
        self.dropped.append((src, reason))


class Converter:
    def __init__(self, schema, args):
        self.schema = schema["components"]
        self.args = args
        self.out = {}         # comp -> {key: value}
        self.src = {}         # (comp, key) -> v4 source label
        self.r = Report()

    # ---- schema checks ------------------------------------------------------
    def field(self, comp, key):
        c = self.schema.get(comp)
        if c is None:
            return None
        for f in c["fields"]:
            if f["key"] == key:
                return f
        return None

    def check(self, comp, key, value):
        """None when the value passes the v6 schema, else the reason."""
        f = self.field(comp, key)
        if f is None:
            return "v6 schema has no %s.%s" % (comp, key)
        if f.get("fork_only") and self.args.target != "bitz":
            return "%s.%s exists only on the bitz fork (--target upstream)" % (comp, key)
        t = f["type"]
        if t == "bool":
            if not isinstance(value, bool):
                return "not a boolean"
        elif t == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return "not an integer"
            if not f["min"] <= value <= f["max"]:
                return "%d outside v6 range %d..%d" % (value, f["min"], f["max"])
        elif t == "string":
            if not isinstance(value, str):
                return "not a string"
            if "enum" in f and value not in f["enum"]:
                return "%r not one of %s" % (value, "|".join(f["enum"]))
            n = len(value.encode("utf-8"))
            # settings_manager_schema.c measures strlen (bytes)
            if f.get("maxlen") and n > f["maxlen"]:
                return "%d bytes > v6 maxLength %d" % (n, f["maxlen"])
            if f.get("minlen") and n < f["minlen"] and not (
                    value == "" and is_secret(comp, key)):
                return "%d bytes < v6 minLength %d" % (n, f["minlen"])
        elif t == "array":
            if not isinstance(value, list) or len(value) > f["max_items"]:
                return "not an array of at most %d items" % f["max_items"]
            for i, item in enumerate(value):
                if not isinstance(item, dict):
                    return "item %d is not an object" % i
                for sub in f["items"] or []:
                    if sub.get("required") and sub["key"] not in item:
                        return "item %d misses required %s" % (i, sub["key"])
        return None

    def put(self, src, comp, key, value, secret=None):
        why = self.check(comp, key, value)
        if why is not None:
            self.r.drop(src, "-> %s.%s: %s" % (comp, key, why))
            return False
        self.out.setdefault(comp, {})[key] = value
        self.src[(comp, key)] = src
        if value == "" and is_secret(comp, key):
            self.r.note("%s.%s is genuinely empty on v4 (%s), but the backup "
                        "is redacted: the v6 device's own stored %s, if any, "
                        "is kept instead of \"\"." % (comp, key, src, key))
        self.r.mapped.append((src, comp, key, fmt_val(comp, key, value, secret)))
        return True

    # ---- the mappings -------------------------------------------------------
    def run(self, cap):
        cfg = cap.get("load_config.json") or {}
        if not isinstance(cfg, dict) or not cfg:
            self.r.errors.append("load_config.json missing or empty: device "
                                 "settings cannot be converted")
            cfg = {}
        self.cfg = cfg
        self.vpn = cap.get("vpn_load_config.json") or {}
        self.wh = cap.get("api_webhook.json") or {}
        self.ap = cap.get("load_auto_pid.json") or {}
        self.status = cap.get("check_status.json") or {}

        self.map_wifi()
        self.map_vpn()
        self.map_sleep()
        self.map_ble()
        self.map_can()
        self.map_mqtt()
        self.map_webhook()
        self.map_autopid()
        self.map_ports()
        self.map_unmapped_v4_keys()
        self.map_autopid_tables(cap)
        self.cross_field()
        return self.out

    @staticmethod
    def en(v):
        """v4 "enable"/"disable" (or a bool) -> bool, None when neither."""
        if isinstance(v, bool):
            return v
        if v == "enable":
            return True
        if v == "disable":
            return False
        return None

    @staticmethod
    def as_int(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
            return int(v)
        return None

    @staticmethod
    def as_float(v):
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return x if math.isfinite(x) else None

    def map_wifi(self):
        c = self.cfg
        mode = {"AP": "ap", "APStation": "apsta", "Station": "sta"}
        wm = c.get("wifi_mode")
        if wm in mode:
            self.put("wifi_mode", "wifi_manager", "mode", mode[wm])
        elif wm is not None:
            self.r.drop("wifi_mode=%r" % wm, "no v6 equivalent (BLEStation = "
                        "station + BLE: set wifi_manager.mode and ble_manager "
                        "by hand)")

        ch = self.as_int(c.get("ap_ch"))
        if ch is not None:
            self.put("ap_ch", "wifi_manager", "ap_channel", ch)

        if self.en(c.get("ap_ssid_en")) and c.get("ap_ssid"):
            self.put("ap_ssid+ap_ssid_en", "wifi_manager", "ap_ssid", c["ap_ssid"])
        elif "ap_ssid_en" in c:
            self.r.drop("ap_ssid/ap_ssid_en", "custom AP SSID disabled on v4: "
                        "v6 default \"\" (= WiCAN_<device_id>) kept")

        ap_pass = c.get("ap_pass")
        if self.args.ap_password is not None:
            self.put("--ap-password", "wifi_manager", "ap_password",
                     self.args.ap_password)
        elif isinstance(ap_pass, str) and ap_pass and ap_pass != WM_AP_PASSWORD_DEFAULT:
            self.put("ap_pass", "wifi_manager", "ap_password", ap_pass)
        else:
            # the factory password is refused by a running v6 device
            # (wm_on_validate), and an omitted key would default to it: carry
            # the device's own value through the redacted-blank rule instead
            self.out.setdefault("wifi_manager", {})["ap_password"] = ""
            self.r.mapped.append(("(none)", "wifi_manager", "ap_password",
                                  '"" (redacted blank: keeps the v6 device\'s own)'))
            self.r.note("wifi_manager.ap_password: v4 had %s, so the backup "
                        "carries \"\" = keep the v6 device's stored AP "
                        "password. If that is still the factory '@meatpi#' "
                        "the WHOLE restore is refused ('the access point "
                        "still has the factory password'): set an AP "
                        "password on the v6 device first, or pass "
                        "--ap-password." % ("the factory password"
                                            if ap_pass == WM_AP_PASSWORD_DEFAULT
                                            else "no AP password"))

        ad = self.en(c.get("ap_auto_disable"))
        if ad is not None:
            self.put("ap_auto_disable", "wifi_manager", "ap_auto_disable", ad)

        ssid, pw = c.get("sta_ssid"), c.get("sta_pass")
        stations = []
        if isinstance(ssid, str) and ssid:
            if (ssid, pw) == V4_FACTORY_STA:
                self.r.drop("sta_ssid/sta_pass", "v4 factory placeholder "
                            "MeatPi/TomatoSauce, not a real network")
            else:
                if self.put("sta_ssid", "wifi_manager", "sta_ssid", ssid):
                    if isinstance(pw, str):
                        self.put("sta_pass", "wifi_manager", "sta_password", pw)
                    stations.append(("sta", ssid))
        if "sta_security" in c:
            self.r.drop("sta_security", "v6 negotiates the station's auth mode "
                        "itself (no setting)")

        fbs = c.get("sta_fallbacks")
        if isinstance(fbs, list):
            n = 0
            for i, fb in enumerate(fbs):
                if not isinstance(fb, dict) or not fb.get("ssid"):
                    continue
                if n >= 5:
                    self.r.drop("sta_fallbacks[%d]" % i, "v6 holds 5 fallbacks")
                    continue
                n += 1
                base = "fallback%d" % n
                if self.put("sta_fallbacks[%d].ssid" % i, "wifi_manager",
                            base + "_ssid", fb["ssid"]):
                    stations.append((base, fb["ssid"]))
                    if isinstance(fb.get("pass"), str):
                        self.put("sta_fallbacks[%d].pass" % i, "wifi_manager",
                                 base + "_password", fb["pass"])
                else:
                    n -= 1
        self.stations = stations

        for k in ("home_ssid", "home_password", "home_security", "home_protocol",
                  "drive_ssid", "drive_password", "drive_security",
                  "drive_protocol", "drive_connection_type",
                  "drive_mode_timeout"):
            if k in c:
                self.r.drop(k, "SmartConnect (home/drive profiles) has no v6 "
                            "equivalent")

    def map_vpn(self):
        v = self.vpn
        if not isinstance(v, dict) or not v:
            self.r.note("vpn_load_config.json missing: vpn_manager not emitted")
            return
        if v.get("success") is False:
            self.r.drop("vpn_load_config", "v4 reported no VPN config")
            return
        # home bypass -> per-network "Pause VPN" (wifi_manager, issue #976)
        if v.get("home_bypass_enabled") is True:
            homes = {s.strip() for s in str(v.get("home_ssids", "")).split(",")
                     if s.strip()}
            hit = set()
            for base, ssid in self.stations:
                if ssid in homes:
                    self.put("vpn.home_ssids", "wifi_manager",
                             base + "_vpn_pause", True)
                    hit.add(ssid)
            for s in sorted(homes - hit):
                self.r.drop("vpn.home_ssids:%s" % s, "not one of the station "
                            "networks carried over; set its 'Pause VPN' by hand")
        wg = v.get("wireguard")
        if v.get("vpn_type") not in (1, None) or not isinstance(wg, dict):
            self.r.drop("vpn_type=%r" % v.get("vpn_type"),
                        "no WireGuard config in the capture")
            return

        C = "vpn_manager"
        self.put("vpn_type", C, "type", "wireguard")
        pk = wg.get("peer_public_key", "")
        if isinstance(pk, str) and WG_KEY_RE.match(pk):
            self.put("wireguard.peer_public_key", C, "peer_public_key", pk,
                     secret=False)
        else:
            self.r.drop("wireguard.peer_public_key", "not a 44-char base64 key")
        # never exported by v4 (vpn_load_config_handler): device keeps its own
        self.out.setdefault(C, {})["private_key"] = ""
        self.r.mapped.append(("(not exported by v4)", C, "private_key",
                              '"" (redacted blank: keeps the v6 device\'s own)'))
        if wg.get("preshared_key_present"):
            self.out[C]["preshared_key"] = ""
            self.r.mapped.append(("wireguard.preshared_key_present", C,
                                  "preshared_key",
                                  '"" (redacted blank: keeps the v6 device\'s own)'))
            self.r.note("vpn_manager.preshared_key: v4 has a PSK but never "
                        "exports it; re-enter it on v6 unless the device "
                        "already holds it.")
        addr = wg.get("address", "")
        if isinstance(addr, str) and addr:
            try:
                ipaddress.ip_interface(addr)
                self.put("wireguard.address", C, "address", addr)
            except ValueError:
                self.r.drop("wireguard.address", "%r is not an IPv4 address" % addr)
        ep = wg.get("endpoint", "")
        if isinstance(ep, str) and ep:
            host, _, port = ep.rpartition(":")
            if not host:
                host, port = ep, "51820"
            if ENDPOINT_RE.match(host):
                self.put("wireguard.endpoint", C, "endpoint", host)
            else:
                self.r.drop("wireguard.endpoint", "host %r fails vpn_check_endpoint" % host)
            p = self.as_int(port)
            if p is not None:
                self.put("wireguard.endpoint(port)", C, "port", p)
        ka = wg.get("persistent_keepalive")
        if isinstance(ka, int) and not isinstance(ka, bool):
            self.put("wireguard.persistent_keepalive", C, "keepalive_s", ka)
        dns = wg.get("dns_main")
        if isinstance(dns, str) and dns:
            try:
                ipaddress.IPv4Address(dns)
                self.put("wireguard.dns_main", C, "dns", dns)
            except ValueError:
                self.r.drop("wireguard.dns_main", "%r is not an IPv4 address" % dns)
        if wg.get("dns_backup"):
            self.r.drop("wireguard.dns_backup", "v6 has one DNS override")

        # routes: from --wg-route, never derived (issue #975 semantics differ)
        routes = self.args.wg_route
        bad = self.routes_error(routes)
        if bad:
            self.r.errors.append("--wg-route %r: %s" % (routes, bad))
        else:
            self.put("--wg-route", C, "routes", routes)
        cap_routes = wg.get("allowed_ips")
        if cap_routes and str(cap_routes).replace(" ", "") != routes.replace(" ", ""):
            self.r.note("vpn_manager.routes = %r (from --wg-route); the v4 "
                        "capture's allowed_ips was %r" % (routes, cap_routes))

        enabled = v.get("enabled") is True and not self.args.vpn_off
        self.put("vpn.enabled", C, "enabled", enabled)
        if enabled:
            self.r.note("vpn_manager.enabled = true (as on v4) with "
                        "private_key \"\": the restore is REFUSED unless the "
                        "v6 device already holds a WireGuard private key "
                        "(vpn_check_config). Generate one on v6 first "
                        "(keygen), add its PUBLIC key as the peer on the "
                        "server (it replaces the v4 key), or convert with "
                        "--vpn-off and enable later.")

    @staticmethod
    def routes_error(routes):
        toks = [t for t in re.split(r"[,\s;]+", routes or "") if t]
        if len(toks) > VPN_MAX_ROUTES:
            return "more than %d routes" % VPN_MAX_ROUTES
        for t in toks:
            if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?", t):
                return "%r is not a network in CIDR form" % t
            try:
                ipaddress.ip_network(t, strict=False)
            except ValueError as e:
                return str(e)
        return None

    def map_sleep(self):
        c = self.cfg
        C = "sleep_manager"
        st, agree = c.get("sleep_status"), c.get("sleep_disable_agree", "no")
        if st in ("enable", "disable"):
            # config_server_get_sleep_config(): off only when disabled AND agreed
            self.put("sleep_status+sleep_disable_agree", C, "enabled",
                     not (st == "disable" and agree == "yes"))
        sv = self.as_float(c.get("sleep_volt"))
        if sv is not None:
            if 12.0 <= sv <= 15.0:
                mv = int(round(sv * 1000))
                if self.put("sleep_volt", C, "sleep_mv", mv):
                    # v4 sleep_mode.c: wakeup_voltage = sleep_voltage + 0.1
                    # (the wakeup_volt setting is read but unused)
                    self.put("sleep_volt(+0.1 V, v4 rule)", C, "wake_mv", mv + 100)
            else:
                self.r.drop("sleep_volt=%r" % c.get("sleep_volt"),
                            "v4 ignored it (outside 12..15 V, used 13.1)")
        if "wakeup_volt" in c:
            self.r.drop("wakeup_volt", "unused by v4 (wake = sleep + 0.1 V); "
                        "wake_mv derived from sleep_volt instead")
        t = self.as_int(c.get("sleep_time"))
        if t is not None:
            if 1 <= t <= 30:
                self.put("sleep_time", C, "sleep_delay_min", t)
            else:
                self.r.drop("sleep_time", "v4 ignored it (outside 1..30 min)")
        pw = self.en(c.get("periodic_wakeup"))
        if pw is not None:
            self.put("periodic_wakeup", C, "periodic_wakeup", pw)
        wi = self.as_int(c.get("wakeup_interval"))
        if wi is not None:
            if 5 <= wi <= 240:
                self.put("wakeup_interval", C, "wakeup_interval_min", wi)
            else:
                self.r.drop("wakeup_interval", "v4 ignored it (outside 5..240 min)")
        th = self.as_int(c.get("imu_threshold"))
        if self.args.motion_mg is not None:
            if self.field(C, "motion_mg") is None or self.args.target != "bitz":
                self.r.drop("--motion-mg", "target firmware has no "
                            "sleep_manager.motion_mg")
            elif self.put("--motion-mg", C, "motion_mg", self.args.motion_mg):
                self.r.note("sleep_manager.motion_mg = %d mg from --motion-mg "
                            "(overrides v4 imu_threshold %s)" % (
                                self.args.motion_mg, th))
        elif th is not None:
            if self.field(C, "motion_mg") is None or self.args.target != "bitz":
                self.r.drop("imu_threshold", "target firmware has no "
                            "sleep_manager.motion_mg")
            else:
                mg = int(round(th * 3.9))   # ICM-42670 WoM: 1 LSB = 3.9 mg
                if self.put("imu_threshold (x3.9 mg)", C, "motion_mg", mg):
                    self.r.note("sleep_manager.motion_mg = %d mg from v4 "
                                "imu_threshold %d. Restores only on the bitz "
                                "fork (upstream ignores the unknown key). The "
                                "fork's default 125 mg assumed a v4 threshold "
                                "of 32: %d is %s sensitive." % (
                                    mg, th, th, "more" if mg < 125 else "less"))
        self.r.note("sleep_manager.motion_wake not emitted: stays at the "
                    "target's default (fork: true).")

    def map_ble(self):
        c = self.cfg
        C = "ble_manager"
        b = self.en(c.get("ble_status"))
        if b is not None:
            self.put("ble_status", C, "enabled", b)
        pk = c.get("ble_pass")
        if isinstance(pk, str) and re.fullmatch(r"\d{1,6}", pk):
            self.put("ble_pass", C, "passkey", int(pk), secret=True)
        elif pk is not None:
            self.r.drop("ble_pass", "not a 0..999999 numeric passkey")
        p = self.as_int(c.get("ble_power"))
        if p is not None:
            self.put("ble_power", C, "tx_power_dbm", p)

    def map_can(self):
        c = self.cfg
        C = "can_manager"
        rate = c.get("can_datarate")
        if isinstance(rate, str):
            m = re.fullmatch(r"(\d+)[kK]", rate)
            if m:
                self.put("can_datarate", C, "baud", m.group(1))
            else:
                self.r.drop("can_datarate", "unrecognised %r" % rate)
        mode = c.get("can_mode")
        if mode in ("normal", "silent"):
            self.put("can_mode", C, "silent", mode == "silent")
        if C in self.out:
            self.r.note("can_manager.enabled left at default false: on v6 it "
                        "is the raw TWAI bus (SLCAN/GVRET/RealDash bridges), "
                        "not the OBD path; enable it if you used those.")

    def map_mqtt(self):
        c = self.cfg
        C = "mqtt_manager"
        en = self.en(c.get("mqtt_en"))
        url = c.get("mqtt_url")
        if en is None and not url:
            return
        built = None
        if isinstance(url, str) and url:
            m = re.fullmatch(r"(?i)(mqtts?)://([^/:]+)(?::(\d+))?/?", url.strip())
            if m:
                scheme = "mqtts" if (c.get("mqtt_security") == "tls" or
                                     m.group(1).lower() == "mqtts") else "mqtt"
                port = self.as_int(c.get("mqtt_port")) or self.as_int(m.group(3))
                built = "%s://%s" % (scheme, m.group(2))
                if port and not (1 <= port <= 65535):
                    self.r.drop("mqtt_port", "outside 1..65535")
                elif port:
                    built += ":%d" % port
                self.put("mqtt_url+mqtt_port+mqtt_security", C, "url", built)
            else:
                self.r.drop("mqtt_url", "%r is not mqtt[s]://host[:port]" % url)
        if en is not None:
            if en and built is None:
                self.r.drop("mqtt_en", "enabled needs a valid url (v6 "
                            "on_validate): left disabled")
            else:
                self.put("mqtt_en", C, "enabled", en)
        if isinstance(c.get("mqtt_user"), str) and c["mqtt_user"]:
            self.put("mqtt_user", C, "username", c["mqtt_user"])
        if isinstance(c.get("mqtt_pass"), str) and c["mqtt_pass"]:
            self.put("mqtt_pass", C, "broker_password", c["mqtt_pass"])
        cs = c.get("mqtt_cert_set")
        if c.get("mqtt_security") == "tls" and isinstance(cs, str) and cs not in ("", "default"):
            if self.put("mqtt_cert_set", C, "cert_set", cs):
                self.r.note("mqtt_manager.cert_set = %r: the certificate set "
                            "itself is not in this backup; recreate it in "
                            "v6's cert manager before the broker connects." % cs)
        if self.en(c.get("mqtt_skip_cn")):
            self.r.drop("mqtt_skip_cn", "v6 always checks the certificate "
                        "name (no setting): fix the broker certificate")

        # topics: v4 tx/rx/status are full topics; v6 has one prefix
        tx, rx, stt = (c.get("mqtt_tx_topic"), c.get("mqtt_rx_topic"),
                       c.get("mqtt_status_topic"))
        prefix = None
        if all(isinstance(t, str) for t in (tx, rx, stt)):
            m = re.fullmatch(r"(.+)/can/tx", tx)
            if m and rx == m.group(1) + "/can/rx" and stt == m.group(1) + "/can/status":
                prefix = m.group(1)
        dev = self.device_id
        if prefix is not None:
            if dev and prefix == "wican/" + dev:
                self.r.drop("mqtt_*_topic", "v4 default layout wican/<id>: v6 "
                            "default topic_prefix \"\" gives the same")
            else:
                self.put("mqtt_tx/rx/status_topic", C, "topic_prefix", prefix)
        elif tx or rx or stt:
            self.r.drop("mqtt_*_topic", "not the <prefix>/can/{tx,rx,status} "
                        "layout v6 builds on one prefix; set topic_prefix and "
                        "mqtt_can topics by hand")
        if "mqtt_elm327_log" in c or "elm327_udp_log" in c:
            self.r.drop("mqtt_elm327_log/elm327_udp_log", "debug taps, no v6 "
                        "setting")

        # CAN over MQTT (v4 mqtt.c: tx_en subscribes the tx topic, rx_en
        # publishes bus frames) -> mqtt_can gates
        txe, rxe = self.en(c.get("mqtt_tx_en")), self.en(c.get("mqtt_rx_en"))
        if txe is not None or rxe is not None:
            M = "mqtt_can"
            on = bool(en) and bool(txe or rxe)
            self.put("mqtt_en+mqtt_tx_en/mqtt_rx_en", M, "enabled", on)
            if txe is not None:
                self.put("mqtt_tx_en", M, "allow_tx", txe)
            if rxe is not None:
                self.put("mqtt_rx_en", M, "allow_rx", rxe)
            if on:
                self.r.note("mqtt_can enabled from v4 mqtt_tx_en/rx_en: the "
                            "v6 frame JSON is not verified to match v4's "
                            "{bus,type,ts,frame[]} format; check consumers.")

    def map_webhook(self):
        w = self.wh
        C = "ha_webhooks"
        if not isinstance(w, dict) or not w:
            return
        urls = [u for u in (w.get("urls") or []) if isinstance(u, str) and u]
        if not urls and isinstance(w.get("url"), str) and w["url"]:
            urls = [w["url"]]
        if urls:
            self.put("api_webhook.urls[0]", C, "url", urls[0])
        if len(urls) > 1:
            self.put("api_webhook.urls[1]", C, "url2", urls[1])
        master = self.en(self.cfg.get("webhook_en"))
        en = w.get("enabled")
        if isinstance(en, bool):
            on = en and master is not False and bool(urls)
            self.put("api_webhook.enabled+webhook_en", C, "enabled", on)
        iv = w.get("interval")
        if isinstance(iv, int) and not isinstance(iv, bool) and iv > 0:
            self.put("api_webhook.interval (s)", C, "interval_s", iv)
        mo = w.get("manual_override")
        if isinstance(mo, bool):
            self.put("api_webhook.manual_override", C, "manual_override", mo)
        dm = self.ap.get("webhook_data_mode") if isinstance(self.ap, dict) else None
        if isinstance(dm, str) and dm:
            self.put("auto_pid.webhook_data_mode", C, "data_mode", dm)
        self.map_webhook_destination(urls)

    def map_webhook_destination(self, wh_urls):
        """v4 can reach HA through an AutoPID HTTP(S) *destination* instead of
        its own webhook (the Sierra did: webhook_en off, posts via a
        destination with an api_key_query auth that a Cloudflare WAF rule
        expects). v6 has no such destination type for HA, so carry it into
        ha_webhooks: url = destination + ?<name>=<key>, enabled, the
        destination's cert_set, and manual_override so ha-wican v3's own URL
        push (which has no key) cannot replace it."""
        C = "ha_webhooks"
        dests = self.ap.get("destinations") if isinstance(self.ap, dict) else None
        dest = None
        for d in dests if isinstance(dests, list) else []:
            if (isinstance(d, dict) and d.get("enabled") is True
                    and str(d.get("type", "")).upper() in ("HTTP", "HTTPS")
                    and "/api/webhook/" in str(d.get("destination", ""))):
                dest = d
                break
        if dest is None:
            return
        url = dest["destination"]
        auth = dest.get("auth") if isinstance(dest.get("auth"), dict) else {}
        if auth.get("type") == "api_key_query":
            name, key = auth.get("api_key_query_name"), auth.get("api_key")
            if not (isinstance(name, str) and name and isinstance(key, str) and key):
                self.r.drop("auto_pid.destinations[HA].auth",
                            "api_key_query without a name/key: not carried")
                return
            url += ("&" if "?" in url else "?") + "%s=%s" % (name, key)
        elif auth.get("type") not in (None, "", "none"):
            self.r.drop("auto_pid.destinations[HA].auth",
                        "auth type %r has no ha_webhooks equivalent: the "
                        "destination is not carried" % auth.get("type"))
            return
        src = "auto_pid.destinations[HA]"
        if not self.put(src, C, "url", url):
            return
        # the second URL: the v4 webhook URL on a different host (e.g. the
        # LAN address), so the device still has a path if the first fails
        host = url.split("/")[2] if url.count("/") >= 2 else ""
        alt = [u for u in wh_urls if u.split("/")[2:3] != [host]]
        if alt:
            self.put("api_webhook.urls (other host)", C, "url2", alt[0])
        elif ("ha_webhooks", "url2") in self.src:
            self.out[C].pop("url2", None)
            self.src.pop((C, "url2"), None)
        self.put(src + ".enabled", C, "enabled", True)
        self.put(src, C, "manual_override", True)
        cs = dest.get("cert_set")
        if isinstance(cs, str) and cs:
            self.put(src + ".cert_set", C, "cert_set", cs)
            self.r.note("ha_webhooks.cert_set = %r: certificate CONTENTS are not "
                        "exported by v4; upload that set on v6 before the "
                        "webhook can verify TLS." % cs)
        self.r.note("ha_webhooks carries the AutoPID HA destination (v4 posted "
                    "to HA through it, not through its webhook); "
                    "manual_override on so ha-wican v3's keyless URL push "
                    "cannot replace it.")

    def map_autopid(self):
        c, a = self.cfg, self.ap if isinstance(self.ap, dict) else {}
        C = "autopid"
        proto = c.get("protocol")
        if proto == "auto_pid":
            self.put("protocol=auto_pid", C, "enabled", True)
        std = self.en(a.get("standard_pids"))
        if std is not None:
            self.put("auto_pid.standard_pids", C, "std_enabled", std)
        spec = self.en(a.get("car_specific"))
        if spec is not None:
            self.put("auto_pid.car_specific", C, "specific_enabled", spec)
        cm = a.get("car_model")
        if isinstance(cm, str) and cm:
            self.put("auto_pid.car_model", C, "vehicle", cm)
        d = a.get("disable_on_sleep_voltage")
        if d is not None:
            if d in ("disable", False):
                self.put("auto_pid.disable_on_sleep_voltage", C,
                         "pause_follow_sleep", False)
            elif d in ("enable", True):
                self.put("auto_pid.disable_on_sleep_voltage", C, "pause_mode", "all")
                self.put("auto_pid.disable_on_sleep_voltage", C,
                         "pause_follow_sleep", True)
            elif d == "disable_pid_requests":
                self.put("auto_pid.disable_on_sleep_voltage", C,
                         "pause_mode", "requests_only")
                self.put("auto_pid.disable_on_sleep_voltage", C,
                         "pause_follow_sleep", True)
            elif d == "automate_threshold":
                v = self.as_float(a.get("pid_polling_min_voltage", 13.1))
                if v is not None and 9.0 <= v <= 18.0:
                    self.put("auto_pid.disable_on_sleep_voltage+"
                             "pid_polling_min_voltage", C, "pause_below_mv",
                             int(round(v * 1000)))
                    self.put("auto_pid.disable_on_sleep_voltage", C,
                             "pause_mode", "requests_only")
            else:
                self.r.drop("auto_pid.disable_on_sleep_voltage",
                            "unknown value %r" % d)
        for k in ("initialisation", "ecu_protocol", "cycle", "grouping",
                  "ha_discovery", "pid_validation", "destinations",
                  "destination", "group_dest_type", "group_api_token"):
            if k in a:
                self.r.drop("auto_pid.%s" % k, {
                    "initialisation": "init-string syntax not verified "
                                      "against v6 custom_init; re-enter it",
                    "ecu_protocol": "v4 value set not verified against v6 "
                                    "std_protocol 0|6|7|8|9",
                    "destinations": "v4 destinations -> v6 data_destinations "
                                    "array not mapped (types/auth differ)",
                }.get(k, "no confident v6 setting"))

    def map_ports(self):
        c = self.cfg
        proto, ptype = c.get("protocol"), c.get("port_type")
        port = self.as_int(c.get("port"))
        if proto in ("slcan", "savvycan", "realdash66"):
            self.r.drop("protocol=%s" % proto, "v6 wires protocols as "
                        "socket_manager servers + bridge_manager translators; "
                        "set the bridge up by hand")
            return
        if proto not in ("elm327", "auto_pid") or port is None:
            return
        if (ptype, port) == ("tcp", 35000):
            self.r.drop("port_type/port", "tcp 35000 = v6 default obd0 server: "
                        "nothing to emit")
            return
        f = self.field("socket_manager", "servers")
        servers = [dict(s) for s in f["default"]]
        for s in servers:
            if s["name"] == "obd0":
                s["proto"], s["port"] = ptype, port
        clash = [s["name"] for s in servers if s["name"] != "obd0"
                 and (s["proto"], s["port"]) == (ptype, port)]
        if ptype not in ("tcp", "udp") or not 1 <= port <= 65535 or clash:
            self.r.drop("port_type/port", "%s %s not usable for obd0%s" % (
                ptype, port, " (clashes with %s)" % clash if clash else ""))
            return
        self.put("port_type+port", "socket_manager", "servers", servers)

    def map_unmapped_v4_keys(self):
        handled = {
            "wifi_mode", "ap_ch", "ap_ssid_en", "ap_ssid", "ap_pass",
            "ap_auto_disable", "sta_ssid", "sta_pass", "sta_security",
            "sta_fallbacks", "home_ssid", "home_password", "home_security",
            "home_protocol", "drive_ssid", "drive_password", "drive_security",
            "drive_protocol", "drive_connection_type", "drive_mode_timeout",
            "sleep_status", "sleep_disable_agree", "sleep_volt", "wakeup_volt",
            "sleep_time", "periodic_wakeup", "wakeup_interval", "imu_threshold",
            "ble_status", "ble_pass", "ble_power", "can_datarate", "can_mode",
            "mqtt_en", "mqtt_url", "mqtt_port", "mqtt_user", "mqtt_pass",
            "mqtt_security", "mqtt_cert_set", "mqtt_skip_cn", "mqtt_tx_topic",
            "mqtt_rx_topic", "mqtt_status_topic", "mqtt_elm327_log",
            "elm327_udp_log", "mqtt_tx_en", "mqtt_rx_en", "webhook_en",
            "protocol", "port_type", "port",
        }
        reasons = {
            "batt_alert": "battery alert was force-disabled in v4; no v6 setting",
            "logger_status": "v4 SD logger -> v6 data_logger not mapped "
                             "(formats/periods differ)",
            "debug": "debug flag, no v6 setting",
        }
        for k in sorted(self.cfg):
            if k in handled:
                continue
            r = next((v for p, v in reasons.items() if k.startswith(p)), None)
            if r is None and k.startswith(("batt_", "log_")):
                r = reasons["batt_alert" if k.startswith("batt_") else "logger_status"]
            self.r.drop(k, r or "no v6 equivalent known to this converter")

    def map_autopid_tables(self, cap):
        n = []
        a = cap.get("load_auto_pid.json")
        if isinstance(a, dict):
            pids = a.get("pids")
            n.append("%d custom PIDs" % (len(pids) if isinstance(pids, list) else 0))
            if isinstance(a.get("can_filters"), list):
                n.append("%d CAN filters" % len(a["can_filters"]))
        cd = cap.get("load_auto_pid_car_data.json")
        if isinstance(cd, dict) and cd:
            n.append("car_data present")
        if n:
            self.r.drop("load_auto_pid*.json tables (%s)" % ", ".join(n),
                        "PID tables are files, not settings: not in a v1 "
                        "settings backup (see AutoPID verdict)")
        cs = cap.get("cert_manager_sets.json")
        if cs:
            self.r.drop("cert_manager_sets", "certificate contents are not "
                        "exported by v4 /cert_manager/sets; re-upload on v6")

    # ---- cross-field rules v6 on_validate enforces ---------------------------
    def cross_field(self):
        w = self.out.get("wifi_manager")
        if w is not None:
            if w.get("sta_password") and not w.get("sta_ssid"):
                w.pop("sta_password")
                self.r.drop("sta_pass", "sta_password without sta_ssid "
                            "(wm_on_validate)")
            if w.get("ap_password") == WM_AP_PASSWORD_DEFAULT:
                w["ap_password"] = ""
                self.r.drop("ap_password", "factory password is refused by a "
                            "running v6 device")
        v = self.out.get("vpn_manager")
        if v is not None and v.get("enabled"):
            for k in ("peer_public_key", "endpoint", "address"):
                if not v.get(k):
                    v["enabled"] = False
                    self.r.drop("vpn.enabled", "no valid %s: tunnel left "
                                "disabled (vpn_check_config)" % k)
                    break

    # ---- report -------------------------------------------------------------
    def defaults_left(self):
        out = {}
        for comp, data in self.out.items():
            left = []
            for f in self.schema[comp]["fields"]:
                if f["key"] in data:
                    continue
                if f.get("fork_only") and self.args.target != "bitz":
                    continue
                left.append("%s=%s" % (f["key"], fmt_val(comp, f["key"], f["default"])
                                       if f["type"] != "array" else
                                       "[%d items]" % len(f["default"])))
            out[comp] = left
        return out


def load_capture(d, r):
    cap = {}
    for fn in CAPTURE_FILES:
        p = os.path.join(d, fn)
        if not os.path.exists(p):
            r.notes.append("capture: %s missing" % fn)
            continue
        raw = open(p, "rb").read().decode("utf-8", errors="replace").strip()
        if not raw:
            r.notes.append("capture: %s empty" % fn)
            continue
        try:
            cap[fn] = json.loads(raw)
        except json.JSONDecodeError as e:
            r.notes.append("capture: %s is not JSON (%s)" % (fn, e))
    return cap


def build(args):
    schema = json.load(open(args.schema))
    conv = Converter(schema, args)
    cap = load_capture(args.capture_dir, conv.r)
    st = cap.get("check_status.json") or {}
    conv.device_id = args.device_id or (st.get("device_id") if isinstance(st, dict) else None)
    if not conv.device_id:
        conv.r.note("device_id unknown (no --device-id, no check_status.json): "
                    "header says 'v4-import'")
    out = conv.run(cap)

    # final gate: every emitted value re-checked against the schema
    for comp, data in out.items():
        sc = schema["components"].get(comp)
        if sc is None:
            raise SystemExit("internal: unknown component %s" % comp)
        for k, v in data.items():
            why = conv.check(comp, k, v)
            if why and not (v == "" and is_secret(comp, k)):
                raise SystemExit("internal: %s.%s fails schema: %s" % (comp, k, why))

    fw = st.get("fw_version") if isinstance(st, dict) else None
    doc = {
        "wican_backup": 1,
        "device_id": conv.device_id or "v4-import",
        "fw_version": "converted-from-v4" + (":%s" % fw if fw else ""),
        "created": datetime.datetime.now(datetime.timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%SZ"),
        # a blank secret = keep what the v6 device stores (cb_unredact)
        "redacted": True,
        "components": {
            comp: {"version": schema["components"][comp]["version"], "data": data}
            for comp, data in sorted(out.items())
        },
    }
    return doc, conv


def render_report(conv, args):
    r = conv.r
    L = []
    if r.errors:
        L.append("ERRORS")
        L += ["  " + e for e in r.errors]
    L.append("MAPPED (v4 -> v6 component.key = value)")
    for src, comp, key, val in r.mapped:
        L.append("  %-44s -> %s.%s = %s" % (src, comp, key, val))
    L.append("DROPPED")
    for src, why in r.dropped:
        L.append("  %-44s : %s" % (src, why))
    L.append("REVIEW")
    for n in r.notes:
        L.append("  - " + n)
    L.append("RESET TO v6 DEFAULT on restore (emitted components are replaced whole)")
    for comp, left in sorted(conv.defaults_left().items()):
        L.append("  %s (%d keys): %s" % (comp, len(left), ", ".join(left)))
    L.append("NOT IN THE BACKUP (device keeps its own): " + ", ".join(
        sorted(set(conv.schema) - set(conv.out))))
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture_dir")
    ap.add_argument("-o", "--out", default="v6-backup.json")
    ap.add_argument("--device-id")
    ap.add_argument("--wg-route", default="10.200.0.0/24")
    ap.add_argument("--target", choices=("bitz", "upstream"), default="bitz",
                    help="bitz = our fork (sleep_manager.motion_mg exists)")
    ap.add_argument("--ap-password", help="set the v6 AP password (8..64)")
    ap.add_argument("--motion-mg", type=int,
                    help="bitz only: set sleep_manager.motion_mg instead of "
                         "deriving it from v4 imu_threshold x 3.9")
    ap.add_argument("--vpn-off", action="store_true",
                    help="emit the tunnel disabled whatever v4 had")
    ap.add_argument("--schema", default=os.path.join(HERE, "v6_schema.json"))
    ap.add_argument("--report", help="also write the report to this file")
    args = ap.parse_args(argv)

    doc, conv = build(args)
    if conv.r.errors:
        sys.stdout.write(render_report(conv, args))
        print("refusing to write %s: fix the errors above" % args.out, file=sys.stderr)
        return 2
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    rep = render_report(conv, args)
    sys.stdout.write(rep)
    if args.report:
        open(args.report, "w").write(rep)
    print("wrote %s: %d components, %d keys" % (
        args.out, len(doc["components"]),
        sum(len(c["data"]) for c in doc["components"].values())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
