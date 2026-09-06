"""Active-home credential pool loading and provenance checks."""
from __future__ import annotations

from functools import wraps

from hermes_cli.provider_policy import (
    ProviderPolicyError, get_provider_auth_policy, provider_auth_scope,
)


def pool_operation(method):
    """Keep the owning policy attached to a pool across deferred refresh work."""
    @wraps(method)
    def scoped(self, *args, **kwargs):
        current = get_provider_auth_policy()
        owner = self._auth_policy
        if current.config_only and current.home != owner.home:
            raise ProviderPolicyError("Credential pool belongs to another Hermes home")
        if current.config_only and not owner.config_only:
            raise ProviderPolicyError("Create a new credential pool after enabling config_only")
        policy = owner if owner.config_only else current
        with provider_auth_scope(policy):
            if policy.config_only:
                policy.require_provider(self.provider)
                with self._lock:
                    self._entries[:] = [e for e in self._entries if policy.allows_record(e.to_dict())]
            return method(self, *args, **kwargs)
    return scoped


def load_config_only_pool(provider, policy):
    """Resolve local sources without healing, importing, or pruning foreign rows."""
    from agent import credential_pool as pools

    policy.require_provider(provider)
    rows = pools.read_credential_pool(provider)
    # Config and dotenv sources are references to live configuration, not stored
    # credentials. Removing the local source must make its cached copy unusable.
    entries = [pools.PooledCredential.from_dict(provider, row) for row in rows
               if policy.allows_record(row)
               and row["provenance"]["source"] in {"local_login", "local_key"}]
    with provider_auth_scope(policy):
        pools._seed_from_singletons(provider, entries)
        pools._seed_from_env(provider, entries)
        entry = policy.provider_config(provider)
        key = str(entry.get("api_key") or "").strip()
        key_env = entry.get("key_env") or entry.get("api_key_env")
        if not key and key_env:
            key = policy.env_value(str(key_env)).strip()
        if key and provider != "copilot":
            pools._upsert_entry(entries, provider, "config:provider", {
                "access_token": key,
                "auth_type": pools.AUTH_TYPE_API_KEY,
                "base_url": entry.get("base_url"),
                "provenance": policy.local_provenance("config"),
            })
        entries = [e for e in entries if policy.allows_record(e.to_dict())]
        return pools.CredentialPool(provider, entries)
