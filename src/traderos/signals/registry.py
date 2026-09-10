"""Instance-scoped registry for controlled, versioned fusion policies."""

from collections.abc import Iterable

from traderos.signals.errors import FusionPolicyRegistryError
from traderos.signals.models import FusionPolicy


class FusionPolicyRegistry:
    """Register policies by explicit ``(policy_id, policy_version)`` identity."""

    def __init__(self) -> None:
        self._policies: dict[tuple[str, str], FusionPolicy] = {}

    def register(self, policy: FusionPolicy) -> None:
        metadata = policy.metadata
        key = metadata.policy_id, metadata.policy_version
        if key in self._policies:
            raise FusionPolicyRegistryError(f"fusion policy {key!r} is already registered")
        if (
            policy.policy_id != metadata.policy_id
            or policy.policy_version != metadata.policy_version
        ):
            raise FusionPolicyRegistryError("policy attributes do not match metadata")
        self._policies[key] = policy

    def get(self, policy_id: str, policy_version: str | None = None) -> FusionPolicy:
        """Resolve exactly one policy; ambiguous unversioned requests fail closed."""

        if policy_version is not None:
            try:
                return self._policies[(policy_id, policy_version)]
            except KeyError as exc:
                raise FusionPolicyRegistryError(
                    f"fusion policy {policy_id}.v{policy_version} is not registered"
                ) from exc
        matches = [
            policy for (identifier, _), policy in self._policies.items() if identifier == policy_id
        ]
        if len(matches) != 1:
            raise FusionPolicyRegistryError(
                f"fusion policy {policy_id!r} requires an explicit version"
            )
        return matches[0]

    def all(self) -> tuple[FusionPolicy, ...]:
        """Return policies in deterministic identity order."""

        return tuple(self._policies[key] for key in sorted(self._policies))


def build_fusion_policy_registry(policies: Iterable[FusionPolicy]) -> FusionPolicyRegistry:
    """Build a policy registry without global mutable policy state."""

    registry = FusionPolicyRegistry()
    for policy in policies:
        registry.register(policy)
    return registry


__all__ = ["FusionPolicyRegistry", "build_fusion_policy_registry"]
