"""Run the fail-closed APEX Milestone 5 governed empirical preflight.

The command accepts only an explicitly configured local data root and dataset
identity.  It never downloads data, searches outside that root, tunes a
strategy, opens a broker connection, or runs strategy performance code after a
data-gate failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from traderos.data.empirical import AdmissionPolicy
from traderos.research.apex import (
    ApexProtocolRegistry,
    build_blocked_result,
    build_first_baseline_protocol,
    collect_source_identity,
    inventory_dataset_ids,
    qualify_admitted_dataset,
    render_blocked_report,
)
from traderos.research.dataset_records import DatasetQualificationRegistry, DatasetRecordReader
from traderos.research.models import ExperimentInputBinding

ROOT = Path(__file__).resolve().parents[1]


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise argparse.ArgumentTypeError("checked-at must be timezone-aware UTC ISO-8601")
    return parsed.astimezone(UTC)


def _json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )


def _inventory(reader: DatasetRecordReader, manifest_root: Path) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for dataset_id in inventory_dataset_ids(manifest_root):
        read = reader.read(dataset_id)
        record = read.record
        values.append(
            {
                "dataset_id": dataset_id,
                "record_status": read.status.value,
                "manifest_status": read.manifest_status.value,
                "normalized_status": read.normalized_status.value,
                "raw_status": read.raw_status.value,
                "quality_report_status": read.quality_report_status.value,
                "source": record.source if record else None,
                "source_version": record.source_version if record else None,
                "instrument_scope": list(record.instruments) if record else [],
                "timeframe": record.timeframe if record else None,
                "coverage": (
                    {
                        "start": record.coverage.start.isoformat(),
                        "end": record.coverage.end.isoformat(),
                    }
                    if record and record.coverage
                    else None
                ),
                "raw_artifact_hashes": list(record.raw_artifact_hashes) if record else [],
            }
        )
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-id", required=True, help="exact inventoried dataset identity")
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "research/results/apex_m5")
    parser.add_argument(
        "--checked-at",
        type=_utc,
        help="stable UTC qualification timestamp; defaults to the manifest created_at",
    )
    args = parser.parse_args(argv)
    data_root = args.data_root.resolve()
    output_dir = args.output_dir.resolve()
    manifest_root = data_root / "manifests"
    normalized_root = data_root / "normalized"
    raw_root = data_root / "raw"
    reader = DatasetRecordReader(
        manifest_root,
        normalized_root,
        raw_root=raw_root,
        quality_report_root=manifest_root,
    )
    inventory = _inventory(reader, manifest_root)
    if args.dataset_id not in {str(item["dataset_id"]) for item in inventory}:
        raise SystemExit(
            f"dataset {args.dataset_id!r} is not present in configured manifest root "
            f"{manifest_root}"
        )
    selected_read = reader.read(args.dataset_id)
    if selected_read.record is None:
        raise SystemExit(f"dataset {args.dataset_id!r} has no readable manifest")
    checked_at = args.checked_at or datetime.fromisoformat(
        str(selected_read.record.manifest_payload["created_at"])
    )
    qualification_root = output_dir / "qualifications"
    qualification_registry = DatasetQualificationRegistry(qualification_root)
    preflight = qualify_admitted_dataset(
        reader=reader,
        qualification_registry=qualification_registry,
        dataset_id=args.dataset_id,
        quality_root=manifest_root,
        admission_policy=AdmissionPolicy(),
        checked_at=checked_at,
    )
    source_identity = collect_source_identity(ROOT)
    invocation_args = sys.argv[1:] if argv is None else argv
    invocation = " ".join(
        [sys.executable, "scripts/run_apex_milestone5.py", *invocation_args]
    )
    protocol = build_first_baseline_protocol(
        preflight=preflight,
        source_identity=source_identity,
        invocation=invocation,
        checked_at=checked_at,
    )
    protocol_registry = ApexProtocolRegistry(output_dir / "protocols")
    protocol_path = protocol_registry.record(protocol)
    qualification_path = qualification_root / f"{preflight.qualification.qualification_id}.json"
    slice_verification: list[dict[str, object]] = []
    protocol_dataset = cast(dict[str, object], protocol.canonical()["dataset"])
    binding_values = cast(list[object], protocol_dataset["experiment_input_bindings"])
    for binding_value in binding_values:
        binding = ExperimentInputBinding.from_canonical(binding_value)
        verification = reader.verify_slice(selected_read, binding)
        slice_verification.append(
            {
                "data_slice_identity": binding.data_slice_identity,
                "status": verification.status.value,
                "code": verification.code,
                "evaluation_rows": verification.evaluation_rows,
                "warmup_rows": verification.warmup_rows,
                "slice_content_hash": verification.slice_content_hash,
            }
        )
    result = build_blocked_result(
        protocol=protocol,
        preflight=preflight,
        source_identity=source_identity,
        protocol_reference=str(protocol_path.relative_to(ROOT)),
        qualification_reference=str(qualification_path.relative_to(ROOT)),
        inventory=inventory,
        slice_verification=slice_verification,
    )
    result_path = output_dir / "result.json"
    report_path = output_dir / "report.md"
    prior_result: object | None = None
    if result_path.exists():
        prior_result = json.loads(result_path.read_text(encoding="utf-8"))
    _json_write(result_path, result)
    report_path.write_text(render_blocked_report(result), encoding="utf-8")
    stable_match = prior_result == result if prior_result is not None else None
    _json_write(
        output_dir / "reproducibility.json",
        {
            "schema_version": "apex-milestone5-reproducibility.v1",
            "verification": (
                "PASS_STABLE_RESULT_MATCH"
                if stable_match
                else "INITIAL_RUN_NOT_COMPARABLE"
                if stable_match is None
                else "FAIL_STABLE_RESULT_MISMATCH"
            ),
            "volatile": {
                "verified_at": datetime.now(UTC).isoformat(),
                "prior_result_present": prior_result is not None,
            },
            "stable_result_identity": result["stable_result_identity"],
        },
    )
    print(f"outcome={result['outcome']}")
    print(f"dataset_id={args.dataset_id}")
    print(f"protocol_id={protocol.protocol_id}")
    print(f"protocol_hash={protocol.protocol_hash}")
    print(f"qualification_id={preflight.qualification.qualification_id}")
    print(f"blockers={','.join(preflight.blockers) if preflight.blockers else 'none'}")
    print(f"source_snapshot_content_digest={source_identity.snapshot_content_digest}")
    print(f"result={result_path.relative_to(ROOT)}")
    print(f"report={report_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
