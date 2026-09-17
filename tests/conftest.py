"""Shared fixtures: environment isolation, project builder, fake dbt, fault injection, Postgres.

Lanes (see TEST_COVERAGE_PLAN.md §4): the default run is the in-memory unit lane;
``-m fake_dbt`` runs dbt-refmerge against ``tests/fakes/fake_dbt.py`` in a real
subprocess; ``-m warehouse`` needs Postgres via ``REFMERGE_TEST_PG_DSN``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

FAKE_DBT = Path(__file__).parent / "fakes" / "fake_dbt.py"

# Read before any fixture runs: isolated_env scrubs the environment per test.
_PG_DSN = os.environ.get("REFMERGE_TEST_PG_DSN", "")
_REQUIRE_WAREHOUSE = os.environ.get("REQUIRE_WAREHOUSE") == "1"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests off the developer's real config, profiles and temp directory."""
    for name in list(os.environ):
        if name.startswith("DBT_REFMERGE_") or name in (
            "DBT_PROFILES_DIR",
            "FAKE_DBT_MODE",
            "FAKE_DBT_ARGV_LOG",
            "GITHUB_WORKSPACE",  # set in CI; scan --format github would name files relative to it
        ):
            monkeypatch.delenv(name)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("tmp")))


ProjectBuilder = Callable[..., Path]


@pytest.fixture
def make_project(tmp_path: Path) -> ProjectBuilder:
    """Build a minimal dbt project: ``make_project({"models/m.sql": "..."})``."""

    def build(files: dict[str, str | bytes], *, name: str = "p", profile: str = "p") -> Path:
        root = tmp_path / "project"
        root.mkdir(exist_ok=True)
        # newline="": files hold exactly the given bytes on every platform (no \r\n on Windows).
        (root / "dbt_project.yml").write_text(f"name: {name}\nprofile: {profile}\n", encoding="utf-8", newline="")
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8", newline="")
        return root

    return build


