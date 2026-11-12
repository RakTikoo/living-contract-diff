"""OpenAPI 3.x loading (JSON or YAML), local $ref resolution, allOf merging.

Spec format: load_spec routes by file extension (.json -> strict JSON,
.yaml/.yml -> the YAML subset in lcdiff/yamlmini.py) and by content
sniffing when the extension is missing/ambiguous (leading '{' after
BOM/whitespace/comments -> JSON, otherwise YAML). The JSON path is
unchanged from v0: same parser, same errors. YAML parse failures and
YAML duplicate keys become IncompleteInputError with file + line
context, so the CLI exits 2 with no traceback and no findings.

Fix-pass changes:
- B3: a $ref already in the current ancestry resolves to a shallow
  {type: object} placeholder (the previously *documented* behaviour);
  sibling $refs to the same component are memoised. Branching recursive
  specs resolve in well under a second.
- B6: spec_endpoints returns documented fields PER STATUS CODE:
  {(method, template): {"200": doc_fields, "201": doc_fields, ...}}
  with a possible "2XX" wildcard key.
- C3/C4: documented fields record additionalProperties / free-form info,
  and additionalProperties schemas are flattened under a ".*" segment.
- C5: oneOf/anyOf are resolved into a union approximation (union of
  properties; required = required in ALL variants; per-field type =
  union of variant types).
- D5/D6: duplicate keys and NaN/Infinity are rejected; the openapi
  version must match ^3\\.\\d+(\\.\\d+)?$ and paths must be non-empty;
  allOf property schemas are deep-merged (scalar conflicts: later wins);
  "trace" is a recognised method; non-string / remote $refs exit 2.

YAML fix pass (2026-11-11, see ANALYSIS §11):
- spec_endpoints raises IncompleteInputError when response-code keys
  collide after str() normalisation (YAML `200:` int vs `'200':` str)
  or when method keys collide after case normalisation — a silent
  overwrite used to be able to flip a drift report to "No drift".
- load_spec hints that a spec must be UTF-8 when a non-JSON file
  fails UTF-8 decoding (e.g. a UTF-16-saved YAML spec).
"""
import copy
import os
import re

from .har import IncompleteInputError, loads_no_duplicates
from .yamlmini import MAX_DOCUMENT_BYTES as YAML_MAX_BYTES
from .yamlmini import YamlMiniError
from .yamlmini import loads as _yaml_loads

MAX_SCHEMA_DEPTH = 24
_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
_VERSION_RE = re.compile(r"^3\.\d+(\.\d+)?$")


def _sniff_format(text):
    """'json' when the first meaningful character is '{', else 'yaml'.

    Skips a BOM, whitespace and leading comment lines before deciding.
    """
    body = text.lstrip("\ufeff")
    for line in body.splitlines():
        s = line.strip()
        if s == "" or s.startswith("#"):
            continue
        return "json" if s.startswith("{") else "yaml"
    return "yaml"  # empty input: the YAML path reports it cleanly


def load_spec(path):
    ext = os.path.splitext(str(path))[1].lower()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise IncompleteInputError(f"OpenAPI file not found: {path}")
    except UnicodeDecodeError as e:
        # Fix pass item 15: a YAML spec saved as UTF-16 (a common
        # editor accident) fails here; say what is actually wrong.
        hint = "" if ext == ".json" else \
            " (YAML specs must be UTF-8 encoded — re-save the file " \
            "as UTF-8; UTF-16 files cannot be read)"
        raise IncompleteInputError(
            f"OpenAPI file unreadable: {path}: {e}{hint}")
    except OSError as e:
        raise IncompleteInputError(f"OpenAPI file unreadable: {path}: {e}")
    if ext in (".yaml", ".yml"):
        fmt = "yaml"
    elif ext == ".json":
        fmt = "json"
    else:
        fmt = _sniff_format(text)
    if fmt == "yaml":
        if len(text.encode("utf-8", "replace")) > YAML_MAX_BYTES:
            raise IncompleteInputError(
                f"{path}: YAML spec exceeds the maximum size of "
                f"{YAML_MAX_BYTES} bytes")
        try:
            spec = _yaml_loads(text)
        except YamlMiniError as e:
            raise IncompleteInputError(f"{path}: invalid YAML spec: {e}")
        if not isinstance(spec, dict):
            raise IncompleteInputError(
                f"{path}: spec is not a YAML mapping/object at the top level")
    else:
        try:
            spec = loads_no_duplicates(text, source=path)
        except Exception as e:
            raise IncompleteInputError(
                f"{path}: spec is not valid strict JSON (duplicate keys "
                f"and NaN/Infinity are rejected): {e}")
        if not isinstance(spec, dict):
            raise IncompleteInputError(f"{path}: spec is not a JSON object")
    version = spec.get("openapi")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise IncompleteInputError(
            f"{path}: not an OpenAPI 3.x document (openapi field must match "
            f"3.<minor>[.<patch>], got {version!r})")
    paths = spec.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise IncompleteInputError(f"{path}: OpenAPI document has no non-empty 'paths' object")
    return spec


