"""``python -m dbt_refmerge``: the ``dbt-refmerge`` command, for environments without console scripts on PATH."""

from dbt_refmerge.cli import app

if __name__ == "__main__":
    app(prog_name="dbt-refmerge")
