#!/usr/bin/env python3
"""Extract the v6 settings schemas from the meatpi component sources.

Reads every components/*/*_settings.c, expands the SETTINGS_* row macros of
settings_manager.h, resolves the #define constants they use (searched in the
component's own sources first, then every component), and reads the
settings_descriptor_t initialiser (.name, .version, .fields, .defaults_json).

    python3 -I extract_schema.py <v6_root> [--fork <fork_root>] -o v6_schema.json

<v6_root> is a checkout/export of the components repo (it holds components/).
--fork adds the fields of a second tree (our bitz fork) that upstream lacks,
marked "fork_only": true, and records the fork's descriptor version.

A source export is easiest made with:
    git -C <meatpi> archive --prefix=v6/ c51c3e2 | tar x
"""
import argparse
import json
import os
import re
import sys

ROW_MACROS = {
    # name: (type, arg names)
    "SETTINGS_STR": ("string", ["key", "maxlen", "default"]),
    "SETTINGS_STR_LEN": ("string", ["key", "minlen", "maxlen", "default"]),
    "SETTINGS_STR_ENUM": ("string", ["key", "enum", "default"]),
    "SETTINGS_STR_FILE": ("string", ["key", "maxlen", "default"]),
    "SETTINGS_INT": ("int", ["key", "min", "max", "default"]),
    "SETTINGS_BOOL": ("bool", ["key", "default"]),
    "SETTINGS_STR_REQ": ("string", ["key", "minlen", "maxlen", "default"]),
    "SETTINGS_STR_ENUM_REQ": ("string", ["key", "enum", "default"]),
    "SETTINGS_INT_REQ": ("int", ["key", "min", "max", "default"]),
    "SETTINGS_ARRAY": ("array", ["key", "max_items", "items", "default"]),
    "SETTINGS_ARRAY_ANY": ("array", ["key", "max_items", "default"]),
}


def strip_comments(src):
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])
            i = j + 1
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" " + "\n" * src.count("\n", i, j))
            i = j
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def match_close(text, i, open_ch, close_ch):
    """text[i] == open_ch; return index of the matching close_ch."""
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced %s" % open_ch)


