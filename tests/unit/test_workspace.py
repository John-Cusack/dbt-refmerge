"""RunWorkspace: project snapshot, package digest, ledger and cleanup."""

import hashlib
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from dbt_refmerge.domain import IdentifierIdentity, ReasonCode, RelationIdentity
from dbt_refmerge.errors import CleanupError, RefmergeError
from dbt_refmerge.verification.harness import ledger_relations
from dbt_refmerge.workspace import RunWorkspace, ScratchObject

WORKSPACE_ENTRIES = ["artifacts", "candidate_project", "harness_project", "run-ledger.json", "source_snapshot"]


def _files(root):
    """Every file under ``root`` (symlinks are not followed) as ``{posix relative path: bytes}``."""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            assert not path.is_symlink(), path
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def _symlink(link, target, *, directory=False):
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")


def _snapshot_error(root, ws=None):
    ws = ws or RunWorkspace.create()
    with pytest.raises(RefmergeError) as exc_info:
        ws.snapshot_project(root)
    assert list(ws.candidate_project.iterdir()) == []
    return exc_info.value


def _relation(name):
    return RelationIdentity(IdentifierIdentity("db"), IdentifierIdentity("scratch"), IdentifierIdentity(name))


# -- create ------------------------------------------------------------------------------------------


def test_create_lays_out_an_empty_workspace_under_the_temp_dir():
    ws = RunWorkspace.create()

    assert ws.root.parent == Path(tempfile.gettempdir())
    assert ws.root.name.startswith("dbt_refmerge_")
    assert re.fullmatch(r"\d{8}T\d{6}_[0-9a-f]{12}", ws.run_id)
    assert sorted(p.name for p in ws.root.iterdir()) == WORKSPACE_ENTRIES
    assert ws.load_ledger() == {"objects": [], "run_id": ws.run_id}


# -- snapshot ----------------------------------------------------------------------------------------


def test_snapshot_copies_project_byte_exact_into_both_trees(make_project):
    root = make_project(
        {
            "models/a.sql": "select 1 as id\n",
            "seeds/raw.csv": b"id,name\r\n1,\xff\x00\n",
            "macros/nested/deep/m.sql": "{% macro m() %}1{% endmacro %}\n",
            ".sqlfluff": "[sqlfluff]\n",
        }
    )
    ws = RunWorkspace.create()

    result = ws.snapshot_project(root)

    expected = _files(root)
    assert sorted(expected) == [
        ".sqlfluff",
        "dbt_project.yml",
        "macros/nested/deep/m.sql",
        "models/a.sql",
        "seeds/raw.csv",
    ]
    assert _files(ws.source_snapshot) == expected
    assert _files(ws.candidate_project) == expected
    assert (result.root, result.file_count) == (ws.source_snapshot, 5)


def test_snapshot_tree_hash_is_stable_and_tracks_content_and_paths(make_project):
    root = make_project({"models/a.sql": "select 1\n"})

    def tree_hash():
        return RunWorkspace.create().snapshot_project(root).tree_hash

    first = tree_hash()
    assert tree_hash() == first
    (root / "target").mkdir()
    (root / "target" / "manifest.json").write_text("{}")
    assert tree_hash() == first
    (root / "models" / "a.sql").write_text("select 2\n")
    edited = tree_hash()
    (root / "models" / "a.sql").rename(root / "models" / "b.sql")
    renamed = tree_hash()
    assert len({first, edited, renamed}) == 3


def test_snapshot_prunes_runtime_dirs_only_at_the_project_root(make_project):
    # §2.3: pruning at every depth silently dropped models under e.g. models/logs/.
    root = make_project(
        {
            "models/a.sql": "select 1\n",
            "target/manifest.json": "{}",
            "logs/dbt.log": "log\n",
            ".git/HEAD": "ref: refs/heads/main\n",
            "node_modules/pkg/index.js": "x\n",
            "__pycache__/m.cpython-312.pyc": b"\x00",
            "models/logs/log_events.sql": "select 2\n",
            "models/target/targets.sql": "select 3\n",
            "models/.git/HEAD": "vendored\n",
            "models/node_modules/n.sql": "select 4\n",
            "models/__pycache__/m.cpython-312.pyc": b"\x00",
        }
    )
    ws = RunWorkspace.create()

    result = ws.snapshot_project(root)

    assert sorted(_files(ws.source_snapshot)) == [
        "dbt_project.yml",
        "models/.git/HEAD",
        "models/a.sql",
        "models/logs/log_events.sql",
        "models/node_modules/n.sql",
        "models/target/targets.sql",
    ]
    assert result.file_count == 6


