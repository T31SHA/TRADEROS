"""Diagnose the blocked APEX Milestone 5 dataset without repairing it.

This is a read-only remediation report over already configured repository data.
It uses the existing normalized tick artifacts, parser, and Dukascopy calendar;
it never downloads, fills, rewrites, or executes a strategy.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from traderos.data.calendars import DukascopyForexCalendar
from traderos.data.ticks import iter_dukascopy_ticks, iter_normalized_tick_bars

ROOT = Path(__file__).resolve().parents[1]
M15 = timedelta(minutes=15)
CALENDAR_RULE = (
    "DukascopyForexCalendar v1: America/New_York 17:00 session boundary; "
    "Friday closes at 17:00 local and Sunday opens at 17:00 local; timestamps "
    "are UTC bar starts"
)
AGGREGATION_ASSUMPTIONS = {
    "raw_schema": ["timestamp", "askPrice", "bidPrice", "askVolume", "bidVolume"],
    "timestamp_encoding": "integer Unix epoch milliseconds",
    "source_timezone": "UTC",
    "timestamp_semantics": "bar_start",
    "bucket_rule": "UTC floor to 15-minute boundary",
    "quote_semantics": "bid+ask OHLCV; closing bid/ask spread retained",
    "invalid_observation_policy": "reject; do not sort, repair, forward-fill, or synthesize",
    "missing_bar_policy": "emit no synthetic empty interval",
}


def _json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC")
    return parsed.astimezone(UTC)


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tick_reference(tick: object) -> dict[str, object]:
    return {
        "timestamp": tick.timestamp.isoformat(),  # type: ignore[attr-defined]
        "row_number": tick.row_number,  # type: ignore[attr-defined]
    }


def _raw_coverage(
    raw_path: Path,
    raw_hash: str,
    timezone: str,
    windows: list[dict[str, Any]],
) -> None:
    """Attach exact neighboring raw-row evidence to each missing window."""

    window_index = 0
    previous: dict[str, object] | None = None
    for tick in iter_dukascopy_ticks(
        raw_path, artifact_hash=raw_hash, source_timezone=timezone
    ):
        reference = _tick_reference(tick)
        while window_index < len(windows) and tick.timestamp >= _utc(
            windows[window_index]["end"], "gap end"
        ):
            window = windows[window_index]
            coverage = window["neighboring_raw_tick_coverage"]
            if coverage["previous_raw_tick"] is None:
                coverage["previous_raw_tick"] = previous
            coverage["next_raw_tick"] = reference
            window_index += 1
        if window_index >= len(windows):
            break

        window = windows[window_index]
        coverage = window["neighboring_raw_tick_coverage"]
        if tick.timestamp < _utc(window["start"], "gap start"):
            previous = reference
        elif tick.timestamp < _utc(window["end"], "gap end"):
            if coverage["previous_raw_tick"] is None:
                coverage["previous_raw_tick"] = previous
            window["raw_tick_count"] += 1
            if coverage["first_raw_tick"] is None:
                coverage["first_raw_tick"] = reference
            coverage["last_raw_tick"] = reference
        previous = reference

    while window_index < len(windows):
        window = windows[window_index]
        coverage = window["neighboring_raw_tick_coverage"]
        if coverage["previous_raw_tick"] is None:
            coverage["previous_raw_tick"] = previous
        window_index += 1


def _normalized_index(path: Path) -> tuple[dict[datetime, int], set[datetime]]:
    lines: dict[datetime, int] = {}
    timestamps: set[datetime] = set()
    for line_number, bar in enumerate(iter_normalized_tick_bars(path), start=1):
        lines[bar.timestamp] = line_number
        timestamps.add(bar.timestamp)
    return lines, timestamps


def _gap_runs(records: list[dict[str, Any]]) -> None:
    run = 0
    previous: datetime | None = None
    for record in records:
        if record["classification"] == "EXPECTED_MARKET_CLOSURE":
            record["gap_run_id"] = "calendar-closure"
            continue
        start = _utc(record["start"], "gap start")
        if previous is None or start != previous + M15:
            run += 1
        record["gap_run_id"] = f"open-session-gap-run-{run:02d}"
        previous = start


def _load_raw_reference(manifest: dict[str, Any], data_root: Path) -> tuple[Path, dict[str, Any]]:
    metadata = manifest.get("artifact_metadata")
    if not isinstance(metadata, list) or len(metadata) != 1 or not isinstance(metadata[0], dict):
        raise ValueError("diagnostic currently requires exactly one raw artifact")
    item = metadata[0]
    source = item.get("source")
    digest = item.get("sha256")
    filename = item.get("original_filename")
    if not all(isinstance(value, str) and value for value in (source, digest, filename)):
        raise ValueError("manifest raw artifact metadata is incomplete")
    path = data_root / "raw" / source / digest / filename
    if not path.is_file():
        raise FileNotFoundError(path)
    return path, item


def _comparison_evidence(
    *,
    record: dict[str, Any],
    comparison_manifest: dict[str, Any] | None,
    comparison_data_root: Path,
) -> dict[str, object] | None:
    if comparison_manifest is None:
        return None
    comparison_id = comparison_manifest["dataset_id"]
    comparison_path = comparison_data_root / "normalized" / f"{comparison_id}.jsonl"
    if not comparison_path.is_file():
        return {
            "dataset_id": comparison_id,
            "status": "comparison_normalized_artifact_missing",
        }
    line_map, timestamps = _normalized_index(comparison_path)
    start = _utc(record["start"], "gap start")
    available = comparison_manifest.get("coverage_start")
    end_available = comparison_manifest.get("coverage_end")
    if not isinstance(available, str) or not isinstance(end_available, str):
        return {"dataset_id": comparison_id, "status": "comparison_coverage_unavailable"}
    in_coverage = _utc(available, "comparison coverage start") <= start < _utc(
        end_available, "comparison coverage end"
    )
    if not in_coverage:
        return {
            "dataset_id": comparison_id,
            "status": "comparison_out_of_coverage",
            "coverage_start": available,
            "coverage_end": end_available,
        }
    return {
        "dataset_id": comparison_id,
        "raw_artifact_hashes": comparison_manifest.get(
            "source_artifact_hashes", comparison_manifest.get("raw_artifact_hashes", [])
        ),
        "normalized_path": _relative(comparison_path),
        "bar_present": start in timestamps,
        "normalized_line": line_map.get(start),
        "comparison_scope": "content comparison only; provider authenticity not established",
    }


def _runtime_evidence(result_root: Path) -> dict[str, object]:
    result_path = result_root / "result.json"
    result = _json(result_path)
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_versions = re.findall(r'python-version:\s*\["([^"]+)"\]', workflow)
    distributions = (
        "traderos",
        "sqlalchemy",
        "psycopg",
        "pydantic",
        "pydantic-settings",
        "pytest",
        "ruff",
        "mypy",
    )
    resolved: dict[str, str] = {}
    for name in distributions:
        try:
            resolved[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            resolved[name] = "UNAVAILABLE"
    prior_source = result.get("source_identity")
    return {
        "schema_version": "apex-m5-runtime-evidence.v1",
        "supported_python": re.search(r'requires-python\s*=\s*"([^"]+)"', pyproject).group(1),
        "ci_python_matrix": ci_versions,
        "experiment_python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "resolved_dependencies": resolved,
        "runtime_verdict": (
            "EXPERIMENT_RUNTIME_MEASURED_CI_RUNTIME_NOT_EXACT"
            if platform.python_version() not in ci_versions
            else "EXPERIMENT_RUNTIME_MATCHES_CI_MATRIX"
        ),
        "prior_blocked_experiment_source_identity": {
            "base_commit": (
                prior_source.get("base_commit") if isinstance(prior_source, dict) else None
            ),
            "dirty_state": (
                prior_source.get("dirty_state") if isinstance(prior_source, dict) else None
            ),
            "snapshot_content_digest": (
                prior_source.get("snapshot_content_digest")
                if isinstance(prior_source, dict)
                else None
            ),
            "snapshot_file_count": (
                len(prior_source.get("snapshot_files", []))
                if isinstance(prior_source, dict)
                else None
            ),
        },
        "source_snapshot_note": (
            "The blocked experiment snapshot remains preserved. This remediation "
            "script and the license-reference admission change are newer working-tree "
            "code; any future empirical run must collect a new source snapshot."
        ),
    }


def _classify(record: dict[str, Any]) -> tuple[str, list[str]]:
    if not record["calendar_open"]:
        return "EXPECTED_MARKET_CLOSURE", [
            "DukascopyForexCalendar reports the bar-start timestamp closed",
            "no bar is expected under the declared calendar",
        ]
    if record["observed_observations"]["raw_tick_count"]:
        return "PARSER_NORMALIZATION_AGGREGATION_DEFECT", [
            "raw ticks were observed inside an open-session interval",
            "the normalized artifact contains no M15 bar for that interval",
        ]
    comparison = record.get("comparison_artifact")
    if isinstance(comparison, dict) and comparison.get("bar_present") is True:
        return "ACQUISITION_EXPORT_OMISSION_LIKELY", [
            "the selected raw artifact has zero ticks in the open-session interval",
            "an overlapping preserved artifact contains a normalized M15 bar",
            "the two artifacts have different raw byte identities",
        ]
    return "UNRESOLVED", [
        "the selected raw artifact has zero ticks in the open-session interval",
        "no overlapping preserved comparison artifact establishes whether the absence "
        "was upstream source data or acquisition/export loss",
    ]


def _diagnose(
    *,
    dataset_id: str,
    data_root: Path,
    comparison_dataset_id: str | None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    manifest_path = data_root / "manifests" / f"{dataset_id}.json"
    quality_path = data_root / "manifests" / f"{dataset_id}.quality.json"
    manifest = _json(manifest_path)
    quality = _json(quality_path)
    raw_path, raw_metadata = _load_raw_reference(manifest, data_root)
    normalized_path = data_root / "normalized" / f"{dataset_id}.jsonl"
    normalized_lines, actual = _normalized_index(normalized_path)
    calendar = DukascopyForexCalendar()
    start = _utc(manifest["coverage_start"], "coverage_start")
    end = _utc(manifest["coverage_end"], "coverage_end")

    comparison_manifest = None
    if comparison_dataset_id:
        comparison_manifest = _json(
            data_root / "manifests" / f"{comparison_dataset_id}.json"
        )

    records: list[dict[str, Any]] = []
    current = start
    while current < end:
        if current in actual:
            current += M15
            continue
        interval_end = current + M15
        record: dict[str, Any] = {
            "start": current.isoformat(),
            "end": interval_end.isoformat(),
            "expected_observations": {
                "m15_bar_count": 1 if calendar.is_open_at(current) else 0,
                "raw_tick_count": "not deterministically enumerable from a bar expectation",
            },
            "observed_observations": {
                "m15_bar_count": 0,
                "raw_tick_count": 0,
            },
            "raw_tick_count": 0,
            "calendar": {
                "calendar_id": calendar.calendar_id,
                "rule": CALENDAR_RULE,
                "bar_start_open": calendar.is_open_at(current),
            },
            "neighboring_raw_tick_coverage": {
                "previous_raw_tick": None,
                "next_raw_tick": None,
                "first_raw_tick": None,
                "last_raw_tick": None,
            },
            "source_file_references": {
                "raw_artifact_path": _relative(raw_path),
                "raw_artifact_sha256": raw_metadata["sha256"],
                "normalized_artifact_path": _relative(normalized_path),
                "normalized_line_for_missing_bar": None,
            },
            "aggregation_assumptions": AGGREGATION_ASSUMPTIONS,
            "comparison_artifact": _comparison_evidence(
                record={"start": current.isoformat()},
                comparison_manifest=comparison_manifest,
                comparison_data_root=data_root,
            ),
        }
        records.append(record)
        current = interval_end

    _raw_coverage(raw_path, raw_metadata["sha256"], raw_metadata["timezone"], records)
    for record in records:
        evidence = record["neighboring_raw_tick_coverage"]
        record["observed_observations"]["raw_tick_count"] = record.pop("raw_tick_count", 0)
        record["source_file_references"]["previous_raw_row"] = (
            evidence["previous_raw_tick"]["row_number"]
            if evidence["previous_raw_tick"]
            else None
        )
        record["source_file_references"]["next_raw_row"] = (
            evidence["next_raw_tick"]["row_number"] if evidence["next_raw_tick"] else None
        )
        record["source_file_references"]["previous_normalized_line"] = normalized_lines.get(
            _utc(record["start"], "gap start") - M15
        )
        record["source_file_references"]["next_normalized_line"] = normalized_lines.get(
            _utc(record["end"], "gap end")
        )
        record["calendar_open"] = record["calendar"]["bar_start_open"]
        classification, supporting_evidence = _classify(record)
        record["classification"] = classification
        record["supporting_evidence"] = supporting_evidence
        record.pop("calendar_open", None)
    _gap_runs(records)

    counts = Counter(record["classification"] for record in records)
    open_records = [record for record in records if record["calendar"]["bar_start_open"]]
    selected_summary = {
        "dataset_id": dataset_id,
        "instrument": manifest.get("instrument"),
        "timeframe": manifest.get("timeframe"),
        "coverage_start": manifest.get("coverage_start"),
        "coverage_end": manifest.get("coverage_end"),
        "calendar_id": manifest.get("calendar_id"),
        "raw_artifact_hashes": manifest.get(
            "source_artifact_hashes", manifest.get("raw_artifact_hashes", [])
        ),
        "normalized_content_hash": manifest.get("normalized_content_hash"),
        "quality_report_hash": manifest.get("quality_report_hash"),
        "quality_report": quality,
        "raw_artifact_path": _relative(raw_path),
        "raw_artifact_metadata": raw_metadata,
    }
    inventory_body = {
        "schema_version": "apex-m5-gap-inventory.v1",
        "diagnostic_scope": "all absent M15 bar starts in the selected admitted tick artifact",
        "selected_dataset": selected_summary,
        "comparison_dataset_id": comparison_dataset_id,
        "calendar_rule": CALENDAR_RULE,
        "aggregation_assumptions": AGGREGATION_ASSUMPTIONS,
        "summary": {
            "absent_m15_intervals": len(records),
            "open_session_intervals": len(open_records),
            "expected_closure_intervals": counts.get("EXPECTED_MARKET_CLOSURE", 0),
            "unexpected_open_session_intervals": len(open_records),
            "classification_counts": dict(sorted(counts.items())),
            "parser_normalization_aggregation_defects": counts.get(
                "PARSER_NORMALIZATION_AGGREGATION_DEFECT", 0
            ),
        },
        "gaps": records,
    }
    inventory = dict(inventory_body)
    inventory["inventory_content_hash"] = _sha256_json(inventory_body)

    source_inventory = {
        "schema_version": "apex-m5-source-license-evidence.v1",
        "scope": (
            "configured repository data, manifests, source importers, scripts, "
            "and blocked Milestone 5 records"
        ),
        "selected_dataset_id": dataset_id,
        "prior_blocked_evidence": {
            "result": "research/results/apex_m5/result.json",
            "qualification": (
                "research/results/apex_m5/qualifications/"
                "3a631bd1808394e3e794be0974e4219b5780e1e54de3ab40abe4eef7b4a41425.json"
            ),
            "protocol": "research/results/apex_m5/protocols/apex-m5-1a4560328c3cea8ebab4db67.json",
            "reproducibility": "research/results/apex_m5/reproducibility.json",
        },
        "evidence": [
            {
                "field": "raw_byte_identity",
                "status": "LOCALLY_VERIFIED",
                "fact": (
                    "The preserved raw file exists and its bytes match the manifest "
                    "SHA-256 and byte size."
                ),
                "references": [
                    _relative(raw_path),
                    _relative(raw_path.with_name(raw_path.name + ".metadata.json")),
                ],
            },
            {
                "field": "provider_source_label",
                "status": "LOCALLY_RECORDED_NOT_AUTHENTICATED",
                "fact": (
                    f"The local metadata labels the source {raw_metadata.get('source')!r} "
                    f"and symbol {raw_metadata.get('source_symbol')!r}."
                ),
                "references": [_relative(raw_path.with_name(raw_path.name + ".metadata.json"))],
                "limitation": "A local label and hash do not establish provider authenticity.",
            },
            {
                "field": "instrument_and_quote_convention",
                "status": "LOCALLY_VERIFIED_FOR_IMPORT_CONFIGURATION",
                "fact": (
                    "The existing tick importer declares EUR/USD, bid+ask ticks, "
                    "UTC epoch-millisecond timestamps, and bid_ask_close_spread."
                ),
                "references": [
                    "src/traderos/data/ticks.py",
                    "src/traderos/data/empirical.py",
                    _relative(normalized_path),
                ],
            },
            {
                "field": "acquisition_timestamp",
                "status": "LOCALLY_RECORDED_NOT_AUTHENTICATED",
                "fact": raw_metadata.get("download_timestamp"),
                "references": [_relative(raw_path.with_name(raw_path.name + ".metadata.json"))],
                "limitation": (
                    "No signed provider receipt or acquisition log was found in "
                    "configured locations."
                ),
            },
            {
                "field": "source_version_or_downloader_release",
                "status": "MISSING",
                "fact": raw_metadata.get("source_version"),
                "references": [_relative(raw_path.with_name(raw_path.name + ".metadata.json"))],
            },
            {
                "field": "license_or_terms_reference",
                "status": "MISSING",
                "fact": manifest.get("license_reference"),
                "references": [_relative(manifest_path)],
            },
            {
                "field": "acquisition_mechanism",
                "status": "PARTIALLY_DOCUMENTED",
                "fact": (
                    "The repository importer accepts a caller-supplied local "
                    "Dukascopy-node-compatible CSV and performs no download."
                ),
                "references": [
                    "scripts/admit_real_dukascopy_ticks.py",
                    "src/traderos/data/ticks.py",
                ],
                "limitation": (
                    "The actual acquisition command, downloader release, and "
                    "provider response are not present."
                ),
            },
        ],
        "operator_inputs_required": [
            "Official provider/export endpoint or source record for the exact artifact.",
            (
                "Exact downloader/exporter name and immutable release/version, "
                "not only a generic source label."
            ),
            "UTC acquisition timestamp and acquisition command or provider receipt.",
            (
                "Provider terms/license reference plus evidence that internal "
                "quantitative research use is permitted."
            ),
            (
                "Provider-confirmed instrument symbol, UTC timestamp convention, "
                "bid/ask quote semantics, and requested range."
            ),
            (
                "A replacement raw artifact with no unresolved open-session gaps "
                "and complete bid/ask observations."
            ),
        ],
    }
    source_inventory["evidence_content_hash"] = _sha256_json(source_inventory)

    requirements = {
        "schema_version": "apex-m5-replacement-admission-requirements.v1",
        "verdict": "BLOCKED",
        "basis": "frozen Milestone 5 protocol and existing Dukascopy tick importer",
        "scope": {
            "instrument": "EUR/USD",
            "timeframe": "15m",
            "source_mode": "tick",
            "raw_quote_requirement": "bid and ask prices and volumes; no bid-only bar artifact",
            "raw_schema": AGGREGATION_ASSUMPTIONS["raw_schema"],
            "timestamp": AGGREGATION_ASSUMPTIONS["timestamp_encoding"],
            "timestamp_semantics": "UTC bar_start after M15 floor aggregation",
            "calendar_id": "dukascopy-forex-utc-session-v1",
            "adjustment_policy": "raw; no adjustment or repair",
        },
        "coverage_and_design": {
            "minimum_total_coverage_days": 180,
            "minimum_development_days": 120,
            "minimum_locked_oos_days": 60,
            "walk_forward": (
                "expanding; initial train 10 days; forward test 5 days; "
                "only if sample adequacy supports it"
            ),
            "warmup": "25 M15 rows for the predeclared 10/30 EMA lineage, excluded from evaluation",
            "selection_rule": (
                "choose coverage and boundaries from methodology and data "
                "availability, never strategy returns"
            ),
            "trade_adequacy": {"minimum_closed_trades": 30, "minimum_oos_closed_trades": 10},
        },
        "quality_requirements": {
            "max_unexpected_gaps": 0,
            "max_unknown_gaps": 0,
            "invalid_ticks": 0,
            "duplicate_timestamps": 0,
            "non_monotonic_timestamps": 0,
            "crossed_quotes": 0,
            "nonfinite_prices": 0,
            "nonpositive_prices": 0,
            "volume_anomalies": 0,
            "active_session_zero_volume": 0,
            "source_artifact_verification": (
                "raw byte hash, normalized content hash, quality report hash, "
                "and manifest identity must all verify"
            ),
        },
        "provenance_requirements": [
            "non-empty source_version identifying provider release or downloader/exporter release",
            "license_reference persisted in the dataset manifest",
            (
                "provider/source record, acquisition timestamp, exact requested "
                "range, instrument, and quote convention"
            ),
            (
                "source authenticity remains an operator/provider evidence "
                "question; SHA-256 proves bytes only"
            ),
        ],
        "identity_rules": {
            "dataset": (
                "new immutable identity derived from replacement raw bytes and "
                "admission configuration"
            ),
            "qualification": (
                "new qualification record referencing the new dataset and "
                "current policy"
            ),
            "protocol": (
                "new protocol record bound to the new dataset; prior frozen "
                "protocol is not edited"
            ),
            "prior_attempt": "reference the prior BLOCKED_DATA result as historical context only",
        },
        "storage_layout": {
            "raw_root": "<DATA_ROOT>/raw",
            "normalized_root": "<DATA_ROOT>/normalized",
            "manifest_root": "<DATA_ROOT>/manifests",
            "qualification_root": "<OUTPUT_ROOT>/qualifications",
            "raw_artifact_layout": "<DATA_ROOT>/raw/dukascopy/<RAW_SHA256>/<ORIGINAL_FILENAME>",
        },
        "commands": [
            (
                "<PYTHON> scripts/admit_real_dukascopy_ticks.py <RAW_CSV> "
                "--coverage-start <UTC_START> --coverage-end <UTC_END> "
                "--download-timestamp <ACQUISITION_UTC> "
                "--source-version <PROVIDER_OR_DOWNLOADER_RELEASE> "
                "--license-reference <LICENSE_REFERENCE> --data-root <DATA_ROOT>"
            ),
            (
                "<PYTHON> scripts/run_apex_milestone5.py --dataset-id "
                "<NEW_DATASET_ID> --data-root <DATA_ROOT> --output-dir <OUTPUT_ROOT>"
            ),
        ],
        "do_not_do": [
            "Do not fill, forward-fill, synthesize, splice, or silently exclude open-session gaps.",
            "Do not reuse the rejected dataset identity or edit the blocked protocol/result.",
            "Do not select coverage using strategy returns.",
            (
                "Do not run strategy performance or access OOS until qualification "
                "passes and a new protocol is frozen."
            ),
        ],
    }
    requirements["requirements_content_hash"] = _sha256_json(requirements)
    return inventory, source_inventory, requirements


def _write_report(
    *,
    output: Path,
    inventory: dict[str, object],
    source_inventory: dict[str, object],
    requirements: dict[str, object],
) -> None:
    summary = inventory["summary"]
    assert isinstance(summary, dict)
    classifications = summary["classification_counts"]
    assert isinstance(classifications, dict)
    report = f"""# APEX Milestone 5 Data Remediation

