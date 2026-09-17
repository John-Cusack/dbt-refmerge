# dbt-refmerge documentation

- [Command-line reference](cli.md): commands, options and exit codes.
- [Configuration](configuration.md): `.dbt-refmerge.toml`, `DBT_REFMERGE_*` variables, precedence and adapters.
- [Verification](verification.md): what `check` creates on the warehouse, the privileges it needs, and its limits.
- [Selecting only needed columns](column-pruning.md): import column pruning, supported SQL and performance expectations.
- [Reason codes](reason-codes.md): every code, what it means and what to do about it.
- [JSON output](json-output.md): the documents `scan`, `check`, `fix` and `cleanup` print.
- [Integrations](integrations.md): the pre-commit hook, the GitHub Action and other CI systems.