def test_snapshot_includes_installed_dbt_packages(make_project):
    # §2.3: compiling the snapshot needs the installed packages; dbt_packages used to be pruned.
    root = make_project(
        {
            "packages.yml": "packages:\n  - package: dbt-labs/dbt_utils\n",
            "dbt_packages/dbt_utils/dbt_project.yml": "name: dbt_utils\n",
            "dbt_packages/dbt_utils/macros/star.sql": "{% macro star() %}*{% endmacro %}\n",
        }
    )
    ws = RunWorkspace.create()

    ws.snapshot_project(root)

    assert sorted(_files(ws.candidate_project)) == [
        "dbt_packages/dbt_utils/dbt_project.yml",
        "dbt_packages/dbt_utils/macros/star.sql",
        "dbt_project.yml",
        "packages.yml",
    ]
    assert _files(ws.source_snapshot) == _files(root)


@pytest.mark.parametrize("where", ["inside", "outside"])
def test_snapshot_refuses_symlinked_directory_outside_dbt_packages(make_project, tmp_path, where):
    # os.walk never descends into a symlinked directory, so its files silently vanished.
    root = make_project({"shared/m.sql": "select 1\n"})
    target = root / "shared" if where == "inside" else tmp_path / "elsewhere"
    target.mkdir(exist_ok=True)
    _symlink(root / "models" / "shared", target, directory=True)

    error = _snapshot_error(root)

    assert error.reason_code is ReasonCode.INTERNAL_ERROR
    assert error.message == f"linked directory in project is not supported: {root.resolve() / 'models' / 'shared'}"


def test_snapshot_copies_local_packages_that_dbt_deps_linked(make_project, tmp_path):
    # `dbt deps` installs a `local:` package as a symlink in dbt_packages; compiling the snapshot needs it.
    root = make_project({"models/a.sql": "select 1\n"})
    package = tmp_path / "shared_package"
    for rel, content in {
        "dbt_project.yml": "name: shared\n",
        "macros/m.sql": "{% macro m() %}1{% endmacro %}\n",
        "target/manifest.json": "{}",
        "dbt_packages/nested/x.sql": "select 2\n",
        "macros/__pycache__/c.pyc": "x",
    }.items():
        (package / rel).parent.mkdir(parents=True, exist_ok=True)
        (package / rel).write_text(content, newline="")
    _symlink(root / "dbt_packages" / "shared", package, directory=True)
    ws = RunWorkspace.create()

    result = ws.snapshot_project(root)

    snapshot = _files(ws.source_snapshot)
    assert sorted(snapshot) == [
        "dbt_packages/shared/dbt_project.yml",
        "dbt_packages/shared/macros/m.sql",
        "dbt_project.yml",
        "models/a.sql",
    ]
    assert snapshot["dbt_packages/shared/macros/m.sql"] == b"{% macro m() %}1{% endmacro %}\n"
    assert result.file_count == 4
    assert _files(ws.candidate_project) == snapshot


@pytest.mark.parametrize("case", ["ancestor", "project", "dangling", "nested-link", "file-link-escapes"])
def test_snapshot_refuses_unsafe_local_package_links(make_project, tmp_path, case):
    root = make_project({"models/a.sql": "select 1\n"})
    package = tmp_path / "shared_package"
    (package / "macros").mkdir(parents=True)
    target = {
        "ancestor": tmp_path,
        "project": root,
        "dangling": tmp_path / "missing",
    }
    if case == "nested-link":
        _symlink(package / "macros" / "more", tmp_path, directory=True)
    if case == "file-link-escapes":
        _symlink(package / "macros" / "secret.sql", root / "models" / "a.sql")
    _symlink(root / "dbt_packages" / "shared", target.get(case, package), directory=True)

    error = _snapshot_error(root)

    assert error.reason_code is ReasonCode.INTERNAL_ERROR


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
def test_snapshot_refuses_directory_junction(make_project):
    import _winapi

    root = make_project({"shared/m.sql": "select 1\n"})
    (root / "models").mkdir()
    link = root / "models" / "shared"
    _winapi.CreateJunction(str(root / "shared"), str(link))

    error = _snapshot_error(root)

    assert error.reason_code is ReasonCode.INTERNAL_ERROR
    assert error.message == f"linked directory in project is not supported: {root.resolve() / 'models' / 'shared'}"


