"""Immutable, time-aware contracts for Phase 9 research validation.

The research boundary records evidence; it never alters a runtime strategy,
risk policy, fusion policy, or paper account.  Locked out-of-sample data is an
evaluation scope, never a candidate-selection scope.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import cast

from traderos.backtesting.models import BacktestConfig, CostConfig, SlippageConfig, SpreadConfig
from traderos.data.bars import MarketBar
from traderos.data.hashing import market_bar_content_hash, market_bar_content_payload
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.time import require_utc
from traderos.data.timeframes import Timeframe


class ResearchError(ValueError):
    """Raised when a research artifact would break reproducibility or chronology."""


class OosContaminationError(ResearchError):
    """Raised when locked OOS data is proposed for candidate selection."""


class ResearchScope(StrEnum):
    """The only permitted uses of data in a Phase 9 experiment."""

    DEVELOPMENT = "development"
    WALK_FORWARD = "walk_forward"
    LOCKED_OUT_OF_SAMPLE = "locked_out_of_sample"


class WalkForwardMode(StrEnum):
    """Temporal training-window choices; both preserve chronological order."""

    EXPANDING = "expanding"
    ROLLING = "rolling"


class ResearchDecision(StrEnum):
    """Evidence-based research outcomes, deliberately separate from deployment."""

    PASS = "pass"
    CONDITIONAL = "conditional"
    KILL = "kill"
    INVALID = "invalid"


@dataclass(frozen=True)
class TemporalRange:
    """A half-open, UTC temporal interval ``[start, end)``."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        require_utc(self.start)
        require_utc(self.end)
        if self.start >= self.end:
            raise ResearchError("research period start must precede end")

    def contains(self, timestamp: datetime) -> bool:
        require_utc(timestamp)
        return self.start <= timestamp < self.end


@dataclass(frozen=True)
class ResearchSplit:
    """Development and final locked OOS periods with no temporal overlap."""

    development: TemporalRange
    locked_out_of_sample: TemporalRange
    split_id: str

    def __post_init__(self) -> None:
        if not self.split_id.strip():
            raise ResearchError("research split identity must not be blank")
        if self.development.end > self.locked_out_of_sample.start:
            raise ResearchError("development and locked OOS periods must not overlap")

    def scope_for(self, period: TemporalRange) -> ResearchScope:
        if self.development.start <= period.start and period.end <= self.development.end:
            return ResearchScope.DEVELOPMENT
        if (
            self.locked_out_of_sample.start <= period.start
            and period.end <= self.locked_out_of_sample.end
        ):
            return ResearchScope.LOCKED_OUT_OF_SAMPLE
        raise ResearchError(
            "experiment period crosses or falls outside the declared research split"
        )


@dataclass(frozen=True)
class WalkForwardConfig:
    """Deterministic fold plan that ends before a locked OOS boundary."""

    mode: WalkForwardMode
    initial_train: TemporalRange
    forward_duration: timedelta
    fold_count: int
    locked_oos_start: datetime
    rolling_train_duration: timedelta | None = None

    def __post_init__(self) -> None:
        require_utc(self.locked_oos_start)
        if self.forward_duration <= timedelta(0):
            raise ResearchError("walk-forward duration must be positive")
        if self.fold_count < 1:
            raise ResearchError("walk-forward fold count must be positive")
        if self.initial_train.end >= self.locked_oos_start:
            raise ResearchError("initial training period must end before locked OOS")
        if self.mode is WalkForwardMode.ROLLING:
            if self.rolling_train_duration is None or self.rolling_train_duration <= timedelta(0):
                raise ResearchError("rolling walk-forward requires a positive training duration")
        elif self.rolling_train_duration is not None:
            raise ResearchError("expanding walk-forward must not set rolling training duration")

    def folds(self) -> tuple[WalkForwardFold, ...]:
        """Generate monotonic folds without inspecting any market performance."""

        values: list[WalkForwardFold] = []
        train_end = self.initial_train.end
        for index in range(self.fold_count):
            test_start = train_end
            test_end = test_start + self.forward_duration
            if test_end > self.locked_oos_start:
                raise ResearchError("walk-forward fold would enter locked OOS")
            if self.mode is WalkForwardMode.EXPANDING:
                train_start = self.initial_train.start
            else:
                assert self.rolling_train_duration is not None
                train_start = train_end - self.rolling_train_duration
            if train_start < self.initial_train.start:
                raise ResearchError("rolling training window predates declared research data")
            values.append(
                WalkForwardFold(
                    fold_index=index,
                    train=TemporalRange(train_start, train_end),
                    test=TemporalRange(test_start, test_end),
                )
            )
            train_end = test_end
        return tuple(values)


