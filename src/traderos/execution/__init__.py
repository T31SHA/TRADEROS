"""Execution orchestration and authority boundary.

Phase 8 exposes only the offline :mod:`traderos.paper` adapter. No live broker
adapter, endpoint, credential, or network transport exists here.
"""

from traderos.execution.authority import ExecutionAuthority

__all__ = ["ExecutionAuthority"]
