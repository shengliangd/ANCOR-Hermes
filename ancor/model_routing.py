"""Ultra-thin model routing: ANCOR forwards a semantic ``category`` to Hermes.

Hermes owns everything else. ``ancor-exec/config.yaml`` holds ``model_aliases``
(category -> provider/model/base_url/key_env) and ``fallback_providers`` (the
profile-level ordered fallback chain). ANCOR never resolves an alias itself: it
launches ``hermes -p ancor-exec chat -m <category>`` and Hermes'
``resolve_startup_model_route`` restores provider, base_url and credentials.
Note: ``--provider`` is deliberately never passed, because an explicit provider
overrides the alias's own provider label inside Hermes.

The only things ANCOR reads are the alias *names* (to reject unknown categories
loudly at spawn time instead of silently falling back to the default model).
"""
from __future__ import annotations

import re
from pathlib import Path

_CATEGORY_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,63}$')
ROLE_DEFAULT_CATEGORY = {'root': 'root', 'judge': 'judge'}
REASONING_LEVELS = frozenset({'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'})


class CategoryError(ValueError):
    """Raised when a /goal names a model category that the exec profile does not define."""


def normalize_category(raw) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('category must be a non-empty string when provided')
    value = raw.strip().lower()
    if not _CATEGORY_RE.match(value):
        raise ValueError(f'category {raw!r} must match {_CATEGORY_RE.pattern}')
    return value


def alias_names_from_config(config: dict | None) -> frozenset[str]:
    """Alias names exactly as Hermes keys them (lower-cased ``model_aliases`` + ``model.aliases``)."""
    names: set[str] = set()
    if not isinstance(config, dict):
        return frozenset()
    aliases = config.get('model_aliases')
    if isinstance(aliases, dict):
        names.update(str(k).strip().lower() for k, v in aliases.items()
                     if isinstance(v, dict) and v.get('model'))
    model = config.get('model')
    simple = model.get('aliases') if isinstance(model, dict) else None
    if isinstance(simple, dict):
        names.update(str(k).strip().lower() for k, v in simple.items()
                     if (isinstance(v, str) and v.strip())
                     or (isinstance(v, dict) and v.get('model')))
    return frozenset(n for n in names if n)


def exec_profile_config_path(profile: str, override: str | None = None) -> Path | None:
    """Locate ``<profile>/config.yaml`` through Hermes' own profile resolver."""
    if override:
        return Path(override).expanduser()
    try:
        from hermes_cli.profiles import get_profile_dir
        return Path(get_profile_dir(profile)) / 'config.yaml'
    except Exception:
        return None


def load_alias_names(path: Path | None) -> frozenset[str] | None:
    """Read only alias names from a profile config. ``None`` means routing is unknown."""
    if path is None or not path.exists():
        return None
    import yaml
    try:
        config = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except Exception:
        return None
    return alias_names_from_config(config)


class CategoryRouter:
    """Resolves category -> Hermes CLI args; validates category names against exec aliases."""

    def __init__(self, *, alias_names=None, reasoning: dict | None = None):
        # alias_names: frozenset | None | callable returning either (re-read lazily so
        # editing ancor-exec/config.yaml needs no ANCOR restart).
        self._alias_source = alias_names
        self.reasoning = {}
        for key, level in (reasoning or {}).items():
            cat = normalize_category(key)
            if level not in REASONING_LEVELS:
                raise ValueError(f'category_reasoning[{key!r}] must be one of {sorted(REASONING_LEVELS)}')
            self.reasoning[cat] = level

    @classmethod
    def from_ctx(cls, ctx) -> 'CategoryRouter':
        profile = ctx.get_config('exec_profile', 'ancor-exec') if ctx else 'ancor-exec'
        override = ctx.get_config('exec_config_path') if ctx else None
        path = exec_profile_config_path(profile, override)
        reasoning = (ctx.get_config('category_reasoning') if ctx else None) or {}
        if not isinstance(reasoning, dict):
            raise ValueError('category_reasoning must be a mapping of category -> reasoning level')
        return cls(alias_names=lambda: load_alias_names(path), reasoning=reasoning)

    def alias_names(self) -> frozenset[str] | None:
        source = self._alias_source
        value = source() if callable(source) else source
        return None if value is None else frozenset(value)

    def validate(self, category: str | None) -> str | None:
        category = normalize_category(category)
        if category is None:
            return None
        names = self.alias_names()
        if names is None:
            raise CategoryError(
                f'category {category!r} requested but ancor-exec model_aliases are unavailable; '
                'omit category to use the ancor-exec default model')
        if category not in names:
            raise CategoryError(
                f'unknown category {category!r}; ancor-exec model_aliases define {sorted(names)}')
        return category

    def role_category(self, role: str) -> str | None:
        """Root/Judge try their same-named alias; absent alias -> ancor-exec default model."""
        candidate = ROLE_DEFAULT_CATEGORY.get(role)
        names = self.alias_names()
        if candidate and names is not None and candidate in names:
            return candidate
        return None

    def cli_args(self, category: str | None) -> list[str]:
        if not category:
            return []
        args = ['-m', category]
        level = self.reasoning.get(category)
        if level:
            args.extend(['--reasoning', level])
        return args
