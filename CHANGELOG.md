# Changelog

All notable changes to Living Contract Diff, newest first. Versions are
tagged on GitHub (`v0.2`, …) and consumers pin them as
`uses: RakTikoo/living-contract-diff@v0.2`.

## v0.3.1 — 2026-11-20

- **Packaging.** A `pyproject.toml` makes the tool pip-installable
  (`pip install git+https://github.com/RakTikoo/living-contract-diff`,
  or pipx) and adds an `lcdiff` console command, equivalent to
  `python3 -m lcdiff`. Running from a clone is unchanged.
- **Library API.** The pipeline functions are now re-exported from the
  top-level `lcdiff` package (`load_har`, `build_baseline`, `load_spec`,
  `spec_endpoints`, `diff_report`, `diff_baselines`, `summarise`,
  `format_text`, and the ledger helpers), with a "Use as a library"
  README section and a worked example. No behaviour change.
- **Pre-commit hook.** A `.pre-commit-hooks.yaml` ships the diff as a
  pre-commit hook (fails on breaking drift); see the README.
- **Docs.** README gains an Install section on the first screen, and
  the GitHub Action example now pins `@v0.3`.
- **Docs.** README test count corrected (239 → 241, as measured by
  `run_tests.py`); README gains the DeepWiki badge and the
  `npx skills add` agent-install line; `CITATION.cff` /
  `codemeta.json` citation metadata added. This release exists so
  the release tag contains the packaging and docs above — the
  v0.3 tag predates them. (Version style: the package has always
  carried the three-part `0.3.0`; the tags used the two-part
  `v0.3`. This release standardises on the three-part `v0.3.1`.)

## v0.3 — 2026-11-14

- **Accepted-drift ledger.** `diff` and `diff-baseline` accept
  `--ledger FILE`: findings whose (endpoint, code, field) identity is
  recorded in the ledger are reported in an "Accepted drift" section
  (JSON: under `accepted`, with the recorded reason) and never affect
  the exit code; new drift is reported as loudly as before, and
  removing an entry makes its finding reappear. A new `accept`
  command records every finding of a JSON diff report into a ledger
  with a `--reason` (extending an existing ledger with `--ledger`).
  Ledgers are strictly validated at load — wrong version, duplicate
  identity, unknown key, or an empty reason is a clean exit-2 error.
  Tests 231 → 239.

## v0.2 — 2026-11-11

First public release.

- **YAML specs.** OpenAPI specs can now be JSON or YAML. The YAML reader
  (`lcdiff/yamlmini.py`) is standard-library only, like the rest of the
  tool: block and flow collections, block scalars, quoted scalars with
  escapes, comments, and YAML 1.2 core typing. A 14-spec equivalence
  corpus proves every YAML spec produces a report identical to its JSON
  twin.
- **Named errors, never silent mis-parses.** YAML features outside the
  supported subset — anchors/aliases, merge keys, custom tags,
  multi-document files, `%YAML`/`%TAG` directives, tabs in structural
  indentation, explicit `?` keys — are rejected with an error that names
  the feature and the fix. Size (32 MiB), depth (200) and node-count
  limits are enforced and tested.
- **Baseline drift detection.** Record a statistics-only baseline from a
  HAR file (types, status codes, field presence — never payload values),
  then diff an OpenAPI 3.x spec against it: added/removed endpoints,
  status-code drift, type mismatches and missing fields, each labelled
  BREAKING or non-breaking with a matching exit code.
- Test suite: 231 tests, all green, including a three-reviewer
  adversarial pass on the YAML reader (empty-sequence re-nesting,
  response-code key smuggling, folded-scalar newlines and quadratic
  rescans were all caught in review and fixed before release).
- Verified end-to-end by a stranger run: a separate dogfood repo runs
  `uses: RakTikoo/living-contract-diff@v0.2` in GitHub Actions against a
  clean YAML spec (passes) and a planted-drift spec (fails with the
  expected BREAKING finding).
