"""Small durable registry for immutable Phase 9 experiment evidence."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from traderos.research.models import ExperimentResult, ExperimentSpec, ResearchError, ResearchScope


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

    def canonical(self) -> dict[str, object]:
        return {
            "schema_version": "phase9-experiment.v1",
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
        if not isinstance(payload, dict) or payload.get("schema_version") != "phase9-experiment.v1":
            raise ResearchError("experiment registry record has an unknown schema")
        return payload

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
