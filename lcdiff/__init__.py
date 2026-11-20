"""Living Contract Diff — learn what an API actually returns, diff vs OpenAPI."""
__version__ = "0.3.1"
# Baseline format v2: 2xx responses are grouped by EXACT status code
# ("200", "201", ...) instead of a single merged "2xx" group. v1 baselines
# are rejected at load with an explicit regeneration message.
BASELINE_VERSION = 2
ENUM_CAP = 20
ENUM_VALUE_MAX_LEN = 64
MAP_KEY_CAP = 50
REQUIRED_PRESENCE_THRESHOLD = 0.99
LOW_SAMPLE_THRESHOLD = 5
MAX_DEPTH = 32

# ---------------------------------------------------------------------------
# Public library API. The CLI is a thin wrapper over these functions:
#   observe  ->  load_har / load_samples_dir + build_baseline
#   diff     ->  load_spec + spec_endpoints, then diff_report
#   accept   ->  load_ledger / apply_ledger / ledger_from_findings
# They are re-exported here so callers can simply ``import lcdiff``.
# (The imports sit at the bottom on purpose: the submodules read the
# constants above from this package while they are being imported.)
from .diff import (diff_baseline_vs_spec, diff_baselines, diff_report,
                   format_text, summarise)
from .har import (Entry, IncompleteInputError, load_har, load_samples_dir,
                  parse_har_text)
from .inference import build_baseline
from .ledger import apply_ledger, ledger_from_findings, load_ledger
from .openapi import load_spec, spec_endpoints

__all__ = [
    "__version__",
    "BASELINE_VERSION",
    "ENUM_CAP",
    "ENUM_VALUE_MAX_LEN",
    "MAP_KEY_CAP",
    "REQUIRED_PRESENCE_THRESHOLD",
    "LOW_SAMPLE_THRESHOLD",
    "MAX_DEPTH",
    # observe
    "Entry",
    "IncompleteInputError",
    "parse_har_text",
    "load_har",
    "load_samples_dir",
    "build_baseline",
    # diff
    "load_spec",
    "spec_endpoints",
    "diff_report",
    "diff_baseline_vs_spec",
    "diff_baselines",
    "summarise",
    "format_text",
    # accepted-drift ledger
    "load_ledger",
    "apply_ledger",
    "ledger_from_findings",
]
