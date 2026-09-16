# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub's private vulnerability reporting](https://github.com/John-Cusack/dbt-refmerge/security/advisories/new),
not in public issues. Include the dbt-refmerge version, the dbt and adapter versions, and the smallest model
or project that shows the problem. You should get a first response within a week.

## What counts

dbt-refmerge rewrites model files only after proving the rewrite on your warehouse, so these are security
issues, not just bugs:

- **A false proof:** `check` reports `snapshot_equivalent` (or `fix` writes) for a merge that changes
  the model's rows or column types.
- **A write outside the target:** `fix` changes anything other than the one model file it names, or
  writes when a precondition failed.
- **Scratch objects outside the boundary:** verification creates, alters or drops anything that is not
  one of its own views in the scratch schema.
- **Injection:** compiled SQL, manifest fields, profile names or dbt output reaching Jinja, SQL, file paths
  or command arguments unescaped.
- **Secret leakage:** credentials from profiles or the environment appearing in output, JSON reports or
  error messages.

## Supported versions

Fixes are released for the latest minor version. Releases are published from GitHub Actions through
PyPI Trusted Publishing, with PEP 740 attestations and a CycloneDX SBOM attached to each GitHub release.
