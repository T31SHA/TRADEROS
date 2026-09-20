"""Small durable registry for immutable Phase 9 experiment evidence."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from traderos.data.time import require_utc
from traderos.research.models import (
    ExperimentInputBinding,
    ExperimentResult,
    ExperimentSpec,
    ResearchDecision,
    ResearchError,
    ResearchScope,
)


@dataclass(frozen=True)
class RegisteredExperiment:
    """One exact persisted experiment/result pair and its self-checking hash."""

    spec: ExperimentSpec
    result: ExperimentResult

    def __post_init__(self) -> None:
        if self.spec.experiment_id != self.result.experiment_id:
            raise ResearchError("result identity must equal its experiment identity")
        expected_oos_touch = self.spec.scope is ResearchScope.LOCKED_OUT_OF_SAMPLE
        if self.result.locked_oos_touched != expected_oos_touch:
            raise ResearchError("result locked-OOS flag differs from its experiment scope")
        if (
            self.spec.selection_eligible
            and not self.spec.dataset.quality_status.lower().endswith("fixture")
            and self.spec.input_binding is None
        ):
            raise ResearchError(
                "empirical selection-eligible experiments require verified input binding"
            )

    def canonical(self) -> dict[str, object]:
        return {
            "schema_version": (
                "phase9-experiment.v2"
                if self.spec.input_binding is not None
                else "phase9-experiment.v1"
            ),
            "experiment_id": self.spec.experiment_id,
            "experiment": self.spec.canonical(),
            "result": {
                "experiment_id": self.result.experiment_id,
                "metrics": list(self.result.metrics),
                "decision": self.result.decision.value,
                "locked_oos_touched": self.result.locked_oos_touched,
                "selection_influenced_by_locked_oos": (
                    self.result.selection_influenced_by_locked_oos
                ),
                "notes": list(self.result.notes),
                "result_hash": self.result.result_hash,
            },
        }


class ExperimentRegistry:
    """Filesystem registry with atomic create-only persistence and replay checks.

    The directory is intentionally caller-provided: the registry cannot write
    into paper-trading persistence or runtime configuration. A repeated write
    is idempotent only when every persisted byte-equivalent research fact is
    identical; conflicting reuse of an experiment ID fails loudly.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def record(self, registered: RegisteredExperiment) -> Path:
        """Persist one immutable record, atomically rejecting conflicting retries."""

        destination = self._path_for(registered.spec.experiment_id)
        encoded = self._encode(registered)
        temporary = self._root / f".{registered.spec.experiment_id}.{uuid4().hex}.tmp"
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                existing = destination.read_bytes()
                if existing != encoded:
                    raise ResearchError(
                        "conflicting result for immutable experiment identity"
                    ) from None
            finally:
                self._remove_temporary(temporary)
        except OSError as exc:
            self._remove_temporary(temporary)
            raise ResearchError("unable to persist experiment registry record") from exc
        return destination

    def read(self, experiment_id: str) -> dict[str, object]:
        """Read and validate the stable JSON document for an existing experiment."""

        if not experiment_id.strip():
            raise ResearchError("experiment identity must not be blank")
        try:
            payload = json.loads(self._path_for(experiment_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ResearchError("experiment record does not exist") from exc
        except json.JSONDecodeError as exc:
            raise ResearchError("experiment registry record is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") not in {
            "phase9-experiment.v1",
            "phase9-experiment.v2",
        }:
            raise ResearchError("experiment registry record has an unknown schema")
        if payload.get("experiment_id") != experiment_id:
            raise ResearchError("experiment registry identity does not match its path")
        experiment = payload.get("experiment")
        if not isinstance(experiment, dict):
            raise ResearchError("experiment registry experiment is malformed")
        if payload["schema_version"] == "phase9-experiment.v2":
            if "input_binding" not in experiment:
                raise ResearchError("versioned experiment is missing its input binding")
            try:
                ExperimentInputBinding.from_canonical(experiment["input_binding"])
            except ResearchError as exc:
                raise ResearchError("experiment registry input binding is malformed") from exc
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ResearchError("experiment registry result is malformed")
        try:
            metrics_value = result["metrics"]
            notes_value = result["notes"]
            if not isinstance(metrics_value, list) or not isinstance(notes_value, list):
                raise TypeError
            metrics = tuple(
                (str(item[0]), item[1] if item[1] is None else str(item[1]))
                for item in metrics_value
                if isinstance(item, list) and len(item) == 2
            )
            if len(metrics) != len(metrics_value):
                raise TypeError
            locked_oos_touched = result["locked_oos_touched"]
            contaminated = result["selection_influenced_by_locked_oos"]
            if type(locked_oos_touched) is not bool or type(contaminated) is not bool:
                raise TypeError
            parsed = ExperimentResult(
                experiment_id=experiment_id,
                metrics=metrics,
                decision=ResearchDecision(str(result["decision"])),
                locked_oos_touched=locked_oos_touched,
                selection_influenced_by_locked_oos=contaminated,
                notes=tuple(str(item) for item in notes_value),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchError("experiment registry result is malformed") from exc
        if result.get("result_hash") != parsed.result_hash:
            raise ResearchError("experiment registry result hash mismatch")
        return payload

    def list_experiment_ids(self, *, limit: int = 100) -> tuple[str, ...]:
        """Return a deterministic, bounded index of immutable experiment records.

        This is intentionally an index operation: callers must use ``read``
        for integrity validation of a selected record.  Unrelated files and
        subdirectories are ignored, and the registry is never modified.
        """

        if limit <= 0:
            raise ResearchError("experiment listing limit must be positive")
        if not self._root.exists():
            return ()
        try:
            paths = sorted(
                (
                    path
                    for path in self._root.glob("research-*.json")
                    if path.is_file()
                ),
                key=lambda path: path.name,
            )
        except OSError as exc:
            raise ResearchError("unable to list experiment registry records") from exc
        return tuple(path.stem for path in paths[:limit])

    def record_oos_access(self, *, experiment: ExperimentSpec, accessed_at: datetime) -> Path:
        """Record each locked-OOS read as an immutable, replay-visible event."""

        if experiment.scope is not ResearchScope.LOCKED_OUT_OF_SAMPLE:
            raise ResearchError("only locked OOS experiments require OOS access records")
        require_utc(accessed_at)
        self._validate_oos_parent(experiment)
        payload = {
            "schema_version": "phase9-oos-access.v1",
            "experiment_id": experiment.experiment_id,
            "dataset_version": experiment.dataset.dataset_version,
            "dataset_hash": experiment.dataset.dataset_hash,
            "accessed_at": accessed_at.isoformat(),
        }
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        access_id = hashlib.sha256(encoded).hexdigest()
        destination = self._root / "oos-access" / experiment.experiment_id / f"{access_id}.json"
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.read_bytes() != encoded:
                    raise ResearchError("conflicting locked OOS access record") from None
        except OSError as exc:
            raise ResearchError("unable to persist locked OOS access record") from exc
        finally:
            self._remove_temporary(temporary)
        return destination

    def _validate_oos_parent(self, experiment: ExperimentSpec) -> None:
        """Require a registered, non-OOS parent with matching research inputs."""

        parent_id = experiment.frozen_from_experiment_id
        if parent_id is None:
            raise ResearchError("locked OOS experiment requires a frozen parent experiment")
        try:
            parent_payload = self.read(parent_id)
        except ResearchError as exc:
            raise ResearchError("locked OOS parent experiment is not registered") from exc
        parent = parent_payload.get("experiment")
        if not isinstance(parent, dict):
            raise ResearchError("locked OOS parent experiment is malformed")
        if parent.get("scope") not in {
            ResearchScope.DEVELOPMENT.value,
            ResearchScope.WALK_FORWARD.value,
        }:
            raise ResearchError("locked OOS parent experiment must be developmental")
        child = experiment.canonical()
        lineage_fields = (
            "dataset_version",
            "dataset_hash",
            "split_id",
            "strategy_id",
            "strategy_version",
            "parameters",
            "feature_versions",
            "regime_configuration_id",
            "fusion_configuration_id",
            "risk_policy_configuration_id",
            "cost_scenario_id",
            "code_version",
            "research_family_id",
            "candidate_id",
        )
        if any(
            json.dumps(parent.get(field), sort_keys=True, separators=(",", ":"))
            != json.dumps(child.get(field), sort_keys=True, separators=(",", ":"))
            for field in lineage_fields
        ):
            raise ResearchError("locked OOS parent experiment lineage differs")

    def _path_for(self, experiment_id: str) -> Path:
        if not experiment_id.startswith("research-") or "/" in experiment_id:
            raise ResearchError("unsafe experiment identity")
        return self._root / f"{experiment_id}.json"

    @staticmethod
    def _encode(registered: RegisteredExperiment) -> bytes:
        return (
            json.dumps(registered.canonical(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()

    @staticmethod
    def _remove_temporary(temporary: Path) -> None:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


__all__ = ["ExperimentRegistry", "RegisteredExperiment"]
