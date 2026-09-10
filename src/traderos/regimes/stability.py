"""Descriptive regime-history analysis that never feeds detector decisions."""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from traderos.regimes.errors import RegimeAnalysisError
from traderos.regimes.models import RegimeState


class RegimeDimension(StrEnum):
    """Independent dimensions reported by the Phase 5 detector."""

    TREND = "trend"
    VOLATILITY = "volatility"
    LIQUIDITY = "liquidity"
    DATA_SESSION = "data_session"


@dataclass(frozen=True)
class RegimeRun:
    """Consecutive observations of one state within one dimension."""

    dimension: RegimeDimension
    state: str
    start: datetime
    end: datetime
    observation_count: int
    duration: timedelta


@dataclass(frozen=True)
class RegimeShare:
    """Descriptive observation share for one state within one dimension."""

    dimension: RegimeDimension
    state: str
    observation_count: int
    percentage: float


@dataclass(frozen=True)
class DimensionStability:
    """Transitions, frequency, runs, and shares for one regime dimension."""

    dimension: RegimeDimension
    transition_count: int
    transition_frequency: float
    runs: tuple[RegimeRun, ...]
    shares: tuple[RegimeShare, ...]


@dataclass(frozen=True)
class RegimeStabilityReport:
    """Offline descriptive report over an already produced state sequence."""

    observation_count: int
    dimensions: tuple[DimensionStability, ...]


def _values(state: RegimeState, dimension: RegimeDimension) -> str:
    return str(getattr(state, dimension.value).value)


def _validate_states(states: tuple[RegimeState, ...]) -> None:
    if not states:
        raise RegimeAnalysisError("at least one regime state is required")
    first = states[0]
    previous = None
    for state in states:
        if state.instrument != first.instrument or state.timeframe is not first.timeframe:
            raise RegimeAnalysisError("stability analysis requires one instrument and timeframe")
        if (
            state.regime_detector_id != first.regime_detector_id
            or state.regime_detector_version != first.regime_detector_version
            or state.configuration_id != first.configuration_id
        ):
            raise RegimeAnalysisError("stability analysis requires one detector configuration")
        if previous is not None and state.decision_timestamp <= previous:
            raise RegimeAnalysisError("regime states must be strictly ordered")
        previous = state.decision_timestamp


def analyze_regime_stability(
    states: tuple[RegimeState, ...] | list[RegimeState],
) -> RegimeStabilityReport:
    """Summarize an existing history without modifying detector inputs or state."""

    ordered = tuple(states)
    _validate_states(ordered)
    results: list[DimensionStability] = []
    for dimension in RegimeDimension:
        values = [_values(state, dimension) for state in ordered]
        transitions = sum(left != right for left, right in zip(values, values[1:], strict=False))
        runs: list[RegimeRun] = []
        run_start = 0
        for index in range(1, len(ordered) + 1):
            if index == len(ordered) or values[index] != values[run_start]:
                start = ordered[run_start].decision_timestamp
                end = ordered[index - 1].decision_timestamp
                runs.append(
                    RegimeRun(
                        dimension=dimension,
                        state=values[run_start],
                        start=start,
                        end=end,
                        observation_count=index - run_start,
                        duration=end - start + ordered[0].timeframe.duration,
                    )
                )
                run_start = index
        counts = Counter(values)
        shares = tuple(
            RegimeShare(
                dimension=dimension,
                state=state,
                observation_count=count,
                percentage=count / len(ordered),
            )
            for state, count in sorted(counts.items())
        )
        results.append(
            DimensionStability(
                dimension=dimension,
                transition_count=transitions,
                transition_frequency=transitions / (len(ordered) - 1) if len(ordered) > 1 else 0.0,
                runs=tuple(runs),
                shares=shares,
            )
        )
    return RegimeStabilityReport(observation_count=len(ordered), dimensions=tuple(results))


__all__ = [
    "DimensionStability",
    "RegimeDimension",
    "RegimeRun",
    "RegimeShare",
    "RegimeStabilityReport",
    "analyze_regime_stability",
]
