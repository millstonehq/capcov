"""Independent semantic verdict and operational status algebra."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SemanticVerdict(str, Enum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    UNRESOLVED = "unresolved"
    CONFLICTING = "conflicting"


class OperationalStatus(str, Enum):
    COMPLETE = "complete"
    INVALID_INPUT = "invalid-input"
    INCONSISTENT_PREMISES = "inconsistent-premises"
    RESOURCE_EXHAUSTED = "resource-exhausted"
    UNSUPPORTED_CONSTRUCT = "unsupported-construct"
    STALE = "stale"
    OUT_OF_SCOPE = "out-of-scope"


class EvaluationBasis(str, Enum):
    DERIVATIONAL = "derivational"
    BOUNDED_HISTORY_MODEL = "bounded-history-model"
    FINITE_BASIS_WITH_CERTIFICATE = "finite-basis-with-certificate"


def verdict(support: bool, refutation: bool) -> SemanticVerdict:
    if support and refutation: return SemanticVerdict.CONFLICTING
    if support: return SemanticVerdict.SUPPORTED
    if refutation: return SemanticVerdict.REFUTED
    return SemanticVerdict.UNRESOLVED


@dataclass(frozen=True)
class EvaluationResult:
    semantic: SemanticVerdict
    operational: OperationalStatus = OperationalStatus.COMPLETE
    basis: EvaluationBasis = EvaluationBasis.DERIVATIONAL
    support: tuple[Any, ...] = ()
    refutation: tuple[Any, ...] = ()
    missing_premises: tuple[Any, ...] = ()
    discrepancies: tuple[Any, ...] = ()
    resources: tuple[tuple[str, int | float], ...] = ()
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "support", tuple(self.support))
        object.__setattr__(self, "refutation", tuple(self.refutation))
        object.__setattr__(self, "missing_premises", tuple(self.missing_premises))
        object.__setattr__(self, "discrepancies", tuple(self.discrepancies))
        object.__setattr__(self, "resources", tuple(sorted(self.resources)))

    @property
    def is_success(self) -> bool:
        return self.operational == OperationalStatus.COMPLETE and self.semantic == SemanticVerdict.SUPPORTED

    def as_dict(self) -> dict[str, Any]:
        return {"semantic": self.semantic.value, "operational": self.operational.value,
                "basis": self.basis.value, "support": list(self.support),
                "refutation": list(self.refutation), "missing_premises": list(self.missing_premises),
                "discrepancies": list(self.discrepancies), "resources": dict(self.resources), "message": self.message}

