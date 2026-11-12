"""Living Contract Diff — learn what an API actually returns, diff vs OpenAPI."""
__version__ = "0.2.0"
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
