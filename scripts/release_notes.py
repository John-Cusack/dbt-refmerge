"""Print the CHANGELOG.md section for a release tag; used by the release workflow.

    python scripts/release_notes.py v0.1.0 [CHANGELOG.md]

Sections start with a level-1 heading naming the version, optionally followed by a date:
``# 0.1.0 (2026-09-16)``. Exits non-zero when the section is missing or empty, so a tag cannot be
released without notes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def section(changelog: str, version: str) -> str:
    lines = changelog.splitlines()
    heading = re.compile(rf"# {re.escape(version)}(\s.*)?")
    start = next((i for i, line in enumerate(lines) if heading.fullmatch(line)), None)
    if start is None:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("# ")), len(lines))
    body = "\n".join(lines[start + 1 : end]).strip()
    if not body:
        raise SystemExit(f"CHANGELOG.md section for {version} is empty")
    return body + "\n"


def main(argv: list[str]) -> None:
    tag = argv[1]
    changelog = Path(argv[2] if len(argv) > 2 else "CHANGELOG.md")
    sys.stdout.write(section(changelog.read_text(encoding="utf-8"), tag.removeprefix("v")))


if __name__ == "__main__":
    main(sys.argv)
