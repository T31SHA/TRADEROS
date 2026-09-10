"""Pure, causal indicator calculations used by the feature engine.

Each routine processes observations in timestamp order and only reads a prefix
ending at the current row.  No routine fills missing observations or consults
rows after the value it is producing.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import log, sqrt
from statistics import mean

from traderos.data.bars import MarketBar
from traderos.features.errors import FeatureComputationError


@dataclass(frozen=True)
class ComputedFeature:
    """Numeric output plus enough state to distinguish warm-up from nulls."""

    values: tuple[float | None, ...]
    warmup_until: int
    undefined_indices: frozenset[int] = frozenset()


def _window(parameters: dict[str, int | float | str], default: int) -> int:
    value = parameters.get("window", default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FeatureComputationError("window must be a positive integer")
    return value


def _period(parameters: dict[str, int | float | str], default: int) -> int:
    value = parameters.get("period", default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FeatureComputationError("period must be a positive integer")
    return value


def _prices(bars: Sequence[MarketBar]) -> list[float]:
    return [float(bar.close) for bar in bars]


def _returns(prices: Sequence[float], logarithmic: bool) -> list[float | None]:
    result: list[float | None] = [None]
    for previous, current in zip(prices[:-1], prices[1:], strict=True):
        if previous <= 0 or current <= 0:
            raise FeatureComputationError("returns require strictly positive prices")
        result.append(log(current / previous) if logarithmic else current / previous - 1.0)
    return result


def _rolling_mean(values: Sequence[float], window: int) -> ComputedFeature:
    result: list[float | None] = [None] * len(values)
    for index in range(window - 1, len(values)):
        result[index] = mean(values[index - window + 1 : index + 1])
    return ComputedFeature(tuple(result), max(0, window - 1))


def _rolling_std(values: Sequence[float | None], window: int) -> ComputedFeature:
    result: list[float | None] = [None] * len(values)
    for index in range(window - 1, len(values)):
        current = values[index - window + 1 : index + 1]
        if all(value is not None for value in current):
            numeric = [value for value in current if value is not None]
            average = mean(numeric)
            result[index] = sqrt(sum((value - average) ** 2 for value in numeric) / len(numeric))
    return ComputedFeature(tuple(result), window)


def simple_return(bars: Sequence[MarketBar], _: dict[str, int | float | str]) -> ComputedFeature:
    return ComputedFeature(tuple(_returns(_prices(bars), logarithmic=False)), 1)


def log_return(bars: Sequence[MarketBar], _: dict[str, int | float | str]) -> ComputedFeature:
    return ComputedFeature(tuple(_returns(_prices(bars), logarithmic=True)), 1)


def rolling_return(
    bars: Sequence[MarketBar], parameters: dict[str, int | float | str]
) -> ComputedFeature:
    prices = _prices(bars)
    window = _window(parameters, 20)
    values: list[float | None] = [None] * len(prices)
    for index in range(window, len(prices)):
        values[index] = prices[index] / prices[index - window] - 1.0
    return ComputedFeature(tuple(values), window)


def sma(bars: Sequence[MarketBar], parameters: dict[str, int | float | str]) -> ComputedFeature:
    return _rolling_mean(_prices(bars), _window(parameters, 20))


def ema(bars: Sequence[MarketBar], parameters: dict[str, int | float | str]) -> ComputedFeature:
    prices = _prices(bars)
    window = _window(parameters, 20)
    values: list[float | None] = [None] * len(prices)
    if len(prices) < window:
        return ComputedFeature(tuple(values), max(0, window - 1))
    initial = mean(prices[:window])
    values[window - 1] = initial
    alpha = 2.0 / (window + 1.0)
    previous = initial
    for index in range(window, len(prices)):
        previous = alpha * prices[index] + (1.0 - alpha) * previous
        values[index] = previous
    return ComputedFeature(tuple(values), window - 1)


def moving_average_distance(
    bars: Sequence[MarketBar], parameters: dict[str, int | float | str]
) -> ComputedFeature:
    prices = _prices(bars)
    average_type = parameters.get("average", "sma")
    if average_type not in {"sma", "ema"}:
        raise FeatureComputationError("average must be 'sma' or 'ema'")
    average = sma(bars, parameters) if average_type == "sma" else ema(bars, parameters)
    values = [
        None if current is None or average_value is None else current / average_value - 1.0
        for current, average_value in zip(prices, average.values, strict=True)
    ]
    return ComputedFeature(tuple(values), average.warmup_until)


def rolling_volatility(
    bars: Sequence[MarketBar], parameters: dict[str, int | float | str]
) -> ComputedFeature:
    window = _window(parameters, 20)
    return _rolling_std(_returns(_prices(bars), logarithmic=False), window)


def realized_volatility(
    bars: Sequence[MarketBar], parameters: dict[str, int | float | str]
) -> ComputedFeature:
    window = _window(parameters, 20)
    return _rolling_std(_returns(_prices(bars), logarithmic=True), window)


def true_ranges(bars: Sequence[MarketBar]) -> tuple[float, ...]:
    if not bars:
        return ()
    result = [float(bars[0].high - bars[0].low)]
    for previous, current in zip(bars[:-1], bars[1:], strict=True):
        high = float(current.high)
        low = float(current.low)
        previous_close = float(previous.close)
        result.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return tuple(result)


def atr(bars: Sequence[MarketBar], parameters: dict[str, int | float | str]) -> ComputedFeature:
    window = _window(parameters, 14)
    return _rolling_mean(true_ranges(bars), window)


def rolling_high(
    bars: Sequence[MarketBar], parameters: dict[str, int | float | str]
) -> ComputedFeature:
    values: list[float] = [float(bar.high) for bar in bars]
    window = _window(parameters, 20)
    result: list[float | None] = [None] * len(values)
    for index in range(window - 1, len(values)):
        result[index] = max(values[index - window + 1 : index + 1])
    return ComputedFeature(tuple(result), window - 1)


def rolling_low(
    bars: Sequence[MarketBar], parameters: dict[str, int | float | str]
) -> ComputedFeature:
    values: list[float] = [float(bar.low) for bar in bars]
    window = _window(parameters, 20)
    result: list[float | None] = [None] * len(values)
    for index in range(window - 1, len(values)):
        result[index] = min(values[index - window + 1 : index + 1])
    return ComputedFeature(tuple(result), window - 1)


def range_position(
    bars: Sequence[MarketBar], parameters: dict[str, int | float | str]
) -> ComputedFeature:
    window = _window(parameters, 20)
    highs = rolling_high(bars, parameters).values
    lows = rolling_low(bars, parameters).values
    values: list[float | None] = [None] * len(bars)
    undefined: set[int] = set()
    for index, bar in enumerate(bars):
        high = highs[index]
        low = lows[index]
        if high is None or low is None:
            continue
        width = high - low
        if width == 0:
            undefined.add(index)
            continue
        values[index] = (float(bar.close) - low) / width
    return ComputedFeature(tuple(values), window - 1, frozenset(undefined))


def _previous_level(
    bars: Sequence[MarketBar], parameters: dict[str, int | float | str], highest: bool
) -> tuple[list[float | None], int]:
    window = _window(parameters, 20)
    source = [float(bar.high if highest else bar.low) for bar in bars]
    levels: list[float | None] = [None] * len(source)
    for index in range(window, len(source)):
        levels[index] = (
            max(source[index - window : index]) if highest else min(source[index - window : index])
        )
    return levels, window


def distance_to_previous_extreme(
    bars: Sequence[MarketBar], parameters: dict[str, int | float | str], highest: bool
) -> ComputedFeature:
    levels, warmup = _previous_level(bars, parameters, highest)
    values: list[float | None] = [None] * len(bars)
    for index, level in enumerate(levels):
        if level is not None:
            values[index] = float(bars[index].close) / level - 1.0
    return ComputedFeature(tuple(values), warmup)


def breakout_state(
    bars: Sequence[MarketBar], parameters: dict[str, int | float | str]
) -> ComputedFeature:
    high_levels, warmup = _previous_level(bars, parameters, True)
    low_levels, _ = _previous_level(bars, parameters, False)
    values: list[float | None] = [None] * len(bars)
    for index, bar in enumerate(bars):
        high = high_levels[index]
        low = low_levels[index]
        if high is not None and low is not None:
            close = float(bar.close)
            values[index] = 1.0 if close > high else -1.0 if close < low else 0.0
    return ComputedFeature(tuple(values), warmup)


def rsi(bars: Sequence[MarketBar], parameters: dict[str, int | float | str]) -> ComputedFeature:
    prices = _prices(bars)
    period = _period(parameters, 14)
    values: list[float | None] = [None] * len(prices)
    if len(prices) <= period:
        return ComputedFeature(tuple(values), period)
    gains = [max(prices[index] - prices[index - 1], 0.0) for index in range(1, len(prices))]
    losses = [max(prices[index - 1] - prices[index], 0.0) for index in range(1, len(prices))]
    average_gain = mean(gains[:period])
    average_loss = mean(losses[:period])

    def rsi_value(gain: float, loss: float) -> float:
        if loss == 0:
            return 50.0 if gain == 0 else 100.0
        relative_strength = gain / loss
        return 100.0 - 100.0 / (1.0 + relative_strength)

    values[period] = rsi_value(average_gain, average_loss)
    for index in range(period + 1, len(prices)):
        average_gain = (average_gain * (period - 1) + gains[index - 1]) / period
        average_loss = (average_loss * (period - 1) + losses[index - 1]) / period
        values[index] = rsi_value(average_gain, average_loss)
    return ComputedFeature(tuple(values), period)


def volatility_percentile(
    bars: Sequence[MarketBar], parameters: dict[str, int | float | str]
) -> ComputedFeature:
    window = _window(parameters, 20)
    volatility = rolling_volatility(bars, parameters).values
    values: list[float | None] = [None] * len(volatility)
    first = 2 * window - 1
    for index in range(first, len(volatility)):
        current = volatility[index]
        sample = volatility[index - window + 1 : index + 1]
        if current is not None and all(value is not None for value in sample):
            numeric = [value for value in sample if value is not None]
            values[index] = sum(value <= current for value in numeric) / len(numeric)
    return ComputedFeature(tuple(values), first)


def volatility_ratio(
    bars: Sequence[MarketBar], parameters: dict[str, int | float | str]
) -> ComputedFeature:
    short_value = parameters.get("short_window", 10)
    long_value = parameters.get("long_window", 30)
    if (
        isinstance(short_value, bool)
        or not isinstance(short_value, int)
        or short_value < 1
        or isinstance(long_value, bool)
        or not isinstance(long_value, int)
        or long_value < short_value
    ):
        raise FeatureComputationError("long_window must be >= short_window >= 1")
    short = rolling_volatility(bars, {"window": short_value}).values
    long = rolling_volatility(bars, {"window": long_value}).values
    values: list[float | None] = [None] * len(bars)
    undefined: set[int] = set()
    for index, (short_value_at, long_value_at) in enumerate(zip(short, long, strict=True)):
        if short_value_at is not None and long_value_at is not None:
            if long_value_at == 0:
                undefined.add(index)
            else:
                values[index] = short_value_at / long_value_at
    return ComputedFeature(tuple(values), long_value, frozenset(undefined))


def equity_volume(bars: Sequence[MarketBar], _: dict[str, int | float | str]) -> ComputedFeature:
    values = [None if bar.volume is None else float(bar.volume) for bar in bars]
    return ComputedFeature(tuple(values), 0)


def dollar_volume(bars: Sequence[MarketBar], _: dict[str, int | float | str]) -> ComputedFeature:
    values = [None if bar.volume is None else float(bar.volume) * float(bar.close) for bar in bars]
    return ComputedFeature(tuple(values), 0)


Computer = Callable[[Sequence[MarketBar], dict[str, int | float | str]], ComputedFeature]


COMPUTERS: dict[tuple[str, int], Computer] = {
    ("simple_return", 1): simple_return,
    ("log_return", 1): log_return,
    ("rolling_return", 1): rolling_return,
    ("rate_of_change", 1): rolling_return,
    ("sma", 1): sma,
    ("ema", 1): ema,
    ("moving_average_distance", 1): moving_average_distance,
    ("rolling_volatility", 1): rolling_volatility,
    ("realized_volatility", 1): realized_volatility,
    ("atr", 1): atr,
    ("rolling_high", 1): rolling_high,
    ("rolling_low", 1): rolling_low,
    ("range_position", 1): range_position,
    ("distance_to_previous_high", 1): lambda bars, params: distance_to_previous_extreme(
        bars, params, True
    ),
    ("distance_to_previous_low", 1): lambda bars, params: distance_to_previous_extreme(
        bars, params, False
    ),
    ("breakout_state", 1): breakout_state,
    ("rsi", 1): rsi,
    ("volatility_percentile", 1): volatility_percentile,
    ("volatility_ratio", 1): volatility_ratio,
    ("equity_volume", 1): equity_volume,
    ("dollar_volume", 1): dollar_volume,
}


__all__ = [
    "COMPUTERS",
    "ComputedFeature",
    "atr",
    "breakout_state",
    "dollar_volume",
    "equity_volume",
    "ema",
    "log_return",
    "moving_average_distance",
    "range_position",
    "realized_volatility",
    "rolling_high",
    "rolling_low",
    "rolling_return",
    "rolling_volatility",
    "rsi",
    "simple_return",
    "sma",
    "true_ranges",
    "volatility_percentile",
    "volatility_ratio",
]
