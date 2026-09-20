"""Local read-only research and governance provenance interface.

The CLI intentionally resolves one fixed local read-only actor through the
internal actor directory.  It is not an HTTP endpoint and it cannot mutate
governance state, approve transitions, or submit trading activity.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from traderos.database.connection import create_database_engine
from traderos.research.dataset_records import DatasetQualificationRegistry, DatasetRecordReader
from traderos.research.provenance import ProvenanceQueryService
from traderos.research.registry import ExperimentRegistry
from traderos.research.strategy_lifecycle import ActorCapability, ActorDirectory
from traderos.research.strategy_store import StrategyGovernanceStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--strategy-id")
    scope.add_argument("--experiment-id")
    scope.add_argument("--dataset-id")
    scope.add_argument("--qualification-id")
    scope.add_argument(
        "--list-experiments",
        action="store_true",
        help="list a bounded deterministic index of local experiment records",
    )
    parser.add_argument("--strategy-version", help="required with --strategy-id")
    parser.add_argument(
        "--explain-data",
        action="store_true",
        help="explain an experiment's immutable dataset eligibility",
    )
    parser.add_argument("--export", type=Path, help="create a provenance export at this path")
    parser.add_argument("--history-limit", type=int, default=100)
    parser.add_argument("--experiment-limit", type=int, default=100)
    parser.add_argument("--after-revision", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.strategy_id is not None and not args.strategy_version:
        raise SystemExit("--strategy-version is required with --strategy-id")
    if args.strategy_id is not None and args.explain_data:
        raise SystemExit("--explain-data requires --experiment-id")
    experiment_root = os.environ.get("TRADEROS_EXPERIMENT_REGISTRY")
    if args.list_experiments:
        if not experiment_root:
            raise SystemExit("TRADEROS_EXPERIMENT_REGISTRY is required")
        registry = ExperimentRegistry(Path(experiment_root))
        print(
            json.dumps(
                {
                    "experiment_limit": args.experiment_limit,
                    "experiments": registry.list_experiment_ids(limit=args.experiment_limit),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0

    database_url = os.environ.get("TRADEROS_DATABASE_URL")
    if not database_url or not experiment_root:
        raise SystemExit("TRADEROS_DATABASE_URL and TRADEROS_EXPERIMENT_REGISTRY are required")

    engine = create_database_engine(database_url)
    store = StrategyGovernanceStore(engine)
    actors = ActorDirectory({"local-readonly": (ActorCapability.PROVENANCE_READ,)})
    manifest_root = os.environ.get("TRADEROS_DATASET_MANIFEST_ROOT")
    normalized_root = os.environ.get("TRADEROS_DATASET_NORMALIZED_ROOT")
    dataset_reader = (
        DatasetRecordReader(
            Path(manifest_root),
            Path(normalized_root),
            raw_root=(
                Path(os.environ["TRADEROS_DATASET_RAW_ROOT"])
                if os.environ.get("TRADEROS_DATASET_RAW_ROOT")
                else None
            ),
            quality_report_root=(
                Path(os.environ["TRADEROS_DATASET_QUALITY_ROOT"])
                if os.environ.get("TRADEROS_DATASET_QUALITY_ROOT")
                else None
            ),
        )
        if manifest_root and normalized_root
        else None
    )
    qualification_root = os.environ.get("TRADEROS_DATASET_QUALIFICATION_ROOT")
    service = ProvenanceQueryService(
        store,
        ExperimentRegistry(Path(experiment_root)),
        actors,
        dataset_reader=dataset_reader,
        qualification_registry=(
            DatasetQualificationRegistry(Path(qualification_root))
            if qualification_root
            else None
        ),
    )
    if args.experiment_id is not None:
        if args.explain_data:
            result = service.explain_experiment_data_eligibility(
                args.experiment_id, actor_id="local-readonly"
            )
            print(json.dumps(result.canonical(), sort_keys=True, separators=(",", ":")))
            return 0
        graph = service.get_experiment_provenance(
            args.experiment_id, actor_id="local-readonly"
        )
        if args.export is not None:
            print(
                service.export_graph_bundle(graph, destination=args.export).to_json(),
                end="",
            )
            return 0
        print(json.dumps(graph.canonical(), sort_keys=True, separators=(",", ":")))
        return 0

    if args.dataset_id is not None:
        graph = service.get_dataset_provenance(args.dataset_id, actor_id="local-readonly")
        if args.export is not None:
            print(service.export_graph_bundle(graph, destination=args.export).to_json(), end="")
        else:
            print(json.dumps(graph.canonical(), sort_keys=True, separators=(",", ":")))
        return 0
    if args.qualification_id is not None:
        graph = service.get_qualification_provenance(
            args.qualification_id, actor_id="local-readonly"
        )
        if args.export is not None:
            print(service.export_graph_bundle(graph, destination=args.export).to_json(), end="")
        else:
            print(json.dumps(graph.canonical(), sort_keys=True, separators=(",", ":")))
        return 0

    if args.export is not None:
        export = service.export_provenance_bundle(
            args.strategy_id,
            args.strategy_version,
            actor_id="local-readonly",
            destination=args.export,
            history_limit=args.history_limit,
            after_revision=args.after_revision,
        )
        print(export.to_json(), end="")
        return 0

    graph = service.get_strategy_provenance(
        args.strategy_id,
        args.strategy_version,
        actor_id="local-readonly",
        history_limit=args.history_limit,
        after_revision=args.after_revision,
    )
    print(json.dumps(graph.canonical(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
