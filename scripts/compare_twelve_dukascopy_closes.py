"""Compare separate local Twelve Data and Dukascopy close series.

This is a data-quality report only. It never merges datasets, runs a strategy,
or chooses a source using performance.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from traderos.data.comparison import compare_close_series
from traderos.data.dukascopy import iter_normalized_quote_records
from traderos.data.empirical import iter_normalized_ohlcv_records


def _unique_series(items: list[tuple[datetime, Decimal]], label: str) -> dict[datetime, Decimal]:
    series: dict[datetime, Decimal] = {}
    for timestamp, close in items:
        if timestamp in series:
            raise ValueError(f"{label} normalized dataset has duplicate timestamp {timestamp}")
        series[timestamp] = close
    return series


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("twelve_data_normalized", type=Path)
    parser.add_argument("dukascopy_normalized", type=Path)
    args = parser.parse_args()
    twelve = _unique_series(
        [
            (row.timestamp, row.close)
            for row in iter_normalized_ohlcv_records(args.twelve_data_normalized)
        ],
        "Twelve Data",
    )
    dukascopy = _unique_series(
        [
            (row.timestamp, row.bid_close)
            for row in iter_normalized_quote_records(args.dukascopy_normalized)
            if row.bid_close is not None
        ],
        "Dukascopy",
    )
    report = compare_close_series(
        twelve,
        dukascopy,
        source_a="twelve_data",
        source_b="dukascopy_bid",
    )
    print(json.dumps(report.canonical(), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
