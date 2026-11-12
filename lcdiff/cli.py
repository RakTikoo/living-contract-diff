"""CLI: observe / diff / diff-baseline. Exit codes: 0 ok, 1 breaking, 2 error.

Error discipline (D1): baselines are validated strictly at load (structure
and required stat keys); a v1 baseline is rejected with an explicit
regeneration message; ALL unexpected exceptions at the CLI boundary are
mapped to a clean "error: ..." + exit 2 — no tracebacks, ever.
"""
import argparse
import json
import sys

from . import BASELINE_VERSION
from . import har as har_mod
from . import inference, openapi, diff as diff_mod

_FIELD_KEYS = {
    "present_count": int, "parent_count": int,
    "types": list, "type_counts": dict, "nullable": bool,
    "enum_candidates": list, "enum_open": bool, "format_hints": list,
}


def _err(msg):
    print(f"error: {msg}", file=sys.stderr)
    return 2


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_baseline(b, source="<baseline>"):
    """Strict structural validation (D1). Raises IncompleteInputError."""
    E = har_mod.IncompleteInputError
    if not isinstance(b, dict):
        raise E(f"{source}: baseline is not a JSON object")
    version = b.get("version")
    if version != BASELINE_VERSION:
        raise E(
            f"{source}: baseline version {version!r} is not supported "
            f"(expected {BASELINE_VERSION}); regenerate it with "
            f"'lcdiff observe' — baseline v2 groups 2xx responses by exact "
            f"status code and adds per-field integrity fields")
    endpoints = b.get("endpoints")
    if not isinstance(endpoints, dict):
        raise E(f"{source}: baseline 'endpoints' must be an object, "
                f"got {type(endpoints).__name__}")
    for ep_key, ep in endpoints.items():
        if not isinstance(ep, dict):
            raise E(f"{source}: endpoint {ep_key!r} must be an object")
        if not isinstance(ep.get("method"), str) \
                or not isinstance(ep.get("path_template"), str):
            raise E(f"{source}: endpoint {ep_key!r} needs string 'method' and 'path_template'")
        groups = ep.get("status_groups")
        if not isinstance(groups, dict):
            raise E(f"{source}: endpoint {ep_key!r}: 'status_groups' must be an object")
        for gkey, g in groups.items():
            if not isinstance(g, dict):
                raise E(f"{source}: endpoint {ep_key!r} group {gkey!r} must be an object")
            if not _is_int(g.get("response_count")) \
                    or not _is_int(g.get("json_response_count")):
                raise E(f"{source}: endpoint {ep_key!r} group {gkey!r}: "
                        f"'response_count'/'json_response_count' must be integers")
            fields = g.get("fields")
            if not isinstance(fields, dict):
                raise E(f"{source}: endpoint {ep_key!r} group {gkey!r}: 'fields' must be an object")
            for fname, f in fields.items():
                if not isinstance(f, dict):
                    raise E(f"{source}: field {fname!r} of {ep_key!r} must be an object")
                for key, typ in _FIELD_KEYS.items():
                    if key not in f:
                        raise E(f"{source}: field {fname!r} of {ep_key!r} is "
                                f"missing required stat key {key!r}")
                    v = f[key]
                    ok = _is_int(v) if typ is int else isinstance(v, typ)
                    if not ok:
                        raise E(f"{source}: field {fname!r} of {ep_key!r}: "
                                f"stat {key!r} has the wrong type")
                rate = f.get("presence_rate")
                if not (isinstance(rate, (int, float)) and not isinstance(rate, bool)):
                    raise E(f"{source}: field {fname!r} of {ep_key!r}: "
                            f"'presence_rate' must be a number")
    return b


def _load_baseline(path):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise har_mod.IncompleteInputError(f"baseline file not found: {path}")
    except (OSError, UnicodeDecodeError) as e:
        raise har_mod.IncompleteInputError(f"baseline unreadable: {path}: {e}")
    try:
        b = har_mod.loads_no_duplicates(text, source=path)
    except Exception as e:
        raise har_mod.IncompleteInputError(
            f"baseline unreadable/truncated or non-strict JSON: {path}: {e}")
    return validate_baseline(b, source=path)


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, sort_keys=False)
        f.write("\n")