@dataclass(frozen=True)
class WalkForwardFold:
    """One frozen training period followed by a strictly later forward test."""

    fold_index: int
    train: TemporalRange
    test: TemporalRange

    def __post_init__(self) -> None:
        if self.fold_index < 0:
            raise ResearchError("walk-forward fold index must be non-negative")
        if self.train.end > self.test.start:
            raise ResearchError("walk-forward train and test periods must not overlap")


@dataclass(frozen=True)
class DatasetManifest:
    """Stable, data-governance metadata for a concrete immutable bar set."""

    dataset_version: str
    dataset_hash: str
    symbols: tuple[str, ...]
    timeframe: Timeframe
    period: TemporalRange
    adjustment_policy: AdjustmentPolicy
    source: str
    quality_status: str
    historical_membership_available: bool
    delistings_available: bool
    corporate_actions_verified: bool

    def __post_init__(self) -> None:
        if not self.dataset_version.strip() or not self.dataset_hash.strip():
            raise ResearchError("dataset version and hash must not be blank")
        if not self.source.strip() or not self.quality_status.strip():
            raise ResearchError("dataset source and quality status must not be blank")
        if not self.symbols or any(not value.strip() for value in self.symbols):
            raise ResearchError("dataset manifest requires non-blank symbols")
        if len(set(self.symbols)) != len(self.symbols):
            raise ResearchError("dataset manifest symbols must be unique")

    @classmethod
    def _content_payload(cls, bars: Sequence[MarketBar]) -> dict[str, object]:
        return market_bar_content_payload(bars)

    @classmethod
    def content_hash(cls, bars: Sequence[MarketBar]) -> str:
        """Hash the exact canonical parent bars used by a research manifest."""

        if not bars:
            raise ResearchError("a dataset content hash requires at least one bar")
        return market_bar_content_hash(bars)

    @classmethod
    def from_bars(
        cls,
        *,
        dataset_version: str,
        bars: Sequence[MarketBar],
        quality_status: str,
        historical_membership_available: bool,
        delistings_available: bool,
        corporate_actions_verified: bool,
    ) -> DatasetManifest:
        """Create a stable manifest after enforcing one logical research series."""

        if not bars:
            raise ResearchError("a dataset manifest requires at least one bar")
        ordered = tuple(sorted(bars, key=lambda item: (item.timestamp, item.symbol, item.source)))
        first = ordered[0]
        if any(
            item.timeframe is not first.timeframe
            or item.adjustment_policy is not first.adjustment_policy
            or item.source != first.source
            for item in ordered
        ):
            raise ResearchError(
                "a dataset manifest cannot mix timeframe, adjustment policy, or source"
            )
        identities = [(item.timestamp, item.symbol, item.source) for item in ordered]
        if len(set(identities)) != len(identities):
            raise ResearchError("dataset manifest bars must have unique logical identities")
        return cls(
            dataset_version=dataset_version,
            dataset_hash=cls.content_hash(ordered),
            symbols=tuple(sorted({item.symbol for item in ordered})),
            timeframe=first.timeframe,
            period=TemporalRange(
                ordered[0].timestamp, ordered[-1].timestamp + first.timeframe.duration
            ),
            adjustment_policy=first.adjustment_policy,
            source=first.source,
            quality_status=quality_status,
            historical_membership_available=historical_membership_available,
            delistings_available=delistings_available,
            corporate_actions_verified=corporate_actions_verified,
        )

    @property
    def equity_bias_unresolved(self) -> bool:
        """Whether an equity/ETF result must disclose survivorship/action uncertainty."""

        return not (
            self.historical_membership_available
            and self.delistings_available
            and self.corporate_actions_verified
        )


