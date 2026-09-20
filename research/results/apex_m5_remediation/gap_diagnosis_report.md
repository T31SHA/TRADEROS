# APEX Milestone 5 Data Remediation

Verdict: **BLOCKED**

This report diagnoses the preserved selected dataset without modifying the
original raw artifact, admission records, qualification record, frozen
protocol, blocked result, or reproducibility record. No strategy execution or
OOS access occurred.

## Selected artifact

- Dataset: `f49cd41e3c0c431a8c9b8812827197fffde81446443c8f8f24fc573a03f7b3f4`
- Instrument/timeframe: `EUR/USD` /
  `15m`
- Coverage: `2024-01-02T00:00:00+00:00` through
  `2024-02-02T00:00:00+00:00`
- Raw artifact: `data/raw/dukascopy/ad734960c1ea96fdf4b0055cdc74bde208f26c0e1ec1106a042ac22b90cf20b6/eurusd-tick-2024-01-02-2024-02-02.csv`
- Raw SHA-256: `ad734960c1ea96fdf4b0055cdc74bde208f26c0e1ec1106a042ac22b90cf20b6`
- Normalized content hash: `8047b30c9a330577365ebb78321311e1daa517c5a5aea62acd15a8f90f8c7c64`

## Gap findings

The existing admission report counted 808 absent M15 intervals: 768 calendar
closures and 40 unexpected open-session intervals. This inventory contains all
808 absent intervals. The 40 unexpected intervals occur in ten four-bar runs.

Classification counts:

{
  "ACQUISITION_EXPORT_OMISSION_LIKELY": 24,
  "EXPECTED_MARKET_CLOSURE": 768,
  "UNRESOLVED": 16
}

The first 24 unexpected intervals are covered by the overlapping preserved
comparison dataset `95aba7e61df680b84e516576fe6f3d96ae06a63ac8b9dbb827c5e9221efe5bdd`. Its normalized
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
