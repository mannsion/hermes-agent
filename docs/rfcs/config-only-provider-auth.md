# Config-only provider authentication

Status: planned; no runtime implementation in this branch.
Repository: mannsion/hermes-agent (personal fork).
Branch: codex/config-only-provider-mode.
Audited base: 7166071fcaadb36df26f6d753dda97da6b5d699e.
Publication: plan remains local. Any later PR must target mannsion/hermes-agent,
never NousResearch/hermes-agent, unless the user explicitly changes that scope.

## Problem and required behavior

A user explicitly configures Hermes to use a proxy, but auxiliary recovery can
still discover another provider from inherited credentials or another program's
login. Clearing config.yaml does not remove separate credential stores or stop
that discovery. The user needs a native setting that makes the active Hermes
home the complete authority for AI providers and authentication.

The motivating consumer is my-hermes: a helper repository managing an unwrapped,
native Hermes install alongside a portable Docker Compose proxy. This feature
belongs in Hermes' provider resolution, without special-casing Headroom or the
helper repository and without changing the native Hermes commands.

## Configuration contract

Proposed config.yaml setting:

```yaml
provider_auth:
  mode: config_only
```

`auto` is the default and retains existing behavior. `config_only` requires all
AI provider routes and credentials to originate in the active Hermes home.
Unknown mode values are configuration errors; they must not silently become
`auto`. No environment variable may relax this policy.

"Hermes configuration" includes config.yaml, the active home's .env, and its
own native authentication/credential files. It does not mean YAML alone.
HERMES_HOME / native profile selection still chooses that home. Once selected,
a different profile's or the global root's credentials are not fallback sources.

| Source or operation | config_only behavior |
| --- | --- |
| Explicit provider definition in active config.yaml | Allowed |
| Literal credential in that definition | Allowed |
| Active-home .env | Allowed when read directly, without ambient interpolation |
| Explicitly configured provider's active-home auth.json or native OAuth file | Allowed; normal refresh stays in that provider and store |
| Credential pools | Only local entries with allowed, recorded provenance |
| Main, auxiliary, review, and delegated model selection | Allowed within explicitly configured providers |
| Configured fallback list | Allowed only when every candidate is explicitly configured and resolves allowed credentials |
| Built-in discovery chain | Disabled |
| Parent process environment provider keys, URLs, or model-route overrides | Ignored for provider resolution |
| Checkout .env, managed .env, .op.env bootstrap, root-profile/shared auth fallback | Not provider credential sources |
| gh auth token, Claude/Codex/Qwen stores, OS keychain | Never consulted for provider credentials, including recovery |
| key_cmd, Bitwarden, 1Password, bulk secret commands, plugin external secret loaders | Cannot supply provider credentials in this mode |
| AWS/Google/Azure SDK ambient credential chains | Never invoked; allow only adapters supporting explicit local credentials |
| External-process model providers | Reject unless their adapter can enforce this same contract; no silent downgrade |
| Missing/expired/unusable permitted credentials | Actionable error; no discovery of another source |

A local .env reference such as key_env may only resolve a literal value in that
home's .env. It must not fall through to os.environ, external vault hydration,
.env interpolation of parent values, or credential pools seeded from those
sources. A symlinked Hermes home is normalized once; credential paths beneath it
must resolve inside the selected home. Out-of-home credential references fail.

An auth.json entry by itself must not make a provider eligible: the provider must
also be selected or explicitly declared in config.yaml. Configuration of a
keyless local endpoint is valid; it must be an explicit endpoint, not one found
through automatic discovery.

## Verified source behavior

The implementation, rather than nearby stale comments, is authoritative:

- agent/auxiliary_client.py::_get_provider_chain has four branches: OpenRouter,
  Nous, local/custom, and the API-key provider registry. Codex is absent.
- _resolve_auto_route and _ladder_provider_fallback can enter discovery after
  main/task fallback failures. Explicit provider selection alone is insufficient.
- hermes_cli/copilot_auth.py::resolve_copilot_token can invoke gh auth token.
- hermes_cli/auth.py reads local and profile-global auth.json, with provider
  state and credential pools as distinct sections.
- hermes_cli/env_loader.py populates process secrets from multiple .env files
  and enabled external secret sources.
- hermes_cli/runtime_provider.py's disabled-provider check is not shared by all
  auxiliary provider readers. Marking providers disabled is not this policy.
- Codex external-token import has specific login/recovery gates; it is not an
  unconditional branch of the auxiliary discovery chain.

Additional adapter paths to cover include agent/anthropic_credentials.py,
hermes_cli/auth_codex.py, hermes_cli/auth_qwen.py, hermes_cli/auth_nous.py,
agent/bedrock_adapter.py, agent/vertex_adapter.py, and
agent/azure_identity_adapter.py. This list guides the audit; tests must exercise
behavior rather than match source text or freeze provider enumeration counts.

## Architecture

Extend the existing profile secret-scope and provider-resolution boundaries.
One immutable, typed provider-auth policy is resolved for the active home and
passed through native resolution. Keep its decision logic pure and narrow;
source adapters perform the reads only after the policy permits that source.
Reuse agent/secret_scope.py's scoped context instead of adding another global
registry or mutating os.environ to emulate isolation.