def cmd_observe(args):
    entries, skipped = [], 0
    if args.har:
        e, s = har_mod.load_har(args.har)
        entries += e
        skipped += s
    if args.samples_dir:
        if not args.manifest:
            return _err("--samples-dir requires --manifest")
        e, s = har_mod.load_samples_dir(args.samples_dir, args.manifest)
        entries += e
        skipped += s
    if not args.har and not args.samples_dir:
        return _err("observe needs --har or --samples-dir")
    baseline = inference.build_baseline(entries)
    if args.out:
        try:
            _write_json(args.out, baseline)
        except OSError as e:
            return _err(f"cannot write baseline to {args.out}: {e}")
    else:
        print(json.dumps(baseline, indent=2, ensure_ascii=False))
    n_eps = len(baseline["endpoints"])
    print(f"observed {len(entries)} entries ({skipped} non-JSON skipped) "
          f"-> {n_eps} endpoint(s)", file=sys.stderr)
    return 0


def _emit_report(args, report, findings, title):
    if args.format == "json":
        out = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    else:
        out = diff_mod.format_text(findings, title=title,
                                   not_observed=report.get("not_observed"),
                                   endpoint_samples=report.get("endpoint_samples"))
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(out)
        except OSError as e:
            return _err(f"cannot write report to {args.out}: {e}")
    else:
        sys.stdout.write(out)
    return None


def cmd_diff(args):
    baseline = _load_baseline(args.baseline)
    spec = openapi.load_spec(args.spec)
    docs = openapi.spec_endpoints(spec)
    result = diff_mod.diff_report(baseline, docs, threshold=args.required_threshold,
                                  ignore_endpoints=args.ignore_endpoint or ())
    findings = result["findings"]
    report = {"tool": "living-contract-diff", "findings": findings,
              "not_observed": result["not_observed"],
              "endpoint_samples": result["endpoint_samples"],
              "summary": diff_mod.summarise(findings)}
    err = _emit_report(args, report, findings, "Living Contract Diff")
    if err is not None:
        return err
    return 1 if report["summary"]["breaking"] > 0 else 0


def cmd_diff_baseline(args):
    old = _load_baseline(args.old)
    new = _load_baseline(args.new)
    findings = diff_mod.diff_baselines(old, new, threshold=args.required_threshold)
    report = {"tool": "living-contract-diff", "mode": "baseline-vs-baseline",
              "findings": findings, "summary": diff_mod.summarise(findings)}
    err = _emit_report(args, report, findings, "Baseline vs Baseline Diff")
    if err is not None:
        return err
    return 1 if report["summary"]["breaking"] > 0 else 0


def _threshold(text):
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"threshold must be a number, got {text!r}")
    if not (0 < v <= 1):
        raise argparse.ArgumentTypeError(
            f"required-threshold must satisfy 0 < x <= 1, got {text!r}")
    return v


def main(argv=None):
    p = argparse.ArgumentParser(prog="lcdiff", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    po = sub.add_parser("observe", help="build a statistics-only baseline from traffic")
    po.add_argument("--har", help="HAR file to import")
    po.add_argument("--samples-dir", help="directory of .json response samples")
    po.add_argument("--manifest", help="endpoint manifest JSON for --samples-dir")
    po.add_argument("--out", help="baseline output path (default: stdout)")
    po.set_defaults(fn=cmd_observe)

    pd = sub.add_parser("diff", help="diff a baseline against an OpenAPI 3.x spec (JSON or YAML)")
    pd.add_argument("--baseline", required=True)
    pd.add_argument("--spec", required=True)
    pd.add_argument("--format", choices=["text", "json"], default="text")
    pd.add_argument("--out", help="report output path (default: stdout)")
    pd.add_argument("--required-threshold", type=_threshold,
                    default=REQUIRED_DEFAULT,
                    help="presence rate below which a required field is "
                         "REQUIRED_MISSING (default 0.99; 0 < x <= 1)")
    pd.add_argument("--ignore-endpoint", action="append", default=None,
                    metavar="GLOB",
                    help="exclude endpoints matching this glob (repeatable), "
                         "matched against 'METHOD /path' and the path template")
    pd.set_defaults(fn=cmd_diff)

    pb = sub.add_parser("diff-baseline", help="diff two baselines against each other")
    pb.add_argument("--old", required=True)
    pb.add_argument("--new", required=True)
    pb.add_argument("--format", choices=["text", "json"], default="text")
    pb.add_argument("--out", help="report output path (default: stdout)")
    pb.add_argument("--required-threshold", type=_threshold, default=REQUIRED_DEFAULT,
                    help="presence threshold (default 0.99; 0 < x <= 1)")
    pb.set_defaults(fn=cmd_diff_baseline)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except har_mod.IncompleteInputError as e:
        return _err(str(e))
    except BrokenPipeError:
        return 2
    except Exception as e:  # D1: clean error + exit 2, never a traceback
        return _err(f"unexpected error: {type(e).__name__}: {e}")


REQUIRED_DEFAULT = 0.99

if __name__ == "__main__":
    sys.exit(main())