def split_args(text):
    args, depth, cur, i, n = [], 0, [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            cur.append(text[i:j + 1])
            i = j + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        args.append("".join(cur).strip())
    return args


def c_string(text):
    """Concatenate adjacent C string literals; None for NULL."""
    text = text.strip()
    if text == "NULL":
        return None
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    if not parts:
        raise ValueError("not a string literal: %r" % text)
    s = "".join(parts)
    return bytes(s, "utf-8").decode("unicode_escape")


class Defines:
    def __init__(self, root):
        self.by_comp = {}
        cdir = os.path.join(root, "components")
        for comp in sorted(os.listdir(cdir)):
            d = {}
            for dp, _, fns in os.walk(os.path.join(cdir, comp)):
                if "host_test" in dp or "test_apps" in dp:
                    continue
                for fn in fns:
                    if fn.endswith((".h", ".c")):
                        self._scan(os.path.join(dp, fn), d)
            self.by_comp[comp] = d

    @staticmethod
    def _scan(path, d):
        try:
            src = strip_comments(open(path, encoding="utf-8", errors="replace").read())
        except OSError:
            return
        src = re.sub(r"\\\n", " ", src)
        for m in re.finditer(r"^\s*#\s*define\s+([A-Za-z_]\w*)(?!\()\s+(.+?)\s*$", src, re.M):
            d.setdefault(m.group(1), m.group(2))

    def lookup(self, comp, name):
        if name in self.by_comp.get(comp, {}):
            return self.by_comp[comp][name]
        for c, d in self.by_comp.items():
            if name in d:
                return d[name]
        return None

    def eval_int(self, comp, expr, depth=0):
        if depth > 20:
            raise ValueError("define recursion: %s" % expr)
        e = expr.strip()

        def sub(m):
            v = self.lookup(comp, m.group(0))
            if v is None:
                raise ValueError("unresolved identifier %s in %r" % (m.group(0), expr))
            return "(%d)" % self.eval_int(comp, v, depth + 1)

        e = re.sub(r"\(\s*(?:u?int\d+_t|unsigned|int|long|size_t)\s*\)", "", e)
        e = re.sub(r"\b(0x[0-9a-fA-F]+|\d+)[uUlL]+\b", r"\1", e)
        e = re.sub(r"\b[A-Za-z_]\w*\b", sub, e)
        if not re.fullmatch(r"[\d\s()+\-*/%<>|&x a-fA-F]*", e):
            raise ValueError("cannot evaluate %r" % expr)
        return int(eval(e.replace("/", "//"), {"__builtins__": {}}))

    def eval_str(self, comp, expr, depth=0):
        """A string row argument: literal(s), NULL, or a #define naming one
        (fw_update's FWU_DEFAULT_BASE_URL), possibly mixed with literals."""
        if depth > 20:
            raise ValueError("define recursion: %s" % expr)
        e = expr.strip()
        if e == "NULL":
            return None
        parts = []
        i, n = 0, len(e)
        while i < n:
            c = e[i]
            if c.isspace():
                i += 1
            elif c == '"':
                j = i + 1
                while j < n and e[j] != '"':
                    j += 2 if e[j] == "\\" else 1
                parts.append(c_string(e[i:j + 1]))
                i = j + 1
            else:
                m = re.match(r"[A-Za-z_]\w*", e[i:])
                if not m:
                    raise ValueError("cannot evaluate string %r" % expr)
                v = self.lookup(comp, m.group(0))
                if v is None:
                    raise ValueError("unresolved identifier %s in %r" % (m.group(0), expr))
                s = self.eval_str(comp, v, depth + 1)
                if s is None:
                    raise ValueError("%s is NULL inside %r" % (m.group(0), expr))
                parts.append(s)
                i += m.end()
        if not parts:
            raise ValueError("not a string expression: %r" % expr)
        return "".join(parts)


def parse_row(comp, defs, macro, argtext, tables):
    typ, names = ROW_MACROS[macro]
    args = split_args(argtext)
    if len(args) != len(names):
        raise ValueError("%s: %d args, want %d: %r" % (macro, len(args), len(names), argtext))
    a = dict(zip(names, args))
    f = {"key": defs.eval_str(comp, a["key"]), "type": typ}
    if macro.endswith("_REQ"):
        f["required"] = True
    if macro == "SETTINGS_STR_FILE":
        f["format"] = "file"
    if typ == "string":
        # defaults may be a macro (fw_update: FWU_DEFAULT_BASE_URL)
        f["default"] = defs.eval_str(comp, a["default"]) or ""
        if "maxlen" in a:
            f["maxlen"] = defs.eval_int(comp, a["maxlen"])
        if "minlen" in a:
            f["minlen"] = defs.eval_int(comp, a["minlen"])
        if "enum" in a:
            f["enum"] = defs.eval_str(comp, a["enum"]).split(",")
    elif typ == "int":
        f["min"] = defs.eval_int(comp, a["min"])
        f["max"] = defs.eval_int(comp, a["max"])
        f["default"] = defs.eval_int(comp, a["default"])
    elif typ == "bool":
        f["default"] = {"true": True, "false": False}[a["default"].strip()]
    elif typ == "array":
        f["max_items"] = defs.eval_int(comp, a["max_items"])
        if "items" in a:
            tname = a["items"].strip()
            if tname not in tables:
                raise ValueError("item table %s not found" % tname)
            f["items"] = tables[tname]
        else:
            f["items"] = None  # free-form objects, on_validate enforces shape
        d = a["default"].strip()
        if d == "NULL":
            f["default"] = []
        else:
            m = re.fullmatch(r"SETTINGS_JSON\((.*)\)", d, re.S)
            if m:
                f["default"] = json.loads(" ".join(m.group(1).split()))
            else:
                f["default"] = json.loads(c_string(d))
    return f


def parse_tables(comp, defs, src):
    tables = {}
    warnings = []
    for m in re.finditer(r"settings_field_t\s+(\w+)\s*\[\s*\]\s*=\s*\{", src):
        start = m.end() - 1
        end = match_close(src, start, "{", "}")
        body = src[start + 1:end]
        if re.search(r"^\s*#\s*(if|ifdef|ifndef)", body, re.M):
            warnings.append("table %s contains preprocessor conditionals; "
                            "all branches were read" % m.group(1))
        rows = []
        i = 0
        for rm in re.finditer(r"\b(SETTINGS_[A-Z_]+)\s*\(", body):
            if rm.start() < i:
                continue
            macro = rm.group(1)
            if macro not in ROW_MACROS:
                continue
            ps = rm.end() - 1
            pe = match_close(body, ps, "(", ")")
            rows.append(parse_row(comp, defs, macro, body[ps + 1:pe], tables))
            i = pe
        tables[m.group(1)] = rows
    return tables, warnings


def parse_descriptors(comp, defs, src):
    out = []
    for m in re.finditer(r"settings_descriptor_t\s+\w+\s*=\s*\{", src):
        start = m.end() - 1
        end = match_close(src, start, "{", "}")
        body = src[start + 1:end]
        d = {}
        for fm in re.finditer(r"\.(\w+)\s*=\s*", body):
            vs = fm.end()
            depth, j = 0, vs
            while j < len(body):
                c = body[j]
                if c == '"':
                    j += 1
                    while body[j] != '"':
                        j += 2 if body[j] == "\\" else 1
                elif c in "([{":
                    depth += 1
                elif c in ")]}":
                    depth -= 1
                elif c == "," and depth == 0:
                    break
                j += 1
            d[fm.group(1)] = body[vs:j].strip()
        out.append(d)
    return out


def extract(root):
    defs = Defines(root)
    comps = {}
    warnings = []
    cdir = os.path.join(root, "components")
    for comp in sorted(os.listdir(cdir)):
        cpath = os.path.join(cdir, comp)
        if not os.path.isdir(cpath):
            continue
        if comp == "settings_manager":
            continue
        for fn in sorted(os.listdir(cpath)):
            # every top-level source registering a descriptor: most use
            # <comp>_settings.c, a few (mqtt_can.c) keep it inline
            if not fn.endswith(".c"):
                continue
            path = os.path.join(cpath, fn)
            raw = open(path, encoding="utf-8", errors="replace").read()
            if "settings_descriptor_t" not in raw:
                continue
            src = strip_comments(raw)
            tables, w = parse_tables(comp, defs, src)
            warnings += ["%s: %s" % (fn, x) for x in w]
            for d in parse_descriptors(comp, defs, src):
                if "name" not in d:
                    continue
                name = c_string(d["name"])
                ver = defs.eval_int(comp, d.get("version", "1"))
                tname = d.get("fields", "NULL").strip()
                if tname == "NULL" or tname not in tables:
                    warnings.append("%s: descriptor %s has no field table" % (fn, name))
                    continue
                dj = d.get("defaults_json", "NULL").strip()
                entry = {
                    "version": ver,
                    "source": os.path.relpath(path, root),
                    "fields": tables[tname],
                    "has_on_validate": d.get("on_validate", "NULL") != "NULL",
                    "has_on_migrate": d.get("on_migrate", "NULL") != "NULL",
                }
                if dj != "NULL":
                    entry["defaults_json"] = dj
                    warnings.append("%s: %s uses defaults_json (whole-object "
                                    "default override)" % (fn, name))
                comps[name] = entry
    return comps, warnings


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root")
    ap.add_argument("--fork", help="second tree (bitz fork) to overlay")
    ap.add_argument("--label", default="")
    ap.add_argument("--fork-label", default="")
    ap.add_argument("-o", "--out", default="v6_schema.json")
    a = ap.parse_args()

    comps, warnings = extract(a.root)
    if a.fork:
        fcomps, fw = extract(a.fork)
        warnings += ["fork: " + x for x in fw]
        for name, fe in fcomps.items():
            base = comps.get(name)
            if base is None:
                fe["fork_only"] = True
                comps[name] = fe
                continue
            have = {f["key"] for f in base["fields"]}
            for f in fe["fields"]:
                if f["key"] not in have:
                    f = dict(f, fork_only=True)
                    base["fields"].append(f)
            if fe["version"] != base["version"]:
                base["fork_version"] = fe["version"]

    doc = {
        "generated_by": "extract_schema.py",
        "upstream": a.label or a.root,
        "fork": (a.fork_label or a.fork) if a.fork else None,
        "warnings": warnings,
        "components": comps,
    }
    with open(a.out, "w") as f:
        json.dump(doc, f, indent=1, sort_keys=False)
        f.write("\n")
    nf = sum(len(c["fields"]) for c in comps.values())
    print("%d components, %d top-level fields -> %s" % (len(comps), nf, a.out))
    for w in warnings:
        print("warning:", w, file=sys.stderr)


if __name__ == "__main__":
    main()
