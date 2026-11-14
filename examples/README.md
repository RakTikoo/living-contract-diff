# Examples

One worked example, end to end, plus a CI workflow. Everything in
`real_world/` is real: the server was executed, its actual responses
were recorded, and the reports below are the tool's real output on that
recording.

## `real_world/` — the worked example

| File | What it is |
|---|---|
| `server.py` | A tiny fixture API (Python standard library only): `GET /health` and `GET /products/{id}` for three hard-coded products. It never returns a `sku` field. |
| `record.py` | Starts `server.py` on a local port, issues real HTTP requests (`/products/1`, `/products/2`, `/products/3`, `/health`, `/products/99`), and writes the actual responses into `traffic.har`. No invented payloads. |
| `traffic.har` | The recorded traffic — the five real responses from `record.py`'s run. |
| `api.openapi.json` | The OpenAPI spec for the same API, in JSON. It declares `sku` as a required field of a product — which the server never sends. |
| `api.openapi.yaml` | The same spec in YAML. Both formats must produce identical reports, and do. |
| `baseline.json` | The statistics-only baseline built from `traffic.har` by the `observe` step (counts and types only — never payload values). |
| `diff_report.txt` / `diff_report.json` | The diff of the baseline against the JSON spec: one finding, `[WARNING] REQUIRED_MISSING GET /products/{id} field=sku`. |
| `diff_report_yaml.txt` / `diff_report_yaml.json` | The same diff against the YAML spec — identical findings, proving the two spec formats behave the same. |

## How the pieces follow from each other

```bash
# 1. (Already done for you — this is how traffic.har was made:)
python3 real_world/record.py

# 2. Build the baseline from the recording:
python3 -m lcdiff observe --har real_world/traffic.har --out real_world/baseline.json

# 3. Diff the baseline against the spec (either format):
python3 -m lcdiff diff --baseline real_world/baseline.json --spec real_world/api.openapi.json
python3 -m lcdiff diff --baseline real_world/baseline.json --spec real_world/api.openapi.yaml
```

## `workflow.yml`

A self-contained GitHub Actions workflow that needs no marketplace
action: it checks out this tool, runs observe-free CI diffing, fails on
new breaking drift (exit 1), and writes the report to
`GITHUB_STEP_SUMMARY`.
