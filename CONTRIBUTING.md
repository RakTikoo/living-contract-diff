# Contributing to Living Contract Diff

Thanks for your interest. Living Contract Diff compares what an API
actually returns with what its spec promises, and it is deliberately
conservative: a drift report people can't trust is worse than no report.
Contributions that protect that trust are very welcome.

## The rules of the house

1. **Standard library only.** The tool must run on a stock Python 3.9+
   install with zero dependencies, no network access, no account and no
   token. PRs that add a dependency will be declined. The YAML reader is
   hand-written (`lcdiff/yamlmini.py`) for exactly this reason — please
   don't propose swapping in PyYAML.
2. **Statistics only, never payloads.** Baselines record types, status
   codes and field presence rates. Payload values are never stored,
   printed or compared. This is a hard invariant.
3. **Deterministic.** Same HAR + same spec, same report, byte for byte.
   No clocks, no randomness, no environment-dependent behaviour.
4. **Named errors, never silent mis-parses.** If a spec uses something
   the tool doesn't support (for example YAML anchors), it must fail
   with an error that names the feature — never guess and produce a
   plausible-looking wrong report.
5. **Equivalence is sacred.** JSON and YAML forms of the same spec must
   produce identical reports. Changes to spec loading come with
   equivalence-corpus cases proving it.

## Getting started

```bash
python3 run_tests.py   # the full suite (231 tests, stdlib unittest)
```

Run the suite before and after your change; it must be green both
times. New behaviour starts as a fixture: a spec/HAR pair under `tests/`
(or the equivalence corpus for loader work) that fails before your
change and passes after, plus trap cases — inputs that look similar but
must **not** change verdicts.

Issues labelled `good first issue` are scoped to be finishable in one
sitting, with the checks spelled out in the issue itself.
