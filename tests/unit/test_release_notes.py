"""scripts/release_notes.py: the release workflow's CHANGELOG extraction."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "release_notes.py"
_spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
assert _spec is not None and _spec.loader is not None
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)

CHANGELOG = """# 0.2.0 (2026-10-01)
- Batched verification.

- Docs.
# 0.1.0 (2026-09-16)
- Initial release.
# 0.0.9
"""


def test_section_extracts_one_version_without_its_heading():
    assert release_notes.section(CHANGELOG, "0.2.0") == "- Batched verification.\n\n- Docs.\n"
    assert release_notes.section(CHANGELOG, "0.1.0") == "- Initial release.\n"


@pytest.mark.parametrize(
    ("version", "message"), [("0.3.0", "no section"), ("0.0.9", "is empty"), ("0.1", "no section")]
)
def test_section_refuses_missing_or_empty_notes(version, message):
    with pytest.raises(SystemExit, match=message):
        release_notes.section(CHANGELOG, version)


def test_script_reads_the_repository_changelog_for_a_tag(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "v0.1.0", str(changelog)], capture_output=True, text=True, check=True
    )
    assert result.stdout == "- Initial release.\n"


def test_current_version_has_release_notes():
    import dbt_refmerge

    changelog = (Path(__file__).parents[2] / "CHANGELOG.md").read_text(encoding="utf-8")
    assert release_notes.section(changelog, dbt_refmerge.__version__)