@dataclass(frozen=True)
class ExperimentInputBinding:
    """Exact immutable dataset and qualification inputs for a new experiment."""

    dataset_id: str
    dataset_content_hash: str
    manifest_hash: str
    qualification_id: str
    qualification_policy_id: str
    qualification_policy_version: str
    evaluation_period: TemporalRange
    warmup_period: TemporalRange | None
    instrument_scope: tuple[str, ...]
    data_slice_identity: str
    feature_input_lineage: tuple[str, ...] = ()
    fixture: bool = False

    def __post_init__(self) -> None:
        text_values = (
            self.dataset_id,
            self.dataset_content_hash,
            self.manifest_hash,
            self.qualification_id,
            self.qualification_policy_id,
            self.qualification_policy_version,
            self.data_slice_identity,
        )
        if any(not value.strip() for value in text_values):
            raise ResearchError("experiment input binding identities must not be blank")
        if not self.instrument_scope or any(not value.strip() for value in self.instrument_scope):
            raise ResearchError("experiment input binding requires instruments")
        if len(set(self.instrument_scope)) != len(self.instrument_scope):
            raise ResearchError("experiment input binding instruments must be unique")
        if any(not value.strip() for value in self.feature_input_lineage):
            raise ResearchError("experiment feature lineage references must not be blank")
        if self.warmup_period is not None and self.warmup_period.end > self.evaluation_period.start:
            raise ResearchError("experiment warmup must end before evaluation begins")
        if self.data_slice_identity != self.derived_slice_identity():
            raise ResearchError("experiment data slice identity does not match its declaration")

    def derived_slice_identity(self) -> str:
        payload = {
            "dataset_id": self.dataset_id,
            "dataset_content_hash": self.dataset_content_hash,
            "manifest_hash": self.manifest_hash,
            "evaluation_period": {
                "start": self.evaluation_period.start.isoformat(),
                "end": self.evaluation_period.end.isoformat(),
            },
            "warmup_period": (
                {
                    "start": self.warmup_period.start.isoformat(),
                    "end": self.warmup_period.end.isoformat(),
                }
                if self.warmup_period is not None
                else None
            ),
            "instrument_scope": self.instrument_scope,
            "feature_input_lineage": self.feature_input_lineage,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def canonical(self) -> dict[str, object]:
        return {
            "schema_version": "experiment-input-binding.v1",
            "dataset_id": self.dataset_id,
            "dataset_content_hash": self.dataset_content_hash,
            "manifest_hash": self.manifest_hash,
            "qualification_id": self.qualification_id,
            "qualification_policy_id": self.qualification_policy_id,
            "qualification_policy_version": self.qualification_policy_version,
            "evaluation_period": {
                "start": self.evaluation_period.start.isoformat(),
                "end": self.evaluation_period.end.isoformat(),
            },
            "warmup_period": (
                {
                    "start": self.warmup_period.start.isoformat(),
                    "end": self.warmup_period.end.isoformat(),
                }
                if self.warmup_period is not None
                else None
            ),
            "instrument_scope": list(self.instrument_scope),
            "data_slice_identity": self.data_slice_identity,
            "feature_input_lineage": list(self.feature_input_lineage),
            "fixture": self.fixture,
        }

    @classmethod
    def from_canonical(cls, payload: object) -> ExperimentInputBinding:
        fields = {
            "schema_version",
            "dataset_id",
            "dataset_content_hash",
            "manifest_hash",
            "qualification_id",
            "qualification_policy_id",
            "qualification_policy_version",
            "evaluation_period",
            "warmup_period",
            "instrument_scope",
            "data_slice_identity",
            "feature_input_lineage",
            "fixture",
        }
        if not isinstance(payload, dict) or set(payload) != fields:
            raise ResearchError("experiment input binding has unknown or missing fields")
        if payload["schema_version"] != "experiment-input-binding.v1":
            raise ResearchError("experiment input binding schema is unsupported")
        identity_fields = (
            "dataset_id",
            "dataset_content_hash",
            "manifest_hash",
            "qualification_id",
            "qualification_policy_id",
            "qualification_policy_version",
            "data_slice_identity",
        )
        if any(
            not isinstance(payload[field], str) or not payload[field].strip()
            for field in identity_fields
        ):
            raise ResearchError("experiment input binding identity is malformed")

        def parse_range(value: object, field: str) -> TemporalRange:
            if not isinstance(value, dict) or set(value) != {"start", "end"}:
                raise ResearchError(f"experiment input binding {field} is malformed")
            try:
                return TemporalRange(
                    datetime.fromisoformat(cast(str, value["start"])),
                    datetime.fromisoformat(cast(str, value["end"])),
                )
            except (TypeError, ValueError) as exc:
                raise ResearchError(f"experiment input binding {field} is malformed") from exc

        evaluation_period = parse_range(payload["evaluation_period"], "evaluation period")
        warmup_value = payload["warmup_period"]
        warmup_period = None if warmup_value is None else parse_range(warmup_value, "warmup period")
        scope = payload["instrument_scope"]
        lineage = payload["feature_input_lineage"]
        if not isinstance(scope, list) or any(not isinstance(item, str) for item in scope):
            raise ResearchError("experiment input binding instrument scope is malformed")
        if not isinstance(lineage, list) or any(not isinstance(item, str) for item in lineage):
            raise ResearchError("experiment feature lineage is malformed")
        if type(payload["fixture"]) is not bool:
            raise ResearchError("experiment fixture classification is malformed")
        return cls(
            dataset_id=cast(str, payload["dataset_id"]),
            dataset_content_hash=cast(str, payload["dataset_content_hash"]),
            manifest_hash=cast(str, payload["manifest_hash"]),
            qualification_id=cast(str, payload["qualification_id"]),
            qualification_policy_id=cast(str, payload["qualification_policy_id"]),
            qualification_policy_version=cast(str, payload["qualification_policy_version"]),
            evaluation_period=evaluation_period,
            warmup_period=warmup_period,
            instrument_scope=tuple(scope),
            data_slice_identity=cast(str, payload["data_slice_identity"]),
            feature_input_lineage=tuple(lineage),
            fixture=payload["fixture"],
        )


@dataclass(frozen=True)
class CostScenario:
    """Versioned multiplier for Phase 3's existing deterministic cost model."""

    scenario_id: str
    multiplier: Decimal

    def __post_init__(self) -> None:
        if not self.scenario_id.strip():
            raise ResearchError("cost scenario identity must not be blank")
        if not self.multiplier.is_finite() or self.multiplier < 0:
            raise ResearchError("cost multiplier must be finite and non-negative")

    def apply(self, config: BacktestConfig) -> BacktestConfig:
        """Return a new Phase 3 configuration; do not mutate a base experiment."""

        return config.model_copy(
            update={
                "commission": CostConfig(
                    per_unit=config.commission.per_unit * self.multiplier,
                    rate=config.commission.rate * self.multiplier,
                    minimum=config.commission.minimum * self.multiplier,
                ),
                "spread": SpreadConfig(
                    fallback_absolute=config.spread.fallback_absolute * self.multiplier,
                    observed_multiplier=self.multiplier,
                ),
                "slippage": SlippageConfig(absolute=config.slippage.absolute * self.multiplier),
            }
        )


@dataclass(frozen=True)
class ExperimentSpec:
    """All non-result inputs required to reproduce and classify an experiment."""

    dataset: DatasetManifest
    split_id: str
    scope: ResearchScope
    period: TemporalRange
    strategy_id: str
    strategy_version: str
    parameters: tuple[tuple[str, str], ...]
    feature_versions: tuple[tuple[str, int], ...]
    regime_configuration_id: str | None
    fusion_configuration_id: str | None
    risk_policy_configuration_id: str | None
    backtest_experiment_id: str
    cost_scenario_id: str
    code_version: str
    random_seed: int | None
    selection_eligible: bool
    frozen_from_experiment_id: str | None = None
    research_family_id: str | None = None
    candidate_id: str | None = None
    input_binding: ExperimentInputBinding | None = None

    def __post_init__(self) -> None:
        required = (
            self.split_id,
            self.strategy_id,
            self.strategy_version,
            self.backtest_experiment_id,
            self.cost_scenario_id,
            self.code_version,
        )
        if any(not value.strip() for value in required):
            raise ResearchError("experiment identity fields must not be blank")
        names = [item[0] for item in self.parameters]
        if any(not name.strip() for name in names) or len(set(names)) != len(names):
            raise ResearchError("experiment parameter names must be unique and non-blank")
        if self.scope is ResearchScope.LOCKED_OUT_OF_SAMPLE and self.selection_eligible:
            raise OosContaminationError("locked OOS experiments cannot be eligible for selection")
        if self.scope is ResearchScope.LOCKED_OUT_OF_SAMPLE and not self.frozen_from_experiment_id:
            raise ResearchError("locked OOS evaluation requires a frozen development experiment")
        if (self.research_family_id is None) != (self.candidate_id is None):
            raise ResearchError(
                "research family and candidate identities must be supplied together"
            )
        if self.input_binding is not None:
            if self.input_binding.dataset_content_hash != self.dataset.dataset_hash:
                raise ResearchError("experiment input binding dataset content does not match spec")
            if self.input_binding.evaluation_period != self.period:
                raise ResearchError(
                    "experiment input binding evaluation period does not match spec"
                )
            if not set(self.input_binding.instrument_scope).issubset(self.dataset.symbols):
                raise ResearchError(
                    "experiment input binding instrument scope exceeds dataset scope"
                )
            fixture_quality = self.dataset.quality_status.lower().endswith("fixture")
            if fixture_quality != self.input_binding.fixture:
                raise ResearchError("experiment fixture classification does not match dataset")

    @classmethod
    def from_parameters(
        cls,
        *,
        parameters: Mapping[str, object],
        **kwargs: object,
    ) -> ExperimentSpec:
        """Canonicalize parameters so caller mapping order cannot affect identity."""

        pairs = tuple(sorted((key, str(value)) for key, value in parameters.items()))
        return cls(parameters=pairs, **kwargs)  # type: ignore[arg-type]

    def canonical(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "dataset_version": self.dataset.dataset_version,
            "dataset_hash": self.dataset.dataset_hash,
            "split_id": self.split_id,
            "scope": self.scope.value,
            "period": {"start": self.period.start.isoformat(), "end": self.period.end.isoformat()},
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "parameters": list(self.parameters),
            "feature_versions": list(self.feature_versions),
            "regime_configuration_id": self.regime_configuration_id,
            "fusion_configuration_id": self.fusion_configuration_id,
            "risk_policy_configuration_id": self.risk_policy_configuration_id,
            "backtest_experiment_id": self.backtest_experiment_id,
            "cost_scenario_id": self.cost_scenario_id,
            "code_version": self.code_version,
            "random_seed": self.random_seed,
            "selection_eligible": self.selection_eligible,
            "frozen_from_experiment_id": self.frozen_from_experiment_id,
            "research_family_id": self.research_family_id,
            "candidate_id": self.candidate_id,
        }
        if self.input_binding is not None:
            payload["input_binding"] = self.input_binding.canonical()
        return payload

    @property
    def experiment_id(self) -> str:
        encoded = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":")).encode()
        return f"research-{hashlib.sha256(encoded).hexdigest()[:24]}"