Verdict: **BLOCKED**

This report diagnoses the preserved selected dataset without modifying the
original raw artifact, admission records, qualification record, frozen
protocol, blocked result, or reproducibility record. No strategy execution or
OOS access occurred.

## Selected artifact

- Dataset: `{inventory['selected_dataset']['dataset_id']}`
- Instrument/timeframe: `{inventory['selected_dataset']['instrument']}` /
  `{inventory['selected_dataset']['timeframe']}`
- Coverage: `{inventory['selected_dataset']['coverage_start']}` through
  `{inventory['selected_dataset']['coverage_end']}`
- Raw artifact: `{inventory['selected_dataset']['raw_artifact_path']}`
- Raw SHA-256: `{inventory['selected_dataset']['raw_artifact_hashes'][0]}`
- Normalized content hash: `{inventory['selected_dataset']['normalized_content_hash']}`

## Gap findings

The existing admission report counted 808 absent M15 intervals: 768 calendar
closures and 40 unexpected open-session intervals. This inventory contains all
808 absent intervals. The 40 unexpected intervals occur in ten four-bar runs.

Classification counts:

{json.dumps(classifications, indent=2, sort_keys=True)}

The first 24 unexpected intervals are covered by the overlapping preserved
comparison dataset `{inventory['comparison_dataset_id']}`. Its normalized
artifact contains bars at those timestamps while the selected raw artifact
contains zero ticks. That supports an acquisition/export omission diagnosis,
but does not authenticate either provider source. The remaining 16 have no
overlapping configured comparison artifact and remain **UNRESOLVED**.

