"""Package marker."""

from dbt_refmerge.domain import ReasonCode, VerificationStatus, is_fixable

__all__ = ["ReasonCode", "VerificationStatus", "is_fixable", "__version__"]

__version__ = "0.3.0"
