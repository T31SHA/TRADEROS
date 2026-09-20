# Read-only research and governance provenance

The provenance service answers which persisted research and governance facts
support a strategy version's current eligibility. It is a read model, not an
execution trace: it does not explain positions, orders, fills, account state,
risk-firewall decisions, or broker activity.

## Supported entities and relationships

The service reuses the existing `ExperimentRegistry` and
`StrategyGovernanceStore`. It emits typed nodes for the records that those
stores actually contain:

- strategy artifacts, current lifecycle state, lifecycle events, evidence,
  approvals, and revocations;
- immutable experiment specifications and results;
- verified dataset content, manifests, raw-artifact references, admission
  reports, qualification records, and derived experiment slices;
- candidate references and policy identity references when the source record
  names them.

Relationships are emitted only from stored identity/hash references. Dataset
edges include `RAW_ARTIFACT_SUPPORTS_DATASET`, `MANIFEST_DESCRIBES_DATASET`,
`ADMISSION_EVALUATES_DATASET`, `QUALIFICATION_EVALUATES_DATASET`,
`EXPERIMENT_USES_DATASET`, `EXPERIMENT_USES_QUALIFICATION`, and
`DATA_SLICE_DERIVED_FROM_DATASET`. Names, timestamps, file names, and
directory layout are never used to infer a relationship.

`DatasetRecordReader` reuses the empirical admission files: preserved raw
bytes belong to `RawArtifactStore`, normalized JSONL belongs to admission,
the manifest owns market-content and provenance identity, while the
normalized-file hash is verified separately; the quality report owns
the admission report. `DatasetQualificationRegistry` is deliberately a
separate create-only store for the research-policy verdict that admission
does not persist. It binds the exact dataset content hash and manifest hash.
No qualification is discovered by scanning similar names.

Dataset integrity, qualification verdict, and empirical eligibility are
separate fields. A hash-verified fixture can therefore be
`VERIFIED`/`QUALIFIED`/`DETERMINISTIC_TEST_FIXTURE` without being empirical
evidence. Raw verification is `UNSUPPORTED` when no configured raw-artifact
root is supplied; the query does not turn that limitation into a green
qualification.

## Integrity statuses and consistency

Every node and relationship has an integrity status. `VERIFIED` means the
stored schema, identity, and content hash were checked. `MISSING`,
`INVALID_SCHEMA`, `HASH_MISMATCH`, `UNAUTHORIZED`, and `UNSUPPORTED` remain
visible as non-valid evidence. Corrupt records are not repaired during a
query.

Governance tables are read through one database transaction. PostgreSQL uses a
repeatable-read snapshot, so the returned governance revision and lifecycle
history are coherent with one another. The experiment registry is immutable
filesystem state read separately. The response labels this boundary and does
not claim a cross-database/filesystem transaction.

History and attachment reads are bounded. Lifecycle history uses a revision
cursor (`after_revision`) and reports truncation plus `next_after_revision`.
Nodes and relationships have deterministic ordering. Graph construction also
checks for directed relationship cycles. No network fetches occur during
resolution.

## Eligibility explanations

`ProvenanceQueryService.explain_current_eligibility` returns the exact
strategy/version/artifact hash, current stage and revision, disablement,
approvals, evidence classification, OOS status, policy identities, blocking
reason codes, and review requirements. It delegates operational eligibility
to the authoritative pure `evaluate_current_eligibility` function; the read
layer does not duplicate promotion rules.

New experiment records with schema `phase9-experiment.v2` carry an
`ExperimentInputBinding`. It names the dataset identity/content hash,
manifest hash, qualification identity and policy, evaluation and warmup
intervals, instruments, slice identity, and feature-input lineage. The
normalized content is checked before a slice is reported as verified;
warmup rows are counted separately from evaluation rows. Locked-OOS
contamination remains a separate result fact. Existing `phase9-experiment.v1`
records are immutable and are reported as `LEGACY_UNBOUND`; no qualification
reference is fabricated for them.

`explain_lifecycle_event` reports the policy, evidence, approval, and recorded
event revision stored with that event. It also distinguishes whether later
revocation exists now from whether revocation existed at event time. Full
as-of reconstruction is not claimed because the current event schema does not
persist every historical projection and policy definition.

## Local read-only interface

The optional local CLI is deliberately not an HTTP endpoint. It resolves a
fixed internal actor named `local-readonly` with only `PROVENANCE_READ`; a
caller cannot supply a role string to gain authority. A production identity
provider is not present, so this interface is intended for a trusted local
operator environment and is not an externally exposed authorization boundary.

```bash
TRADEROS_DATABASE_URL=sqlite:////tmp/traderos.db \
TRADEROS_EXPERIMENT_REGISTRY=/srv/traderos/experiments \
TRADEROS_DATASET_MANIFEST_ROOT=/srv/traderos/manifests \
TRADEROS_DATASET_NORMALIZED_ROOT=/srv/traderos/normalized \
TRADEROS_DATASET_RAW_ROOT=/srv/traderos/raw \
TRADEROS_DATASET_QUALIFICATION_ROOT=/srv/traderos/qualifications \
python -m traderos.research.provenance_cli \
  --strategy-id mean-reversion --strategy-version 3
```

An experiment-only read is also available:

```bash
TRADEROS_DATABASE_URL=sqlite:////tmp/traderos.db \
TRADEROS_EXPERIMENT_REGISTRY=/srv/traderos/experiments \
python -m traderos.research.provenance_cli \
  --experiment-id research-0123456789abcdef01234567
```

Dataset, qualification, and experiment-input reads are available without
mutation:

```bash
python -m traderos.research.provenance_cli --dataset-id DATASET_ID
python -m traderos.research.provenance_cli --qualification-id QUALIFICATION_ID
python -m traderos.research.provenance_cli \
  --experiment-id research-... --explain-data
```

A bounded read-only experiment index is also available. It enumerates registry
filenames deterministically and does not validate or modify their contents;
select a returned identity with `--experiment-id` for integrity-checked reads:

```bash
TRADEROS_EXPERIMENT_REGISTRY=/srv/traderos/experiments \
python -m traderos.research.provenance_cli \
  --list-experiments --experiment-limit 100
```

The CLI still requires the local database and experiment-registry variables
because it uses the same fixed read service for all scopes. Dataset roots are
configured explicitly; paths are resolved beneath those roots and no network
download is attempted. The fixed local actor is trusted tooling based on
operating-system and file permissions, not an identity-provider replacement.

The command prints structured JSON. `--export PATH` writes a create-only
provenance bundle to a caller-selected existing directory; it never modifies
source records. Existing identical output is idempotent and conflicting
destination content is rejected.

## Export format

Exports use `provenance-export.v1` and contain stable `content`, generation
metadata, and a SHA-256 `content_digest`. Stable content includes query scope,
referenced identities and hashes, governance revision, integrity findings,
missing/unsupported references, bounded-history metadata, and the structured
eligibility explanation. Generation time is kept outside the digest so equal
queries produce equal content digests.

No credentials, connection strings, or unrestricted filesystem paths are
included.

Lifecycle mutation, approval, risk, capital, order, fill, and live-trading
authority remain outside this read model.
