# Living Contract Diff — Analysis

Date of analysis: **2026-11-10**. Builder standard: Python stdlib only, deterministic, no LLM, fixture corpus first, honest numbers.

## 1. Incumbent re-verification

Searches run 2026-11-10 (web index reads only, no sign-ins):

1. `Optic API GitHub repository archived January 2026 Atlassian`
2. `oasdiff OpenAPI spec vs spec diff tool Keploy record replay API testing`

**Optic — VERIFIED archived.** Multiple independent migration write-ups (dev.to, CodeRifts) agree: the `opticdev/optic` repo was archived **12 Jan 2026**, after Atlassian's April 2024 acquisition; last release v1.0.9 (Aug 2025); the `useoptic.com` domain no longer resolves; the expected Atlassian Compass integration never shipped; no community fork has emerged as successor. The migration guide states for Optic's traffic-capture capability ("OpenAPI generation from test traffic"): *"There is no direct replacement"* — the traffic→spec niche this product occupies is, per these sources, vacant. Consistent with the Round 4 report (`~/workspace/research_notes/daily-use-complex-micro-saas-round4-20261110-2052/report.md`, Candidate 2).

**oasdiff — VERIFIED adjacent, not a competitor to this job.** Actively maintained OSS (Apache 2.0), compares **two OpenAPI specs** (spec-vs-spec breaking-change detection, CLI + GitHub Action). It has no traffic input and cannot detect spec-vs-reality drift ("only as accurate as the spec" — Keploy's Nov 2026 comparison). Complementary, not overlapping.

**Keploy — VERIFIED adjacent, different job.** OSS record/replay: captures real API traffic (plus DB/downstream calls) and generates **regression tests and mocks** from it. It does not infer a contract baseline or diff observed behaviour against an OpenAPI spec.

No code or text was copied from any of these projects; concepts only (infer → baseline → diff is the shape the Round 4 report independently sketched).

## 2. Design decisions

- **Baseline = versioned JSON, not SQLite.** Counts are small; JSON is git-diffable, reviewable in PRs, and portable. (Round 4's guardrail pattern 4: snapshot baselines + diff; pattern 1: local-first plain files.)
- **HAR-first, no proxy in v0** (guardrail: the proxy — TLS, websockets, streaming, auth — is the break-risk; HAR is a stable format).
- **Kernel / leaves:** kernel = inference + merge + diff (`lcdiff/inference.py`, `lcdiff/diff.py`); leaves = input adapters (HAR, samples dir) and the OpenAPI reader. New formats are new leaves, never kernel rewrites.
- **Privacy by construction:** enum candidates capped at 20 distinct values; exceeding the cap drops *all* candidates for that field and sets `enum_open`. High-cardinality fields leak nothing. (Layered further by the fix pass — §10 cluster A — with the residual risk stated honestly in the README.)
- **Threshold:** `REQUIRED_MISSING` fires below presence rate 0.99 (configurable via `--required-threshold`), with the exact rate in the evidence. No cascading: children of a never-observed parent are not separately flagged. A sample-size guard (fix pass C1) downgrades findings from endpoints with <5 observed responses to warnings.
- **Severity / exits:** `REQUIRED_MISSING` + `TYPE_MISMATCH` = breaking (exit 1); `UNDOCUMENTED_FIELD` + `ENUM_GROWTH` = warning; unreadable/truncated/invalid input = exit 2, always explicit, never a clean report, never a traceback.

## 3. Corpus design

Hand-built fixtures in `tests/fixtures/` (HAR + OpenAPI JSON pairs):

| Fixture | Planted truth |
|---|---|
| `clean` | Perfectly matching API → must yield **zero** findings |
| `fault_required_nested` | Required `address.zip` never sent (nested-object shape) |
| `fault_required_array` | Required `items[].sku` never sent (array-item shape) |
| `fault_undocumented_top` | Extra top-level field `nickname` |
| `fault_undocumented_array` | Extra array-item field `items[].discount` |
| `fault_type_top` | `id` sent as string, spec says integer |
| `fault_type_array` | `items[].qty` sent as string, spec says integer |
| `fault_enum_top` | `status` observed `deleted`, spec enum `[active, suspended]` |
| `fault_enum_array` | `items[].state` observed `lost`, spec enum `[packed, shipped]` |
| `sparse` | Optional `coupon` present in exactly 1 of 50 responses → rate 0.02 in baseline, no finding |
| `intfloat_ok` / `intfloat_bad` | `1` accepted for `number`; `1.0` rejected for `integer` |
| `canary` | Unique canary string among 30 distinct values of one field → must appear **nowhere** in baseline |
| `edge_bodies` | 204, empty 200, `text/html` body, a real JSON 200, plus a 500 (grouped separately) → no crash, no findings |
| `refs` | Spec built from `$ref` + `allOf` + a self-referencing schema, traffic matches → zero findings |
| `malformed` / `truncated_body` | Truncated HAR file / truncated JSON body → explicit error, exit 2 |

Each of the four finding types is planted **twice, in two different shapes** (nested object vs array items), per the brief.

## 4. Measured results (2026-11-10, updated after the fix pass in §10)

Test command: `python3 run_tests.py` (equivalently `python3 -m unittest discover -s tests`).

- **Tests: 98 run, 98 passed, 0 failed** (31 original — 4 of them updated for intentional behaviour changes listed in §10 — plus 67 fix-pass regression tests in `tests/test_fixpass.py`). (YAML update 2026-11-11: **170 run, 170 passed** — the YAML test that asserted rejection flipped by design, plus 72 new YAML tests; see §11. YAML fix pass, later the same day: **231 run, 231 passed** — 61 further regression tests in `tests/test_yaml_fixpass.py`; see the §11 fix-pass addendum.)
- **Planted-fault corpus: 8 expected findings, 8 found** — exactly one finding per fault fixture, of the expected code and field, in both shapes per type. No extra findings in any fault fixture. (Severity of the n=1 fixtures is now governed by the C1 sample guard — see §10.)
- **Clean-fixture false positives: 0** (`clean`, `refs`, `sparse`, `intfloat_ok`, `edge_bodies` all yield zero findings).
- **Sparse-field rate:** `coupon` recorded as present 1 / parent 50, presence_rate **0.02** — reported as a rate, not as missing/present.
- **Integer/float discipline:** observed `1` → type `integer` (accepted where spec says `number`); observed `1.0` → type `number`, now *accepted* where spec says `integer` because it is integral (C7); observed `1.5` → TYPE_MISMATCH where spec says `integer`.
- **Canary test: PASS.** The string `CANARY-UNIQUE-7f3a9d-DO-NOT-LEAK` (and every other value of that field) appears nowhere in the serialised baseline; the field is recorded as `enum_open: true`, `enum_candidates: []`, types/counts only. Boundary verified: exactly 20 distinct values → candidates kept; 21 → `enum_open`, candidates dropped. The fix pass extended canary coverage to stderr and diff output (A5).
- **Error discipline:** malformed HAR → exit 2; truncated JSON body → exit 2 with an `INCOMPLETE:` message; missing file → exit 2; malformed/hand-edited baselines → exit 2 with no traceback (D1). (YAML specs were exit-2-rejected at this point; they are supported since the 2026-11-11 YAML update — §11 — with unsupported YAML constructs exiting 2 by the same discipline.)

## 5. Real-world run (labelled)

Materials in `examples/real_world/`. `server.py` is a real fixture API on Python's stdlib `http.server`; `record.py` **actually started that server locally and captured its real HTTP responses** (3× `GET /products/{id}` 200, `GET /health` 200, `GET /products/99` 404) into `traffic.har`. `api.openapi.json` documents that API with **one deliberate drift**: it marks `sku` as a required product field, which the server never returns.

Pipeline run 2026-11-10 (outputs saved in `examples/real_world/`: `baseline.json`, `diff_report.txt`, `diff_report.json`). **Re-run after the fix pass (baseline regenerated at format v2; the old v1 baseline was first correctly rejected with the explicit regeneration message):**

```
Endpoint sample sizes (JSON responses per status):
  GET /health  200: 1
  GET /products/{id}  200: 3
[WARNING] REQUIRED_MISSING  GET /products/{id}  field=sku
    evidence: {'present_count': 0, 'parent_count': 3, 'presence_rate': 0.0, 'threshold': 0.99, 'status': '200', 'note': 'never observed in 3 parent instance(s)', 'low_sample': True, 'sample_size': 3}
Summary: 1 finding(s), 0 breaking, 1 warning   (exit 0)
```

**The 1 deliberate drift is still found (planted drift recall 1/1; hand-labelled precision 1/1).** Its severity changed from breaking to warning *by design*: the C1 sample-size guard downgrades all findings for endpoints with <5 observed responses, and this capture has only 3 product responses. With ≥5 product responses the same drift would be breaking again (exit 1, as in the original run). No other findings — `currency` enum (USD/EUR), `price` as number, `stock` integer, and the 404 group (recorded in the baseline, correctly excluded from the 2xx diff) all behaved as designed. The server is a small hand-written fixture, honestly labelled as such — it is a real executed capture, not a production API, and no external project's traffic was used (no suitable local project with an OpenAPI JSON + sample responses was found in `~/workspace`).

## 6. Known limitations

- **YAML specs: subset only** — YAML OpenAPI is supported since 2026-11-11 via the stdlib-only subset parser in `lcdiff/yamlmini.py` (§11); anchors/aliases/merge keys/tags, multi-document files and the other constructs listed there are rejected with named errors, and YAML scalar typing follows the YAML 1.2 core schema (deliberate divergences from PyYAML's YAML 1.1 are listed in §11).
- **Sparse fieldsets (`?fields=`) cannot be detected from a HAR** — request query parameters are not part of the observed contract, so fieldset-dependent responses blend into one endpoint's presence rates. Documented prominently in the README; the mitigation is separate captures per fieldset.
- **Local `$ref`s only**; remote/file refs exit 2. `allOf` merging is now deep (D6); `oneOf`/`anyOf` are modelled as a union approximation (C5); `not`, discriminators, and pattern properties are not modelled.
- **`$ref` fan-out expansion cost (pre-existing, formats alike):** `openapi.resolve_schema` materialises cross-referenced schemas multiplicatively; a reviewer's synthetic 500×500-ref spec produced ~250k materialised fields in ~4 s. It affects JSON and YAML specs identically, is bounded by the schema-depth cap for recursive shapes, and has no separate expansion budget yet — documented, deliberately not fixed in the YAML fix pass (§11 addendum).
- **Privacy residuals (honest):** the enum-candidate feature stores low-cardinality values by design; the sensitive-name denylist and 64-char cap are heuristics, not guarantees; the candidate cap is union-checked across status groups *of one endpoint* but **not across endpoints**; short/pure-letter URL segments (slugs, usernames) can persist in path templates even though token-like segments now collapse.
- The baseline file may stay slug-fragmented; aggregation happens at diff time (C2). Two different spec endpoints whose templates both segment-match one baseline endpoint share it by exact-match-first, then first-claim — an approximation for genuinely ambiguous routing.
- Multiple JSON media types per response (e.g. `application/json` vs a vendor type) are not separated; the first JSON media schema per status is used.
- Presence denominators assume one structural shape per path; a field path reused with different parent shapes merges their counts.
- Duplicate keys in HAR bodies are last-wins (JSON-parser behaviour), counted into a baseline `warnings` note; duplicate detection beyond counting was judged disproportionate for bodies (they are *specs/baselines* where duplicates are a hard error).
- Enum candidates include boolean/integer/float values of low-cardinality fields by design (they are contract values, not payloads); teams that consider even those sensitive should treat baselines as internal artefacts.
- Baseline-vs-baseline diff is a bonus with adapted semantics (new field = `UNDOCUMENTED_FIELD`, vanished high-rate field = `REQUIRED_MISSING`); it has lighter test coverage (2 tests) than the spec diff.

## 7. Expansion ladder (kernel untouched)

1. ~~YAML spec reader (leaf module beside `openapi.py`)~~ — **DONE 2026-11-11** (§11): `lcdiff/yamlmini.py`, a new leaf exactly as this ladder prescribed; the kernel was not touched.
2. Live proxy / framework middleware capture → same baseline (new leaf beside `har.py`).
3. CI mode polish: accepted-drift ledger (blessed findings suppressed by endpoint+field+code), drift history across baselines.
4. More spec surface: `anyOf`/`oneOf`, request-body inference (separate privacy review), status-specific schemas.
5. Team layer (later, per Round 4): shared baselines per service, third-party API watching.

## 8. GitHub integration story

- Commit `baseline.json` + `openapi.json` to the API repo.
- PRs run the diff (`action.yml` composite action, or `examples/workflow.yml` with no marketplace dependency): exit 1 fails the check on **new breaking drift**, exit 2 fails on unreadable input, and the full text report is appended to `GITHUB_STEP_SUMMARY`.
- Updating the baseline is an explicit, reviewable commit (`observe` from fresh recorded traffic) — the baseline diff *is* the contract-change review.

## 10. Adversarial review & fix pass (2026-11-10)

Three independent reviews of v0 (red team, false-positive hunter, code auditor) produced ~30 findings in five clusters. All were fixed in code with a regression test each, or explicitly documented as limitations. **Tests: 31 → 98, all green** (`python3 run_tests.py`). Baseline format bumped **v1 → v2** (per-exact-status 2xx grouping); v1 baselines are rejected with an explicit regeneration message, verified against the real-world artefact before regenerating it.

### A. Privacy — before: the "statistics only" claim was false in five ways; after: layered caps

| # | Before | After |
|---|---|---|
| A1 | Any field with ≤20 distinct values stored values verbatim, no length cap (a 20M-char value was copied whole); secrets in small captures leaked | String candidates >64 chars never stored (still count toward cardinality); sensitive-named fields (`password/token/secret/api-key/auth/session/cookie/credential`, final segment) never store candidates; README now states the residual honestly — heuristics, not a guarantee |
| A2 | Cap was per status group: splitting 30 values across statuses of one endpoint leaked 30/30 | Union of candidates re-checked per field across all status groups of the endpoint; >20 drops candidates in *all* groups. Cross-*endpoint* split documented as a limitation |
| A3 | Map keys became field paths verbatim, uncapped (10k keys stored) | Per parent object, >50 distinct child keys → the object becomes a map: no per-key subfields, parent records `map_keys_open` + key count; diff emits no per-key findings under maps |
| A4 | Only numeric/UUID path segments templated; `/reset/<token>` persisted verbatim | Token-like segments (≥20 chars, letters+digits or hex/base64url-ish), decimals and 32-hex also collapse; 4-digit years (1900–2100) explicitly preserved |
| A5 | INCOMPLETE errors printed the untemplated path | All error messages use the templated path; canary test extended to stderr **and** diff output |

### B. Inference/diff correctness

| # | Before | After |
|---|---|---|
| B1 | `True` and `1` collided as one dict key (cap bypass, order-dependent baselines); floats never became enum candidates | Enum storage keyed by `(json_type, canonical repr)`; numbers are candidates; deterministic `(type, repr)` ordering |
| B2 | Array-item field presence used object-items only as denominator (`items[].sku` showed 1/1 when 1 of 3 items was an object) | Denominator = total items observed at that path; evidence shows the true counts |
| B3 | Branching recursive `$ref` (3 self-properties) hung >20 s — the documented ancestry check was never implemented | Ancestry placeholder `{type: object}` + memoisation; 2/3/4 self-properties now resolve in **<1 ms** each (timing regression tests) |
| B4 | `{"items": []}` always → spec-required `items[].sku` silently passed, exit 0 | REQUIRED_MISSING at **warning** severity, worded *unverifiable-because-empty* with `arrays_observed`/`items_observed` evidence (severity choice documented: absence of items is not proof of absence of the field) |
| B5 | Zero-traffic spec endpoints fabricated REQUIRED_MISSING with `parent_count 0` | Not diffed; listed in a `not_observed` report section (text + JSON), no exit-code effect |
| B6 | Inference merged all 2xx; diff checked only the first documented 2xx → async/creation APIs false-failed | Baseline v2 groups 2xx by exact status; diff is per-status; undocumented observed 2xx statuses surface their fields once as status-scoped warnings |
| B7 | Never-observed nested fields used the endpoint response count as `parent_count` | Denominator is the parent field's instance count (e.g. `address.zip`: 2 when only 2 of 6 responses had an `address`) |
| B8 | `enum_open` ENUM_GROWTH fired even when the spec enum was larger than the provable lower bound | Fires only when `len(spec_enum) < 21` (enum_open implies ≥21 distinct) |

### C. False-positive structure

| # | Before | After |
|---|---|---|
| C1 | n=1–3 baselines produced breaking findings; the fixed 0.99 threshold flipped CI at 98/100 with no recourse | Endpoints (per status) with <5 JSON responses: all findings downgraded to warning + `low_sample` evidence; per-endpoint sample sizes in the report header/JSON; `--required-threshold` option (validated `0<x≤1`); sparse-fieldsets (`?fields=`) limitation documented prominently (undetectable from HAR) |
| C2 | Slugs never collapsed → N phantom endpoints, 2N+ findings, phantom REQUIRED_MISSING against `/articles/{slug}` | Diff-side segment matching merges all baseline endpoints matching one spec endpoint before diffing; baseline file may stay fragmented (documented) |
| C3 | Every map key of an FX-style response flagged (18 warnings for one response) | `additionalProperties` (true or schema) suppresses per-key findings; schema-typed map values are type-checked via a `.*` wildcard doc |
| C4 | Free-form objects flagged every key | OpenAPI default semantics: no-properties objects allow extra keys; objects *with* documented properties keep the extra-key warnings (documented rule) |
| C5 | `oneOf`/`anyOf` ignored → every variant field flagged | Union approximation: properties = union, required = required in all variants, types = union |
| C6 | Health/metrics/OPTIONS spam per-field warnings | Wholly undocumented endpoints collapse to ONE warning listing their fields; repeatable `--ignore-endpoint <glob>` |
| C7 | Spec `integer` receiving `1.0` was breaking | Inference tracks `all_integral`; integral floats satisfy `integer` (documented deviation); mixed integral+non-integral still mismatches |
| C8 | One bad instance in 100 was indistinguishable from 100% bad | Exactly 1 mismatched instance in ≥20 observations → warning with count/rate evidence |

### D. Error discipline & robustness

| # | Before | After |
|---|---|---|
| D1 | Tracebacks + exit 1 on malformed baselines, bad `--out` paths, non-string `$ref`s, etc.; `{"endpoints": []}` fabricated findings | Strict baseline validation at load (structure + required stat keys) → INCOMPLETE; **all** unexpected exceptions map to clean `error: …` + exit 2 at the CLI boundary; no fabricated findings |
| D2 | Invalid base64 JSON body silently became "no body" (exit 0); per-entry exceptions swallowed | Undecodable/invalid JSON-typed bodies are INCOMPLETE; per-entry structural errors are collected into the INCOMPLETE report |
| D3 | `action.yml` could not run in a consumer repo (`lcdiff` not on the module path) | `PYTHONPATH=${{ github.action_path }}`, no `cd`, inputs stay consumer-relative; verified by a local consumer-workspace simulation test (copied tree, foreign cwd, env var only) |
| D4 | Field names with newlines forged report lines | Control characters escaped in all names printed in text reports |
| D5 | `NaN`/`Infinity` accepted; duplicate keys silent | `parse_constant` rejection everywhere; duplicates in specs/baselines → exit 2; in HAR bodies → last-wins **counted** into a baseline `warnings` note (full body-duplicate detection judged disproportionate; documented) |
| D6 | `allOf` merge replaced same-named properties wholesale; no `trace`; loose spec validation | Deep-merge of property schemas (scalar conflicts: later wins, documented); `trace` whitelisted; openapi version must match `^3\.\d+(\.\d+)?$` and paths must be non-empty, else exit 2 |
| D7 | Date regex unanchored at the end, no range checks; a bare date was hinted `date-time` | Fully anchored + range-validated (month/day/hour/min/sec); bare date → hint `date` |

### E. Test quality

- E1: `test_yaml_spec_rejected_exit_2` was **vacuous** (it diffed against a nonexistent baseline, so the YAML was never read) — rewritten to build a real baseline first.
- E2: added coverage for missing/remote/non-string `$ref` rejection, `--samples-dir`/`--manifest`, OpenAPI 3.1 type-list nullables, branching refs (B3), bool/int enum collision (B1), mixed arrays (B2), malformed-baseline exit codes (D1), and INCOMPLETE message content incl. templated paths (A5).

### Intentional behaviour changes (original tests updated, each noted in place)

1. Baseline group keys `"2xx"` → exact `"200"/"201"/…` (B6) — several baseline-inspection tests updated.
2. `test_required_missing_nested`: severity breaking → warning (C1 low-sample, n=1 fixture).
3. `test_float_for_integer_is_mismatch`: `1.0` for spec `integer` is now accepted (C7); the mismatch case moved to a new `1.5` regression test.
4. `test_cli_clean_exit_0_and_breaking_exit_1`: the n=1 fault fixture now exits 0 (C1); a new inline 6-response case preserves the breaking exit-1 assertion.
5. Real-world example (§5): the deliberate drift is still found, now as a low-sample warning; artefacts regenerated at baseline v2.

## 9. Break-risk guardrails (from Round 4, applied)

- Snapshot-baseline architecture bounds data scale by construction (counts, not payloads).
- No proxy, no GUI, no sync, no accounts in v0 — CLI first, files only.
- Deterministic kernel, stdlib only: nothing in the core can be broken by a dependency or a model change.
- One input format done properly (HAR) + one spec format (OpenAPI JSON) until the kernel is proven; every addition is a leaf. (Superseded 2026-11-11: a second spec format, YAML, was added as a leaf once the kernel was proven — §11. The guardrail's shape held: kernel untouched.)

## 11. YAML spec support (2026-11-11)

The §7 expansion ladder's first rung, built as prescribed: a new leaf module `lcdiff/yamlmini.py` (stdlib only, no third-party imports — PyYAML is used only inside tests as an oracle, and a hygiene test pins that nothing under `lcdiff/` imports it) that converts YAML text into the same Python structures `json.loads` produces. `lcdiff/openapi.py:load_spec` routes by extension (`.json` / `.yaml` / `.yml`) and by content sniffing when the extension is missing or ambiguous (skip BOM/whitespace/comment lines; leading `{` → JSON, otherwise YAML). The JSON path is byte-for-byte unchanged (same parser, same errors — only the stale "YAML specs are not supported" clause was removed from its error string). YAML errors become `IncompleteInputError` with file + line context → CLI exit 2, no tracebacks, no fabricated findings. Downstream (`$ref`, `allOf`, diff) untouched; version stays 0.2.0.

**Subset supported** (target: FastAPI / Swagger / Springdoc / hand-written specs): block mappings & sequences by indentation, nesting, compact sequences of mappings (`- key: value`), indentless sequences under a key; flow mappings/sequences incl. nesting and flow↔block mixing, multi-line flow; plain, single-quoted (`''` escaping) and double-quoted scalars with the full YAML escape table; block scalars `|`/`>` with chomping (`+`/`-`) and explicit indent indicators; comments at comment position only; multi-line plain scalars (fold with a space); quoted keys; keys/values containing colons not followed by a space (URLs), braces, and `#` not at comment position; one leading `---`; a trailing `...` is allowed. Duplicate mapping keys are rejected at every depth, block and flow, exactly as strict as the JSON path — collision judged the way Python dict storage judges it (fix-pass addendum below): keys equal as numbers are duplicates (`true:`/`True:`, `1:`/`1.0:`/`true:` pairwise, `-0.0:`/`0.0:`), while a string stays distinct from a numerically equal number (`1:`/`'1':` distinct).

**Scalar-typing decisions** (each pinned by a test in `tests/test_yaml.py::ScalarTyping`):

| Input | Result | Rationale |
|---|---|---|
| `openapi: 3.1` | float `3.1` | identical to `json.loads("3.1")` |
| `version: 3.10` | float `3.1` | mirrors JSON/PyYAML; equivalence with the JSON form is the contract — no invented string preservation |
| `1.0` | float `1.0` | distinct from int `1` (LCD's int/float discipline depends on it) |
| `1e3` | float `1000.0` | YAML 1.2 core + JSON agree (PyYAML 1.1 keeps it a string — deliberate divergence) |
| `2026-01-01` | string | no date objects are ever constructed |
| `012` | string `"012"` | YAML 1.2 core (PyYAML 1.1 reads octal 10 — deliberate divergence) |
| `0x1F` / `0o17` | int 31 / 15 | YAML 1.2 core (PyYAML 1.1 lacks `0o` — deliberate divergence) |
| `1:30` | string | sexagesimal not in the 1.2 core schema (PyYAML 1.1 gives 90 — deliberate divergence) |
| `true/True/TRUE` (+ false spellings) | bool | YAML 1.2 core spellings ONLY (fix pass: arbitrary casings such as `tRuE` are strings) |
| `yes/no/on/off` | strings | never booleans (1.2 dropped the 1.1 spellings) |
| `null/Null/NULL/~/empty` | None | YAML 1.2 core |
| `.inf` / `-.inf` / `.nan` | floats | mirrors PyYAML; JSON cannot express them, and in a YAML spec they are compared as ordinary floats — never silently stringified or dropped |

**Anchors / aliases / merge keys / tags: REJECTED with named errors** (the §4-style decision, documented in README + module docstring). Chosen over bounded support: generator output does not need them, rejection is deterministic, and an alias ("billion laughs") bomb is impossible when nothing is ever expanded — the bomb shape fails in <1 ms with the anchor error. Also named-error rejections: multi-document files, tab indentation, and indentation that cannot be resolved deterministically (with line number).

**Safety limits (enforced, tested):** document ≤ 32 MiB, nesting depth ≤ 200 (identical boundary in block and flow syntax after the fix pass: 200 levels allowed, 201 errors), total nodes ≤ 2,000,000 with honest counting (fix pass: every mapping key and value, sequence item and scalar — including empty/null values and block scalars — counts, on block, flow and compact paths; `_split_key` probes no longer burn the budget). Adversarial probes measured 2026-11-11: alias bomb → named error in <1 ms; 100k-deep flow → depth error in 44 ms; 5,000-deep block nesting → depth error in 91 ms. A 2,000-path flow-style spec parses comfortably (a 10k-path spec is far below the node cap). Fix-pass re-verification: a 2.5M empty-valued-key document (23.9 MB) that previously parsed *despite* the 2M cap now fails with the node-limit error.

**Equivalence results:** every `tests/fixtures/*.openapi.json` spec — **14 of 14** (canary, clean, edge_bodies, fault_enum_array, fault_enum_top, fault_required_array, fault_required_nested, fault_type_array, fault_type_top, fault_undocumented_array, fault_undocumented_top, intfloat, refs, sparse) — has a YAML twin in `tests/fixtures/*.openapi.yaml` (generated by dumping the JSON, then verified three ways: typed-canonical equality of the parsed structures, PyYAML-oracle equality, and full CLI pipeline parity). For each pair, the same HAR → baseline → diff run produces **identical JSON reports and identical exit codes (14/14)**. Note the commissioning brief said "12 fixtures" naming 13; the directory actually contains 14 spec fixtures — all 14 were twinned. One hand-written generator-style fixture (`handwritten_style.openapi.yaml`: block-scalar descriptions, flow style, quoted `'200'`, compact tag mappings) diffs the clean HAR to zero findings with `spec_endpoints` identical to the JSON fixture's. A seeded differential fuzz against PyYAML (3,600 dumps across block/flow styles and wrap widths during development; a 60-document seeded version is in the suite) found and fixed three real bugs before shipping: nested `- - -` compact sequences misread as a mapping, apostrophes inside flow plain scalars treated as quote openers, and line breaks inside flow plain scalars not folded. Real-world check: `examples/real_world/api.openapi.yaml` is the YAML twin of `api.openapi.json`; the full pipeline (traffic.har → baseline → diff) produces byte-identical reports for both specs (`diff_report_yaml.txt` / `diff_report_yaml.json` are left in place beside the JSON ones), same single low-sample `REQUIRED_MISSING sku` warning, exit 0.

**Tests: 98 → 170** (`python3 run_tests.py`): 97 original tests unchanged, the one YAML test flipped by design (`test_yaml_spec_rejected_exit_2` → `test_yaml_spec_loads_and_unsupported_rejected`), and 72 new tests in `tests/test_yaml.py` (14 equivalence + scalar typing + syntax features + named rejections + limits + CLI/sniffing discipline + fuzz regressions + package hygiene).

**Remaining YAML limitations:** no anchors/aliases/merge keys/tags, directives or multi-document files (named errors above); explicit `?` complex mapping keys are rejected in block and flow alike; non-scalar (flow collection) mapping keys spanning multiple lines are not supported. None of these appear in generator-produced OpenAPI YAML we have seen; each fails loudly rather than mis-parsing.

### §11 addendum — YAML adversarial review & fix pass (2026-11-11, later)

Three independent reviewers (red team, false-positive hunter, code auditor) attacked `yamlmini.py` after it shipped; 15 findings (1 critical, 4 high, 5 medium, a low bundle, 2 false positives, an auditor item and a nice-to-have) were **all fixed**, each with a regression test in `tests/test_yaml_fixpass.py` (**tests: 170 → 231, all green**; equivalence corpus re-run: **14/14** identical reports/exit codes; version stays 0.2.0; JSON path behaviour unchanged except where noted in item 2). Before → after:

| # | Before | After |
|---|---|---|
| 1 CRIT | Empty sequence items swallowed siblings: `-\n-\n-\n` → `[[[None]]]`; 250 bare `-` items hit a spurious depth error | Empty items are `None` siblings (`[None, None, None]`); an empty item's value can only be a more-indented block — the indentless-sequence rule now applies to mapping values only. Mapping-value indentless sequences (`key:` + `- a`) unchanged |
| 2 HIGH | Response-code smuggling: YAML `200:` (int) + `'200':` (str) both survived parsing; `str()` normalisation in `spec_endpoints` let one silently win and could flip a 6-finding diff to "No drift" | Normalisation collisions (response codes incl. `2XX`/`2xx`, and method keys after case-folding) raise `IncompleteInputError` naming the duplicate → CLI exit 2. The int/str form cannot arise from JSON (string keys; exact duplicates already rejected at parse), so ordinary JSON behaviour is unchanged; the wildcard case-variant form is the same ambiguity in either format and is rejected too |
| 3 HIGH | `1:`+`true:`, `1:`+`1.0:`, `-0.0:`+`0.0:` (incl. flow) silently overwrote | The int/float/bool family is one duplicate-identity space judged by numeric equality (dict-storage reality) → named duplicate-key errors. `1:` vs `'1':` remain distinct; all prior rejections (0x10/16, 1_000/1000, null/~, bool spellings) kept |
| 4 HIGH | Folded `>` scalars lost a `\n` around blank lines next to more-indented lines, manufacturing drift (an ENUM_GROWTH vs the JSON twin) | YAML 1.2 §8.1.3 folding implemented exactly (more-indented lines never fold; a blank run of k → k newlines, k+1 next to a more-indented line; leading blanks preserved). Validated against PyYAML on a 13-case oracle matrix + end-to-end vs the JSON twin |
| 5 HIGH | Quadratic rescan in flow/quoted collection: reviewers measured 29.4 s for a 4000-entry multi-line flow map (58 KB) and 7.2 s for a 2000-line unterminated quote | Incremental scanners carry state across lines (O(total size)). Re-measured on this VM: 4000-entry flow map **17.9 s → 0.045 s** (this VM's pre-fix reproduction), 2000-line unterminated quote **1.65 s → 0.005 s**. Timing-bounded regression tests pin both shapes |
| 6 MED | Node cap was dishonest: keys, empty values and block scalars uncounted — a 2.5M empty-key doc (23.9 MB) parsed under a 2M cap; `_split_key` probes burned ~10 nodes on `{a: 1}: v` | Every mapping entry (key+value), sequence item and scalar counts on all paths; probes parse with counting disabled and splits are memoised per line (probe burn on `{a: 1}: v` is now 0). The 2.5M-key doc now fails with the node-limit error |
| 7 MED | Raw `ValueError` escapes: >4300-digit integers (dec/hex/oct, value or key), `\U` beyond U+10FFFF, lone-surrogate escapes accepted | All become named `YamlMiniError`s (digit length checked *before* conversion; escape range/surrogate validated in the escape parser) and surface via `load_spec` as exit 2 with a clean message — verified via the CLI, no tracebacks |
| 8 MED | U+2028 / U+2029 / NEL (U+0085) inside a document were swallowed into scalars | Normalised to line breaks at input splitting (like CR), in all contexts: `"a: 1\u2028b: 2"` → `{a: 1, b: 2}` |
| 9 MED | Explicit keys in flow silently mis-parsed: `{? k: 1}` → `{'? k': 1}` | A `?` token starting a flow key/item raises the same named explicit-key error as block |
| 10 MED | Plain scalars containing `: ` accepted in block context (`a: b: c` → `'b: c'`; PyYAML errors) | Named "mapping values are not allowed here" error for `: `/`:\t` inside, or a trailing `:`, in block plain scalars (value, sequence-item and continuation positions). Colons without following separation (URLs, `12:34:56`) and quoted scalars unaffected; full suite + equivalence corpus are the guard |
| 11 LOW bundle | (a) `%YAML` produced a misleading "multiple documents" error; (b) `... foo` silently dropped the junk; (c) keep-chomped all-blank scalars returned `''`; (d) `@`/`` ` ``-initial plain scalars accepted; (e) booleans accepted in any casing; (f) tab as separation (`a:\t1`) accepted | (a) directives named as unsupported; (b) named error (a trailing comment stays legal); (c) `\|+`/`>+` over n blank lines returns n newlines, clip/strip `''` (oracle-verified); (d) reserved-indicator named error; (e) only the six YAML 1.2 core spellings are bools; (f) tab-as-separation named error (tabs only inside block-scalar content, per item 12) |
| 12 FP | Tabs inside block-scalar *content* triggered the "tab in indentation" error (PyYAML accepts) | The global pre-scan is gone; the tab check lives in `_peek`, which structural lines pass through and block-scalar content never does. Tab-leading content lines also count as more-indented for folding, matching the oracle. Structural tab indentation still rejected |
| 13 FP | `version: '1'# comment` (comment glued to a quoted scalar) errored; PyYAML parses it | Glued `#` comments are allowed after quoted scalars in block contexts (mapping values and sequence items); flow context unchanged (both parsers reject there) |
| 14 AUD | Depth accounting differed by syntax: block allowed 200 levels, flow errored at 200 | One convention everywhere (depth = enclosing collections; levels 1–200 allowed, 201 errors), pinned by boundary tests for block/flow × map/seq |
| 15 NICE | Non-UTF-8 spec files got a bare codec error | `load_spec` exit-2 message for non-JSON files now hints the file must be UTF-8 (the UTF-16-saved-YAML case) |

**Explicitly not fixed (documented, by decision):** the `$ref` fan-out expansion cost in `openapi.resolve_schema` (pre-existing, affects JSON identically — §6); anchors/aliases/merge keys/`!!str` remain named errors; the BOM+JSON sniffing asymmetry remains the JSON loader's pre-existing behaviour.