Credential resolution must retain source provenance: inline config, local env,
local native auth, inherited env, shared store, external process, or SDK chain.
A cached/pool token is not trusted just because its latest copy is on disk.
Reject externally seeded or unknown-provenance legacy entries in config_only;
provide a local login/key-save action to establish permitted provenance. Preserve
legacy pool behavior in auto mode. Apply the same provenance rule to singleton
auth.json tokens and native OAuth files: external tokens copied into a local file
must not become trusted merely by changing location. An explicit native login or
key-save in the active home establishes local provenance. Unknown legacy records
require that explicit action before use in config_only; auto mode is unchanged.

Use the same resolver contract for native chat and auxiliary clients. Remove
auxiliary bypasses rather than sprinkling mode checks through each fallback
rung. Provider-specific adapters remain responsible for protocol and refresh;
they do not decide whether unrelated credential sources are allowed.

Resolution must be policy-scoped before imports or constructors that can fetch
credentials, run commands, contact metadata services, or hydrate vault secrets.
Provider credential lookup must not consume the general process environment
simply because some other feature populated it earlier.

Model inventory, health/probe operations, token refresh, retries, credential
rotation, and fallback all use the same policy. Cache identities include the
active home and auth-policy mode; cache invalidation on changes must not expose
models or credentials from another profile or the previous auto mode. Never log
credential values; diagnostics can report the blocked source category.

Profiles multiplexed in a single gateway process must carry independent policy
contexts. Delegation, background work, cron, and the desktop backend inherit the
originating profile's policy. Mode changes cannot silently downgrade an in-flight
strict request. Reuse existing profile/config invalidation mechanisms and test
concurrent auto and config_only sessions.

## Delivery plan

1. **Policy and configuration:** add the validated setting through native config
   defaults/schema and the existing scoped resolution infrastructure. Define
   allowed provenance, home boundaries, errors, and cache identity.
2. **Credential boundary:** route main and auxiliary lookups, pool seeding,
   refresh/recovery, secret hydration, and model inventory through the policy.
   Audit built-in and plugin model-provider adapters; unsupported external
   adapters fail explicitly in config_only.
3. **Routing consistency:** apply the same contract to retries, all fallback
   stages, vision/compression, review, delegation, background tasks and gateway
   profiles. Ensure model switching continues to use the selected provider's
   catalog and request model unchanged.
4. **Native UX and documentation:** expose the setting through Hermes' existing
   configuration surfaces; show active mode and useful blocked-source errors.
   Preserve CLI names, argument meanings, prompt construction and conversation
   cache behavior.
5. **Helper integration after implementation passes:** my-hermes connect --clean
   backs up and clears the project's provider/auth state, configures the proxy,
   and enables config_only. Verify native support before deleting anything;
   an older Hermes build must produce a clear error instead of a false claim of
   isolation. This integration remains in the parent helper repository.

## Acceptance tests

Use scripts/run_tests.sh and real native imports with temporary HERMES_HOME and
mocked HOME. Use fake credentials and local HTTP fixtures; no live provider calls.
Patch only external boundaries to record or forbid access, not the resolver
whose behavior is under test.

- The proxy succeeds when deliberately valid-looking keys for other providers
  are exported. Main, compression, vision and delegation reach only the fixture
  proxy and retain selected model IDs, streaming, and Responses mode.
- On connection failures, quota/rate errors, invalid credentials, expired tokens,
  failed refresh, and exhausted pool entries, strict requests fail locally or
  use an explicitly configured permitted fallback. No gh subprocess, external
  auth-file/keychain read, vault helper, metadata lookup, or unlisted-provider
  request occurs as part of provider credential resolution. Independently
  configured tool secret loading can still run, but its results cannot become
  inference credentials.
- A configured local key or locally stored login works; an identically named
  ambient key cannot override it. Empty local config cannot discover providers.
  Local dotenv interpolation cannot import an ambient credential.
- Inventory/GUI model discovery lists only configured providers and uses their
  model catalogs. A warm auto-mode cache cannot reintroduce outside providers.
- A named profile cannot inherit global/shared credentials. Concurrent gateway
  profiles with different modes cannot contaminate one another's caches or
  authentication context.
- Native refresh and multiple-account rotation work for permitted local entries;
  foreign-seeded/unknown-provenance pool entries are rejected in strict mode.
- Explicitly configured permitted fallback providers still work. An explicitly
  requested unconfigured provider cannot bypass the mode through CLI arguments.
- Auto mode retains existing behavior, including its supported ambient discovery.
  Existing profile, auth, auxiliary, gateway and model-selector regressions pass.

## Scope boundary

This is a provider-configuration boundary. It is not an operating-system sandbox
for arbitrary terminal commands, MCP servers, malicious plugins, or tool network
traffic. It neither deletes external credentials nor changes GitHub CLI login.
Non-provider environment settings remain available. First-party AI calls and
model-provider adapters must obey the contract; ordinary tool authentication is
separate. No dependency on the Headroom/Compose implementation is introduced.

## Completion criteria

Do not label the feature implemented until strict-mode success and error paths
pass end to end and the default-mode regression suite passes. This branch
currently contains the plan only. No application restart, deployment, push, or
pull request is part of the planning task.
