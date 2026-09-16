"""apply_verified_source: the only code that writes a user's model file."""

import hashlib
import os
import sys

import pytest

from dbt_refmerge.errors import RefmergeError, SourceChangedError
from dbt_refmerge.orchestrator import apply_verified_source

ORIGINAL = b"select 1 as id\n"
CANDIDATE = b"select 2 as id\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _apply(path):
    apply_verified_source(
        path,
        expected_original_sha256=_sha(ORIGINAL),
        candidate_bytes=CANDIDATE,
        expected_candidate_sha256=_sha(CANDIDATE),
    )


def _model(tmp_path):
    path = tmp_path / "m.sql"
    path.write_bytes(ORIGINAL)
    return path


def test_apply_leaves_only_the_model_behind(tmp_path):
    # S9: no lock or temp file may be left in the user's models directory.
    path = _model(tmp_path)
    _apply(path)
    assert path.read_bytes() == CANDIDATE
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.sql"]


def test_apply_refuses_symlinked_lock_path_without_touching_its_target(tmp_path):
    # S9: a planted lock-path symlink must not be followed (opening it with "w" truncated the target).
    path = _model(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep me")
    try:
        os.symlink(victim, tmp_path / "m.sql.dbt-refmerge.lock")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(RefmergeError):
        _apply(path)
    assert victim.read_bytes() == b"keep me"
    assert path.read_bytes() == ORIGINAL


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only")
def test_apply_fails_closed_when_the_lock_cannot_be_taken(tmp_path, faults):
    # S9: proceeding without the lock used to be silent.
    path = _model(tmp_path)

    def refuse(args):
        raise OSError("lock unavailable")

    faults.on("fcntl.flock", refuse, once=True)
    with pytest.raises(RefmergeError):
        _apply(path)
    assert path.read_bytes() == ORIGINAL


def test_apply_refuses_same_size_edit_that_keeps_mtime(tmp_path, faults):
    # S9: the pre-replace re-check used to run only when mtime or size changed.
    path = _model(tmp_path)
    stat = path.stat()
    other_writer = b"select 9 as id\n"
    assert len(other_writer) == len(ORIGINAL)

    def concurrent_edit(args):
        path.write_bytes(other_writer)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    faults.on("os.chmod", concurrent_edit, once=True)
    with pytest.raises(SourceChangedError):
        _apply(path)
    assert path.read_bytes() == other_writer
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.sql"]
