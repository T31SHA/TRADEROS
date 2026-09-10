"""Deterministic local provider used for tests and offline research."""

from collections.abc import Iterable

from traderos.data.bars import BarCandidate
from traderos.data.instruments import Instrument
from traderos.data.providers.base import HistoricalBarsRequest, ProviderHealth
from traderos.data.timeframes import Timeframe


class DeterministicLocalProvider:
    """Serve an immutable, caller-supplied fixture without network access."""

    provider_id = "local"

    def __init__(
        self,
        candidates: Iterable[BarCandidate] = (),
        instruments: Iterable[Instrument] = (),
    ) -> None:
        self._candidates = tuple(candidates)
        self._instruments = tuple(instruments)

    def get_instruments(self) -> tuple[Instrument, ...]:
        return self._instruments

    def get_historical_bars(self, request: HistoricalBarsRequest) -> tuple[BarCandidate, ...]:
        return tuple(
            candidate
            for candidate in self._candidates
            if candidate.instrument.canonical_symbol == request.instrument.canonical_symbol
            and candidate.timeframe is request.timeframe
            and (
                candidate.timestamp.tzinfo is None
                or request.start <= candidate.timestamp < request.end
            )
        )

    def get_latest_bar(self, instrument: Instrument, timeframe: Timeframe) -> BarCandidate | None:
        matching = [
            candidate
            for candidate in self._candidates
            if candidate.instrument.canonical_symbol == instrument.canonical_symbol
            and candidate.timeframe is timeframe
        ]
        return max(matching, key=lambda candidate: candidate.timestamp) if matching else None

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.provider_id,
            healthy=True,
            message="Deterministic local provider is available",
        )


__all__ = ["DeterministicLocalProvider"]
