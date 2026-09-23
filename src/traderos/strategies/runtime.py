"""Trusted source-manifest identities for registered strategy runtimes."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from types import ModuleType

import traderos.strategies.models as strategy_models
from traderos.strategies.base import Strategy


class RuntimeIdentityError(ValueError):
    """Raised when a loaded strategy cannot be tied to trusted source."""


def implementation_locator(strategy: Strategy) -> str:
    """Return the diagnostic locator for already-loaded trusted runtime code."""

    return f"{strategy.__class__.__module__}:{strategy.__class__.__qualname__}"


def _module_source(module: ModuleType) -> tuple[str, bytes]:
    path = getattr(module, "__file__", None)
    if not isinstance(path, str) or not path.endswith(".py"):
        raise RuntimeIdentityError("strategy runtime source is unavailable")
    source_path = Path(path).resolve()
    trusted_root = Path(__file__).resolve().parents[1]
    try:
        source_path.relative_to(trusted_root)
    except ValueError as exc:
        raise RuntimeIdentityError(
            "strategy runtime source is outside the trusted package"
        ) from exc
    try:
        return source_path.as_posix(), source_path.read_bytes()
    except OSError as exc:
        raise RuntimeIdentityError("strategy runtime source cannot be read") from exc


def runtime_content_identity(strategy: Strategy) -> str:
    """Hash the loaded implementation plus its fixed local strategy dependencies.

    The dependency set is deliberately code-owned, not artifact-owned.  An
    artifact can name a logical implementation, but it cannot supply a module
    path or cause arbitrary code loading.  Missing source or a source outside
    the installed TRADEROS package fails closed.
    """

    implementation_module = inspect.getmodule(strategy.__class__)
    base_module = inspect.getmodule(Strategy)
    if implementation_module is None or base_module is None:
        raise RuntimeIdentityError("strategy runtime module is unavailable")
    dependency_modules = (implementation_module, base_module, strategy_models)
    manifest: list[dict[str, str]] = []
    for module in dependency_modules:
        path, content = _module_source(module)
        manifest.append(
            {
                "module": module.__name__,
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    try:
        class_source = inspect.getsource(strategy.__class__)
    except (OSError, TypeError) as exc:
        raise RuntimeIdentityError("strategy implementation source is unavailable") from exc
    payload = {
        "manifest_version": "strategy-runtime-source.v1",
        "locator": implementation_locator(strategy),
        "class_source": class_source,
        "dependencies": sorted(manifest, key=lambda item: item["module"]),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"runtime-source-sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = ["RuntimeIdentityError", "implementation_locator", "runtime_content_identity"]
