"""Semantic diff: observed baseline vs declared OpenAPI contract.

Rules (v0 + adversarial fix pass):
- B6: diffing is PER EXACT STATUS CODE within 2xx. Each documented 2xx
  status is compared with the observed group for that same status. An
  observed 2xx status with no documented schema surfaces its fields once
  as UNDOCUMENTED_FIELD warnings scoped to that status. A documented
  "2XX" wildcard applies to observed 2xx statuses with no exact doc.
- B5: spec endpoints/statuses with NO matching observed traffic are not
  diffed and fabricate no findings; they are listed in the report's
  "not_observed" section and do not affect the exit code.
- C2: baseline endpoints are matched to spec endpoints segment-by-segment
  (spec {param} positions match any single baseline segment) and the
  stats of all baseline endpoints matching one spec endpoint are MERGED
  before diffing. The baseline file itself may stay fragmented.
- C1: endpoints (per status group) with fewer than LOW_SAMPLE_THRESHOLD
  (5) observed JSON responses have ALL findings downgraded to warning
  with a low_sample flag in the evidence. The report states per-endpoint
  sample sizes.
- C6: a wholly undocumented observed endpoint collapses into ONE warning
  finding (its fields are listed in the evidence). --ignore-endpoint
  globs exclude endpoints entirely.
- REQUIRED_MISSING (breaking): spec-required field below the presence
  threshold (default 0.99, --required-threshold). Evidence carries exact
  counts; for a never-observed field the denominator is the parent
  field's instance count when the parent was observed (B7). No cascade
  into children of a never-observed parent.
- B4: a required field whose parent array was observed but always empty
  yields REQUIRED_MISSING at WARNING severity, worded as
  unverifiable-because-empty (arrays_observed>0, items_observed=0).
- TYPE_MISMATCH (breaking): observed type outside the spec type. Spec
  "number" accepts observed "integer"; observed integral floats satisfy
  spec "integer" (C7); observed "null" only if nullable. If exactly one
  instance mismatches out of >=20 observations, the finding is
  downgraded to warning with the count/rate in evidence (C8).
- UNDOCUMENTED_FIELD (warning): observed field the spec does not
  document — except under additionalProperties maps (C3), free-form
  objects (C4), map parents (A3), and oneOf/anyOf variants (C5).
- ENUM_GROWTH (warning): observed values beyond the documented enum.
  For an enum_open field it fires only when the spec enum is provably
  exceeded: enum_open implies >=21 distinct observed values, so it
  fires iff len(spec_enum) < 21 (B8).
"""
import fnmatch

from . import LOW_SAMPLE_THRESHOLD, REQUIRED_PRESENCE_THRESHOLD
from .har import is_2xx_group_key

SEVERITY = {
    "REQUIRED_MISSING": "breaking",
    "TYPE_MISMATCH": "breaking",
    "UNDOCUMENTED_FIELD": "warning",
    "ENUM_GROWTH": "warning",
}


def _finding(code, endpoint, field, evidence, severity=None):
    return {"code": code, "severity": severity or SEVERITY[code],
            "endpoint": endpoint, "field": field, "evidence": evidence}


# ---------------------------------------------------------------- merging

def _canon(v):
    return (type(v).__name__, repr(v))


