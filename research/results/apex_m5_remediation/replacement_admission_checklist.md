# Replacement Dataset Admission Checklist

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
<PYTHON> scripts/admit_real_dukascopy_ticks.py <RAW_CSV> \
  --coverage-start <UTC_START> \
  --coverage-end <UTC_END> \
  --download-timestamp <ACQUISITION_UTC> \
  --source-version <PROVIDER_OR_DOWNLOADER_RELEASE> \
  --license-reference <LICENSE_REFERENCE> \
  --data-root <DATA_ROOT>

<PYTHON> scripts/run_apex_milestone5.py \
  --dataset-id <NEW_DATASET_ID> \
  --data-root <DATA_ROOT> \
  --output-dir <OUTPUT_ROOT>
```

The existing script does not download data. Replace placeholders only after the
operator has supplied and verified the source/license evidence. Do not use
these commands with the rejected dataset as a shortcut.
