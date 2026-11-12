"""Statistical schema inference. Stores statistics only — never payloads.

Privacy (fix pass A1-A3, layered and deterministic):
- Enum candidates are collected up to ENUM_CAP (20) distinct values per
  field per status group; exceeding the cap drops ALL candidates for that
  field in that group and marks it enum_open.
- After an endpoint's groups are built, candidates are re-checked across
  its status groups (A2): if the union for a field exceeds the cap, the
  candidates are dropped in ALL of the endpoint's groups.
- String candidates longer than ENUM_VALUE_MAX_LEN (64) chars are never
  stored (they still count toward cardinality via a fingerprint).
- Values of sensitive-named fields (password/token/secret/...) are never
  stored as candidates; only types/counts/enum_open are recorded.
- Map keys: per parent-object path, distinct child keys are capped at
  MAP_KEY_CAP (50) across observations. Beyond that the object is treated
  as a MAP: no per-key subfields are emitted, and the parent records
  map_keys_open + the distinct key count (A3). Key *names* are never
  stored for a map parent beyond that count.
These are heuristics layered on the cap, not a guarantee — see README.
"""
import re
from . import ENUM_CAP, ENUM_VALUE_MAX_LEN, MAP_KEY_CAP, MAX_DEPTH
from .har import value_fingerprint

_DATE_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_SENSITIVE_RE = re.compile(
    r"password|passwd|secret|token|api[-_]?key|apikey|auth|session|cookie|credential",
    re.IGNORECASE)


def json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def format_hint(s):
    """D7: fully-anchored date/time detection with range sanity checks.

    A bare valid date is hint "date"; only a date WITH a time component is
    "date-time". Out-of-range components (month 13, hour 25) yield no hint.
    """
    if _UUID_RE.match(s):
        return "uuid"
    if _EMAIL_RE.match(s):
        return "email"
    m = _DATE_RE.match(s)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        if m.group(4) is None:
            return "date"
        hour, minute = int(m.group(4)), int(m.group(5))
        second = int(m.group(6)) if m.group(6) is not None else 0
        if hour >= 24 or minute >= 60 or second >= 60:
            return None
        return "date-time"
    return None


def _final_segment(path):
    seg = path.rsplit(".", 1)[-1] if path else ""
    return seg[:-2] if seg.endswith("[]") else seg


class FieldStats:
    __slots__ = ("path", "sensitive", "present", "parent", "type_counts",
                 "_enum", "enum_open", "formats", "all_integral",
                 "map_keys_open", "map_key_count", "arrays_observed",
                 "items_observed")

    def __init__(self, path=""):
        self.path = path
        self.sensitive = bool(_SENSITIVE_RE.search(_final_segment(path)))
        self.present = 0
        self.parent = 0
        self.type_counts = {}
        # (json_type, canonical) -> [stored_value_or_None, count]  (B1:
        # keying by (type, canonical) makes True and 1 distinct, and lets
        # floats participate via their canonical repr.)
        self._enum = {}
        self.enum_open = False
        self.formats = {}
        self.all_integral = True
        self.map_keys_open = False
        self.map_key_count = 0
        self.arrays_observed = 0
        self.items_observed = 0

    def observe(self, value):
        t = json_type(value)
        self.type_counts[t] = self.type_counts.get(t, 0) + 1
        if t == "number" and isinstance(value, float) and not value.is_integer():
            self.all_integral = False
        if t in ("string", "integer", "number", "boolean"):
            if t == "string":
                h = format_hint(value)
                if h:
                    self.formats[h] = self.formats.get(h, 0) + 1
            if not self.enum_open:
                if t == "string":
                    storable = (not self.sensitive
                                and len(value) <= ENUM_VALUE_MAX_LEN)
                    canonical = value if storable else value_fingerprint(value)
                else:
                    storable = not self.sensitive
                    # canonical float repr; ints/bools use repr as well —
                    # the (type, canonical) pair keeps 1, 1.0 and True apart.
                    canonical = repr(value)
                key = (t, canonical)
                slot = self._enum.get(key)
                if slot is not None:
                    slot[1] += 1
                elif len(self._enum) < ENUM_CAP:
                    self._enum[key] = [value if storable else None, 1]
                else:
                    # Cap exceeded: drop ALL stored candidates (privacy).
                    self.enum_open = True
                    self._enum = {}

    def enum_candidates(self):
        if self.enum_open:
            return []
        vals = [v for (v, _c) in self._enum.values() if v is not None]
        # B1: deterministic ordering by (json type, repr).
        vals.sort(key=lambda v: (json_type(v), repr(v)))
        return vals

    def to_dict(self):
        rate = (self.present / self.parent) if self.parent else 0.0
        return {
            "present_count": self.present,
            "parent_count": self.parent,
            "presence_rate": round(rate, 6),
            "types": sorted(self.type_counts.keys()),
            "type_counts": dict(sorted(self.type_counts.items())),
            "nullable": "null" in self.type_counts,
            "enum_candidates": self.enum_candidates(),
            "enum_open": self.enum_open,
            "format_hints": sorted(self.formats.keys()),
            "all_integral": self.all_integral,
            "map_keys_open": self.map_keys_open,
            "map_key_count": self.map_key_count,
            "arrays_observed": self.arrays_observed,
            "items_observed": self.items_observed,
        }