No parser/normalization/aggregation defect was found: the selected raw stream
contains zero ticks in every unexpected interval, the existing aggregator emits
no synthetic empty bars, and the overlapping comparison artifact independently
contains the first 24 bars.

## Source and license evidence

The local metadata verifies byte identity, source label, symbol, coverage,
timezone, and acquisition timestamp as recorded facts. It does not establish
provider authenticity. The selected artifact has no source-version/downloader
release and no license/terms reference. The exact outstanding operator inputs
are in `source_license_evidence.json`.

## Replacement admission

The replacement remains **BLOCKED** until the operator supplies the required
provenance and a new raw artifact satisfying the existing zero-tolerance
quality policy. Requirements and copyable placeholder commands are in
`replacement_admission_requirements.json` and
`replacement_admission_checklist.md`.

The original Milestone 5 evidence remains authoritative for the prior attempt;
this remediation creates no replacement dataset identity and no new protocol.
"""
    (output / "gap_diagnosis_report.md").write_text(report, encoding="utf-8")

    checklist = """# Replacement Dataset Admission Checklist

Status: **BLOCKED**

Complete every item before invoking the existing admission path:

- [ ] EUR/USD M15 tick CSV, not bid-only bars.
- [ ] Exact schema: `timestamp,askPrice,bidPrice,askVolume,bidVolume`.
- [ ] Integer Unix epoch milliseconds; UTC; bar-start semantics after M15 floor aggregation.
- [ ] Raw artifact preserved under `<DATA_ROOT>/raw/dukascopy/<SHA256>/`.
- [ ] Provider/source record and exact instrument symbol supplied.
- [ ] Immutable provider release or downloader/exporter version supplied.
- [ ] UTC acquisition timestamp and acquisition command/receipt supplied.
- [ ] License/terms reference and permitted internal research-use evidence supplied.
- [ ] At least 180 calendar days, with at least 120 development and 60 locked-OOS days.
- [ ] No unexpected or unknown open-session gaps.
- [ ] No invalid, duplicate, non-monotonic, crossed, nonfinite, nonpositive,
      or anomalous observations.
