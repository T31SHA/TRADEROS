"""Governed APEX Milestone 5 preflight and immutable protocol artifacts.

This module is deliberately a preflight boundary.  It inventories only caller-
configured dataset roots, verifies the existing admission artifacts, records a
research qualification fact, and freezes the evaluation protocol before any
performance code can run.  A failed data gate is terminal for the runner.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import uuid4

from traderos.data.empirical import AdmissionPolicy
from traderos.data.time import require_utc
from traderos.research.dataset import DatasetQualificationStatus, ResearchDatasetEligibility
from traderos.research.dataset_records import (
    DatasetClassification,
    DatasetQualificationRecord,
    DatasetQualificationRegistry,
    DatasetRecordRead,
    DatasetRecordReader,
    DatasetRecordStatus,
)
from traderos.research.models import ExperimentInputBinding, ResearchError, TemporalRange


class ApexOutcome(StrEnum):
    """Policy-backed Milestone 5 outcomes."""

    EXECUTED = "EXECUTED"
    BLOCKED_DATA = "BLOCKED_DATA"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    REJECT = "REJECT"
    INCONCLUSIVE = "INCONCLUSIVE"


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ResearchError("APEX artifact is not canonically serializable") from exc


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ResearchError(f"APEX artifact contains unsupported value {type(value)!r}")


def _read_json(path: Path) -> object:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    require_utc(parsed)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class SourceIdentity:
    """Reproducibility identity for the actual dirty working tree."""

    base_commit: str
    dirty_state: str
    snapshot_content_digest: str
    snapshot_files: tuple[tuple[str, str, int], ...]
    python_version: str
    python_executable: str
    platform_identity: str
    dependencies: tuple[tuple[str, str], ...]

    def canonical(self) -> dict[str, object]:
        return {
            "base_commit": self.base_commit,
            "dirty_state": self.dirty_state,
            "snapshot_content_digest": self.snapshot_content_digest,
            "snapshot_files": [list(item) for item in self.snapshot_files],
            "python_version": self.python_version,
            "python_executable": self.python_executable,
            "platform_identity": self.platform_identity,
            "dependencies": [list(item) for item in self.dependencies],
        }


def _git(repo_root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ResearchError("source identity requires a readable Git worktree") from exc
    return completed.stdout.strip()


def _source_paths(repo_root: Path) -> tuple[Path, ...]:
    tracked = _git(repo_root, "ls-files").splitlines()
    untracked = _git(repo_root, "ls-files", "--others", "--exclude-standard").splitlines()
    names = set(tracked) | set(untracked)
    excluded_prefixes = (
        ".git/",
        ".venv/",
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
        "data/raw/",
        "data/normalized/",
        "data/manifests/",
        "data/qualifications/",
        "experiments/artifacts/",
        "research/results/",
        "notebooks/.ipynb_checkpoints/",
    )
    excluded_names = {".env", ".env.local", ".env.production"}
    paths: list[Path] = []
    for name in sorted(names):
        if name in excluded_names or name.startswith(excluded_prefixes):
            continue
        if Path(name).name.startswith(".env.") and name != ".env.example":
            continue
        path = (repo_root / name).resolve()
        try:
            path.relative_to(repo_root.resolve())
        except ValueError:
            continue
        if path.is_file():
            paths.append(path)
    return tuple(paths)


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def collect_source_identity(repo_root: Path) -> SourceIdentity:
    """Hash relevant tracked and untracked source without exporting private data."""

    root = repo_root.resolve()
    files: list[tuple[str, str, int]] = []
    for path in _source_paths(root):
        digest, size = _file_digest(path)
        files.append((path.relative_to(root).as_posix(), digest, size))
    digest = _hash(files)
    dirty_state = "dirty" if _git(root, "status", "--porcelain=v1") else "clean"
    dependency_names = (
        "traderos",
        "sqlalchemy",
        "psycopg",
        "pydantic",
        "pydantic-settings",
        "pytest",
        "ruff",
        "mypy",
    )
    dependencies = tuple(
        (name, importlib.metadata.version(name))
        for name in dependency_names
        if _distribution_available(name)
    )
    return SourceIdentity(
        base_commit=_git(root, "rev-parse", "HEAD"),
        dirty_state=dirty_state,
        snapshot_content_digest=digest,
        snapshot_files=tuple(files),
        python_version=sys.version,
        python_executable=sys.executable,
        platform_identity=platform.platform(),
        dependencies=dependencies,
    )


def _distribution_available(name: str) -> bool:
    try:
        importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


@dataclass(frozen=True)
class ApexProtocol:
    """Frozen declaration of one first-baseline evaluation."""

    strategy: Mapping[str, object]
    hypothesis: str
    dataset: Mapping[str, object]
    temporal_design: Mapping[str, object]
    warmup: Mapping[str, object]
    controls: tuple[str, ...]
    cost_scenarios: tuple[Mapping[str, object], ...]
    execution_policy: Mapping[str, object]
    position_sizing_policy: Mapping[str, object]
    risk_configuration: Mapping[str, object]
    metrics: tuple[str, ...]
    sample_adequacy: Mapping[str, object]
    statistical_methods: Mapping[str, object]
    random_seeds: Mapping[str, int]
    failure_criteria: tuple[str, ...]
    inconclusive_criteria: tuple[str, ...]
    oos_access_rules: tuple[str, ...]
    source_identity: Mapping[str, object]
    environment_configuration: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.hypothesis.strip():
            raise ResearchError("APEX protocol hypothesis must not be blank")
        if not self.controls or not self.metrics:
            raise ResearchError("APEX protocol requires controls and metrics")
        for period_name in ("development", "locked_oos"):
            period = self.temporal_design.get(period_name)
            if not isinstance(period, dict) or set(period) != {"start", "end"}:
                raise ResearchError(f"APEX protocol {period_name} boundary is malformed")
        development_value = cast(Mapping[str, object], self.temporal_design["development"])
        locked_oos_value = cast(Mapping[str, object], self.temporal_design["locked_oos"])
        development = TemporalRange(
            _utc(str(development_value["start"])),
            _utc(str(development_value["end"])),
        )
        locked_oos = TemporalRange(
            _utc(str(locked_oos_value["start"])),
            _utc(str(locked_oos_value["end"])),
        )
        if development.end > locked_oos.start:
            raise ResearchError("APEX protocol development and OOS periods overlap")
        warmup = self.warmup.get("period")
        if not isinstance(warmup, dict) or set(warmup) != {"start", "end"}:
            raise ResearchError("APEX protocol warmup period is malformed")
        warmup_period = TemporalRange(_utc(str(warmup["start"])), _utc(str(warmup["end"])))
        if warmup_period.end > development.start:
            raise ResearchError("APEX protocol warmup must end before development")
        if not self.cost_scenarios:
            raise ResearchError("APEX protocol requires explicit cost scenarios")
        if not self.oos_access_rules:
            raise ResearchError("APEX protocol requires OOS access rules")

    def payload(self) -> dict[str, object]:
        return cast(
            dict[str, object],
            _json_value(
                {
                    "schema_version": "apex-milestone5-protocol.v1",
                    "strategy": self.strategy,
                    "hypothesis": self.hypothesis,
                    "dataset": self.dataset,
                    "temporal_design": self.temporal_design,
                    "warmup": self.warmup,
                    "controls": self.controls,
                    "cost_scenarios": self.cost_scenarios,
                    "execution_policy": self.execution_policy,
                    "position_sizing_policy": self.position_sizing_policy,
                    "risk_configuration": self.risk_configuration,
                    "metrics": self.metrics,
                    "sample_adequacy": self.sample_adequacy,
                    "statistical_methods": self.statistical_methods,
                    "random_seeds": self.random_seeds,
                    "failure_criteria": self.failure_criteria,
                    "inconclusive_criteria": self.inconclusive_criteria,
                    "oos_access_rules": self.oos_access_rules,
                    "source_identity": self.source_identity,
                    "environment_configuration": self.environment_configuration,
                }
            ),
        )

    @property
    def protocol_hash(self) -> str:
        return _hash(self.payload())

    @property
    def protocol_id(self) -> str:
        return f"apex-m5-{self.protocol_hash[:24]}"

    def canonical(self) -> dict[str, object]:
        return {
            **self.payload(),
            "protocol_id": self.protocol_id,
            "protocol_hash": self.protocol_hash,
        }


class ApexProtocolRegistry:
    """Create-only protocol registry with retry/idempotency semantics."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def record(self, protocol: ApexProtocol) -> Path:
        destination = self._root / f"{protocol.protocol_id}.json"
        encoded = (
            json.dumps(protocol.canonical(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        self._root.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != encoded:
                raise ResearchError("conflicting APEX protocol identity")
            return destination
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
            try:
                temporary.rename(destination)
            except FileExistsError:
                if destination.read_bytes() != encoded:
                    raise ResearchError("conflicting APEX protocol identity") from None
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def read(self, protocol_id: str) -> Mapping[str, object]:
        try:
            payload = _read_json(self._root / f"{protocol_id}.json")
        except FileNotFoundError as exc:
            raise ResearchError("APEX protocol does not exist") from exc
        if not isinstance(payload, dict) or payload.get("protocol_id") != protocol_id:
            raise ResearchError("APEX protocol record is malformed")
        expected_hash = payload.get("protocol_hash")
        body = dict(payload)
        body.pop("protocol_id", None)
        body.pop("protocol_hash", None)
        if not isinstance(expected_hash, str) or _hash(body) != expected_hash:
            raise ResearchError("APEX protocol hash mismatch")
        return cast(Mapping[str, object], payload)


@dataclass(frozen=True)
class DatasetPreflight:
    """Read-only admission result used by the Milestone 5 runner."""

    read: DatasetRecordRead
    qualification: DatasetQualificationRecord
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    quality_report: Mapping[str, object] | None

    @property
    def empirical_eligible(self) -> bool:
        return (
            not self.blockers
            and self.qualification.empirical_eligibility
            is ResearchDatasetEligibility.EMPIRICALLY_QUALIFIED_DATASET
        )

    def canonical(self, qualification_reference: str | None = None) -> dict[str, object]:
        record = self.read.record
        return {
            "dataset_id": self.read.dataset_id,
            "record_status": self.read.status.value,
            "manifest_status": self.read.manifest_status.value,
            "normalized_status": self.read.normalized_status.value,
            "raw_status": self.read.raw_status.value,
            "quality_report_status": self.read.quality_report_status.value,
            "failure_code": self.read.failure_code,
            "qualification_id": self.qualification.qualification_id,
            "qualification_reference": qualification_reference,
            "qualification_verdict": self.qualification.verdict.value,
            "empirical_eligibility": self.qualification.empirical_eligibility.value,
            "manifest_hash": record.manifest_hash if record else None,
            "dataset_content_hash": record.content_hash if record else None,
            "normalized_content_hash": record.normalized_content_hash if record else None,
            "raw_artifact_hashes": list(record.raw_artifact_hashes) if record else [],
            "coverage": (
                {
                    "start": record.coverage.start.isoformat(),
                    "end": record.coverage.end.isoformat(),
                }
                if record and record.coverage
                else None
            ),
            "source": record.source if record else None,
            "source_version": record.source_version if record else None,
            "calendar_id": record.calendar_id if record else None,
            "timestamp_semantics": record.timestamp_semantics if record else None,
            "quote_semantics": record.quote_semantics if record else None,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "quality_report": self.quality_report,
        }


def inventory_dataset_ids(manifest_root: Path) -> tuple[str, ...]:
    """Inventory only the explicitly configured manifest directory."""

    if not manifest_root.exists():
        return ()
    return tuple(
        sorted(
            path.stem
            for path in manifest_root.glob("*.json")
            if not path.name.endswith(".quality.json")
        )
    )


def _quality_report(
    quality_root: Path, dataset_id: str, expected_hash: str
) -> Mapping[str, object] | None:
    path = quality_root / f"{dataset_id}.quality.json"
    try:
        payload = _read_json(path)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or _hash(payload) != expected_hash:
        return None
    return payload


def qualify_admitted_dataset(
    *,
    reader: DatasetRecordReader,
    qualification_registry: DatasetQualificationRegistry,
    dataset_id: str,
    quality_root: Path,
    admission_policy: AdmissionPolicy,
    checked_at: datetime,
    minimum_coverage: timedelta = timedelta(days=180),
) -> DatasetPreflight:
    """Verify admission artifacts and persist a fail-closed qualification fact."""

    require_utc(checked_at)
    read = reader.read(dataset_id)
    blockers: list[str] = []
    warnings: list[str] = []
    record = read.record
    quality: Mapping[str, object] | None = None
    if read.status is not DatasetRecordStatus.VERIFIED or record is None:
        blockers.append(f"DATASET_RECORD_{read.status.value.upper()}")
    if read.raw_status is not DatasetRecordStatus.VERIFIED:
        blockers.append(f"RAW_ARTIFACT_{read.raw_status.value.upper()}")
    if read.normalized_status is not DatasetRecordStatus.VERIFIED:
        blockers.append(f"NORMALIZED_CONTENT_{read.normalized_status.value.upper()}")
    if read.quality_report_status is not DatasetRecordStatus.VERIFIED:
        blockers.append(f"QUALITY_REPORT_{read.quality_report_status.value.upper()}")
    if record is not None:
        quality = _quality_report(quality_root, dataset_id, record.quality_report_hash)
        if quality is None:
            blockers.append("QUALITY_REPORT_UNREADABLE")
        if record.classification is DatasetClassification.TEST_FIXTURE:
            blockers.append("FIXTURE_DATASET_NOT_ALLOWED")
        if record.source_version is None:
            blockers.append("SOURCE_VERSION_MISSING")
        if record.source != "dukascopy":
            blockers.append("SOURCE_NOT_PREDECLARED_DUKASCOPY")
        if record.instruments != ("EUR/USD",):
            blockers.append("INSTRUMENT_SCOPE_MISMATCH")
        if record.timeframe != "15m":
            blockers.append("TIMEFRAME_SCOPE_MISMATCH")
        if record.manifest_payload.get("source_mode") != "tick":
            blockers.append("DATA_MODE_NOT_PREDECLARED_TICK")
        if record.timestamp_semantics != "bar_start":
            blockers.append("TIMESTAMP_SEMANTICS_UNSUPPORTED")
        if not record.calendar_id.strip():
            blockers.append("CALENDAR_ID_MISSING")
        if record.coverage is None:
            blockers.append("COVERAGE_MISSING")
        elif record.coverage.end - record.coverage.start < minimum_coverage:
            blockers.append("COVERAGE_SHORT_FOR_DECLARED_PROTOCOL")
        manifest = record.manifest_payload
        if not manifest.get("license_reference"):
            blockers.append("LICENSE_REFERENCE_MISSING")
        if not manifest.get("source_version") and not record.source_version:
            blockers.append("SOURCE_IDENTITY_INCOMPLETE")
    if quality is not None:
        quality_blocking_fields = (
            "invalid_ticks",
            "duplicate_timestamps",
            "non_monotonic_timestamps",
            "crossed_quotes",
            "nonpositive_prices",
            "nonfinite_prices",
            "volume_anomalies",
            "ohlc_violations",
            "unexpected_gaps",
            "unknown_gaps",
            "active_session_zero_volume",
        )
        for field in quality_blocking_fields:
            value = quality.get(field)
            if not isinstance(value, int) or value < 0:
                blockers.append(f"QUALITY_FIELD_INVALID_{field.upper()}")
            elif value:
                blockers.append(f"QUALITY_{field.upper()}_{value}")
        policy_identity = (
            record.manifest_payload.get("quality_policy_identity") if record is not None else None
        )
        if policy_identity != admission_policy.identity:
            blockers.append("QUALIFICATION_POLICY_IDENTITY_MISMATCH")
        if quality.get("calendar_id") != (record.calendar_id if record else None):
            blockers.append("QUALITY_CALENDAR_IDENTITY_MISMATCH")
        missing = quality.get("missing_m15_intervals")
        if isinstance(missing, int) and missing > 0:
            warnings.append(f"MISSING_INTERVALS_{missing}_INCLUDING_EXPECTED_CLOSURES")
    blockers = sorted(set(blockers))
    fixture = record is not None and record.classification is DatasetClassification.TEST_FIXTURE
    if blockers:
        verdict = DatasetQualificationStatus.REJECTED
        eligibility = (
            ResearchDatasetEligibility.DETERMINISTIC_TEST_FIXTURE
            if fixture
            else ResearchDatasetEligibility.BLOCKED
        )
    else:
        verdict = DatasetQualificationStatus.QUALIFIED
        eligibility = ResearchDatasetEligibility.EMPIRICALLY_QUALIFIED_DATASET
    qualification = DatasetQualificationRecord(
        dataset_id=dataset_id,
        dataset_content_hash=record.content_hash if record else "unavailable",
        manifest_hash=record.manifest_hash if record else "unavailable",
        policy_id=admission_policy.policy_id,
        policy_version=admission_policy.policy_version,
        implementation_id="apex-m5-dataset-preflight",
        implementation_version="1",
        verdict=verdict,
        empirical_eligibility=eligibility,
        checked_at=checked_at,
        check_results=tuple(
            sorted(
                (
                    name,
                    "blocked" if any(name in blocker.lower() for blocker in blockers) else "passed",
                )
                for name in (
                    "raw_artifact_hashes",
                    "normalized_content_hash",
                    "quality_report_hash",
                    "source_version",
                    "license_reference",
                    "calendar_coverage",
                    "execution_quotes",
                    "qualification_policy",
                )
            )
        ),
        warnings=tuple(sorted(set(warnings))),
        unavailable_checks=(
            "license_verification",
            "source_version_verification",
            "financing_inputs",
            "commission_schedule",
            "slippage_schedule",
        ),
        report_reference=f"{dataset_id}.quality.json",
        fixture=fixture,
    )
    qualification_registry.record(qualification)
    return DatasetPreflight(
        read,
        qualification,
        tuple(blockers),
        tuple(sorted(set(warnings))),
        quality,
    )


def strategy_artifact_hash(
    *, strategy_id: str, strategy_version: str, parameters: Mapping[str, object], source_digest: str
) -> str:
    return _hash(
        {
            "schema_version": "strategy-artifact-reference.v1",
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "parameters": parameters,
            "source_snapshot_content_digest": source_digest,
        }
    )


def build_first_baseline_protocol(
    *,
    preflight: DatasetPreflight,
    source_identity: SourceIdentity,
    invocation: str,
    checked_at: datetime,
) -> ApexProtocol:
    """Declare the existing Forex trend baseline without inspecting returns."""

    record = preflight.read.record
    if record is None or record.coverage is None:
        raise ResearchError("a protocol requires a dataset coverage range")
    coverage = record.coverage
    development_start = coverage.start + timedelta(minutes=15 * 29)
    development_total = coverage.end - development_start
    development_end = development_start + development_total * 2 // 3
    locked_oos_start = development_end
    warmup_start = coverage.start
    warmup_end = coverage.start + timedelta(minutes=15 * 29)
    strategy_parameters: dict[str, object] = {
        "fast_period": 10,
        "slow_period": 30,
        "target_quantity": "1",
        "trend_strength_threshold": 0.0,
    }
    artifact_hash = strategy_artifact_hash(
        strategy_id="forex_trend_following",
        strategy_version="1",
        parameters=strategy_parameters,
        source_digest=source_identity.snapshot_content_digest,
    )
    dataset: dict[str, object] = {
        "dataset_id": record.dataset_id,
        "dataset_content_hash": record.content_hash,
        "manifest_hash": record.manifest_hash,
        "qualification_id": preflight.qualification.qualification_id,
        "qualification_policy_id": preflight.qualification.policy_id,
        "qualification_policy_version": preflight.qualification.policy_version,
        "source": record.source,
        "source_version": record.source_version,
        "instrument_scope": list(record.instruments),
        "timeframe": record.timeframe,
        "coverage": {"start": coverage.start.isoformat(), "end": coverage.end.isoformat()},
        "raw_artifact_hashes": list(record.raw_artifact_hashes),
        "normalized_content_hash": record.normalized_content_hash,
        "calendar_id": record.calendar_id,
        "timestamp_semantics": record.timestamp_semantics,
        "quote_semantics": record.quote_semantics,
    }
    feature_lineage = ["ema.v1(window=10)", "ema.v1(window=30)"]
    warmup_period = TemporalRange(warmup_start, warmup_end)
    development_period = TemporalRange(development_start, development_end)
    locked_oos_period = TemporalRange(locked_oos_start, coverage.end)

    def binding_for(period: TemporalRange) -> dict[str, object]:
        slice_payload = {
            "dataset_id": record.dataset_id,
            "dataset_content_hash": record.content_hash,
            "manifest_hash": record.manifest_hash,
            "evaluation_period": {
                "start": period.start.isoformat(),
                "end": period.end.isoformat(),
            },
            "warmup_period": {
                "start": warmup_period.start.isoformat(),
                "end": warmup_period.end.isoformat(),
            },
            "instrument_scope": record.instruments,
            "feature_input_lineage": tuple(feature_lineage),
        }
        binding = ExperimentInputBinding(
            dataset_id=record.dataset_id,
            dataset_content_hash=record.content_hash,
            manifest_hash=record.manifest_hash,
            qualification_id=preflight.qualification.qualification_id,
            qualification_policy_id=preflight.qualification.policy_id,
            qualification_policy_version=preflight.qualification.policy_version,
            evaluation_period=period,
            warmup_period=warmup_period,
            instrument_scope=record.instruments,
            data_slice_identity=_hash(slice_payload),
            feature_input_lineage=tuple(feature_lineage),
            fixture=False,
        )
        return binding.canonical()

    dataset["experiment_input_bindings"] = [
        binding_for(development_period),
        binding_for(locked_oos_period),
    ]
    config = {
        "feature_input_lineage": feature_lineage,
        "regime_configuration_id": "ema_percentile_regime.v1:default",
        "fusion_configuration_id": "majority_vote.v1:minimum_votes=1,minimum_margin=1",
        "risk_configuration_id": "risk_firewall.v1:default",
        "sizing_configuration_id": "paper_execution.v1:target_quantity=1",
        "admission_policy_identity": AdmissionPolicy().identity,
        "invocation": invocation,
    }
    return ApexProtocol(
        strategy={
            "strategy_id": "forex_trend_following",
            "strategy_version": "1",
            "strategy_artifact_hash": artifact_hash,
            "parameters": strategy_parameters,
            "registered_semantics": (
                "causal fast/slow EMA signal; target position +/-1; next-bar execution"
            ),
        },
        hypothesis=(
            "On EUR/USD M15, the pre-registered causal 10/30 EMA trend-following baseline "
            "will produce directional signals whose net performance differs from the no-trade "
            "control after observed bid/ask and explicitly declared adverse cost scenarios; "
            "this evaluation does not require or assume profitability."
        ),
        dataset=dataset,
        temporal_design={
            "development": {
                "start": development_start.isoformat(),
                "end": development_end.isoformat(),
            },
            "locked_oos": {"start": locked_oos_start.isoformat(), "end": coverage.end.isoformat()},
            "split_basis": "coverage endpoints and fixed two-thirds/one-third chronological split",
            "walk_forward": {
                "mode": "expanding",
                "initial_train_days": 10,
                "forward_test_days": 5,
                "fold_count": 2,
                "selection_eligible": False,
                "status": "not_executable_until_data_gate_and_sample_adequacy_pass",
            },
        },
        warmup={
            "period": {"start": warmup_start.isoformat(), "end": warmup_end.isoformat()},
            "bars": 29,
            "handling": (
                "features may warm up before development; warmup rows are excluded "
                "from evaluation metrics"
            ),
        },
        controls=("always_flat.v1", "buy_and_hold.v1"),
        cost_scenarios=(
            {
                "id": "base-observed-spread",
                "spread": "observed historical bid/ask at fill time",
                "commission": "UNAVAILABLE_PENDING_DECLARED_SCHEDULE",
                "slippage": "UNAVAILABLE_PENDING_DECLARED_SCHEDULE",
                "financing": "UNAVAILABLE_PENDING_ROLLOVER_INPUTS",
            },
            {
                "id": "adverse-observed-spread",
                "spread": "observed historical bid/ask widened 1.5x; raw artifacts unchanged",
                "commission": "UNAVAILABLE_PENDING_DECLARED_SCHEDULE",
                "slippage": "UNAVAILABLE_PENDING_DECLARED_SCHEDULE",
                "financing": "UNAVAILABLE_PENDING_ROLLOVER_INPUTS",
            },
        ),
        execution_policy={
            "execution_policy": "next_bar_open",
            "fill_observations": "only observations available at stated fill timestamp",
            "historical_quote_semantics": (
                "bid/ask tick-derived M15; closing quote timestamp retained"
            ),
            "fallback_spread": "forbidden for this empirical protocol",
        },
        position_sizing_policy={
            "owner": "Phase 8 pure quantity_for_authorization",
            "target_quantity": "1",
            "sizing_configuration_identity": config["sizing_configuration_id"],
        },
        risk_configuration={
            "owner": "Phase 7 RiskFirewall",
            "configuration_identity": config["risk_configuration_id"],
            "veto_on_failure": True,
        },
        metrics=(
            "net_return",
            "drawdown",
            "turnover",
            "gross_exposure",
            "trade_count",
            "holding_periods",
            "open_position_treatment",
            "equity_curve",
        ),
        sample_adequacy={
            "minimum_coverage_days": 180,
            "minimum_development_days": 120,
            "minimum_locked_oos_days": 60,
            "minimum_closed_trades": 30,
            "minimum_oos_closed_trades": 10,
            "annualization": (
                "elapsed UTC days over 365.2425; unavailable below adequacy thresholds"
            ),
            "equity_sampling": "one snapshot per completed-bar decision event; no interpolation",
            "sharpe_threshold": "none declared",
        },
        statistical_methods={
            "uncertainty": "moving-block bootstrap only when sample adequacy passes",
            "block_size": "predeclared 10 trades",
            "multiple_testing": "Benjamini-Yekutieli over declared test family",
            "test_family": "one baseline, one dataset, two cost scenarios, one no-trade control",
        },
        random_seeds={"moving_block_bootstrap": 20260919, "trade_path_monte_carlo": 20260920},
        failure_criteria=(
            "any raw/normalized/quality hash mismatch",
            "any invalid or unavailable safety dependency",
            "any temporal boundary or future-information violation",
            "any material execution/accounting defect",
        ),
        inconclusive_criteria=(
            "sample adequacy not met",
            "financing, commission, or slippage inputs unavailable",
            "cost scenarios cannot be evaluated as declared",
            "uncertainty remains unavailable",
        ),
        oos_access_rules=(
            "development protocol and configuration must be frozen before OOS access",
            "record every locked-OOS read in the experiment registry",
            "OOS is never selection eligible",
            "any post-access material change invalidates pristine OOS status",
        ),
        source_identity=source_identity.canonical(),
        environment_configuration={
            **config,
            "checked_at": checked_at.isoformat(),
            "configuration_identity": _hash(config),
        },
    )


def build_blocked_result(
    *,
    protocol: ApexProtocol,
    preflight: DatasetPreflight,
    source_identity: SourceIdentity,
    protocol_reference: str,
    qualification_reference: str,
    inventory: Sequence[Mapping[str, object]],
    slice_verification: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Produce stable evidence with unavailable metrics rather than fabricated zeros."""

    return {
        "schema_version": "apex-milestone5-result.v1",
        "milestone": "APEX Milestone 5 — First Governed Empirical Baseline Evaluation",
        "outcome": ApexOutcome.BLOCKED_DATA.value,
        "executed": False,
        "protocol_id": protocol.protocol_id,
        "protocol_hash": protocol.protocol_hash,
        "protocol_reference": protocol_reference,
        "qualification_reference": qualification_reference,
        "source_identity": source_identity.canonical(),
        "dataset_inventory": list(inventory),
        "selected_dataset": preflight.canonical(qualification_reference),
        "slice_verification": list(slice_verification),
        "stages": {
            "development": {"status": "NOT_EXECUTED", "metrics": "UNAVAILABLE"},
            "walk_forward": {"status": "NOT_EXECUTED", "metrics": "UNAVAILABLE"},
            "locked_oos": {"status": "NOT_EXECUTED", "metrics": "UNAVAILABLE"},
        },
        "controls": {"no_trade": "NOT_EXECUTED", "buy_and_hold": "NOT_EXECUTED"},
        "cost_sensitivity": "NOT_EXECUTED — required cost inputs unavailable",
        "sample_adequacy": protocol.sample_adequacy,
        "uncertainty": "UNAVAILABLE",
        "oos_access": {"status": "NOT_ACCESSED", "records": []},
        "known_limitations": [
            "No empirical strategy execution occurred.",
            "The configured dataset failed raw-data qualification gates.",
            "No source license/version evidence is bound to the manifest.",
            "Financing, commission, and slippage schedules are not established.",
        ],
        "research_verdict": ApexOutcome.BLOCKED_DATA.value,
        "side_effects": {
            "strategy_execution": False,
            "promotion": False,
            "capital_allocation": False,
            "broker_connection": False,
            "live_execution": False,
        },
        "stable_result_identity": _hash(
            {
                "protocol_hash": protocol.protocol_hash,
                "preflight": preflight.canonical(qualification_reference),
                "source_snapshot_content_digest": source_identity.snapshot_content_digest,
            }
        ),
    }


def render_blocked_report(result: Mapping[str, object]) -> str:
    selected = cast(Mapping[str, object], result["selected_dataset"])
    source = cast(Mapping[str, object], result["source_identity"])
    blockers = cast(Sequence[object], selected["blockers"])
    lines = [
        "# APEX Milestone 5 — First Governed Empirical Baseline Evaluation",
        "",
        f"Research verdict: **{result['outcome']}**",
        "",
        (
            "No strategy execution occurred because the configured historical dataset "
            "failed the authoritative data gate. Profitability was not evaluated."
        ),
        "",
        "## Frozen identities",
        "",
        f"- Protocol: `{result['protocol_id']}` / `{result['protocol_hash']}`",
        f"- Base commit: `{source['base_commit']}`",
        f"- Source snapshot/content digest: `{source['snapshot_content_digest']}`",
        f"- Dirty state: `{source['dirty_state']}`",
        f"- Dataset: `{selected['dataset_id']}`",
        f"- Qualification: `{selected['qualification_id']}` ({selected['qualification_verdict']})",
        "",
        "## Data-gate blockers",
        "",
    ]
    lines.extend(f"- `{blocker}`" for blocker in blockers)
    lines.extend(
        [
            "",
            "## Evaluation status",
            "",
            "| Stage | Status | Metrics |",
            "|---|---|---|",
            "| Development | NOT_EXECUTED | UNAVAILABLE |",
            "| Walk-forward | NOT_EXECUTED | UNAVAILABLE |",
            "| Locked OOS | NOT_EXECUTED | UNAVAILABLE |",
            "",
            (
                "The predeclared baseline is `forex_trend_following.v1` with its "
                "registered 10/30 EMA parameters and target quantity 1. The protocol "
                "includes no-trade and buy-and-hold controls, explicit base/adverse cost "
                "scenarios, next-bar execution, warmup exclusion, sample-adequacy rules, "
                "and locked-OOS access rules. These declarations were frozen before any "
                "performance inspection."
            ),
            "",
            "No promotion, capital allocation, broker connection, or live execution occurred.",
        ]
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "ApexOutcome",
    "ApexProtocol",
    "ApexProtocolRegistry",
    "DatasetPreflight",
    "SourceIdentity",
    "build_blocked_result",
    "build_first_baseline_protocol",
    "collect_source_identity",
    "inventory_dataset_ids",
    "qualify_admitted_dataset",
    "render_blocked_report",
    "strategy_artifact_hash",
]
