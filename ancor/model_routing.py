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
import time
from pathlib import Path

_CATEGORY_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,63}$')
ROLE_DEFAULT_CATEGORY = {'root': 'root', 'judge': 'judge'}
REASONING_LEVELS = frozenset({'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'})
DEFAULT_PROVIDER_COOLDOWN_SECONDS = 600.0


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


def _load_yaml_dict(text: str) -> dict:
    try:
        import hermes_yaml as yaml
        data = yaml.safe_load(text)
    except ModuleNotFoundError:
        try:
            import yaml
            data = yaml.safe_load(text)
        except ModuleNotFoundError:
            from ruamel.yaml import YAML
            data = YAML(typ='safe').load(text)
    return data if isinstance(data, dict) else {}


def _normalize_route_entry(entry: dict | None, *, default_provider: str = '') -> dict | None:
    if not isinstance(entry, dict):
        return None
    model = str(entry.get('model') or '').strip()
    if not model:
        return None
    provider = str(entry.get('provider') or default_provider or '').strip()
    out = {'provider': provider, 'model': model}
    if entry.get('base_url'):
        out['base_url'] = str(entry['base_url']).strip()
    if entry.get('reasoning'):
        lvl = str(entry['reasoning']).strip().lower()
        if lvl in REASONING_LEVELS:
            out['reasoning'] = lvl
    return out


def alias_reasoning_from_config(config: dict | None) -> dict[str, str]:
    """Read optional per-alias ``reasoning`` levels from ``model_aliases``."""
    out: dict[str, str] = {}
    if not isinstance(config, dict):
        return out
    aliases = config.get('model_aliases')
    if not isinstance(aliases, dict):
        return out
    for raw_name, entry in aliases.items():
        if not isinstance(entry, dict) or not entry.get('reasoning'):
            continue
        lvl = str(entry['reasoning']).strip().lower()
        if lvl in REASONING_LEVELS:
            out[str(raw_name).strip().lower()] = lvl
    return out


def category_fallback_chain_from_config(config: dict | None, category: str | None) -> list[dict]:
    """Return ordered, deduplicated [primary, *category_fallbacks, *global_fallbacks]."""
    if not isinstance(config, dict):
        return []
    default_provider = str((config.get('model') or {}).get('provider') or '').strip()
    raw_entries: list[dict] = []

    if category:
        cat_key = category.strip().lower()
        aliases = config.get('model_aliases')
        if isinstance(aliases, dict):
            for k, v in aliases.items():
                if str(k).strip().lower() == cat_key and isinstance(v, dict):
                    primary = _normalize_route_entry(v, default_provider=default_provider)
                    if primary:
                        raw_entries.append(primary)
                    cat_fbs = v.get('fallback_providers')
                    if isinstance(cat_fbs, list):
                        for fb in cat_fbs:
                            norm = _normalize_route_entry(fb, default_provider=default_provider)
                            if norm:
                                raw_entries.append(norm)
                    break

    global_fbs = config.get('fallback_providers')
    if isinstance(global_fbs, list):
        for fb in global_fbs:
            norm = _normalize_route_entry(fb, default_provider=default_provider)
            if norm:
                raw_entries.append(norm)

    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw_entries:
        key = (entry['provider'].lower(), entry['model'].lower())
        if key not in seen:
            seen.add(key)
            deduped.append(entry)
    return deduped


def load_exec_config(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        return _load_yaml_dict(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def load_alias_names(path: Path | None) -> frozenset[str] | None:
    """Read only alias names from a profile config. ``None`` means routing is unknown."""
    config = load_exec_config(path)
    if config is None:
        return None
    return alias_names_from_config(config)


class CategoryRouter:
    """Resolves category -> Hermes CLI args; validates categories and merges per-category + global fallback chains."""

    def __init__(self, *, alias_names=None, reasoning: dict | None = None,
                 config_source=None, clock=None):
        # alias_names: frozenset | None | callable returning either (re-read lazily so
        # editing ancor-exec/config.yaml needs no ANCOR restart).
        self._alias_source = alias_names
        self._config_source = config_source
        self._clock = clock or time.monotonic
        self._provider_cooldowns: dict[str, float] = {}
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
        return cls(
            alias_names=lambda: load_alias_names(path),
            reasoning=reasoning,
            config_source=lambda: load_exec_config(path),
        )

    def _config(self) -> dict | None:
        src = self._config_source
        return src() if callable(src) else src

    def alias_names(self) -> frozenset[str] | None:
        source = self._alias_source
        if source is None and self._config_source is not None:
            cfg = self._config()
            return None if cfg is None else alias_names_from_config(cfg)
        value = source() if callable(source) else source
        return None if value is None else frozenset(value)

    def fallback_chain(self, category: str | None) -> list[dict]:
        """Effective ordered fallback chain: [primary, *category_fallbacks, *global_fallbacks] (deduped)."""
        return category_fallback_chain_from_config(self._config(), category)

    def mark_provider_cooldown(self, provider: str, seconds: float = DEFAULT_PROVIDER_COOLDOWN_SECONDS) -> None:
        if not provider:
            return
        self._provider_cooldowns[provider.strip().lower()] = float(self._clock()) + max(0.0, float(seconds))

    def is_provider_in_cooldown(self, provider: str | None) -> bool:
        if not provider:
            return False
        exp = self._provider_cooldowns.get(provider.strip().lower(), 0.0)
        return float(self._clock()) < exp

    def reasoning_level(self, category: str | None) -> str | None:
        if not category:
            return None
        cat = category.strip().lower()
        if cat in self.reasoning:
            return self.reasoning[cat]
        return alias_reasoning_from_config(self._config()).get(cat)

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

    def cli_args(self, category: str | None, *, resume: bool = False) -> list[str]:
        if not category:
            return []
        level = self.reasoning_level(category)
        if not resume:
            chain = self.fallback_chain(category)
            if chain and self.is_provider_in_cooldown(chain[0].get('provider')):
                for entry in chain[1:]:
                    if not self.is_provider_in_cooldown(entry.get('provider')):
                        args = ['-m', entry['model']]
                        if entry.get('provider'):
                            args.extend(['--provider', entry['provider']])
                        eff_level = entry.get('reasoning') or level
                        if eff_level:
                            args.extend(['--reasoning', eff_level])
                        return args
        args = ['-m', category]
        if level:
            args.extend(['--reasoning', level])
        return args