def merge_field_dicts(a, b):
    """Merge two baseline field-stat dicts (C2 aggregation / B6 groups)."""
    if a is None:
        return dict(b)
    if b is None:
        return dict(a)
    out = dict(a)
    out["present_count"] = a.get("present_count", 0) + b.get("present_count", 0)
    out["parent_count"] = a.get("parent_count", 0) + b.get("parent_count", 0)
    pc, par = out["present_count"], out["parent_count"]
    out["presence_rate"] = round(pc / par, 6) if par else 0.0
    tc = dict(a.get("type_counts") or {})
    for k, v in (b.get("type_counts") or {}).items():
        tc[k] = tc.get(k, 0) + v
    out["type_counts"] = dict(sorted(tc.items()))
    out["types"] = sorted(tc.keys())
    out["nullable"] = bool(a.get("nullable")) or bool(b.get("nullable"))
    out["all_integral"] = bool(a.get("all_integral", True)) and bool(b.get("all_integral", True))
    if a.get("enum_open") or b.get("enum_open"):
        out["enum_open"] = True
        out["enum_candidates"] = []
    else:
        union = {_canon(v): v for v in (a.get("enum_candidates") or [])}
        for v in (b.get("enum_candidates") or []):
            union[_canon(v)] = v
        if len(union) > 20:
            out["enum_open"] = True
            out["enum_candidates"] = []
        else:
            out["enum_open"] = False
            vals = sorted(union.values(), key=lambda v: (type(v).__name__, repr(v)))
            out["enum_candidates"] = vals
    out["format_hints"] = sorted(set(a.get("format_hints") or []) | set(b.get("format_hints") or []))
    out["map_keys_open"] = bool(a.get("map_keys_open")) or bool(b.get("map_keys_open"))
    out["map_key_count"] = max(a.get("map_key_count", 0), b.get("map_key_count", 0))
    out["arrays_observed"] = a.get("arrays_observed", 0) + b.get("arrays_observed", 0)
    out["items_observed"] = a.get("items_observed", 0) + b.get("items_observed", 0)
    return out


def merge_groups(groups):
    """Merge a list of baseline status-group dicts into one group dict."""
    fields = {}
    resp = 0
    jresp = 0
    for g in groups:
        resp += g.get("response_count", 0)
        jresp += g.get("json_response_count", 0)
        for fname, f in (g.get("fields") or {}).items():
            fields[fname] = merge_field_dicts(fields.get(fname), f)
    return {"response_count": resp, "json_response_count": jresp, "fields": fields}


def _endpoint_2xx_groups(ep):
    """{status_key: group} for the 2xx groups of a baseline endpoint."""
    out = {}
    for key, g in ((ep or {}).get("status_groups") or {}).items():
        if is_2xx_group_key(key):
            out[key] = g
    return out


# ------------------------------------------------------------- matching

def _segments(template):
    return [p for p in str(template).split("/") if p != ""]


def _segment_match(spec_template, baseline_template):
    """C2: static segments must be equal; spec {param} matches any segment."""
    ss, bs = _segments(spec_template), _segments(baseline_template)
    if len(ss) != len(bs):
        return False
    for s, b in zip(ss, bs):
        if s.startswith("{") and s.endswith("}"):
            continue
        if s != b:
            return False
    return True


def _ignored(ep_key, template, patterns):
    for pat in patterns or []:
        if fnmatch.fnmatch(ep_key, pat) or fnmatch.fnmatch(template, pat):
            return True
    return False


# ------------------------------------------------------------- type/enum

def _type_ok(observed_type, doc, field_stats):
    spec_types = set(doc.get("types") or [])
    if observed_type == "null":
        return bool(doc.get("nullable"))
    if not spec_types:
        return True
    if observed_type in spec_types:
        return True
    if observed_type == "integer" and "number" in spec_types:
        return True
    if observed_type == "number" and "integer" in spec_types \
            and (field_stats or {}).get("all_integral", True):
        return True  # C7: integral floats satisfy spec integer
    return False


def _check_type(findings, ep_key, field, o, doc):
    bad = {}
    for t, c in (o.get("type_counts") or {}).items():
        if not _type_ok(t, doc, o):
            bad[t] = c
    if not bad:
        return
    evidence = {"observed_types": o["types"], "mismatched_type_counts": bad,
                "spec_types": doc.get("types"), "spec_nullable": doc.get("nullable")}
    severity = None
    total_obs = sum((o.get("type_counts") or {}).values())
    mismatched = sum(bad.values())
    if mismatched == 1 and total_obs >= 20:
        severity = "warning"  # C8: single bad instance in a large sample
        evidence["mismatched_count"] = mismatched
        evidence["total_observations"] = total_obs
        evidence["mismatch_rate"] = round(mismatched / total_obs, 6)
        evidence["note"] = "single mismatched instance in >=20 observations; downgraded"
    findings.append(_finding("TYPE_MISMATCH", ep_key, field, evidence, severity=severity))