class _Ctx:
    def __init__(self):
        self.fields = {}
        self.obj_parents = {}    # path -> [key-set per object instance]
        self.array_totals = {}   # "x[]" path -> [arrays_observed, items_observed]
        self.map_key_sets = {}   # parent path -> set of distinct child keys
        self.map_parents = set()  # parent paths that exceeded MAP_KEY_CAP

    def stats(self, path):
        st = self.fields.get(path)
        if st is None:
            st = FieldStats(path)
            self.fields[path] = st
        return st


def _parent_path(path):
    if path.endswith("[]"):
        return path[:-2]
    if "." in path:
        return path.rsplit(".", 1)[0]
    return "" if path else None


def _ancestors(path):
    out = []
    p = _parent_path(path)
    while p is not None:
        out.append(p)
        if p == "":
            break
        p = _parent_path(p)
    return out


def _walk(value, path, ctx, depth):
    st = ctx.stats(path)
    st.present += 1
    st.observe(value)
    if depth >= MAX_DEPTH:
        return
    if isinstance(value, dict):
        ctx.obj_parents.setdefault(path, []).append(set(value.keys()))
        keys = ctx.map_key_sets.setdefault(path, set())
        keys.update(value.keys())
        if len(keys) > MAP_KEY_CAP:
            ctx.map_parents.add(path)
        if path in ctx.map_parents:
            return  # A3: map — no per-key subfields
        for k, v in value.items():
            child = f"{path}.{k}" if path else k
            _walk(v, child, ctx, depth + 1)
    elif isinstance(value, list):
        item_path = path + "[]"
        totals = ctx.array_totals.setdefault(item_path, [0, 0])
        totals[0] += 1
        totals[1] += len(value)
        ist = ctx.stats(item_path)
        ist.parent += len(value)
        for item in value:
            _walk(item, item_path, ctx, depth + 1)