def _resolve_pointer(spec, ref):
    if not isinstance(ref, str):
        raise IncompleteInputError(f"$ref must be a string, got: {ref!r}")
    if not ref.startswith("#/"):
        raise IncompleteInputError(f"only local $refs are supported in v0, got: {ref}")
    node = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise IncompleteInputError(f"unresolvable $ref: {ref}")
        node = node[part]
    return node


def resolve_schema(spec, schema, seen_refs=(), depth=0, _memo=None):
    """Return a schema dict with local $refs inlined and simple allOf merged.

    A $ref already in the current ancestry resolves to a shallow
    {type: object} placeholder, so recursive (including branching
    recursive) schemas terminate immediately (B3).
    """
    if _memo is None:
        _memo = {}
    if depth > MAX_SCHEMA_DEPTH or not isinstance(schema, dict):
        return {"type": "object"}
    schema = copy.deepcopy(schema)
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str):
            raise IncompleteInputError(f"$ref must be a string, got: {ref!r}")
        siblings = {k: v for k, v in schema.items() if k != "$ref"}
        if ref in seen_refs:
            base = {"type": "object"}  # ancestry placeholder (B3)
        else:
            memo_key = (ref, seen_refs)
            if memo_key in _memo:
                base = copy.deepcopy(_memo[memo_key])
            else:
                base = resolve_schema(spec, _resolve_pointer(spec, ref),
                                      seen_refs + (ref,), depth + 1, _memo)
                _memo[memo_key] = copy.deepcopy(base)
        base.update(siblings)
        schema = base
    if "allOf" in schema and isinstance(schema["allOf"], list):
        merged = {"type": "object", "properties": {}, "required": []}
        for part in schema["allOf"]:
            rp = resolve_schema(spec, part, seen_refs, depth + 1, _memo)
            _merge_into(merged, rp)
        rest = {k: v for k, v in schema.items() if k != "allOf"}
        if rest:
            _merge_into(merged, resolve_schema(spec, rest, seen_refs, depth + 1, _memo))
        schema = merged
    if isinstance(schema.get("properties"), dict):
        for k, v in schema["properties"].items():
            schema["properties"][k] = resolve_schema(spec, v, seen_refs, depth + 1, _memo)
    if isinstance(schema.get("items"), dict):
        schema["items"] = resolve_schema(spec, schema["items"], seen_refs, depth + 1, _memo)
    if isinstance(schema.get("additionalProperties"), dict):
        schema["additionalProperties"] = resolve_schema(
            spec, schema["additionalProperties"], seen_refs, depth + 1, _memo)
    for kw in ("oneOf", "anyOf"):
        if isinstance(schema.get(kw), list):
            schema[kw] = [resolve_schema(spec, v, seen_refs, depth + 1, _memo)
                          if isinstance(v, dict) else v for v in schema[kw]]
    return schema


def _deep_merge(dst, src):
    """Dict-merge recursively; scalar conflicts: later (src) wins (D6)."""
    for k, v in src.items():
        if k == "required" and isinstance(v, list) and isinstance(dst.get(k), list):
            for r in v:
                if r not in dst[k]:
                    dst[k].append(r)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def _merge_into(dst, src):
    if not isinstance(src, dict):
        return
    for k, v in src.items():
        if k == "properties" and isinstance(v, dict):
            props = dst.setdefault("properties", {})
            for name, sub in v.items():
                if name in props and isinstance(props[name], dict) and isinstance(sub, dict):
                    _deep_merge(props[name], sub)  # D6: deep-merge, not replace
                else:
                    props[name] = copy.deepcopy(sub)
        elif k == "required" and isinstance(v, list):
            cur = dst.setdefault("required", [])
            for r in v:
                if r not in cur:
                    cur.append(r)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)  # scalar conflicts: later part wins


