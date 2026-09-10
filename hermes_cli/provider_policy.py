"""Authority for provider routing and credentials in the active Hermes home.

This is deliberately separate from general tool secrets. A tool may use an
inherited key without making that key eligible for inference.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


class ProviderPolicyError(ValueError):
    """Provider configuration cannot satisfy the selected authentication policy."""


def validate_provider_auth(config: Mapping[str, Any]) -> str:
    block = config.get("provider_auth", {})
    if not isinstance(block, Mapping):
        raise ProviderPolicyError("provider_auth must be a mapping with mode: auto or config_only")
    mode = block.get("mode", "auto")
    if mode not in ("auto", "config_only"):
        raise ProviderPolicyError("provider_auth.mode must be auto or config_only")
    return mode


def _canonical_provider(name: str, *, declared: bool = False) -> str:
    from hermes_cli.auth import PROVIDER_REGISTRY, _PROVIDER_ALIASES
    normalized = str(name).strip().lower()
    if normalized.startswith("custom:"):
        return normalized
    if declared and normalized not in PROVIDER_REGISTRY and normalized not in {"custom", "openrouter"}:
        return "custom:" + normalized
    return _PROVIDER_ALIASES.get(normalized, normalized)


@dataclass(frozen=True)
class ProviderAuthPolicy:
    mode: str
    home: Path
    _config_json: str = field(repr=False)
    _local_env: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    @property
    def config_only(self) -> bool:
        return self.mode == "config_only"

    @property
    def config(self) -> dict[str, Any]:
        return json.loads(self._config_json)

    @property
    def cache_key(self) -> tuple[str, str, str]:
        # Include a digest, never credential text, in cache identities.
        digest = hashlib.sha256((self._config_json + repr(self._local_env)).encode()).hexdigest()
        return str(self.home), self.mode, digest

    @property
    def default_provider(self) -> str:
        model = self.config.get("model", {})
        if not isinstance(model, dict):
            return ""
        selected = str(model.get("provider") or "").strip()
        if not selected and isinstance(model.get("default"), dict):
            selected = str(model["default"].get("provider") or "").strip()
        if not selected or selected == "auto":
            return "custom" if model.get("base_url") else ""
        canonical = _canonical_provider(selected)
        entries = self._entries()
        return canonical if canonical in entries else "custom:" + canonical

    def _entries(self) -> dict[str, dict[str, Any]]:
        config = self.config
        result: dict[str, dict[str, Any]] = {}
        providers = config.get("providers", {})
        if isinstance(providers, dict):
            for name, entry in providers.items():
                if isinstance(entry, dict):
                    result[_canonical_provider(name, declared=True)] = dict(entry)
        legacy = config.get("custom_providers", [])
        if isinstance(legacy, list):
            for entry in legacy:
                if isinstance(entry, dict) and entry.get("name"):
                    result.setdefault("custom:" + str(entry["name"]).strip().lower(), dict(entry))
        model = config.get("model", {})
        if isinstance(model, dict):
            selected = str(model.get("provider") or "").strip()
            if not selected and isinstance(model.get("default"), dict):
                selected = str(model["default"].get("provider") or "").strip()
            if (not selected or selected == "auto") and model.get("base_url"):
                selected = "custom"
            if selected and selected != "auto":
                name = _canonical_provider(selected)
                if name not in result and "custom:" + name in result:
                    name = "custom:" + name
                # Main-model credentials describe this provider only.
                result[name] = {**model, **result.get(name, {})}
        return {name: entry for name, entry in result.items() if entry.get("enabled", True) is not False}

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(self._entries())

    def provider_config(self, provider: str) -> dict[str, Any]:
        self.require_provider(provider)
        entries = self._entries()
        name = _canonical_provider(provider)
        return dict(entries.get(name, entries.get("custom:" + name, {})))

    def permits_provider(self, provider: str) -> bool:
        if not self.config_only:
            return True
        name = _canonical_provider(provider)
        entries = self._entries()
        return name in entries or "custom:" + name in entries

    def require_provider(self, provider: str) -> None:
        if not self.permits_provider(provider):
            raise ProviderPolicyError(
                f"Provider {provider!r} is not configured in this Hermes home. "
                "Declare it in config.yaml before using provider_auth.mode: config_only."
            )

    def local_path(self, path: str | Path) -> Path:
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = self.home / target
        target = target.resolve()
        if self.config_only and not target.is_relative_to(self.home):
            raise ProviderPolicyError("Provider credential path leaves the active Hermes home")
        return target

    def env_value(self, name: str, default: str = "") -> str:
        if not self.config_only:
            from agent.secret_scope import get_secret
            return get_secret(name, default) or default
        value = dict(self._local_env).get(name, default)
        if "${" in value:
            raise ProviderPolicyError(
                f"Provider credential {name!r} must be a literal in the active home's .env; interpolation is disabled"
            )
        return value

    def require_external_source(self, source: str) -> None:
        if self.config_only:
            raise ProviderPolicyError(f"Provider source {source!r} is disabled by provider_auth.mode: config_only")

    def local_provenance(self, source: str = "local_login") -> dict[str, str]:
        return {"source": source, "home": str(self.home)}

    def allows_record(self, record: Mapping[str, Any]) -> bool:
        if not self.config_only:
            return True
        provenance = record.get("provenance")
        return (isinstance(provenance, Mapping)
                and provenance.get("source") in {"local_login", "local_key", "local_env", "config"}
                and provenance.get("home") == str(self.home))


_PROVIDER_AUTH_POLICY: ContextVar[ProviderAuthPolicy | None] = ContextVar("provider_auth_policy", default=None)


def set_provider_auth_policy(policy: ProviderAuthPolicy | None) -> Token:
    return _PROVIDER_AUTH_POLICY.set(policy)


def reset_provider_auth_policy(token: Token) -> None:
    _PROVIDER_AUTH_POLICY.reset(token)


@contextmanager
def provider_auth_scope(policy: ProviderAuthPolicy):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_provider_auth_policy(policy)
    home_token = set_hermes_home_override(policy.home)
    try:
        yield policy
    finally:
        reset_hermes_home_override(home_token)
        reset_provider_auth_policy(token)


def get_provider_auth_policy(config: Mapping[str, Any] | None = None) -> ProviderAuthPolicy:
    scoped = _PROVIDER_AUTH_POLICY.get()
    if scoped is not None:
        return scoped
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    try:
        home = home.resolve()
        if config is None:
            stamps = (_file_stamp(home / "config.yaml"), _file_stamp(home / ".env"))
    except (OSError, RuntimeError) as exc:
        # Resolving/stating paths can fail before the YAML reader is reached.
        raise _policy_config_error(home) from exc
    if config is None:
        # Read the selected configuration to determine mode. Auto preserves
        # native support for dotfile symlinks; strict mode validates its path
        # before making any configured credential available.
        return _cached_policy(home, *stamps)
    return _build_policy(home, config)


def _policy_config_error(home: Path):
    from hermes_cli.auth_constants import AuthError

    return AuthError(
        f"Cannot read provider policy from config.yaml at {home / 'config.yaml'}. "
        "Fix the YAML mapping, file permissions, and symlink targets, then retry; "
        "no inference provider or credentials were selected.",
        code="corrupt_config",
    )


def _file_stamp(path: Path) -> tuple:
    target = path.resolve()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (str(target), None)
    return str(target), stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_mode


@lru_cache(maxsize=128)
def _cached_policy(home: Path, config_stamp: tuple, env_stamp: tuple) -> ProviderAuthPolicy:
    # Keyed by native file identity, including symlink targets and replacement
    # inodes, so rotation/removal invalidates a snapshot without reparsing YAML
    # at every provider lookup. Request scopes keep their existing snapshot.
    import yaml
    from hermes_cli.config import InvalidUserConfigError, read_user_config_raw

    try:
        config = read_user_config_raw(home / "config.yaml", require_mapping=True)
    except (yaml.YAMLError, OSError, UnicodeError, InvalidUserConfigError) as exc:
        # Unknown policy must never become auto, even for an explicit provider.
        # Do not include parser source snippets: YAML can contain credentials.
        raise _policy_config_error(home) from exc
    return _build_policy(home, config)


def _build_policy(home: Path, config: Mapping[str, Any]) -> ProviderAuthPolicy:
    mode = validate_provider_auth(config)
    policy = ProviderAuthPolicy(mode, home, json.dumps(config, sort_keys=True))
    if policy.config_only:
        policy.local_path(home / "config.yaml")
        from agent.secret_scope import load_env_file
        local_env = load_env_file(policy.local_path(home / ".env"))
        policy = ProviderAuthPolicy(mode, home, policy._config_json, tuple(sorted(local_env.items())))
    return policy
