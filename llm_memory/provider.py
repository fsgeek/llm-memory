from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from llm_memory.contract import SearchRequest
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.reconcile import ReconcileReport, WorkBudget


@dataclass(frozen=True)
class ProviderDescriptor:
    provider: str
    implementation_version: str
    strategies: tuple[str, ...]
    analyzer: str
    indexed_fields: tuple[str, ...]
    match_semantics: str
    score_ordering: str
    raw_score_polarity: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PurgeScope:
    corpus_id: str | None = None
    source_id: str | None = None

    def __post_init__(self) -> None:
        if self.source_id is not None and self.corpus_id is None:
            raise ValueError("source_id requires corpus_id")


@dataclass(frozen=True)
class ProviderMeasurement:
    provider: str
    standing: str
    observations: dict[str, int | float | str | None]


class ProviderUnavailable(RuntimeError):
    """A bounded provider operation could not complete and may be retried."""


class ProviderUnsupported(RuntimeError):
    """The configured runtime cannot implement the declared provider."""


class EpisodicProvider(Protocol):
    def capabilities(self) -> dict[str, object]: ...

    def ensure(self) -> dict[str, object]: ...

    def reconcile(
        self, registry: EnrollmentRegistry, budget: WorkBudget
    ) -> ReconcileReport: ...

    def search(
        self,
        registry: EnrollmentRegistry,
        request: SearchRequest,
        budget: WorkBudget,
    ) -> dict[str, object]: ...

    def resolve_supersession(
        self, enrollment: SourceEnrollment, old_ref: str
    ) -> str | None: ...

    def purge(
        self, scope: PurgeScope, state_classes: frozenset[str]
    ) -> dict[str, int]: ...

    def remove_all(self) -> dict[str, object]: ...

    def measure(self, scope: PurgeScope) -> ProviderMeasurement: ...
