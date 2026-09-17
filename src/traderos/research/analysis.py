"""Deterministic Phase 9 analysis helpers with explicit statistical limits."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import ceil, isfinite
from random import Random
from statistics import fmean

from traderos.research.models import ResearchDecision, ResearchError


@dataclass(frozen=True)
class BootstrapSummary:
    """Moving-block bootstrap facts; ``None`` means the sample is inadequate."""

    observations: int
    replications: int
    block_size: int
    mean: float | None
    lower: float | None
    upper: float | None
    probability_mean_nonpositive: float | None


@dataclass(frozen=True)
class OutlierDependence:
    """Contribution of the best observations, retained rather than hidden."""

    observation_count: int
    total: Decimal
    top_one_contribution: Decimal | None
    top_five_contribution: Decimal | None
    top_ten_contribution: Decimal | None
    total_without_top_one: Decimal | None
    total_without_top_five: Decimal | None
    total_without_top_ten: Decimal | None


@dataclass(frozen=True)
class DecisionEvidence:
    """Multidimensional facts used for a transparent, non-score-based decision."""

    data_available: bool
    data_integrity: bool
    locked_oos_pristine: bool
    sample_adequate: bool
    net_expectancy: Decimal | None
    net_oos_return: float | None
    benchmark_excess_return: float | None
    cost_survives: bool | None
    slippage_survives: bool | None
    parameter_robust: bool | None
    walk_forward_consistent: bool | None
    multiple_testing_supported: bool | None
    regime_concentrated: bool | None
    outlier_dependent: bool | None


@dataclass(frozen=True)
class DecisionMatrixRow:
    """One human-reviewable dimension; it is never collapsed into a magic score."""

    dimension: str
    result: str
    interpretation: str


@dataclass(frozen=True)
class MonteCarloSummary:
    """Deterministic trade-path stress summary, not a forecast."""

    method: str
    seed: int
    simulations: int
    observations: int
    terminal_p05: float | None
    terminal_p50: float | None
    terminal_p95: float | None
    loss_probability: float | None


def benjamini_hochberg_adjusted_p_values(p_values: Mapping[str, float]) -> dict[str, float]:
    """Apply BH FDR correction when the research family assumptions support it."""

    return _fdr_adjust(p_values, dependence_multiplier=1.0)


def _require_finite(values: Sequence[float]) -> tuple[float, ...]:
    result = tuple(values)
    if any(not isfinite(value) for value in result):
        raise ResearchError("statistical input must contain only finite values")
    return result


def _quantile(values: Sequence[float], probability: float) -> float:
    """Use deterministic nearest-rank quantiles, adequate for report summaries."""

    if not values or not 0 <= probability <= 1:
        raise ResearchError("quantile requires values and a probability in [0, 1]")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, ceil(probability * len(ordered)) - 1)]


def moving_block_bootstrap_mean(
    values: Sequence[float],
    *,
    replications: int,
    block_size: int,
    seed: int,
    confidence: float = 0.95,
) -> BootstrapSummary:
    """Estimate mean uncertainty while preserving short-range time dependence.

    This is deliberately not an IID bootstrap.  Blocks are sampled with
    replacement from contiguous observations.  It estimates uncertainty in the
    supplied series only; it does not prove predictability or correct for
    researcher degrees of freedom.
    """

    series = _require_finite(values)
    if replications < 1 or block_size < 1:
        raise ResearchError("bootstrap replications and block size must be positive")
    if not 0 < confidence < 1:
        raise ResearchError("bootstrap confidence must lie strictly between zero and one")
    if len(series) < max(2, block_size):
        return BootstrapSummary(len(series), replications, block_size, None, None, None, None)
    random = Random(seed)
    means: list[float] = []
    starts = len(series) - block_size + 1
    for _ in range(replications):
        sampled: list[float] = []
        while len(sampled) < len(series):
            start = random.randrange(starts)
            sampled.extend(series[start : start + block_size])
        means.append(fmean(sampled[: len(series)]))
    tail = (1 - confidence) / 2
    return BootstrapSummary(
        observations=len(series),
        replications=replications,
        block_size=block_size,
        mean=fmean(series),
        lower=_quantile(means, tail),
        upper=_quantile(means, 1 - tail),
        probability_mean_nonpositive=sum(value <= 0 for value in means) / len(means),
    )


def benjamini_yekutieli_adjusted_p_values(p_values: Mapping[str, float]) -> dict[str, float]:
    """Apply conservative FDR adjustment valid under arbitrary dependence.

    This adjustment addresses a family of comparable, pre-specified hypothesis
    tests.  It is not a substitute for a locked OOS period, walk-forward
    validation, or disclosure of all candidate configurations.
    """

    count = len(p_values)
    harmonic = sum(1 / index for index in range(1, count + 1))
    return _fdr_adjust(p_values, dependence_multiplier=harmonic)


def _fdr_adjust(p_values: Mapping[str, float], *, dependence_multiplier: float) -> dict[str, float]:
    if not p_values:
        return {}
    if any(
        not key.strip() or not isfinite(value) or not 0 <= value <= 1
        for key, value in p_values.items()
    ):
        raise ResearchError("p-values require non-blank identities and finite values in [0, 1]")
    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    adjusted_by_key: dict[str, float] = {}
    running = 1.0
    for index in range(count, 0, -1):
        key, value = ordered[index - 1]
        candidate = min(1.0, value * count * dependence_multiplier / index)
        running = min(running, candidate)
        adjusted_by_key[key] = running
    return adjusted_by_key


def trade_path_monte_carlo(
    returns: Sequence[float],
    *,
    simulations: int,
    seed: int,
    block_size: int = 1,
) -> MonteCarloSummary:
    """Bootstrap trade paths, using contiguous blocks where dependence matters.

    The supplied values are simple returns.  Results describe perturbations of
    this observed sample only and are intentionally not probability forecasts.
    """

    values = _require_finite(returns)
    if simulations < 1 or block_size < 1:
        raise ResearchError("simulation count and block size must be positive")
    if len(values) < block_size:
        return MonteCarloSummary(
            "moving_block_bootstrap", seed, simulations, len(values), None, None, None, None
        )
    random = Random(seed)
    terminals: list[float] = []
    starts = len(values) - block_size + 1
    for _ in range(simulations):
        path: list[float] = []
        while len(path) < len(values):
            start = random.randrange(starts)
            path.extend(values[start : start + block_size])
        terminal = 1.0
        for value in path[: len(values)]:
            terminal *= 1.0 + value
        terminals.append(terminal - 1.0)
    return MonteCarloSummary(
        "moving_block_bootstrap",
        seed,
        simulations,
        len(values),
        _quantile(terminals, 0.05),
        _quantile(terminals, 0.50),
        _quantile(terminals, 0.95),
        sum(value <= 0 for value in terminals) / len(terminals),
    )


def outlier_dependence(pnls: Sequence[Decimal]) -> OutlierDependence:
    """Show whether a small number of winners dominates a result."""

    values = tuple(pnls)
    if any(not item.is_finite() for item in values):
        raise ResearchError("outlier analysis requires finite Decimal P&L values")
    total = sum(values, Decimal("0"))

    def contribution(count: int) -> tuple[Decimal | None, Decimal | None]:
        if not values:
            return None, None
        selected = tuple(sorted(values, reverse=True)[:count])
        top = sum(selected, Decimal("0"))
        return top, total - top

    top_one, without_one = contribution(1)
    top_five, without_five = contribution(5)
    top_ten, without_ten = contribution(10)
    return OutlierDependence(
        observation_count=len(values),
        total=total,
        top_one_contribution=top_one,
        top_five_contribution=top_five,
        top_ten_contribution=top_ten,
        total_without_top_one=without_one,
        total_without_top_five=without_five,
        total_without_top_ten=without_ten,
    )


def net_after_costs(gross_pnl: Decimal, transaction_cost: Decimal) -> Decimal:
    """Separate economics: increased stated costs can never improve net P&L."""

    if not gross_pnl.is_finite() or not transaction_cost.is_finite() or transaction_cost < 0:
        raise ResearchError("gross P&L and non-negative transaction cost must be finite")
    return gross_pnl - transaction_cost


def classify_evidence(evidence: DecisionEvidence) -> ResearchDecision:
    """Classify without arbitrary Sharpe or return thresholds.

    A PASS demands all available evidence dimensions support survival.  A KILL
    is reserved for adequate, methodologically valid evidence of negative net
    economics or a failed required robustness dimension.  Missing data is
    INVALID rather than a fabricated positive/negative performance result.
    """

    if (
        not evidence.data_available
        or not evidence.data_integrity
        or not evidence.locked_oos_pristine
    ):
        return ResearchDecision.INVALID
    if not evidence.sample_adequate:
        return ResearchDecision.CONDITIONAL
    negative_economics = (
        evidence.net_expectancy is not None
        and evidence.net_oos_return is not None
        and evidence.benchmark_excess_return is not None
        and (
            evidence.net_expectancy <= 0
            or evidence.net_oos_return <= 0
            or evidence.benchmark_excess_return <= 0
        )
    )
    failed_robustness = evidence.cost_survives is False or evidence.slippage_survives is False
    if negative_economics or failed_robustness:
        return ResearchDecision.KILL
    required = (
        evidence.net_expectancy is not None and evidence.net_expectancy > 0,
        evidence.net_oos_return is not None and evidence.net_oos_return > 0,
        evidence.benchmark_excess_return is not None and evidence.benchmark_excess_return > 0,
        evidence.cost_survives is True,
        evidence.slippage_survives is True,
        evidence.parameter_robust is True,
        evidence.walk_forward_consistent is True,
        evidence.multiple_testing_supported is True,
        evidence.regime_concentrated is False,
        evidence.outlier_dependent is False,
    )
    return ResearchDecision.PASS if all(required) else ResearchDecision.CONDITIONAL


def decision_matrix(evidence: DecisionEvidence) -> tuple[DecisionMatrixRow, ...]:
    """Return transparent decision inputs for a review report."""

    rows = (
        ("Net OOS expectancy", evidence.net_expectancy, "economic edge after costs"),
        ("Net OOS return", evidence.net_oos_return, "forward-period economics"),
        ("Benchmark advantage", evidence.benchmark_excess_return, "value beyond reference"),
        ("Cost sensitivity", evidence.cost_survives, "survival under cost stress"),
        ("Slippage sensitivity", evidence.slippage_survives, "survival under adverse execution"),
        ("Parameter robustness", evidence.parameter_robust, "plateau rather than isolated peak"),
        ("Walk-forward consistency", evidence.walk_forward_consistent, "not one historical era"),
        ("Multiple-testing support", evidence.multiple_testing_supported, "family-wise evidence"),
        ("Regime concentration", evidence.regime_concentrated, "edge concentrated in one regime"),
        ("Outlier dependence", evidence.outlier_dependent, "top trades dominate outcome"),
        ("Sample adequacy", evidence.sample_adequate, "enough observations for interpretation"),
        (
            "Leakage / OOS integrity",
            evidence.data_integrity and evidence.locked_oos_pristine,
            "causal validity",
        ),
    )
    return tuple(
        DecisionMatrixRow(name, "UNAVAILABLE" if value is None else str(value), interpretation)
        for name, value, interpretation in rows
    )


__all__ = [
    "BootstrapSummary",
    "DecisionEvidence",
    "DecisionMatrixRow",
    "MonteCarloSummary",
    "OutlierDependence",
    "benjamini_hochberg_adjusted_p_values",
    "benjamini_yekutieli_adjusted_p_values",
    "classify_evidence",
    "decision_matrix",
    "moving_block_bootstrap_mean",
    "net_after_costs",
    "outlier_dependence",
    "trade_path_monte_carlo",
]