def _check_enum(findings, ep_key, field, o, doc):
    spec_enum = doc.get("enum")
    if spec_enum is None:
        return
    if o.get("enum_open"):
        # B8: enum_open implies >=21 distinct observed; only provably
        # exceeded when the spec enum holds fewer than 21 values.
        if len(spec_enum) < 21:
            findings.append(_finding("ENUM_GROWTH", ep_key, field, {
                "enum_open": True, "spec_enum": spec_enum,
                "note": "observed >=21 distinct values; spec enum is closed "
                        f"with {len(spec_enum)} value(s)"}))
        return
    extra = [v for v in (o.get("enum_candidates") or []) if v not in spec_enum]
    if extra:
        findings.append(_finding("ENUM_GROWTH", ep_key, field, {
            "extra_values": extra, "spec_enum": spec_enum}))


# ------------------------------------------------------------- doc lookup

def _parent_path(field):
    if field.endswith("[]"):
        return field[:-2]
    if "." in field:
        return field.rsplit(".", 1)[0]
    return "" if field else None


def _wildcard_variants(field):
    """Variants of `field` with segments replaced by the map wildcard '*',
    fewest replacements first (C3)."""
    segs = field.split(".")
    variants = []
    n = len(segs)
    for mask in range(1, 1 << n):
        v = ".".join("*" if (mask >> i) & 1 else s for i, s in enumerate(segs))
        variants.append((bin(mask).count("1"), v))
    variants.sort(key=lambda x: x[0])
    return [v for _c, v in variants]


def _doc_for(field, doc_fields):
    if field in doc_fields:
        return doc_fields[field]
    for v in _wildcard_variants(field):
        if v in doc_fields:
            return doc_fields[v]
    return None


def _under_free_parent(field, doc_fields):
    """True when a documented ancestor is a free-form / AP-true object (C4)
    or a map parent whose keys were not enumerated (A3 handled at source;
    this covers hand-written baselines too)."""
    p = _parent_path(field)
    while p is not None:
        doc = doc_fields.get(p)
        if doc is not None and doc.get("ap_allowed") and doc.get("ap_free"):
            return True
        if p == "":
            break
        p = _parent_path(p)
    return False


# ------------------------------------------------------------- main diff