@dataclass(frozen=True)
class ResearchPlan:
    """Immutable experiment guardrail joining a split, data manifest, and folds.

    The plan has no optimization operation by design. Callers may evaluate a
    locked period only after they reference a frozen development experiment;
    they can never mark that evaluation as selection-eligible.
    """

    split: ResearchSplit
    dataset: DatasetManifest
    walk_forward: WalkForwardConfig

    def __post_init__(self) -> None:
        if self.walk_forward.locked_oos_start != self.split.locked_out_of_sample.start:
            raise ResearchError("walk-forward and research split disagree on locked OOS boundary")
        if self.dataset.period.start > self.split.development.start:
            raise ResearchError("dataset does not include the declared development start")
        if self.dataset.period.end < self.split.locked_out_of_sample.end:
            raise ResearchError("dataset does not include the declared locked OOS end")
        for fold in self.walk_forward.folds():
            if (
                fold.train.start < self.split.development.start
                or fold.test.end > self.split.development.end
            ):
                raise ResearchError("walk-forward fold falls outside the development period")

    def validate_experiment(self, spec: ExperimentSpec) -> None:
        """Reject cross-dataset, cross-split, or locked-selection submissions."""

        if spec.dataset != self.dataset:
            raise ResearchError("experiment dataset differs from the research plan")
        if spec.split_id != self.split.split_id:
            raise ResearchError("experiment split identity differs from the research plan")
        if spec.scope is ResearchScope.LOCKED_OUT_OF_SAMPLE:
            if self.split.scope_for(spec.period) is not ResearchScope.LOCKED_OUT_OF_SAMPLE:
                raise ResearchError("locked OOS experiment lies outside the locked period")
            return
        if self.split.scope_for(spec.period) is not ResearchScope.DEVELOPMENT:
            raise ResearchError(
                "development or walk-forward experiment lies outside development data"
            )


