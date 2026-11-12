"""HAR parsing. Static analysis only — nothing from a HAR is ever executed.

Strictness (fix pass D2/D5):
- NaN / Infinity / -Infinity constants are rejected everywhere (INCOMPLETE).
- Undecodable or unparseable bodies for JSON content types are INCOMPLETE,
  never silently treated as "no body".
- Per-entry structural errors are collected and reported as INCOMPLETE,
  never swallowed.
- Duplicate keys inside HAR *bodies*: last value wins (JSON's usual
  behaviour) but the duplicates are counted and surfaced as a baseline
  "warnings" note. Duplicate keys in specs/baselines are a hard error
  (see openapi.py / cli.py).
"""
import base64
import hashlib
import json
import re
from urllib.parse import urlparse


class IncompleteInputError(Exception):
    """Input is unreadable / truncated. Callers must exit 2, never report clean."""


def _reject_constant(name):
    raise ValueError(f"non-strict JSON constant {name!r} is not accepted")


def strict_loads(text):
    """json.loads that rejects NaN/Infinity."""
    return json.loads(text, parse_constant=_reject_constant)


def loads_no_duplicates(text, source="<json>"):
    """json.loads that rejects NaN/Infinity AND duplicate object keys."""
    def hook(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise ValueError(f"{source}: duplicate key {k!r}")
            obj[k] = v
        return obj
    return json.loads(text, parse_constant=_reject_constant,
                      object_pairs_hook=hook)


def loads_counting_duplicates(text):
    """Return (obj, duplicate_key_count); last value wins per key."""
    count = [0]

    def hook(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                count[0] += 1
            obj[k] = v
        return obj
    obj = json.loads(text, parse_constant=_reject_constant,
                     object_pairs_hook=hook)
    return obj, count[0]


def _is_uuid(seg):
    return bool(re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", seg))


_BASE64URL_ALPHABET = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")


def _is_token_like(seg):
    """Heuristic (A4): a long segment that looks like an opaque token.

    Collapses when length >= 20 AND (it mixes letters and digits, OR it is
    drawn from the hex/base64url alphabet with a high non-alpha ratio).
    Pure-letter long segments (slugs/titles) are NOT collapsed here; they
    are handled at diff time by structural segment matching (C2).
    """
    if len(seg) < 20:
        return False
    has_alpha = any(c.isalpha() for c in seg)
    has_digit = any(c.isdigit() for c in seg)
    if has_alpha and has_digit:
        return True
    if all(c in _BASE64URL_ALPHABET for c in seg):
        non_alpha = sum(1 for c in seg if not c.isalpha())
        if seg and non_alpha / len(seg) >= 0.25:
            return True
    return False


def _collapse_segment(seg):
    if seg.isdigit():
        # A4: do NOT collapse 4-digit year-like segments (1900-2100).
        if len(seg) == 4 and 1900 <= int(seg) <= 2100:
            return False
        return True
    if _is_uuid(seg):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{32}", seg):  # undashed UUID / md5-style hex
        return True
    if re.fullmatch(r"\d+\.\d+", seg):  # decimal number, e.g. "1.5"
        return True
    return _is_token_like(seg)


def path_template(raw_path):
    """Collapse numeric, UUID and token-like path segments into {id}.

    All error messages use this templated form (A5) so raw tokens in URLs
    never leak into output.
    """
    parts = [p for p in raw_path.split("/") if p != ""]
    out = ["{id}" if _collapse_segment(p) else p for p in parts]
    return "/" + "/".join(out)


def status_group(status):
    try:
        s = int(status)
    except Exception:
        return "unknown"
    return "%dxx" % (s // 100)


def baseline_group_key(status):
    """Baseline v2 grouping: exact status code for 2xx, class otherwise."""
    try:
        s = int(status)
    except Exception:
        return "unknown"
    if 200 <= s < 300:
        return str(s)
    return "%dxx" % (s // 100)


def is_2xx_group_key(key):
    if key == "2xx":  # defensive: v1-style key inside a hand-made baseline
        return True
    try:
        return 200 <= int(key) < 300
    except (TypeError, ValueError):
        return False


class Entry:
    __slots__ = ("method", "path", "template", "status", "group", "body",
                 "has_body", "dup_keys")

    def __init__(self, method, path, status, body, has_body, dup_keys=0):
        self.method = method
        self.path = path
        self.template = path_template(path)
        self.status = status
        self.group = baseline_group_key(status)
        self.body = body
        self.has_body = has_body
        self.dup_keys = dup_keys


def parse_har_text(text, source="<har>"):
    """Return (entries, skipped_count). Raise IncompleteInputError on bad input."""
    try:
        data = strict_loads(text)
    except Exception as e:
        raise IncompleteInputError(f"{source}: not valid JSON (truncated or malformed HAR): {e}")
    if not isinstance(data, dict) or not isinstance(data.get("log"), dict):
        raise IncompleteInputError(f"{source}: HAR missing top-level 'log' object")
    raw_entries = data["log"].get("entries")
    if not isinstance(raw_entries, list):
        raise IncompleteInputError(f"{source}: HAR log.entries is missing or not a list")
    entries, skipped, errors = [], 0, []
    for i, raw in enumerate(raw_entries):
        try:
            if not isinstance(raw, dict):
                errors.append(f"entry {i}: HAR entry is not an object")
                continue
            req = raw.get("request")
            resp = raw.get("response")
            if req is not None and not isinstance(req, dict):
                errors.append(f"entry {i}: 'request' is not an object")
                continue
            if resp is not None and not isinstance(resp, dict):
                errors.append(f"entry {i}: 'response' is not an object")
                continue
            req = req or {}
            resp = resp or {}
            method = str(req.get("method") or "GET").upper()
            url = str(req.get("url") or "")
            path = urlparse(url).path or "/"
            template = path_template(path)  # templated in ALL messages (A5)
            status = resp.get("status", 0)
            content = resp.get("content")
            if content is not None and not isinstance(content, dict):
                errors.append(f"entry {i} ({method} {template}): 'content' is not an object")
                continue
            content = content or {}
            mime = str(content.get("mimeType") or "")
            raw_text = content.get("text")
            if raw_text is None or raw_text == "":
                entries.append(Entry(method, path, status, None, False))
                continue
            if not isinstance(raw_text, str):
                errors.append(f"entry {i} ({method} {template}): content.text is not a string")
                continue
            text_body = raw_text
            if content.get("encoding") == "base64":
                try:
                    decoded = base64.b64decode(raw_text, validate=True)
                    text_body = decoded.decode("utf-8", errors="strict")
                except Exception as e:
                    if "json" in mime.lower():
                        errors.append(
                            f"entry {i} ({method} {template}): base64 body for a "
                            f"JSON content type could not be decoded: {e}")
                    else:
                        skipped += 1
                        entries.append(Entry(method, path, status, None, False))
                    continue
            stripped = text_body.strip()
            if stripped == "":
                entries.append(Entry(method, path, status, None, False))
                continue
            looks_json = ("json" in mime.lower()) or stripped[:1] in ("{", "[")
            if not looks_json:
                skipped += 1
                entries.append(Entry(method, path, status, None, False))
                continue
            try:
                body, dup_keys = loads_counting_duplicates(text_body)
            except Exception as e:
                errors.append(f"entry {i} ({method} {template}): JSON body failed to parse: {e}")
                continue
            entries.append(Entry(method, path, status, body, True, dup_keys))
        except Exception as e:  # collected, not swallowed (D2)
            errors.append(f"entry {i}: unexpected entry structure: {e}")
            continue
    if errors:
        raise IncompleteInputError(
            "INCOMPLETE: %d entr(ies) could not be processed; refusing to "
            "produce a clean baseline. First: %s" % (len(errors), errors[0]))
    return entries, skipped


def load_har(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise IncompleteInputError(f"HAR file not found: {path}")
    except (OSError, UnicodeDecodeError) as e:
        raise IncompleteInputError(f"HAR file unreadable: {path}: {e}")
    return parse_har_text(text, source=path)


def load_samples_dir(samples_dir, manifest_path):
    """Optional input: directory of .json response samples + endpoint manifest.

    Manifest format (JSON):
      {"samples": [{"method": "GET", "path": "/users/123", "status": 200,
                    "file": "user1.json"}]}
    """
    import os
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = loads_no_duplicates(f.read(), source=manifest_path)
    except IncompleteInputError:
        raise
    except Exception as e:
        raise IncompleteInputError(f"manifest unreadable: {manifest_path}: {e}")
    if not isinstance(manifest, dict):
        raise IncompleteInputError("manifest must be a JSON object")
    items = manifest.get("samples")
    if not isinstance(items, list):
        raise IncompleteInputError("manifest must contain a 'samples' list")
    entries = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise IncompleteInputError(f"manifest sample {i} is not an object")
        fname = item.get("file")
        fpath = os.path.join(samples_dir, fname) if fname else None
        try:
            with open(fpath, encoding="utf-8") as f:
                body, dup_keys = loads_counting_duplicates(f.read())
        except Exception as e:
            raise IncompleteInputError(f"sample {i} ({fname}) truncated/unreadable: {e}")
        entries.append(Entry(str(item.get("method", "GET")).upper(),
                             str(item.get("path", "/")), item.get("status", 200),
                             body, True, dup_keys))
    return entries, 0


def value_fingerprint(value):
    """Stable fingerprint for a value we must count but never store (A1)."""
    return hashlib.sha256(repr(value).encode("utf-8", "replace")).hexdigest()
