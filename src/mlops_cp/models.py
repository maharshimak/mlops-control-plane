from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Evaluation:
    metric: str
    value: float
    threshold: float
    higher_is_better: bool = True

    @property
    def passed(self) -> bool:
        if not isfinite(self.value) or not isfinite(self.threshold):
            return False
        if self.higher_is_better:
            return self.value >= self.threshold
        return self.value <= self.threshold


@dataclass(slots=True)
class ModelVersion:
    name: str
    version: str
    artifact_uri: str
    dataset_fingerprint: str
    stage: str = "registered"
    evaluations: list[Evaluation] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    @property
    def key(self) -> str:
        return f"{self.name}:{self.version}"
