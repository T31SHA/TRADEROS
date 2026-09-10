"""Observation-only orchestration for deterministic regime detectors."""

from collections.abc import Iterable

from traderos.regimes.errors import RegimeCausalityError
from traderos.regimes.models import RegimeContext, RegimeDetector, RegimeState


class RegimeEngine:
    """Evaluate supplied causal contexts without retaining mutable history."""

    def __init__(self, detector: RegimeDetector) -> None:
        self.detector = detector

    def evaluate(self, context: RegimeContext) -> RegimeState:
        """Classify one completed-bar context."""

        return self.detector.evaluate(context)

    def evaluate_series(self, contexts: Iterable[RegimeContext]) -> tuple[RegimeState, ...]:
        """Classify an ordered single-series replay without cross-asset state."""

        states: list[RegimeState] = []
        previous_timestamp = None
        reference: tuple[object, object] | None = None
        for context in contexts:
            identity = (context.instrument, context.timeframe)
            if reference is None:
                reference = identity
            elif identity != reference:
                raise RegimeCausalityError(
                    "regime series must contain one instrument and timeframe"
                )
            if previous_timestamp is not None and context.decision_timestamp <= previous_timestamp:
                raise RegimeCausalityError("regime contexts must be strictly ordered")
            states.append(self.evaluate(context))
            previous_timestamp = context.decision_timestamp
        return tuple(states)


__all__ = ["RegimeEngine"]