def test_snapshot_ignores_symlinks_inside_pruned_runtime_dirs(make_project, tmp_path):
    root = make_project({"models/a.sql": "select 1\n"})
    elsewhere = tmp_path / "fast_disk_target"
    elsewhere.mkdir()
    _symlink(root / "target", elsewhere, directory=True)
    ws = RunWorkspace.create()

    ws.snapshot_project(root)

    assert sorted(_files(ws.source_snapshot)) == ["dbt_project.yml", "models/a.sql"]


def test_snapshot_reads_through_in_project_file_symlink(make_project):
    root = make_project({"models/a.sql": "select 1\n"})
    _symlink(root / "models" / "alias.sql", Path("a.sql"))
    ws = RunWorkspace.create()

    ws.snapshot_project(root)

    assert _files(ws.source_snapshot) == {
        "dbt_project.yml": (root / "dbt_project.yml").read_bytes(),
        "models/a.sql": b"select 1\n",
        "models/alias.sql": b"select 1\n",
    }


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("outside", "symlink escapes project"),
        ("dangling", "unsafe symlink"),
        ("loop", "unsafe symlink"),
    ],
)
def test_snapshot_refuses_unsafe_file_symlink(make_project, tmp_path, case, message):
    root = make_project({})
    link = root / "models" / "m.sql"
    if case == "outside":
        secret = tmp_path / "secret.txt"
        secret.write_text("password\n")
        _symlink(link, secret)
    elif case == "dangling":
        _symlink(link, Path("missing.sql"))
    else:
        _symlink(link, Path("m.sql"))

    error = _snapshot_error(root)

    assert error.reason_code is ReasonCode.INTERNAL_ERROR
    assert error.message == f"{message}: {root.resolve() / 'models' / 'm.sql'}"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
@pytest.mark.parametrize("through_symlink", [False, True])
def test_snapshot_refuses_fifo(make_project, through_symlink):
    root = make_project({})
    if through_symlink:
        # The FIFO sits in the pruned target/ directory, so only the link to it is walked.
        (root / "target").mkdir()
        os.mkfifo(root / "target" / "pipe")
        rel = "models/pipe.sql"
        _symlink(root / rel, Path("..") / "target" / "pipe")
    else:
        rel = "pipe"
        os.mkfifo(root / rel)

    error = _snapshot_error(root)

    assert error.reason_code is ReasonCode.INTERNAL_ERROR
    assert error.message == f"special file rejected: {root.resolve() / rel}"


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_snapshot_detects_source_change_during_copy(make_project, faults, change):
    root = make_project({"models/a.sql": "select 1\n"})
    ws = RunWorkspace.create()
    copy = ws.source_snapshot / "models" / "a.sql"

    def change_source(args):
        if args[0] == str(copy):
            if change == "edit":
                (root / "models" / "a.sql").write_text("select 2\n")
            else:
                (root / "models" / "a.sql").unlink()

    faults.on("open", change_source)

    error = _snapshot_error(root, ws)

    assert error.reason_code is ReasonCode.SOURCE_CHANGED_DURING_SNAPSHOT
    assert error.message == "source changed during snapshot: models/a.sql"


def test_snapshot_into_a_workspace_that_was_never_created(make_project, tmp_path):
    root = make_project({"models/a.sql": "select 1\n"})
    ws = RunWorkspace("20260101T000000_abcdef123456", tmp_path / "ws")

    ws.snapshot_project(root)

    assert _files(ws.candidate_project) == _files(root)


# -- package state -----------------------------------------------------------------------------------


def test_package_state_digest_tracks_package_files_and_macros(tmp_path):
    ws = RunWorkspace.create()
    root = tmp_path / "snapshot"
    root.mkdir()
    digests = [ws.package_state_digest(root)]
    assert digests[0] == hashlib.sha256().hexdigest()

    def changed(rel, content):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        digest = ws.package_state_digest(root)
        assert digest not in digests, rel
        digests.append(digest)

    def unchanged(rel, content):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        assert ws.package_state_digest(root) == digests[-1], rel

    unchanged("macros/README.md", "docs\n")
    for rel in ("dbt_project.yml", "dbt_project.yaml", "packages.yml", "package-lock.yml", "dependencies.yml"):
        changed(rel, f"{rel}\n")
    changed("macros/a.sql", "{% macro a() %}{% endmacro %}\n")
    changed("macros/sub/b.sql", "{% macro b() %}{% endmacro %}\n")
    changed("packages.yml", "packages: []\n")
    unchanged("models/m.sql", "select 1\n")
    unchanged("macros/notes.txt", "x\n")
    unchanged("dbt_packages/dbt_utils/macros/star.sql", "{% macro star() %}{% endmacro %}\n")


