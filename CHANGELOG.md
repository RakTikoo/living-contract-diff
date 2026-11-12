# Changelog

All notable changes to Living Contract Diff, newest first. Versions are
tagged on GitHub (`v0.2`, …) and consumers pin them as
`uses: RakTikoo/living-contract-diff@v0.2`.

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