def diff_report(baseline, spec_docs, threshold=REQUIRED_PRESENCE_THRESHOLD,
                ignore_endpoints=()):
    """Full diff. Returns {"findings", "not_observed", "endpoint_samples"}.

    spec_docs: {(method, template): {status_key: doc_fields}} (openapi.py).
    """
    findings = []
    not_observed = []
    endpoint_samples = {}
    endpoints = baseline.get("endpoints") or {}

    # Index observed endpoints, applying ignore globs (C6).
    observed = []  # (method, template, ep_key, ep)
    for ep_key, ep in sorted(endpoints.items()):
        if not isinstance(ep, dict):
            continue
        method, template = ep.get("method", ""), ep.get("path_template", "")
        if _ignored(ep_key, template, ignore_endpoints):
            continue
        observed.append((method, template, ep_key, ep))

    claimed = set()  # id(ep) of baseline endpoints matched to a spec endpoint

    for (method, template), per_status in sorted(spec_docs.items()):
        ep_key = f"{method} {template}"
        if _ignored(ep_key, template, ignore_endpoints):
            continue
        # C2: merge ALL baseline endpoints that segment-match this spec
        # endpoint; exact-template matches are always included, structural
        # (slug-fragment) matches only if not already claimed by an
        # earlier (more specific) spec endpoint.
        matches = []
        for o in observed:
            if o[0] != method:
                continue
            if o[1] == template:
                matches.append(o)
            elif _segment_match(template, o[1]) and o[2] not in claimed:
                matches.append(o)
        for o in matches:
            claimed.add(o[2])
        # Merge observed groups per exact status (B6 + C2).
        per_status_obs = {}
        for _m, _t, _k, ep in matches:
            for status_key, group in _endpoint_2xx_groups(ep).items():
                per_status_obs.setdefault(status_key, []).append(group)
        merged = {s: merge_groups(gs) for s, gs in per_status_obs.items()}
        if matches:
            endpoint_samples[ep_key] = {
                s: g.get("json_response_count", 0) for s, g in sorted(merged.items())}

        wildcard_doc = per_status.get("2XX")
        for status_key in sorted(per_status.keys()):
            if status_key == "2XX":
                continue
            doc_fields = per_status[status_key]
            obs = merged.get(status_key)
            if obs is None:
                not_observed.append({"endpoint": ep_key, "status": status_key,
                                     "reason": "documented status not observed in traffic"})
                continue
            _diff_one(ep_key, status_key, obs, doc_fields, findings, threshold)
        if wildcard_doc is not None:
            for status_key, obs in sorted(merged.items()):
                if status_key not in per_status:
                    _diff_one(ep_key, status_key, obs, wildcard_doc, findings, threshold)
        if not matches and not any(k != "2XX" for k in per_status.keys()):
            not_observed.append({"endpoint": ep_key, "status": None,
                                 "reason": "endpoint not observed in traffic"})
        # Observed 2xx statuses with no documented schema (B6): fields
        # surface once as UNDOCUMENTED_FIELD warnings scoped to the status.
        for status_key, obs in sorted(merged.items()):
            if status_key in per_status or wildcard_doc is not None:
                continue
            sample = obs.get("json_response_count", 0)
            for field, o in sorted((obs.get("fields") or {}).items()):
                if field == "" or field.endswith("[]"):
                    continue
                if o.get("present_count", 0) > 0:
                    ev = {"present_count": o["present_count"],
                          "parent_count": o["parent_count"],
                          "presence_rate": o["presence_rate"],
                          "types": o["types"], "status": status_key,
                          "note": "observed status not documented in spec"}
                    f = _finding("UNDOCUMENTED_FIELD", ep_key, field, ev)
                    findings.append(f)
            if sample < LOW_SAMPLE_THRESHOLD:
                for f in findings:
                    if f["endpoint"] == ep_key and f["evidence"].get("status") == status_key:
                        _apply_low_sample(f, sample)

    # Wholly undocumented observed endpoints: ONE collapsed warning (C6).
    spec_keys = set(spec_docs.keys())
    for method, template, ep_key, ep in observed:
        if ep_key in claimed or (method, template) in spec_keys:
            continue
        groups = _endpoint_2xx_groups(ep)
        if not groups:
            continue
        merged_all = merge_groups(list(groups.values()))
        fields_list = sorted(f for f, o in (merged_all.get("fields") or {}).items()
                             if f != "" and not f.endswith("[]")
                             and o.get("present_count", 0) > 0)
        if not fields_list:
            continue  # e.g. a bare 204 endpoint: nothing observed to report
        sample = merged_all.get("json_response_count", 0)
        endpoint_samples.setdefault(ep_key, {
            s: g.get("json_response_count", 0) for s, g in sorted(groups.items())})
        ev = {"endpoint_documented": False, "fields": fields_list,
              "statuses": sorted(groups.keys()),
              "sample_size": sample,
              "note": "wholly undocumented endpoint; per-field findings collapsed into this one"}
        f = _finding("UNDOCUMENTED_FIELD", ep_key, "", ev)
        if sample < LOW_SAMPLE_THRESHOLD:
            _apply_low_sample(f, sample)
        findings.append(f)

    findings.sort(key=lambda f: (f["endpoint"], f["field"], f["code"]))
    return {"findings": findings, "not_observed": not_observed,
            "endpoint_samples": endpoint_samples}


def _apply_low_sample(f, sample):
    f["severity"] = "warning"
    f["evidence"]["low_sample"] = True
    f["evidence"]["sample_size"] = sample


