"""apply_verified_source: the only code that writes a user's model file."""

import hashlib
import os
import sys

import pytest

from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import RefmergeError, SourceChangedError
from dbt_refmerge.orchestrator import _unlink_quietly, apply_verified_source

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


def _listing(directory):
    return sorted(p.name for p in directory.iterdir())


def _refuse_replace(args):
    raise PermissionError("replace refused")


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


def test_apply_refuses_non_regular_target(tmp_path):
    path = tmp_path / "m.sql"
    path.mkdir()

    with pytest.raises(SourceChangedError) as exc_info:
        _apply(path)

    assert exc_info.value.reason_code is ReasonCode.SOURCE_CHANGED_BEFORE_APPLY
    assert exc_info.value.message == f"refusing non-regular file: {path}"
    assert _listing(tmp_path) == ["m.sql"]
    assert _listing(path) == []


def test_apply_refuses_symlinked_target(tmp_path):
    real = tmp_path / "real.sql"
    real.write_bytes(ORIGINAL)
    path = tmp_path / "m.sql"
    try:
        os.symlink(real, path)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    with pytest.raises(SourceChangedError) as exc_info:
        _apply(path)

    assert exc_info.value.reason_code is ReasonCode.SOURCE_CHANGED_BEFORE_APPLY
    assert real.read_bytes() == ORIGINAL
    assert path.is_symlink()
    assert _listing(tmp_path) == ["m.sql", "real.sql"]


def test_apply_refuses_candidate_digest_mismatch(tmp_path):
    path = _model(tmp_path)

    with pytest.raises(SourceChangedError) as exc_info:
        apply_verified_source(
            path,
            expected_original_sha256=_sha(ORIGINAL),
            candidate_bytes=CANDIDATE,
            expected_candidate_sha256=_sha(b"select 3 as id\n"),
        )

    assert exc_info.value.reason_code is ReasonCode.SOURCE_CHANGED_BEFORE_APPLY
    assert exc_info.value.message == "candidate digest mismatch"
    assert path.read_bytes() == ORIGINAL
    assert _listing(tmp_path) == ["m.sql"]


def test_apply_refuses_source_edited_since_verification(tmp_path):
    path = _model(tmp_path)
    path.write_bytes(b"select 9 as id\n")

    with pytest.raises(SourceChangedError) as exc_info:
        _apply(path)

    assert exc_info.value.reason_code is ReasonCode.SOURCE_CHANGED_BEFORE_APPLY
    assert exc_info.value.message == "source changed before apply"
    assert path.read_bytes() == b"select 9 as id\n"
    assert _listing(tmp_path) == ["m.sql"]


def test_apply_fails_closed_when_the_lock_file_cannot_be_opened(tmp_path):
    path = _model(tmp_path)
    lock = tmp_path / "m.sql.dbt-refmerge.lock"
    lock.mkdir()

    with pytest.raises(RefmergeError) as exc_info:
        _apply(path)

    assert exc_info.value.reason_code is ReasonCode.INTERNAL_ERROR
    assert exc_info.value.message.startswith(f"cannot open lock file {lock}: ")
    assert path.read_bytes() == ORIGINAL
    # The planted directory is not ours to remove.
    assert _listing(tmp_path) == ["m.sql", "m.sql.dbt-refmerge.lock"]
    assert _listing(lock) == []


def test_apply_replace_failure_keeps_original_and_removes_temp_and_lock(tmp_path, faults):
    path = _model(tmp_path)
    faults.on("os.rename", _refuse_replace, once=True)

    with pytest.raises(PermissionError, match="^replace refused$"):
        _apply(path)

    assert path.read_bytes() == ORIGINAL
    assert _listing(tmp_path) == ["m.sql"]


def test_apply_temp_unlink_failure_does_not_mask_the_replace_error(tmp_path, faults):
    path = _model(tmp_path)
    faults.on("os.rename", _refuse_replace, once=True)

    def refuse_temp_unlink(args):
        if os.path.basename(args[0]).startswith(".dbt-refmerge-"):
            raise PermissionError("unlink refused")

    faults.on("os.remove", refuse_temp_unlink)

    with pytest.raises(PermissionError, match="^replace refused$"):
        _apply(path)

    assert path.read_bytes() == ORIGINAL
    # Only the temp file the OS refused to delete is left; the lock is still removed.
    (leftover,) = (p for p in tmp_path.iterdir() if p.name != "m.sql")
    assert leftover.name.startswith(".dbt-refmerge-")
    assert leftover.read_bytes() == CANDIDATE


@pytest.mark.skipif(sys.platform == "win32", reason="directory fsync is POSIX-only")
def test_apply_succeeds_when_directory_fsync_is_unavailable(tmp_path, faults):
    # The rename has already happened; failing here would report an applied change as not applied.
    path = _model(tmp_path)
    refused = []

    def refuse_directory_open(args):
        if args[0] == str(tmp_path) and args[2] & os.O_DIRECTORY:
            refused.append(args[0])
            raise OSError("directory fsync unavailable")

    faults.on("open", refuse_directory_open)

    _apply(path)

    assert refused == [str(tmp_path)]
    assert path.read_bytes() == CANDIDATE
    assert _listing(tmp_path) == ["m.sql"]


def test_unlink_quietly_ignores_a_missing_path(tmp_path):
    _unlink_quietly(tmp_path / "m.sql.dbt-refmerge.lock")

    assert _listing(tmp_path) == []