@dataclass
class FakeDbt:
    """Handle for tests/fakes/fake_dbt.py; modes are passed through the environment."""

    monkeypatch: pytest.MonkeyPatch
    argv_log: Path
    command: tuple[str, ...] = (sys.executable, str(FAKE_DBT))

    def set_mode(self, *modes: str) -> None:
        self.monkeypatch.setenv("FAKE_DBT_MODE", ",".join(modes))

    def calls(self) -> list[list[str]]:
        import json

        if not self.argv_log.is_file():
            return []
        return [json.loads(line) for line in self.argv_log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def fake_dbt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDbt:
    log = tmp_path / "fake_dbt_argv.jsonl"
    monkeypatch.setenv("FAKE_DBT_ARGV_LOG", str(log))
    return FakeDbt(monkeypatch=monkeypatch, argv_log=log)


# -- fault injection ---------------------------------------------------------------------------------
#
# sys.addaudithook hooks cannot be removed, so one dispatcher is installed per process and tests
# register handlers through the ``faults`` fixture. Handlers run for audit events such as ``open``,
# ``os.chmod``, ``os.rename`` (raised by os.replace), ``os.remove`` and ``fcntl.flock``; raising
# inside a handler makes the audited operation raise.

AuditHandler = Callable[[tuple[Any, ...]], None]
_HANDLERS: list[tuple[str, AuditHandler]] = []
_HOOK_STATE = threading.local()
_HOOK_INSTALLED = False


def _dispatch(event: str, args: tuple[Any, ...]) -> None:
    if not _HANDLERS or getattr(_HOOK_STATE, "active", False):
        return
    matches = [handler for name, handler in _HANDLERS if name == event]
    if not matches:
        return
    _HOOK_STATE.active = True
    try:
        for handler in matches:
            handler(args)
    finally:
        _HOOK_STATE.active = False


@dataclass
class Faults:
    registered: list[tuple[str, AuditHandler]] = field(default_factory=list)

    def on(self, event: str, handler: AuditHandler, *, once: bool = False) -> None:
        """Run ``handler(args)`` on each audit ``event`` (only the first time when ``once``)."""
        fired = False

        def wrapped(args: tuple[Any, ...]) -> None:
            nonlocal fired
            if once and fired:
                return
            fired = True
            handler(args)

        entry = (event, wrapped)
        _HANDLERS.append(entry)
        self.registered.append(entry)

    def clear(self) -> None:
        for entry in self.registered:
            _HANDLERS.remove(entry)
        self.registered.clear()


@pytest.fixture
def faults() -> Iterator[Faults]:
    global _HOOK_INSTALLED
    if not _HOOK_INSTALLED:
        sys.addaudithook(_dispatch)
        _HOOK_INSTALLED = True
    handle = Faults()
    try:
        yield handle
    finally:
        handle.clear()


# -- warehouse ---------------------------------------------------------------------------------------


def require_pg_dsn() -> str:
    """The warehouse DSN, or skip (fail when REQUIRE_WAREHOUSE=1 so CI cannot skip silently)."""
    if not _PG_DSN:
        if _REQUIRE_WAREHOUSE:
            pytest.fail("REQUIRE_WAREHOUSE=1 but REFMERGE_TEST_PG_DSN is not set")
        pytest.skip("set REFMERGE_TEST_PG_DSN to run warehouse tests")
    return _PG_DSN


def find_dbt() -> str:
    """The dbt installed next to the running interpreter (the `integration` extra)."""
    import shutil

    found = shutil.which("dbt", path=str(Path(sys.executable).parent))
    if found is None:
        pytest.fail("dbt is not installed next to this interpreter; install the `integration` extra")
    return found


def drop_pg_schemas(dsn: str, names: list[str]) -> None:
    import psycopg2

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for name in names:
                cur.execute(f'drop schema if exists "{name}" cascade')
    finally:
        conn.close()


@pytest.fixture
def pg_dsn() -> str:
    return require_pg_dsn()


@pytest.fixture
def pg_schemas(pg_dsn: str) -> Iterator[Callable[[str], str]]:
    """Unique schema names (``pg_schemas("model")``); all dropped with cascade after the test."""
    names: list[str] = []

    def make(purpose: str) -> str:
        name = f"refmerge_it_{purpose}_{uuid.uuid4().hex[:8]}"
        names.append(name)
        return name

    yield make
    drop_pg_schemas(pg_dsn, names)


@pytest.fixture
def dbt_executable() -> str:
    return find_dbt()


def write_pg_profiles(directory: Path, dsn: str, *, profile: str, schema: str) -> Path:
    """profiles.yml for ``profile`` pointing at ``dsn`` with ``schema`` as the target schema."""
    from urllib.parse import urlparse

    url = urlparse(dsn)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profiles.yml").write_text(
        f"{profile}:\n"
        "  target: it\n"
        "  outputs:\n"
        "    it:\n"
        "      type: postgres\n"
        f"      host: {url.hostname}\n"
        f"      port: {url.port or 5432}\n"
        f"      user: {url.username}\n"
        f"      password: {url.password or ''}\n"
        f"      dbname: {url.path.lstrip('/')}\n"
        f"      schema: {schema}\n"
        "      threads: 1\n",
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def scratch_schema(pg_dsn: str) -> Iterator[str]:
    """A unique, empty schema dropped (cascade) after the test, independent of the tool's cleanup."""
    import psycopg2

    name = f"refmerge_it_{uuid.uuid4().hex[:8]}"
    conn = psycopg2.connect(pg_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'create schema "{name}"')
        yield name
    finally:
        with conn.cursor() as cur:
            cur.execute(f'drop schema if exists "{name}" cascade')
        conn.close()


# -- GitHub workflow commands ------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowCommand:
    command: str
    properties: dict[str, str]
    message: str


def parse_workflow_command(line: str) -> WorkflowCommand:
    """Parse ``::command key=value,...::message`` the way the runner does, undoing the toolkit's escaping."""

    def unescape(text: str) -> str:
        for escaped, char in (("%0D", "\r"), ("%0A", "\n"), ("%3A", ":"), ("%2C", ","), ("%25", "%")):
            text = text.replace(escaped, char)
        return text

    assert line.startswith("::"), line
    head, message = line[2:].split("::", 1)
    command, _, raw_properties = head.partition(" ")
    properties = dict(item.split("=", 1) for item in raw_properties.split(",")) if raw_properties else {}
    return WorkflowCommand(command, {key: unescape(value) for key, value in properties.items()}, unescape(message))