def _diff_one(ep_key, status_key, obs, doc_fields, findings, threshold):
    obs_fields = obs.get("fields") or {}
    total = obs.get("json_response_count", 0)
    local = []
    for field, doc in sorted(doc_fields.items()):
        if "*" in field.split("."):
            continue  # wildcard map docs are consulted via _doc_for, not iterated
        if field == "" or field.endswith("[]"):
            if field in obs_fields:
                _check_type(local, ep_key, field, obs_fields[field], doc)
            continue
        o = obs_fields.get(field)
        parent_path = _parent_path(field)
        parent_field = obs_fields.get(parent_path) if parent_path is not None else None
        if doc.get("required"):
            if o is None:
                # B4: parent array observed but always empty.
                if parent_path and parent_path.endswith("[]") and parent_field is not None \
                        and parent_field.get("arrays_observed", 0) > 0 \
                        and parent_field.get("items_observed", 0) == 0:
                    local.append(_finding("REQUIRED_MISSING", ep_key, field, {
                        "present_count": 0, "parent_count": 0, "presence_rate": 0.0,
                        "threshold": threshold, "status": status_key,
                        "arrays_observed": parent_field["arrays_observed"],
                        "items_observed": 0,
                        "note": "unverifiable-because-empty: the parent array was "
                                "observed but always empty, so this required field "
                                "could not be verified"},
                        severity="warning"))
                elif parent_path == "" or parent_field is not None:
                    # B7: denominator = the parent field's instance count
                    # (its present_count), not the endpoint response count.
                    parent_count = total if parent_path == "" else parent_field["present_count"]
                    local.append(_finding("REQUIRED_MISSING", ep_key, field, {
                        "present_count": 0, "parent_count": parent_count,
                        "presence_rate": 0.0, "threshold": threshold,
                        "status": status_key,
                        "note": f"never observed in {parent_count} parent instance(s)"}))
                # else: parent itself never observed -> no cascade.
            elif o["presence_rate"] < threshold:
                ev = {"present_count": o["present_count"],
                      "parent_count": o["parent_count"],
                      "presence_rate": o["presence_rate"],
                      "threshold": threshold, "status": status_key}
                local.append(_finding("REQUIRED_MISSING", ep_key, field, ev))
        if o is not None:
            _check_type(local, ep_key, field, o, doc)
            _check_enum(local, ep_key, field, o, doc)
    # Undocumented fields.
    for field, o in sorted(obs_fields.items()):
        if field == "" or field.endswith("[]"):
            continue
        if o.get("present_count", 0) <= 0:
            continue
        doc = _doc_for(field, doc_fields)
        if doc is not None:
            # Documented via map wildcard (C3): type/enum-check the value.
            if field not in doc_fields:
                _check_type(local, ep_key, field, o, doc)
                _check_enum(local, ep_key, field, o, doc)
            continue
        if _under_free_parent(field, doc_fields):
            continue  # C4 / additionalProperties: true
        parent_doc = doc_fields.get(_parent_path(field) or "")
        if parent_doc is not None and parent_doc.get("ap_allowed"):
            continue  # C3: additionalProperties schema without flattenable doc
        local.append(_finding("UNDOCUMENTED_FIELD", ep_key, field, {
            "present_count": o["present_count"],
            "parent_count": o["parent_count"],
            "presence_rate": o["presence_rate"],
            "types": o["types"], "status": status_key}))
    if total < LOW_SAMPLE_THRESHOLD:  # C1 sample-size guard
        for f in local:
            _apply_low_sample(f, total)
    findings.extend(local)


def diff_baseline_vs_spec(baseline, spec_docs, threshold=REQUIRED_PRESENCE_THRESHOLD,
                          ignore_endpoints=()):
    """Back-compatible wrapper returning just the findings list."""
    return diff_report(baseline, spec_docs, threshold=threshold,
                       ignore_endpoints=ignore_endpoints)["findings"]


def not_observed_endpoints(baseline, spec_docs, ignore_endpoints=()):
    return diff_report(baseline, spec_docs, ignore_endpoints=ignore_endpoints)["not_observed"]


# ------------------------------------------------------- baseline vs baseline

def _merged_2xx(ep):
    groups = _endpoint_2xx_groups(ep or {})
    if not groups:
        return {}
    return merge_groups(list(groups.values()))


