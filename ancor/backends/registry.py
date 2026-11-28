"""Backend registry and category-to-(backend, args) router with cooldown & role policy."""
from __future__ import annotations

from dataclasses import dataclass
import time

from ..config import AncorConfig, BackendConfig, CategoryRouteTarget
from ..model_routing import CategoryError, DEFAULT_PROVIDER_COOLDOWN_SECONDS, normalize_category
from .agy import AgyBackend
from .base import ExecutionBackend, RoleName
from .codex import CodexBackend
from .dsh import DshBackend
from .hermes import HermesBackend

ADAPTER_CLASSES: dict[str, type[ExecutionBackend]] = {
    'hermes': HermesBackend,
    'codex': CodexBackend,
    'agy': AgyBackend,
    'dsh': DshBackend,
}


@dataclass(frozen=True)
class ResolvedRoute:
    backend: ExecutionBackend
    args: list[str]
    category: str | None = None
    fallback_used: bool = False


class BackendRegistry:
    """Instantiates and holds configured ExecutionBackend instances."""

    def __init__(self, backends: dict[str, ExecutionBackend]):
        self._backends = dict(backends)

    @classmethod
    def from_config(cls, config: AncorConfig) -> 'BackendRegistry':
        instances: dict[str, ExecutionBackend] = {}
        for name, bcfg in config.backends.items():
            instances[name] = cls._build_one(bcfg)
        return cls(instances)

    @staticmethod
    def _build_one(bcfg: BackendConfig) -> ExecutionBackend:
        adapter_cls = ADAPTER_CLASSES.get(bcfg.adapter)
        if adapter_cls is None:
            raise ValueError(
                f'unsupported backend adapter {bcfg.adapter!r} for backend {bcfg.name!r}; '
                f'supported adapters: {sorted(ADAPTER_CLASSES)}'
            )
        return adapter_cls(
            name=bcfg.name,
            command=bcfg.command,
            default_args=bcfg.default_args,
            env=bcfg.env,
            extra=bcfg.extra,
        )

    def get(self, name: str) -> ExecutionBackend:
        key = name.strip().lower()
        if key not in self._backends:
            raise KeyError(f'backend {name!r} is not configured')
        return self._backends[key]

    def names(self) -> list[str]:
        return sorted(self._backends.keys())

    def all(self) -> dict[str, ExecutionBackend]:
        return dict(self._backends)


class BackendRouter:
    """Routes (role, category, bound_backend) -> (ExecutionBackend, args) with cooldowns & role fence."""

    def __init__(self, config: AncorConfig, registry: BackendRegistry | None = None, *, clock=time.monotonic):
        self.config = config
        self.registry = registry or BackendRegistry.from_config(config)
        self._clock = clock
        self._cooldowns: dict[str, float] = {}

    def alias_names(self) -> frozenset[str]:
        return frozenset(self.config.categories.keys())

    def validate(self, category: str | None) -> str | None:
        cat = normalize_category(category)
        if cat is None:
            return None
        names = self.alias_names()
        if not names:
            raise CategoryError(f'category {cat!r} requested but no categories are configured')
        if cat not in names:
            raise CategoryError(f'unknown category {cat!r}; configured categories: {sorted(names)}')
        return cat

    def mark_backend_cooldown(self, backend_name: str, seconds: float = DEFAULT_PROVIDER_COOLDOWN_SECONDS) -> None:
        if backend_name:
            self._cooldowns[backend_name.strip().lower()] = float(self._clock()) + max(0.0, float(seconds))

    def is_backend_in_cooldown(self, backend_name: str) -> bool:
        exp = self._cooldowns.get(backend_name.strip().lower(), 0.0)
        return float(self._clock()) < exp

    def _check_role_policy(self, role: RoleName, backend: ExecutionBackend) -> None:
        policy = self.config.role_policy
        if role not in policy.require_readonly_fence_for:
            return
        if backend.capabilities.supports_readonly_fence:
            return
        if backend.name in policy.allow_soft_fence_backends:
            return
        raise PermissionError(
            f'backend {backend.name!r} does not support a readonly fence required for role {role!r}; '
            f'add {backend.name!r} to role_policy.allow_soft_fence_backends to override'
        )

    def role_category(self, role: RoleName) -> str | None:
        if role in self.config.categories:
            return role
        return None

    def resolve(
        self,
        *,
        role: RoleName,
        category: str | None = None,
        bound_backend: str | None = None,
        bound_args: list[str] | None = None,
    ) -> ResolvedRoute:
        """Resolve execution backend + raw CLI args.

        When ``bound_backend`` is set (resume of an existing goal session), lock to that backend
        if it is still registered; otherwise fall back to the category chain (caller will clear
        session_id and cold-start with a recovery signal).
        """
        if bound_backend:
            try:
                be = self.registry.get(bound_backend)
                self._check_role_policy(role, be)
                return ResolvedRoute(backend=be, args=list(bound_args or []), category=category, fallback_used=False)
            except KeyError:
                pass

        eff_cat = normalize_category(category) or self.role_category(role)
        chain: list[CategoryRouteTarget]
        if eff_cat and eff_cat in self.config.categories:
            chain = self.config.categories[eff_cat].chain()
        elif eff_cat:
            raise CategoryError(f'unknown category {eff_cat!r}')
        else:
            chain = [CategoryRouteTarget(backend=self.config.default_backend, args=[])]

        # Pick first non-cooldown candidate that satisfies role policy; if all in cooldown, use primary.
        candidates: list[tuple[int, ExecutionBackend, list[str]]] = []
        last_policy_err: PermissionError | None = None
        for idx, target in enumerate(chain):
            be = self.registry.get(target.backend)
            try:
                self._check_role_policy(role, be)
            except PermissionError as exc:
                last_policy_err = exc
                continue
            candidates.append((idx, be, list(target.args)))

        if not candidates:
            if last_policy_err is not None:
                raise last_policy_err
            raise RuntimeError(f'no eligible backend found for role={role!r}, category={eff_cat!r}')

        for idx, be, args in candidates:
            if not self.is_backend_in_cooldown(be.name):
                return ResolvedRoute(backend=be, args=args, category=eff_cat, fallback_used=(idx > 0))

        idx, be, args = candidates[0]
        return ResolvedRoute(backend=be, args=args, category=eff_cat, fallback_used=(idx > 0))
