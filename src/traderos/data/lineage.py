"""Dataset lineage and ingestion-run records."""

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from traderos.data.time import require_utc


class AdjustmentPolicy(StrEnum):
    """Explicit raw/adjusted-data policy."""

    RAW = "raw"
    ADJUSTED = "adjusted"


class IngestionStatus(StrEnum):
    """Lifecycle status for one bounded ingestion run."""

    STARTED = "started"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class DatasetMetadata(BaseModel):
    """Reproducibility metadata for a logical dataset version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    start: datetime
    end: datetime
    adjustment_policy: AdjustmentPolicy
    configuration_version: str = Field(min_length=1)
    quality_status: str = Field(min_length=1)
    created_at: datetime

    def model_post_init(self, __context: object) -> None:
        require_utc(self.start)
        require_utc(self.end)
        require_utc(self.created_at)


class IngestionRun(BaseModel):
    """Auditable ingestion-run summary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID = Field(default_factory=uuid4)
    dataset_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    requested_start: datetime
    requested_end: datetime
    adjustment_policy: AdjustmentPolicy
    configuration_version: str = Field(min_length=1)
    started_at: datetime
    finished_at: datetime | None = None
    records_received: int = Field(default=0, ge=0)
    records_accepted: int = Field(default=0, ge=0)
    records_rejected: int = Field(default=0, ge=0)
    duplicates: int = Field(default=0, ge=0)
    warnings: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    status: IngestionStatus = IngestionStatus.STARTED
    error_message: str | None = None

    def model_post_init(self, __context: object) -> None:
        require_utc(self.requested_start)
        require_utc(self.requested_end)
        require_utc(self.started_at)
        if self.finished_at is not None:
            require_utc(self.finished_at)


def dataset_version(
    *,
    provider: str,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    adjustment_policy: AdjustmentPolicy,
    configuration_version: str,
    bar_fingerprint: str,
) -> str:
    """Compute a stable version from request, policy, configuration, and bars."""

    payload = {
        "provider": provider,
        "symbol": symbol,
        "timeframe": timeframe,
        "start": require_utc(start).isoformat(),
        "end": require_utc(end).isoformat(),
        "adjustment_policy": adjustment_policy.value,
        "configuration_version": configuration_version,
        "bar_fingerprint": bar_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "AdjustmentPolicy",
    "DatasetMetadata",
    "IngestionRun",
    "IngestionStatus",
    "dataset_version",
]