def infer_endpoint(entries_for_group):
    """entries_for_group: list of Entry with parsed JSON bodies (has_body)."""
    ctx = _Ctx()
    response_count = 0
    bodies = 0
    for e in entries_for_group:
        response_count += 1
        if not e.has_body:
            continue
        bodies += 1
        root = ctx.stats("")
        root.parent += 1
        _walk(e.body, "", ctx, 0)
    fields = ctx.fields
    # A3: prune every field living under a map parent, then flag the parent.
    if ctx.map_parents:
        for path in list(fields.keys()):
            if path and any(mp in _ancestors(path) or path.startswith(mp + ".")
                            for mp in ctx.map_parents if mp != ""):
                del fields[path]
            elif path and "" in ctx.map_parents:
                del fields[path]
        for mp in ctx.map_parents:
            st = fields.get(mp)
            if st is not None:
                st.map_keys_open = True
    for path, st in fields.items():
        if path in ctx.map_key_sets:
            st.map_key_count = len(ctx.map_key_sets[path])
        if path in ctx.array_totals:
            st.arrays_observed, st.items_observed = ctx.array_totals[path]
    # Parent counts (B2): an object child's denominator is the number of
    # parent-object instances; an array-item child's denominator is the
    # TOTAL number of items observed in arrays at that path (non-object
    # items count as not-present for object fields).
    def container_count(p):
        if p is None:
            return None
        if p.endswith("[]"):
            t = ctx.array_totals.get(p)
            return t[1] if t else None
        if p in ctx.obj_parents:
            return len(ctx.obj_parents[p])
        return None

    for path, st in fields.items():
        if path == "":
            st.parent = st.present
        elif path.endswith("[]"):
            pass  # parent accumulated per item during _walk
        else:
            c = container_count(_parent_path(path))
            if c is not None:
                st.parent = c
            else:
                st.parent = max(st.parent, st.present)
    return {
        "response_count": response_count,
        "json_response_count": bodies,
        "fields": {k: fields[k].to_dict() for k in sorted(fields.keys())},
    }


def _apply_endpoint_union_cap(endpoints):
    """A2: per-field candidate union across an endpoint's status groups.

    If the union exceeds ENUM_CAP, drop candidates in ALL groups for that
    field and mark enum_open everywhere the field appears.
    """
    from .har import value_fingerprint  # canonicalisation helper

    def canon(v):
        return (type(v).__name__, repr(v))

    unions = {}
    for ep in endpoints.values():
        per_field = {}
        for group in ep["status_groups"].values():
            for fname, f in (group.get("fields") or {}).items():
                if f.get("enum_open"):
                    per_field[fname] = None  # already open somewhere
                    continue
                s = per_field.setdefault(fname, set())
                if s is not None:
                    for v in f.get("enum_candidates") or []:
                        s.add(canon(v))
        for fname, s in per_field.items():
            if s is not None and len(s) > ENUM_CAP:
                unions[(id(ep), fname)] = True
                for group in ep["status_groups"].values():
                    f = (group.get("fields") or {}).get(fname)
                    if f is not None:
                        f["enum_candidates"] = []
                        f["enum_open"] = True
    return endpoints


def build_baseline(entries):
    """entries: list of har.Entry. Returns the versioned (v2) baseline dict."""
    from . import BASELINE_VERSION
    grouped = {}
    dup_total = 0
    for e in entries:
        dup_total += getattr(e, "dup_keys", 0) or 0
        key = (e.method, e.template)
        grouped.setdefault(key, {}).setdefault(e.group, []).append(e)
    endpoints = {}
    for (method, template) in sorted(grouped.keys()):
        ep_key = f"{method} {template}"
        groups = {}
        for group in sorted(grouped[(method, template)].keys()):
            groups[group] = infer_endpoint(grouped[(method, template)][group])
        endpoints[ep_key] = {"method": method, "path_template": template,
                             "status_groups": groups}
    _apply_endpoint_union_cap(endpoints)
    baseline = {
        "version": BASELINE_VERSION,
        "tool": "living-contract-diff",
        "note": ("Statistics only: counts, types, format classes, and enum "
                 "candidates for low-cardinality fields (<=20 distinct, "
                 "strings <=64 chars, sensitive-named fields never stored). "
                 "2xx responses are grouped by exact status code."),
        "endpoints": endpoints,
    }
    if dup_total:
        baseline["warnings"] = [
            f"{dup_total} duplicate JSON key(s) in response bodies; "
            "last value wins (counted here, values not stored)"]
    return baseline
