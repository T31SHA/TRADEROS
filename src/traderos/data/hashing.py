"""Canonical content identity for validated market bars."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from traderos.data.bars import MarketBar


def market_bar_content_payload(bars: Sequence[MarketBar]) -> dict[str, object]:
    """Return the canonical payload used by research and operational lineage."""

    if not bars:
        raise ValueError("a dataset content hash requires at least one bar")
    ordered = tuple(sorted(bars, key=lambda item: (item.timestamp, item.symbol, item.source)))
    first = ordered[0]
    return {
        "timeframe": first.timeframe.value,
        "adjustment_policy": first.adjustment_policy.value,
        "bars": [
            {
                "symbol": item.symbol,
                "timestamp": item.timestamp.isoformat(),
                "open": str(item.open),
                "high": str(item.high),
                "low": str(item.low),
                "close": str(item.close),
                "volume": str(item.volume) if item.volume is not None else None,
                "source": item.source,
                "bid": str(item.bid) if item.bid is not None else None,
                "ask": str(item.ask) if item.ask is not None else None,
                "quote_timestamp": (
                    item.quote_timestamp.isoformat()
                    if item.quote_timestamp is not None
                    else None
                ),
                "spread": str(item.spread) if item.spread is not None else None,
                "adjusted_close": (
                    str(item.adjusted_close) if item.adjusted_close is not None else None
                ),
                "trade_count": item.trade_count,
                "vwap": str(item.vwap) if item.vwap is not None else None,
            }
            for item in ordered
        ],
    }


def market_bar_content_hash(bars: Sequence[MarketBar]) -> str:
    """Hash the exact canonical validated bars, independent of caller order."""

    encoded = json.dumps(
        market_bar_content_payload(bars), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["market_bar_content_hash", "market_bar_content_payload"]
