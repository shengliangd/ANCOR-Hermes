"""Standalone ANCOR configuration loader (~/.ancor/config.yaml).

Zero Hermes dependency: backends, categories (backend + raw CLI args + fallback chain),
role security policy, and multi-channel notifiers are all configured here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any

from .model_routing import normalize_category

DEFAULT_CONFIG_DIR = Path.home() / '.ancor'
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / 'config.yaml'
DEFAULT_STORE_PATH = DEFAULT_CONFIG_DIR / 'requests.db'

_ENV_VAR_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


def _load_yaml(text: str) -> dict[str, Any]:
    try:
        import yaml
        data = yaml.safe_load(text)
    except ModuleNotFoundError:
        try:
            import hermes_yaml as yaml  # type: ignore[import-not-found]
            data = yaml.safe_load(text)
        except ModuleNotFoundError:
            from ruamel.yaml import YAML  # type: ignore[import-not-found]
            data = YAML(typ='safe').load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError('ANCOR config root must be a YAML mapping')
    return data


def expand_env_vars(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ''), value)
    if isinstance(value, list):
        return [expand_env_vars(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    return value


def _str_list(raw: Any, field_name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError(f'{field_name} must be a list of strings')
    return list(raw)


@dataclass(frozen=True)
class BackendConfig:
    name: str
    adapter: str
    command: str
    default_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CategoryRouteTarget:
    backend: str
    args: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CategoryConfig:
    name: str
    backend: str
    args: list[str] = field(default_factory=list)
    fallbacks: list[CategoryRouteTarget] = field(default_factory=list)

    def chain(self) -> list[CategoryRouteTarget]:
        return [CategoryRouteTarget(self.backend, list(self.args)), *self.fallbacks]


@dataclass(frozen=True)
class RolePolicyConfig:
    require_readonly_fence_for: frozenset[str] = frozenset({'root', 'judge'})
    allow_soft_fence_backends: frozenset[str] = frozenset()


@dataclass(frozen=True)
class NotifierConfig:
    name: str
    type: str  # 'command' | 'webhook'
    kinds: frozenset[str] = frozenset({'question', 'completion'})
    timeout_seconds: float = 60.0
    # command notifier fields
    command: str | None = None
    args: list[str] = field(default_factory=list)
    input_mode: str = 'stdin_json'  # 'stdin_json' | 'stdin_text' | 'template_args'
    env: dict[str, str] = field(default_factory=dict)
    # webhook notifier fields
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AncorConfig:
    config_path: Path | None
    store_path: Path
    cluster_state_path: Path
    poll_interval: float = 15.0
    event_batch_size: int = 12
    max_judge_rejects: int = 3
    global_limits: dict[str, float | int] = field(default_factory=dict)
    default_backend: str = 'hermes'
    backends: dict[str, BackendConfig] = field(default_factory=dict)
    categories: dict[str, CategoryConfig] = field(default_factory=dict)
    role_policy: RolePolicyConfig = field(default_factory=RolePolicyConfig)
    notifiers: list[NotifierConfig] = field(default_factory=list)
    memory_dir: Path | None = None
    memory_stale_days: int = 30
    session_reuse_ttl_seconds: float = 300.0
    max_wakes_per_session: int = 3
    max_shifts_per_goal: int = 20
    daemon_pid_path: Path | None = None
    daemon_state_path: Path | None = None

    @property
    def notifications_path(self) -> Path:
        return Path(str(self.store_path) + '.notifications.db')

    @property
    def runtime_dir(self) -> Path:
        return self.store_path.parent

    def effective_memory_dir(self) -> Path:
        return self.memory_dir or (self.runtime_dir / 'memory')

    def effective_daemon_pid_path(self) -> Path:
        return self.daemon_pid_path or (self.runtime_dir / 'ancor-daemon.pid')

    def effective_daemon_state_path(self) -> Path:
        return self.daemon_state_path or (self.runtime_dir / 'ancor-daemon.status.json')


def resolve_config_path(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_path = os.environ.get('ANCOR_CONFIG')
    if env_path:
        return Path(env_path).expanduser().resolve()
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    return None


def _default_backends() -> dict[str, BackendConfig]:
    return {
        'hermes': BackendConfig(
            name='hermes',
            adapter='hermes',
            command='hermes',
            default_args=['-p', 'ancor-exec'],
        ),
        'codex': BackendConfig(
            name='codex',
            adapter='codex',
            command='codex',
            default_args=[],
        ),
        'agy': BackendConfig(
            name='agy',
            adapter='agy',
            command='agy',
            default_args=[],
        ),
        'dsh': BackendConfig(
            name='dsh',
            adapter='dsh',
            command='dsh',
            default_args=[],
        ),
    }


def _parse_backends(raw: Any) -> dict[str, BackendConfig]:
    if not raw:
        return _default_backends()
    if not isinstance(raw, dict):
        raise ValueError('backends must be a mapping')
    out: dict[str, BackendConfig] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError('backend name must be a non-empty string')
        key = name.strip().lower()
        if not isinstance(entry, dict):
            raise ValueError(f'backends.{key} must be a mapping')
        adapter = str(entry.get('adapter') or key).strip().lower()
        command = str(entry.get('command') or key).strip()
        default_args = _str_list(entry.get('default_args'), f'backends.{key}.default_args')
        env = entry.get('env') or {}
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise ValueError(f'backends.{key}.env must be a string-to-string mapping')
        known = {'adapter', 'command', 'default_args', 'env'}
        extra = {k: v for k, v in entry.items() if k not in known}
        out[key] = BackendConfig(
            name=key,
            adapter=adapter,
            command=command,
            default_args=default_args,
            env=expand_env_vars(env),
            extra=extra,
        )
    return out


def _parse_categories(raw: Any, backends: dict[str, BackendConfig]) -> dict[str, CategoryConfig]:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError('categories must be a mapping')
    out: dict[str, CategoryConfig] = {}
    for raw_name, entry in raw.items():
        name = normalize_category(raw_name)
        if not name:
            raise ValueError('category name must be non-empty')
        if not isinstance(entry, dict):
            raise ValueError(f'categories.{name} must be a mapping')
        backend = str(entry.get('backend') or '').strip().lower()
        if not backend:
            raise ValueError(f'categories.{name}.backend is required')
        if backend not in backends:
            raise ValueError(f'categories.{name}.backend {backend!r} is not defined in backends')
        args = _str_list(entry.get('args'), f'categories.{name}.args')
        fallbacks: list[CategoryRouteTarget] = []
        raw_fbs = entry.get('fallbacks') or []
        if not isinstance(raw_fbs, list):
            raise ValueError(f'categories.{name}.fallbacks must be a list')
        for idx, fb in enumerate(raw_fbs):
            if not isinstance(fb, dict):
                raise ValueError(f'categories.{name}.fallbacks[{idx}] must be a mapping')
            fb_backend = str(fb.get('backend') or '').strip().lower()
            if not fb_backend or fb_backend not in backends:
                raise ValueError(
                    f'categories.{name}.fallbacks[{idx}].backend {fb_backend!r} is not defined in backends')
            fb_args = _str_list(fb.get('args'), f'categories.{name}.fallbacks[{idx}].args')
            fallbacks.append(CategoryRouteTarget(backend=fb_backend, args=fb_args))
        out[name] = CategoryConfig(name=name, backend=backend, args=args, fallbacks=fallbacks)
    return out


def _parse_role_policy(raw: Any) -> RolePolicyConfig:
    if not raw:
        return RolePolicyConfig()
    if not isinstance(raw, dict):
        raise ValueError('role_policy must be a mapping')
    req = raw.get('require_readonly_fence_for', ['root', 'judge'])
    allow = raw.get('allow_soft_fence_backends', [])
    return RolePolicyConfig(
        require_readonly_fence_for=frozenset(s.strip().lower() for s in _str_list(req, 'role_policy.require_readonly_fence_for')),
        allow_soft_fence_backends=frozenset(s.strip().lower() for s in _str_list(allow, 'role_policy.allow_soft_fence_backends')),
    )


def _parse_notifiers(raw: Any) -> list[NotifierConfig]:
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError('notifiers must be a list')
    out: list[NotifierConfig] = []
    seen: set[str] = set()
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f'notifiers[{idx}] must be a mapping')
        name = str(entry.get('name') or f'notifier-{idx + 1}').strip()
        if not name or name in seen:
            raise ValueError(f'duplicate or empty notifier name {name!r}')
        seen.add(name)
        kind = str(entry.get('type') or 'command').strip().lower()
        if kind not in {'command', 'webhook'}:
            raise ValueError(f'notifiers[{name!r}].type must be "command" or "webhook"')
        kinds = frozenset(
            k.strip().lower()
            for k in _str_list(entry.get('kinds') or ['question', 'completion'], f'notifiers[{name!r}].kinds')
        )
        timeout = float(entry.get('timeout_seconds', 60.0))
        if timeout <= 0:
            raise ValueError(f'notifiers[{name!r}].timeout_seconds must be positive')
        if kind == 'command':
            cmd = str(entry.get('command') or '').strip()
            if not cmd:
                raise ValueError(f'notifiers[{name!r}].command is required')
            args = _str_list(entry.get('args'), f'notifiers[{name!r}].args')
            input_mode = str(entry.get('input_mode') or 'stdin_json').strip().lower()
            if input_mode not in {'stdin_json', 'stdin_text', 'template_args'}:
                raise ValueError(f'notifiers[{name!r}].input_mode must be stdin_json, stdin_text, or template_args')
            env = entry.get('env') or {}
            if not isinstance(env, dict):
                raise ValueError(f'notifiers[{name!r}].env must be a mapping')
            out.append(NotifierConfig(
                name=name,
                type='command',
                kinds=kinds,
                timeout_seconds=timeout,
                command=cmd,
                args=expand_env_vars(args),
                input_mode=input_mode,
                env=expand_env_vars({str(k): str(v) for k, v in env.items()}),
            ))
        else:
            url = expand_env_vars(str(entry.get('url') or '').strip())
            if not url:
                raise ValueError(f'notifiers[{name!r}].url is required')
            headers = entry.get('headers') or {}
            if not isinstance(headers, dict):
                raise ValueError(f'notifiers[{name!r}].headers must be a mapping')
            out.append(NotifierConfig(
                name=name,
                type='webhook',
                kinds=kinds,
                timeout_seconds=timeout,
                url=url,
                headers=expand_env_vars({str(k): str(v) for k, v in headers.items()}),
            ))
    return out


def load_config(path: str | Path | None = None, *, store_path_override: str | Path | None = None) -> AncorConfig:
    resolved = resolve_config_path(path)
    raw: dict[str, Any] = {}
    if resolved is not None:
        if not resolved.exists():
            raise FileNotFoundError(f'ANCOR config file not found: {resolved}')
        raw = _load_yaml(resolved.read_text(encoding='utf-8'))

    raw_store = (
        store_path_override
        or (raw.get('store_path') if path is not None else None)
        or os.environ.get('ANCOR_STORE_PATH')
        or os.environ.get('ANCOR_DB_PATH')
        or raw.get('store_path')
        or str(DEFAULT_STORE_PATH)
    )
    store_path = Path(str(raw_store)).expanduser().resolve()

    raw_cluster = os.environ.get('ANCOR_CLUSTER_STATE_PATH') or raw.get('cluster_state_path')
    cluster_path = (
        Path(str(raw_cluster)).expanduser().resolve()
        if raw_cluster else (store_path.parent / 'cluster.json')
    )

    backends = _parse_backends(raw.get('backends'))
    categories = _parse_categories(raw.get('categories'), backends)
    default_backend = str(raw.get('default_backend') or next(iter(backends))).strip().lower()
    if default_backend not in backends:
        raise ValueError(f'default_backend {default_backend!r} is not defined in backends')

    poll_interval = max(1.0, float(raw.get('poll_interval', 15.0)))
    event_batch_size = int(raw.get('event_batch_size', 12))
    if not 1 <= event_batch_size <= 32:
        raise ValueError('event_batch_size must be between 1 and 32')
    max_judge_rejects = int(raw.get('max_judge_rejects', 3))
    if max_judge_rejects < 1:
        raise ValueError('max_judge_rejects must be >= 1')

    global_limits = raw.get('global_limits') or {}
    if not isinstance(global_limits, dict):
        raise ValueError('global_limits must be a mapping')

    raw_memory_dir = os.environ.get('ANCOR_MEMORY_DIR') or raw.get('memory_dir')
    memory_dir = Path(str(raw_memory_dir)).expanduser().resolve() if raw_memory_dir else None
    memory_stale_days = max(1, int(raw.get('memory_stale_days', 30)))
    session_reuse_ttl_seconds = max(0.0, float(raw.get('session_reuse_ttl_seconds', 300.0)))
    max_wakes_per_session = max(1, int(raw.get('max_wakes_per_session', 3)))
    max_shifts_per_goal = max(1, int(raw.get('max_shifts_per_goal', 20)))

    return AncorConfig(
        config_path=resolved,
        store_path=store_path,
        cluster_state_path=cluster_path,
        poll_interval=poll_interval,
        event_batch_size=event_batch_size,
        max_judge_rejects=max_judge_rejects,
        global_limits=dict(global_limits),
        default_backend=default_backend,
        backends=backends,
        categories=categories,
        role_policy=_parse_role_policy(raw.get('role_policy')),
        notifiers=_parse_notifiers(raw.get('notifiers')),
        memory_dir=memory_dir,
        memory_stale_days=memory_stale_days,
        session_reuse_ttl_seconds=session_reuse_ttl_seconds,
        max_wakes_per_session=max_wakes_per_session,
        max_shifts_per_goal=max_shifts_per_goal,
    )


def default_config_yaml(store_path: str | Path | None = None) -> str:
    sp = str(Path(store_path).expanduser().resolve()) if store_path else '~/.ancor/requests.db'
    return f'''# ANCOR standalone configuration
store_path: {sp}
poll_interval: 15
event_batch_size: 12
max_judge_rejects: 3

default_backend: hermes

backends:
  hermes:
    adapter: hermes
    command: hermes
    default_args: ["-p", "ancor-exec"]
  codex:
    adapter: codex
    command: codex
    default_args: []

categories:
  root:
    backend: hermes
    args: ["-m", "root"]
  judge:
    backend: codex
    args: []
    fallbacks:
      - backend: hermes
        args: ["-m", "judge"]
  coding:
    backend: codex
    args: []
    fallbacks:
      - backend: hermes
        args: ["-m", "coding"]
  research:
    backend: hermes
    args: ["-m", "research"]

role_policy:
  require_readonly_fence_for: ["root", "judge"]
  allow_soft_fence_backends: []

notifiers: []
'''