# -- ledger ------------------------------------------------------------------------------------------


def test_record_object_upserts_by_relation():
    ws = RunWorkspace.create()

    ws.record_object(ScratchObject(_relation("baseline"), "view"))
    ws.record_object(ScratchObject(_relation("candidate"), "view", state="created"))
    ws.record_object(ScratchObject(_relation("baseline"), "view", state="dropped"))

    assert ws.load_ledger() == {
        "run_id": ws.run_id,
        "objects": [
            {"database": "db", "schema": "scratch", "identifier": "baseline", "kind": "view", "state": "dropped"},
            {"database": "db", "schema": "scratch", "identifier": "candidate", "kind": "view", "state": "created"},
        ],
    }


def test_ledger_round_trips_for_a_reopened_workspace():
    ws = RunWorkspace.create()
    ws.record_object(ScratchObject(_relation("baseline"), "view"))
    ws.record_object(ScratchObject(_relation("candidate"), "view"))

    reopened = RunWorkspace(ws.run_id, ws.root)

    assert reopened.load_ledger() == ws.load_ledger()
    assert ledger_relations(reopened) == [_relation("baseline"), _relation("candidate")]


# -- cleanup -----------------------------------------------------------------------------------------


def test_cleanup_files_removes_root_unless_kept():
    ws = RunWorkspace.create()
    ws.cleanup_files()
    assert not ws.root.exists()
    ws.cleanup_files()  # already gone

    kept = RunWorkspace.create(keep=True)
    kept.cleanup_files()
    assert sorted(p.name for p in kept.root.iterdir()) == WORKSPACE_ENTRIES

    # A workspace reopened by path is not kept unless asked.
    RunWorkspace(kept.run_id, kept.root).cleanup_files()
    assert not kept.root.exists()


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX directory permissions that the current user cannot bypass",
)
def test_cleanup_files_reports_entries_it_cannot_remove():
    # §2.3: rmtree(ignore_errors=True) meant cleanup could never report a failure.
    ws = RunWorkspace.create()
    locked = ws.source_snapshot / "locked"
    locked.mkdir()
    (locked / "m.sql").write_bytes(b"select 1\n")
    locked.chmod(0o555)
    try:
        with pytest.raises(CleanupError) as exc_info:
            ws.cleanup_files()
        assert exc_info.value.reason_code is ReasonCode.CLEANUP_FAILED
        assert re.match(
            rf"could not remove \d+ path\(s\) under {re.escape(str(ws.root))}; first: {re.escape(str(locked / 'm.sql'))}: ",
            exc_info.value.message,
        )
        assert _files(ws.root) == {"source_snapshot/locked/m.sql": b"select 1\n"}
    finally:
        locked.chmod(0o755)


def test_snapshot_skips_project_virtualenvs(make_project, tmp_path):
    # A .venv/venv in the project root holds interpreter symlinks that point outside the project.
    root = make_project({"models/a.sql": "select 1\n", ".venv/pyvenv.cfg": "home = /usr\n", "venv/pyvenv.cfg": "x\n"})
    outside = tmp_path / "python3"
    outside.write_text("interpreter\n")
    try:
        (root / ".venv" / "bin").mkdir()
        os.symlink(outside, root / ".venv" / "bin" / "python")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    ws = RunWorkspace.create()

    ws.snapshot_project(root)

    assert sorted(_files(ws.source_snapshot)) == ["dbt_project.yml", "models/a.sql"]


def test_snapshot_refuses_package_link_that_breaks_during_the_snapshot(make_project, tmp_path, faults):
    root = make_project({"models/a.sql": "select 1\n"})
    package = tmp_path / "shared_package"
    (package / "macros").mkdir(parents=True)
    _symlink(root / "dbt_packages" / "shared", package, directory=True)
    packages_dir = str(root.resolve() / "dbt_packages")
    listings = []

    def remove_package(args):
        # The walk lists dbt_packages first; break the link just before the package copy lists it again.
        if str(args[0]) == packages_dir:
            listings.append(args[0])
            if len(listings) == 2:
                shutil.rmtree(package)

    for event in ("os.listdir", "os.scandir"):
        faults.on(event, remove_package)
    error = _snapshot_error(root)

    assert error.message.startswith("unsafe package link: ")