def diff_baselines(old, new, threshold=REQUIRED_PRESENCE_THRESHOLD):
    """Bonus: baseline-vs-baseline diff (same finding codes, adapted).

    2xx groups of each endpoint are merged across exact statuses first
    (baseline v2). Low-sample guard (C1) applies using the newer
    baseline's sample size.
    """
    findings = []
    old_eps = old.get("endpoints") or {}
    new_eps = new.get("endpoints") or {}
    keys = sorted(set(old_eps) | set(new_eps))
    for ep_key in keys:
        og = _merged_2xx(old_eps.get(ep_key))
        ng = _merged_2xx(new_eps.get(ep_key))
        of = og.get("fields") or {}
        nf = ng.get("fields") or {}
        local = []
        for field, o in sorted(nf.items()):
            if field == "" or field.endswith("[]"):
                continue
            old_f = of.get(field)
            if old_f is None:
                if not _field_under_map_parent(field, of, nf):
                    local.append(_finding("UNDOCUMENTED_FIELD", ep_key, field, {
                        "present_count": o["present_count"],
                        "presence_rate": o["presence_rate"], "types": o["types"],
                        "note": "new in newer baseline"}))
                continue
            new_types = [t for t in o["types"] if t not in old_f["types"]
                         and not (t == "null" and old_f.get("nullable"))
                         and not (t == "number" and "integer" in old_f["types"]
                                  and o.get("all_integral", True))]
            if new_types:
                local.append(_finding("TYPE_MISMATCH", ep_key, field, {
                    "observed_types": o["types"], "new_types": new_types,
                    "old_types": old_f["types"]}))
            if not o.get("enum_open") and not old_f.get("enum_open"):
                extra = [v for v in o.get("enum_candidates") or []
                         if v not in (old_f.get("enum_candidates") or [])]
                if extra:
                    local.append(_finding("ENUM_GROWTH", ep_key, field, {
                        "extra_values": extra,
                        "old_enum_candidates": old_f.get("enum_candidates")}))
            elif o.get("enum_open") and not old_f.get("enum_open"):
                local.append(_finding("ENUM_GROWTH", ep_key, field, {
                    "enum_open": True, "note": "field became high-cardinality"}))
        for field, o in sorted(of.items()):
            if field == "" or field.endswith("[]"):
                continue
            n = nf.get(field)
            if o["presence_rate"] >= threshold:
                if n is None or n["presence_rate"] < threshold:
                    local.append(_finding("REQUIRED_MISSING", ep_key, field, {
                        "present_count": (n or {}).get("present_count", 0),
                        "presence_rate": (n or {}).get("presence_rate", 0.0),
                        "old_presence_rate": o["presence_rate"],
                        "threshold": threshold}))
        sample = ng.get("json_response_count", 0)
        if local and sample < LOW_SAMPLE_THRESHOLD:
            for f in local:
                _apply_low_sample(f, sample)
        findings.extend(local)
    findings.sort(key=lambda f: (f["endpoint"], f["field"], f["code"]))
    return findings


def _field_under_map_parent(field, *field_dicts):
    p = _parent_path(field)
    while p:
        for d in field_dicts:
            st = d.get(p)
            if st is not None and st.get("map_keys_open"):
                return True
        p = _parent_path(p)
    return False


# ------------------------------------------------------------- reporting

def summarise(findings):
    breaking = sum(1 for f in findings if f["severity"] == "breaking")
    warning = sum(1 for f in findings if f["severity"] == "warning")
    return {"total": len(findings), "breaking": breaking, "warning": warning}


def _sanitise(text):
    """D4: escape control characters so field/endpoint names cannot forge
    report lines (newlines, CR, tabs, ESC, ...)."""
    out = []
    for ch in str(text):
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


def format_text(findings, title="Living Contract Diff", not_observed=None,
                endpoint_samples=None):
    lines = [title, "=" * len(title)]
    if endpoint_samples:
        lines.append("Endpoint sample sizes (JSON responses per status):")
        for ep_key in sorted(endpoint_samples.keys()):
            parts = ", ".join(f"{s}: {n}" for s, n in sorted(endpoint_samples[ep_key].items()))
            lines.append(f"  {_sanitise(ep_key)}  {parts}")
    if not findings:
        lines.append("No drift found.")
    for f in findings:
        lines.append(f"[{f['severity'].upper()}] {f['code']}  "
                     f"{_sanitise(f['endpoint'])}  field={_sanitise(f['field'] or '<root>')}")
        lines.append(f"    evidence: {_sanitise(repr(f['evidence']))}")
    if not_observed:
        lines.append("Not observed in traffic (not diffed, does not affect exit code):")
        for item in not_observed:
            status = item.get("status")
            suffix = f" status {status}" if status else ""
            lines.append(f"  {_sanitise(item.get('endpoint', ''))}{suffix} — "
                         f"{_sanitise(item.get('reason', ''))}")
    s = summarise(findings)
    lines.append(f"Summary: {s['total']} finding(s), {s['breaking']} breaking, "
                 f"{s['warning']} warning")
    return "\n".join(lines) + "\n"