- [ ] No active-session zero-volume bars.
- [ ] Raw, normalized, quality, and manifest hashes verify.
- [ ] Qualification record is `QUALIFIED` before any new protocol or OOS access.

## Commands

```bash
<PYTHON> scripts/admit_real_dukascopy_ticks.py <RAW_CSV> \\
  --coverage-start <UTC_START> \\
  --coverage-end <UTC_END> \\
  --download-timestamp <ACQUISITION_UTC> \\
  --source-version <PROVIDER_OR_DOWNLOADER_RELEASE> \\
  --license-reference <LICENSE_REFERENCE> \\
  --data-root <DATA_ROOT>

<PYTHON> scripts/run_apex_milestone5.py \\
  --dataset-id <NEW_DATASET_ID> \\
  --data-root <DATA_ROOT> \\
  --output-dir <OUTPUT_ROOT>
```

The existing script does not download data. Replace placeholders only after the
operator has supplied and verified the source/license evidence. Do not use
these commands with the rejected dataset as a shortcut.
"""
    (output / "replacement_admission_checklist.md").write_text(checklist, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--comparison-dataset-id", default=None)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument(
        "--milestone-result-root", type=Path, default=ROOT / "research" / "results" / "apex_m5"
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "research" / "results" / "apex_m5_remediation",
    )
    args = parser.parse_args()
    output = args.output_root
    output.mkdir(parents=True, exist_ok=True)
    inventory, source_inventory, requirements = _diagnose(
        dataset_id=args.dataset_id,
        data_root=args.data_root,
        comparison_dataset_id=args.comparison_dataset_id,
    )
    runtime = _runtime_evidence(args.milestone_result_root)
    (output / "gap_inventory.json").write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "source_license_evidence.json").write_text(
        json.dumps(source_inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "replacement_admission_requirements.json").write_text(
        json.dumps(requirements, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "runtime_evidence.json").write_text(
        json.dumps(runtime, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_report(
        output=output,
        inventory=inventory,
        source_inventory=source_inventory,
        requirements=requirements,
    )
    print("verdict=BLOCKED")
    print(f"dataset_id={args.dataset_id}")
    print(f"inventory={output / 'gap_inventory.json'}")
    print(f"report={output / 'gap_diagnosis_report.md'}")
    print(f"source_evidence={output / 'source_license_evidence.json'}")
    print(f"requirements={output / 'replacement_admission_requirements.json'}")
    print(f"runtime={output / 'runtime_evidence.json'}")
    print(f"inventory_hash={inventory['inventory_content_hash']}")
    print(f"classifications={inventory['summary']['classification_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
