"""The user documentation in docs/ can't drift from the code: every code, command, option and key is covered."""

import re
from pathlib import Path

import pytest
import typer

from dbt_refmerge.adapters import ADAPTER_SPECS
from dbt_refmerge.cli import app
from dbt_refmerge.config import AppConfig
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.reporting import JSON_SCHEMA_VERSION

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"
REPO_BLOB = "https://github.com/John-Cusack/dbt-refmerge/blob/main/"


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def _headings(text: str, level: str) -> list[str]:
    return re.findall(rf"^{level} (.+)$", text, flags=re.MULTILINE)


def test_every_reason_code_is_documented_once_and_nothing_else_is():
    documented = _headings(_read("reason-codes.md"), "###")

    assert sorted(documented) == sorted(code.value for code in ReasonCode)
    assert len(documented) == len(set(documented))


def test_every_command_and_option_is_in_the_cli_reference():
    text = _read("cli.md")
    group = typer.main.get_command(app)
    sections = _headings(text, "##")

    for name, command in group.commands.items():
        assert name in sections, f"no section for {name}"
        for param in command.params:
            for opt in param.opts:
                if opt.startswith("--") and opt != "--help":
                    assert f"`{opt}" in text or f" {opt}" in text, f"{name} {opt} is undocumented"
    assert "--version" in text


def test_every_setting_and_adapter_is_in_the_configuration_page():
    text = _read("configuration.md")

    for field in AppConfig.model_fields:
        if field != "project_dir":
            assert f"| `{field}` |" in text, f"setting {field} is undocumented"
    for adapter in ADAPTER_SPECS:
        assert f"`{adapter}`" in text, f"adapter {adapter} is undocumented"


def test_the_json_page_names_the_current_schema_version():
    assert f'"schema_version": "{JSON_SCHEMA_VERSION}"' in _read("json-output.md")


def _slug(heading: str) -> str:
    # GitHub's anchors: lower case, punctuation other than hyphens and spaces dropped, spaces to hyphens.
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


@pytest.mark.parametrize("page", sorted([*DOCS.glob("*.md"), REPO / "README.md"]), ids=lambda p: p.name)
def test_relative_links_point_at_existing_pages_and_headings(page):
    for target in re.findall(r"\]\(([^)\s]+)\)", page.read_text(encoding="utf-8")):
        # The README is also PyPI's project page, so it links to this repository's files by absolute URL.
        in_repo = target.startswith(REPO_BLOB)
        if re.match(r"[a-z]+:", target) and not in_repo:
            continue
        path_part, _, anchor = target.removeprefix(REPO_BLOB).partition("#")
        base = REPO if in_repo else page.parent
        linked = (base / path_part).resolve() if path_part else page
        assert linked.exists(), f"{page.name} links to missing {target}"
        if anchor:
            slugs = {_slug(h) for h in re.findall(r"^#+ (.+)$", linked.read_text(encoding="utf-8"), re.MULTILINE)}
            assert anchor in slugs, f"{page.name} links to missing heading {target}"