def schema_types(schema):
    """Normalise a schema's declared types to a set of JSON types + nullable."""
    t = schema.get("type")
    types = set()
    nullable = bool(schema.get("nullable", False))
    if isinstance(t, list):  # OpenAPI 3.1 style: type: ["string", "null"]
        for x in t:
            if x == "null":
                nullable = True
            elif isinstance(x, str):
                types.add(x)
    elif isinstance(t, str):
        types.add(t)
    if not types and isinstance(schema.get("properties"), dict):
        types.add("object")
    if not types and isinstance(schema.get("items"), dict):
        types.add("array")
    return types, nullable


def _ap_info(schema):
    """additionalProperties summary for an object schema (C3/C4).

    Returns (ap_allowed, ap_free): ap_allowed means extra keys are not
    findings; ap_free means they are allowed *without* a value schema
    (additionalProperties: true, or a free-form object with no properties).
    """
    types, _n = schema_types(schema)
    if "object" not in types and not isinstance(schema.get("properties"), dict):
        return False, False  # additionalProperties is meaningless off objects
    ap = schema.get("additionalProperties")
    props = schema.get("properties")
    has_props = isinstance(props, dict) and len(props) > 0
    if ap is True:
        return True, True
    if isinstance(ap, dict):
        return True, False
    if ap is False:
        return False, False
    if not has_props:
        return True, True  # free-form object: AP defaults to allowed (C4)
    return False, False


def _new_doc(schema):
    types, nullable = schema_types(schema)
    enum = schema.get("enum") if isinstance(schema.get("enum"), list) else None
    ap_allowed, ap_free = _ap_info(schema)
    return {"types": sorted(types), "nullable": nullable, "enum": enum,
            "required": False, "ap_allowed": ap_allowed, "ap_free": ap_free}


def documented_fields(spec, schema, path="", out=None, depth=0):
    """Flatten a (resolved) response schema into {field_path: doc}."""
    if out is None:
        out = {}
    if depth > MAX_SCHEMA_DEPTH or not isinstance(schema, dict):
        return out
    if path not in out:
        out[path] = _new_doc(schema)
    types, _nullable = schema_types(schema)
    # C5: oneOf/anyOf union approximation.
    for kw in ("oneOf", "anyOf"):
        variants = schema.get(kw)
        if isinstance(variants, list) and variants:
            _union_variants(spec, variants, path, out, depth)
    if "object" in types or isinstance(schema.get("properties"), dict):
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        for name, sub in props.items():
            if not isinstance(sub, dict):
                continue
            child = f"{path}.{name}" if path else name
            documented_fields(spec, sub, child, out, depth + 1)
            if child in out:
                out[child]["required"] = name in required
        ap = schema.get("additionalProperties")
        if isinstance(ap, dict):
            # C3: map values are documented under a ".*" wildcard segment.
            documented_fields(spec, ap, f"{path}.*" if path else "*", out, depth + 1)
    if "array" in types or isinstance(schema.get("items"), dict):
        items = schema.get("items")
        if isinstance(items, dict):
            documented_fields(spec, items, path + "[]", out, depth + 1)
    return out


