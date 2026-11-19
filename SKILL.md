---
name: living-contract-diff
description: Detect drift between what an API actually returns and what its OpenAPI spec promises. Builds a statistics-only baseline from recorded traffic (a HAR file), then diffs it against an OpenAPI 3.x spec (JSON or YAML) to report required fields the API never sends, type mismatches, undocumented fields and enum growth. Use when asked to check API contract drift, compare a spec against real responses, audit an OpenAPI spec against recorded traffic, or gate API changes in CI.
license: MIT
compatibility: Requires Python 3.9+ and pip. Fully offline; traffic comes from a HAR file you supply — there is no live proxy.
---

# Living Contract Diff

## What this tool does

Living Contract Diff learns what an API actually returns from recorded
traffic, then diffs that observed contract against the declared
OpenAPI spec. `observe` reads a HAR file and writes a baseline of
statistics only — presence counts, type unions, nullability — never
payload values (one designed exception: fields with at most 20 distinct
values store those candidate values, because they are the contract).
`diff` compares a baseline with the spec and reports drift. Nothing
from a HAR or a spec is ever executed.

## Install

```bash
pip install git+https://github.com/RakTikoo/living-contract-diff
```

This provides the `lcdiff` command (standard library only).

## Run

Build a baseline from recorded traffic, then diff it against the spec:

```bash
lcdiff observe --har traffic.har --out baseline.json
lcdiff diff --baseline baseline.json --spec openapi.json
```

Machine-readable report: add `--format json` to `diff`. The spec may
be YAML (`.yaml`/`.yml`) instead of JSON. `lcdiff diff-baseline --old
old.json --new new.json` compares two baselines with the same codes.

## Reading the results

Finding codes: `REQUIRED_MISSING` and `TYPE_MISMATCH` are breaking;
`UNDOCUMENTED_FIELD` and `ENUM_GROWTH` are warnings. Every finding
names the endpoint, the field, and the evidence counts. Endpoints or
statuses in the spec with no observed traffic are listed as
`not_observed` — reported, never turned into findings.

Exit codes: `0` — no breaking drift (warnings may still be present).
`1` — breaking drift found. `2` — usage or tool error: an unreadable,
malformed or invalid input always exits 2 with an `error: …` message,
never a traceback and never a clean report.

Sample-size guard: an endpoint (per status) with fewer than 5 observed
JSON responses has all its findings downgraded to warnings, flagged
`low_sample` in the evidence — a tiny capture cannot break your build.
The JSON report carries `findings`, `not_observed`,
`endpoint_samples`, `accepted`, `summary` and `tool`.

## Limits

- HAR is the only traffic source in this version — no live proxy or
  traffic interception, no request-body inference, no GraphQL/gRPC.
- Privacy is heuristic, not a guarantee: a short secret in a
  low-cardinality, innocuously named field *will* be stored as an enum
  candidate. Treat baselines as internal artefacts when traffic may
  contain sensitive values. (Fields named like passwords/tokens are
  denylisted, and anything over 20 distinct values stores no values.)
- Sparse fieldsets (`?fields=id,name`) cannot be detected from a HAR;
  mixed subsets blend into meaningless presence rates.
- YAML specs: a documented subset only — anchors, aliases, merge keys,
  custom tags and multi-document files are rejected with a named
  error. Local `$ref`s only; remote refs exit 2.
- Baselines are format version 2; a v1 baseline is rejected at load
  with a message telling you to regenerate it, never silently misread.