@dataclass(frozen=True)
class ExperimentResult:
    """Immutable result facts; selection contamination is explicit and durable."""

    experiment_id: str
    metrics: tuple[tuple[str, str | None], ...]
    decision: ResearchDecision
    locked_oos_touched: bool
    selection_influenced_by_locked_oos: bool
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.experiment_id.strip():
            raise ResearchError("result experiment identity must not be blank")
        names = [item[0] for item in self.metrics]
        if any(not name.strip() for name in names) or len(set(names)) != len(names):
            raise ResearchError("result metric names must be unique and non-blank")
        if self.selection_influenced_by_locked_oos and not self.locked_oos_touched:
            raise ResearchError("only a touched locked OOS result can be marked contaminated")
        if (
            self.selection_influenced_by_locked_oos
            and self.decision is not ResearchDecision.INVALID
        ):
            raise ResearchError("a locked-OOS-contaminated result must be classified invalid")

    @property
    def contaminated(self) -> bool:
        return self.selection_influenced_by_locked_oos

    @property
    def result_hash(self) -> str:
        payload = {
            "experiment_id": self.experiment_id,
            "metrics": self.metrics,
            "decision": self.decision.value,
            "locked_oos_touched": self.locked_oos_touched,
            "selection_influenced_by_locked_oos": self.selection_influenced_by_locked_oos,
            "notes": self.notes,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CostScenario",
    "DatasetManifest",
    "ExperimentResult",
    "ExperimentInputBinding",
    "ExperimentSpec",
    "OosContaminationError",
    "ResearchDecision",
    "ResearchError",
    "ResearchScope",
    "ResearchSplit",
    "ResearchPlan",
    "TemporalRange",
    "WalkForwardConfig",
    "WalkForwardFold",
    "WalkForwardMode",
]