def _union_variants(spec, variants, path, out, depth):
    """Merge variant docs into `out`: union of properties/types; a field is
    required only if required in ALL variants that are objects (C5)."""
    temps = []
    for v in variants:
        if isinstance(v, dict):
            temps.append(documented_fields(spec, v, path, {}, depth + 1))
    if not temps:
        return
    all_paths = set()
    for t in temps:
        all_paths.update(t.keys())
    for p in sorted(all_paths):
        present = [t[p] for t in temps if p in t]
        merged_types = sorted({x for d in present for x in d["types"]})
        merged_nullable = any(d["nullable"] for d in present)
        enums = [d["enum"] for d in present]
        merged_enum = None
        if all(e is not None for e in enums) and len(present) == len(temps):
            merged_enum = []
            for e in enums:
                for v in e:
                    if v not in merged_enum:
                        merged_enum.append(v)
        required = len(present) == len(temps) and all(d["required"] for d in present)
        doc = {"types": merged_types, "nullable": merged_nullable,
               "enum": merged_enum, "required": required,
               "ap_allowed": any(d["ap_allowed"] for d in present),
               "ap_free": any(d["ap_free"] for d in present)}
        if p == path:
            # Variant info enriches the path's own doc but does not erase
            # a required flag set by the enclosing object.
            existing = out.get(p)
            if existing is not None:
                existing["types"] = sorted(set(existing["types"]) | set(merged_types))
                existing["nullable"] = existing["nullable"] or merged_nullable
                existing["ap_allowed"] = existing["ap_allowed"] or doc["ap_allowed"]
                existing["ap_free"] = existing["ap_free"] or doc["ap_free"]
                if existing["enum"] is None:
                    existing["enum"] = merged_enum
                continue
        out[p] = doc


def _json_schema_for_response(resp):
    if not isinstance(resp, dict):
        return None
    content = resp.get("content") or {}
    if not isinstance(content, dict):
        return None
    for mime, media in content.items():
        if "json" in str(mime).lower() and isinstance(media, dict) \
                and isinstance(media.get("schema"), dict):
            return media["schema"]
    return None


def spec_endpoints(spec):
    """Return {(method, template): {status_key: documented_fields}} (B6).

    status_key is the exact documented status code ("200", "201", ...) or
    "2XX" for a wildcard response key. Each documented 2xx status is
    diffed against the observed group for that same status.
    """
    result = {}
    for raw_path, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            raise IncompleteInputError(f"spec path {raw_path!r}: operations must be an object")
        template = _spec_template(raw_path)
        seen_methods = {}
        for method, op in ops.items():
            if not isinstance(method, str) or method.lower() not in _METHODS:
                continue
            # Same normalisation-collision guard as for response codes
            # below: 'get' and 'GET' are distinct YAML keys that would
            # silently collapse onto one (method, template) entry.
            mkey = method.lower()
            if mkey in seen_methods:
                raise IncompleteInputError(
                    f"spec operation {raw_path}: duplicate method "
                    f"{method!r} after case normalisation (already "
                    f"have {seen_methods[mkey]!r})")
            seen_methods[mkey] = method
            if not isinstance(op, dict):
                raise IncompleteInputError(
                    f"spec operation {method} {raw_path}: must be an object")
            responses = op.get("responses") or {}
            if not isinstance(responses, dict):
                raise IncompleteInputError(
                    f"spec operation {method} {raw_path}: 'responses' must be an object")
            per_status = {}
            seen_codes = {}
            for code, resp in responses.items():
                code_s = str(code)
                if code_s.upper() == "2XX":
                    key = "2XX"
                elif code_s.isdigit() and 200 <= int(code_s) < 300:
                    key = code_s
                else:
                    continue
                # Fix pass item 2: a YAML spec can hold both 200 (int)
                # and '200' (str) as distinct parser keys; str()
                # normalisation would let one silently overwrite the
                # other and flip a drift report to "No drift". The
                # int/str form can never arise from JSON (its keys are
                # strings and its parser rejects exact duplicates);
                # the wildcard case-variant form ('2XX'/'2xx') is the
                # same ambiguity in either format and is rejected too.
                if key in seen_codes:
                    raise IncompleteInputError(
                        f"spec operation {method} {raw_path}: duplicate "
                        f"response code {key!r} after normalisation "
                        f"(keys {seen_codes[key]!r} and {code!r} collide); "
                        f"quote all response codes consistently")
                seen_codes[key] = code
                schema = _json_schema_for_response(resp)
                if schema is None:
                    continue
                resolved = resolve_schema(spec, schema)
                per_status[key] = documented_fields(spec, resolved)
            if per_status:
                result[(method.upper(), template)] = per_status
    return result


def _spec_template(raw_path):
    """OpenAPI paths already use {param}; normalise every {..} to {id} to
    match HAR templating, keeping literal segments untouched."""
    parts = [p for p in raw_path.split("/") if p != ""]
    out = []
    for p in parts:
        if p.startswith("{") and p.endswith("}"):
            out.append("{id}")
        else:
            out.append(p)
    return "/" + "/".join(out)
