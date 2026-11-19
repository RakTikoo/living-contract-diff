# Security policy

Living Contract Diff reads recorded traffic (HAR files) and OpenAPI
specs — both are untrusted input, often captured from real systems. A
malformed or hostile HAR or spec that can crash the tool, hang it, or
make it store payload values the baseline is designed never to store
defeats the tool's purpose.

## Reporting a vulnerability

Please report security issues **privately**, not as a public issue:

- Use GitHub's private vulnerability reporting for this repository:
  **Security → Report a vulnerability** on
  https://github.com/RakTikoo/living-contract-diff
  (GitHub Security Advisories). This keeps the report visible only to
  the reporter and the maintainer until a fix is available.

If private vulnerability reporting is not available to you, open a
minimal public issue saying you have a security report and asking for
a private contact route — do not post the details in the issue.

Please include, as far as you can:

- the Living Contract Diff version and your Python version,
- a minimal HAR or spec file (with real payload values removed) that
  triggers the issue,
- what you expected to happen and what actually happened.

Do **not** include real credentials, tokens, or anyone else's private
data in a report — a synthetic file that reproduces the problem is
enough.

## Supported versions

Living Contract Diff is pre-1.0. Only the **latest release on the
`main` branch** is supported with security fixes; older checkouts
should be updated rather than patched.
