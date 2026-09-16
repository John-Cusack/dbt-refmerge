"""Typed operational errors with stable reason codes."""

from __future__ import annotations

from dbt_refmerge.domain import ReasonCode


class RefmergeError(Exception):
    """Typed operational error. Plain class: frozen dataclasses cannot carry tracebacks."""

    def __init__(self, reason_code: ReasonCode, message: str) -> None:
        super().__init__(f"[{reason_code.value}] {message}")
        self.reason_code = reason_code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.reason_code.value}] {self.message}"


class ConfigError(RefmergeError):
    pass


class DbtError(RefmergeError):
    def __init__(
        self,
        message: str,
        reason_code: ReasonCode = ReasonCode.DBT_COMMAND_FAILED,
        argv: tuple[str, ...] = (),
    ) -> None:
        super().__init__(reason_code=reason_code, message=message)
        object.__setattr__(self, "argv", argv)


class ArtifactError(RefmergeError):
    pass


class SourceParseError(RefmergeError):
    pass


class SemanticError(RefmergeError):
    pass


class RewriteError(RefmergeError):
    pass


class VerificationError(RefmergeError):
    pass


class ScratchBoundaryError(RefmergeError):
    def __init__(self, message: str) -> None:
        super().__init__(reason_code=ReasonCode.SCRATCH_BOUNDARY_VIOLATION, message=message)


class CleanupError(RefmergeError):
    def __init__(self, message: str) -> None:
        super().__init__(reason_code=ReasonCode.CLEANUP_FAILED, message=message)


class InternalInvariantError(RefmergeError):
    def __init__(self, message: str) -> None:
        super().__init__(reason_code=ReasonCode.INTERNAL_ERROR, message=message)


class SourceChangedError(RefmergeError):
    pass
