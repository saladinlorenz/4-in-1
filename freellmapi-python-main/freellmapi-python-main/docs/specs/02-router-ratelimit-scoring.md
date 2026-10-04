# FreeLLMAPI Routing / Rate-Limiting Subsystem — Python Reimplementation Specification

Scope: everything needed to reimplement the TypeScript services in `server/src/services` + supporting libs without access to the original code. All constants are exact values from source.

## 0. Shared data model (SQLite)

**api_keys**: `id, label, platform, encrypted_key/iv/auth_tag (AES-GCM)`, optional per-key proxy override (`proxy_encrypted/proxy_iv/proxy_auth_tag`, all NULL = none), `status` ∈ {healthy, error, unknown} (unknown treated as usable), `enabled` (0/1), `base_url` (custom relays), `model_scope_json` (NULL = every model of the platform, else JSON array of allowed model_ids).

**models**: `id, platform, model_id, display_name, intelligence_rank (lower = smarter), size_label, monthly_token_budget (label string), rpm_limit/rpd_limit/tpm_limit/tpd_limit (nullable), supports_vision, supports_tools, context_window, observed_speed_rank (1–10, user-overridable), key_id (binds custom model to its endpoint's key), endpoint_scope ('' for catalog platforms, hash-derived for custom so two relays of one model_id are distinct), enabled, available, source ('catalog'|'user')`.

**fallback_config**: global chain `model_db_id, priority, enabled`. **profiles / profile_models**: named chains with `priority`, `enabled`, `auto_include_new_models`; `settings['active_profile_id']`.

**requests** log: `platform, model_id, key_id, age_days derivable, status ('success'|'timeout'|'canceled'|other error markers), input_tokens, output_tokens, latency_ms, ttfb_ms, endpoint_scope` — the raw feed for stats.

**Aux tables**: `rate_limit_usage`, `rate_limit_cooldowns`, `provider_quota_state`, `provider_quota_observations`, `model_overrides`, `catalog_model_tombstones`, `quirks`, `quirk_targets`.

**ChainRow** (scoring input): model_db_id, priority, enabled, platform, model_id, display_name, intelligence_rank, size_label, monthly_token_budget, rpm/rpd/tpm/tpd limits, supports_vision, supports_tools, context_window, key_id, endpoint_scope, `match_tier` (0 = normal candidate; >0 = fallback that may serve only after all lower tiers exhaust — set for group members reached via auto-slug rather than the literal requested id).

**RouteResult**: provider, modelId, modelDbId, apiKey, keyId, keyLabel (operator label, never credential/id), platform, displayName, endpointScope, proxyUrl (decrypted per-key override, '' default), rpdLimit, tpdLimit, `release()` (releases the concurrency lease; call in `finally`, tolerate absence).

Token estimate (routing, TPM checks, handoff accounting): `ceil((text.length + Σ(tool_call.function.name.length + arguments.length)) / 4)` over messages; images detected via `image_url` content blocks.

## 1. Router

### 1.1 Chain resolution
1. If an active profile exists, **the profile's own chain IS the routing chain even if it contains zero entries** (#1021 — no silent fallback to global chain). If that empty chain has no enabled entries → HTTP 400.
2. Otherwise use `fallback_config`.
3. `resolveRoutingChain(requested)`:
   - `'auto'`/undefined/empty → active-profile chain (or throw).
   - `'auto:<suffix>'` → `<suffix>` matched against **global axis aliases** first, then against profile names by `LOWER(name)`; miss → 400. Aliases: smart/smartest/intelligence→smart, fast/fastest/speed→fast, cheap/cheapest/price/budget→cheap, reliable/reliability→reliable, balanced.
   - Strategy name mapping: smart→smartest, fast→fastest, cheap→balanced (reliable/balanced identity).

### 1.2 Stats cache (rebuilt at most every `CACHE_TTL_MS = 60s`)
One grouped SQL over `requests` grouped by `(platform, model_id, key_id, age_days)`, window `WINDOW_MS = 7 days`, exponential decay `weight = 0.5^(ageDays / HALF_LIFE_DAYS)`, `HALF_LIFE_DAYS = 2`. Per bucket: weighted successes, timeouts, errors; timeout detection via status column OR message containing any of `['timeout','stalled','etimedout','aborted']` (lowercased); 'canceled' excluded everywhere. Speed: `tokPerSec = (Σ w·output·1000) / Σ w·latency` over success+timeout rows; `avgTtfb` includes timeout ttfb samples; `speedSamples = wSucc + wTimeouts`. Monthly usage = calendar-month chat input+output. Buckets keyed `"platform:model_id"` or `"custom:model_id@<endpoint_scope>"` (ModelStats) plus per-key KeyStats buckets `"...:key_id"`. Speed-rank writeback job: every 10 min, models with `speedSamples ≥ SPEED_RANK_MIN_SAMPLES = 20` and no user override get `observed_speed_rank = map(speedScore → 1..10)` (BEST=1, WORST=10).

### 1.3 Entry ordering (`orderChain`) — always sort by `match_tier ?? 0` ascending FIRST
- **Priority mode**: dense-rank entries by manual `priority`, then add each entry's rate-limit penalty positions to its rank (so penalties actually bite).
- **Bandit mode**: within equal tier, sort by sampled score descending; manual priority is only the tiebreaker.
- `sampled=false` (fusion panel/dashboard): deterministic expected values instead of Thompson draws.

### 1.4 Per-entry scoring (`scoreChainEntry`)
- Reliability: `sampleBeta(alpha=succ+communitySucc+1, beta=fail+communityFail+1)` when routing; expected `alpha/(alpha+beta)` otherwise. Community prior optional (setting).
- Budget scaling (#456): `budget = parseBudget(monthly_token_budget) × max(1, usableKeyCount(platform))`.
- Headroom = **worse-of** `monthlyHeadroom = 1 − monthlyUsed/budget` and `windowHeadroom = 1 − windowUsed/(rpm·DAY)` (not a product).
- Intelligence: min-max normalization of composites across the current chain; composite = `tierValue·1000 − sqrt(max(1, rank))·31`; tierValue {Frontier:4, Large:3, Medium:2, Small:1}, unknown label → 0; if max ≤ min, everyone scores 1.
- Final = `applyModelWeightOverride(combineScore(weights, axes…))` (see §10 of scoring spec).

### 1.5 Key selection (`selectKeyForModel`) — gate order
Provider registered → keys where enabled ∧ status∈{healthy,unknown} → `model_scope` filter → custom-endpoint pool membership (keyIds of keys whose base_url matches the model's endpoint) → not in `skipKeys` → `!isOnCooldown(platform, model, key)` → `canUseProvider` (account daily req cap) → `canUseProviderMinute` → `canUseKeyConcurrency` → `canMakeRequest` (rpm/rpd) → `canUseTokens` (tpm/tpd, estimated tokens) → `canUseProviderTokens` (account daily token cap) → decrypt key (failure ⇒ status='error', skip) → for custom platforms re-resolve provider with the key's base_url. Concurrency lease acquired **last**, after all gates. Round-robin cursor per modelStatsKey. Key ordering: per-key Thompson bandit (`KEY_SCORE_WEIGHTS = {reliability: 0.75, speed: 0.25}`) when ≥2 keys have any data, else plain round-robin. Strategy `'least-remaining'`: stable-sort roomiest-headroom-first using quota observations, unknown headroom = `UNKNOWN_QUOTA_HEADROOM = 0.5`, disabled for `'::account'` pools. Diagnostics tally reasons: cooldown, provider-daily-cap, provider-minute-cap, key-concurrency, rpm-limit, rpd-limit, tpm-limit, tpd-limit, provider-daily-token-cap, decrypt-error, custom-key-mismatch, no-resolved-provider.

### 1.6 routeRequest main flow
Build candidate chain (resolved strategy/profile/fusion), filter per request: `skipModels`, `skipPlatforms`, requireVision, requireTools, requireStructured (drop platforms known to drop `response_format`), context fit, `tpm_limit ≥ estimatedTokens`. Splice preferred model (sticky/pin/group-stickiness) to front — injected even if absent from chain. Exploration (bandit only, disabled while degraded): chance `EXPLORE_CHANCE = 0.1`, pick uniformly among candidates with `samples < EXPLORE_MIN_SAMPLES = 5` passing the same gates; weight-override 0 excludes. Margin between best and runner-up is a SOFT preference (serve deferred entries later in the same request, not a rejection). Exhaustion → `RouteError(summarizeExhaustion(diagnostics, soonestCooldownExpiry), 429, diag)`.

Context fit: reserve `OUTPUT_RESERVE_CAP = 2000` output tokens (`min(max_tokens ?? 1000, 2000)`); need = estimatedInput×`CONTEXT_WINDOW_SAFETY_FACTOR = 1.25` (factor applies only to the chars/4 estimate portion) + unscaled reserve; strict check (`fitsContextWindowStrict`, no factor games) for trim-guarded platforms `{github}`; emit diagnostic `context N < estimated M x1.25 = K` on failure.

`hasOtherUsableKey`: identical gate sequence minus concurrency, probing with tokens=1 — decides whether a failure benches just the key or penalizes the model. `routableKeyIdsForModel`: structural set only (no transient gates).

### 1.7 Exhaustion summary (exact algorithm)
`formatResetEta(ms)`: null if absent/past; `~Ns` (<90s), `~Nm` (<90min), `~Nh`. Classify each lowercase diagnostic LINE (whole-line matching; model ids contain ':' so never split label/reason): 'no provider registered'→unsupported provider; `/no enabled\+healthy key|no usable key|decrypt-error/`→no usable key configured; '< estimated'→prompt too large; 'no vision support'; 'no tool-calling support'; 'drops response_format'; `/ruled out|already-failed/`→failed earlier this request; `/cooldown|rpm|rpd|tpm|tpd|provider-daily-cap/`→rate-limited or on cooldown; else unavailable. Output: `All models exhausted: {total} routes checked ({counts in fixed actionable-first order}). Add more API keys or wait for rate limits to reset.{ ' Soonest reset ' + eta}` ; empty-diag variant omits the counts clause.

## 2. Scoring formulas (exact)

Presets `{reliability, speed, intelligence}`: balanced {.5,.25,.25} (default), smartest {.35,.1,.55}, fastest {.35,.55,.1}, reliable {.7,.15,.15}. Custom weights validated, sum renormalized in `combineScore`.

**Peak hours** (off by default; settings routing_peak_hours_adjust/start_hour/end_hour/timezone): defaults 18→6 spanning midnight, TZ UTC; hour computed via Intl h23 in the zone; `start == end` ⇒ empty window (never active). During peaks: weight shift toward reliability with `PEAK_SPEED_TO_RELIABILITY = 0.6` for affected strategies; exempt: fastest, reliable.

**Reliability**: Beta prior `PRIOR_SUCCESS = PRIOR_FAILURE = 1` (Beta(1,1)); sampling via Marsaglia-Tsang gamma method.

**Speed**: `throughput = 1 − exp(−(tokPerSec / 60))` (`SPEED_SCALE_TOK_S = 60`); `ttfb = clamp01((TTFB_WORST − ttfb)/(TTFB_WORST − TTFB_BEST))` with BEST=300ms, WORST=5000ms; blend `0.6·throughput + 0.4·ttfb`; no data ⇒ prior 0.6; partial data uses whichever axis exists. Timeout rows count toward speed with zero output tokens and latency capped at `TIMEOUT_LATENCY_CAP_MS = 120000` (used both as latency and ttfb sample).

**Guardrails**: headroom ramp linear from `HEADROOM_RAMP_START = 0.2` down to `HEADROOM_FLOOR = 0.1` at zero headroom (invalid operator values fall back to defaults, not clamped); `rateLimitFactor = 1 − (penalty/10)·0.6` (`MAX_PENALTY = 10`, keeps 40% weight at max penalty).

**Intelligence composite**: tierValue {Frontier:4, Large:3, Medium:2, Small:1} × 1000 − sqrt(max(1, rank)) × 31.

## 3. Rate limiting

**Tables**: `rate_limit_usage(platform, model_id, key_id, kind('request'|'tokens'), tokens, created_at_ms)` + index (platform, model_id, key_id, kind, created_at_ms); pruned older than 24h every 60s. `rate_limit_cooldowns(platform, model_id, key_id, expires_at_ms, source('heuristic'|'authoritative'|'credit'|'tier'), set_at_ms)`, PK (platform, model_id, key_id), upsert.

**Counting**: DB COUNT/SUM is authoritative; in-memory windows are a degraded-mode mirror written ONLY when the DB write fails. Per-model/key windows are SLIDING minute/day. Gates: requests rejected when `count + inFlightLeases >= limit`; tokens rejected when `used + inFlight + estimated > limit`.

**Concurrency leases**: opt-in per platform via env `MAX_CONCURRENT_REQUESTS_PER_KEY[_<PLATFORM>]` (unset = unlimited); leases expire after `LEASE_MAX_AGE_MS = 2 min` backstop; provisional lease counting closes the check-then-act race.

**Account-level provider caps**: defaults {openrouter:1000 req/d, modelscope:1800 req/d, nvidia:40 req/min, navy:150000 tok/d}; env overrides `PROVIDER_DAILY_REQUEST_CAP_<P>` / `PROVIDER_MINUTE_REQUEST_CAP_<P>` / `PROVIDER_DAILY_TOKEN_CAP_<P>`; 0 disables. Daily counters measured from **UTC midnight**, unlike sliding per-model windows. Navy billed-token multiplier: parse "Nx" from monthly_token_budget label or fall back to `dailyTokenCap/tpd_limit` ratio; bill `ceil(raw × mult)` only when the platform has a daily token cap.

**Window snapshot**: grouped usage scan + api_keys read, cached 5s; `keyWindowPressure` = worst of the four ratios (rpm, rpd, tpm, tpd); `modelWindowUsedFraction` follows the eligible key with MOST headroom; excludes leases.

**Cooldown decision** (`getCooldownDecisionForLimit(err, model, key)`), in order:
1. Local-endpoint key (base_url loopback/RFC1918, locality cached per keyId) → 5s heuristic regardless of Retry-After.
2. rpd/tpd genuinely exhausted (persisted count ≥ cap) → `retryAfterMs ?? msUntilNextUtcMidnight()` (floored 60s), source 'authoritative'.
3. Unknown limits + rate-limit signal → escalation ladder `[2min, 10min, 1h, 24h]` stepped per hit (in-memory rolling-24h hit counts per (model,key), cleared on success), guessed path capped at `UNKNOWN_LIMIT_MAX_COOLDOWN_MS = 10min`.
4. NULL limits + signal: count hits; below `NULL_LIMIT_HIT_THRESHOLD = 2` per 1h → transient 90s (`TRANSIENT_COOLDOWN_MS`); at/above → ladder.
5. Retry-After present (and none of the above stronger) → floor honored, capped at DAY, source 'authoritative'.
Defaults: 402 → DAY/'credit'; 403 tier-forbidden → DAY/'tier'.

Lifecycle: `setCooldown` persists (default 60s/'heuristic') + memory; `isOnCooldown` persisted-first with lazy expiry delete; `cleanupExpiredCooldowns` at boot; `clearCooldownEarly` (probe success) does NOT reset escalation history; operator `clearCooldownsForKey` does; `getProbeableCooldowns` = DB-only source='heuristic' ordered by soonest expiry; `getSoonestCooldownExpiry` = MIN(expires_at_ms) live.

**Ceiling learning**: `parseProviderLimit(message)`: require a numeric `/\blimit[:\s]+([\d,]+)\b/i` AND an axis keyword, checked in fixed order **tpd → tpm → rpd → rpm**; refuse to guess which axis otherwise. `learnLimitFromError`: `UPDATE models SET <col> = ? WHERE id = ? AND (<col> IS NULL OR <col> > ?)` — only fills NULLs or lowers existing ceilings, never raises.

## 4. Shared fallback/retry loop (`lib/fallback-loop.ts`)

Shared by OpenAI-chat, /responses, and Anthropic surfaces. Request-scoped `FallbackState`: `skipKeys` ("platform:modelId:keyId"), `skipModels` (db ids), `skipPlatforms`. Constants: `FALLBACK_MAX_RETRIES = 20`; time budget setting `fallback_time_budget_ms` → env `FALLBACK_TIME_BUDGET_MS` → default 45 000 ms (0 disables; only stops attempts ≥2; hedge timer aborts a stalled attempt >1, disarmed at first streamed byte). Circuit breaker: consecutive-failure limit from setting/env `max_consecutive_upstream_fails` (default off); tripped ⇒ immediate 503 `upstream_unhealthy`. Dispatch contract: handler returns 'done'|'committed'; any other value ⇒ immediate fatal 502 (never retried).

Per failing attempt (`recordRetryableFailure`): classify error → 404 / 403-model-tier / context-too-large / caller-requested skips add to `skipModels`; retirement signals recorded per endpoint_scope; key failures add `skipKeys`; provider-level failures add `skipPlatforms`; empty completions bench only after `EMPTY_COMPLETION_STREAK_LIMIT = 3` consecutive; persist cooldown via decision fn; model-level bench: ≥3 failures within 15 min across ALL keys ⇒ 10-min 'heuristic' cooldown on every routable key of the model, never shortening an existing longer bench, counter reset afterwards; model penalty (`recordRateLimitHit` +3 vs `recordModelFailure` +1) applied only when `hasOtherUsableKey` is false; learn ceilings from the message.

401 auth failures (`recordAuthFailure`): key-fatal — 5-min bench (`AUTH_FAILURE_COOLDOWN_MS`) plus fire-and-forget health revalidation (30s dedupe); NO model penalty, NO ceiling learning. Success (`recordUpstreamSuccess`): recordRequest + recordTokens + recordSuccess (penalty decay/clear) + clear empty-completion streak + mark key healthy.

Exhaustion taxonomy: all-auth → 502 `provider_authentication_failed`; all-context → 413; all model-not-found → 404; degraded-mode 400s → 503; provider bad-request → 400 passthrough; breaker → 503; every remaining failure class in `UNAVAILABLE_UNTIL_KNOWN_TIME` {rate_limited, daily_quota_exhausted, out_of_credits, forbidden} → 429 with body.retryAtMs = `getSoonestCooldownExpiry()` and `Retry-After: ceil(s)` header; mixed → 502 `upstream_failed`. Client aborts and hedge-abort kills leave NO bench. Observability: `X-Fallback-Attempts` count, `X-Fallback-Trail` (last `TRAIL_MAX_SHOWN = 10` entries, `DETAIL_SUMMARY_MAX_LENGTH = 120` chars each), opt-in `X-Fallback-Detail` (setting `expose_fallback_detail_header` / env `FALLBACK_DETAIL_HEADER`, total cap 2048 chars); per-attempt traces persisted to `request_attempts`.

## 5. Model groups (Unify)

Unify is always on (`isUnifyEnabled()` hard-wired true; setting kept only for compat). Overrides JSON: `merges[{into, keys[≥1]}]`, `splits[{member, groupKey?}]`.

Normalization: strip ONE trailing "(…)" parenthetical then a trailing standalone word "free"; group key = stripped, lowercased, whitespace/hyphen/underscore collapsed; **"+" is preserved** (Command R ≠ Command R+). Slug keeps digits/dots. Token precedence: explicit split (groupKey normalized, else `__split__:<member>` singleton) > merge redirect > normalized name. Representative label = member with lowest intelligence_rank, then shortest stripped name, then lowest db id; canonicalId = slugged label, collision-suffixed "-2","-3".

Resolution (`resolveRequestedIdToTieredMembers`): (1) qualified `custom:model#endpointRef` → that literal relay copy; (2) exact `platform:model_id` → literal; (3) bare model_id or canonical slug → ordered UNION of literal matches before slug matches, dedup by db id. Slug-tier members become `demotedMemberIds`; `resolveModelGroupCandidates` gives them `match_tier = 1` so they serve only after literal matches exhaust. Dispatch-time reverse lookup maps a chosen member back to the id the client asked for.

## 6. Cooldown probe

Pass every 60s over `getProbeableCooldowns`. A bench is "ripe" when ≥50% elapsed (`MIN_ELAPSED_FRACTION = 0.5`) AND >60s remain. First sighting schedules a probe at now+jitter(45s) instead of probing immediately (restart stagger). Per-pass cap 3 (`COOLDOWN_PROBE_MAX_PER_PASS`); kill switch env `COOLDOWN_PROBE_DISABLED=1`. Success (any cheap completion) → `clearCooldownEarly` for ALL heuristic cooldowns of that key + provider-log event; failure/inconclusive NEVER extends the bench — backs off 2min, doubling to 15min max. Pass overlap-guarded (single-flight flag); pacing state pruned when a key loses all probeable cooldowns.

## 7. Context handoff

Env mode `FREELLMAPI_CONTEXT_HANDOFF` ('on_model_switch' | off). In-memory store keyed by session: last 12 user/assistant messages, each content trimmed to 500 chars (+ellipsis), whole-record cap 6000 chars, TTL 3h, store cap 500 (TTL prune, then oldest-evict); records `lastModelKey`. Recording happens BEFORE reasoning-restoration mutations; no assistant turns yet ⇒ clear lastModelKey. Injection (before dispatch): only when a previous model exists, differs from the newly selected one, and no message already starts with the sentinel prefix `'FreeLLMAPI context handoff:'`; a system message carrying the takeover instructions + `Recent session summary:` block is inserted after any leading system messages; injected cost accounted as `ceil(len/4)` tokens against headroom.

## 8. Degradation

Health snapshot: distinct platforms among enabled keys; platform healthy iff ≥1 key with status healthy|unknown; ratio = healthy/total, 1.0 when no providers. Env-tunable: `DEGRADED_HEALTHY_RATIO = 0.5`, `DEGRADED_MIN_PROVIDERS = 3`, `DEGRADED_ENTRY_GRACE_MS = 60 000`, `DEGRADED_EXIT_GRACE_MS = 120 000`. State machine: below-ratio continuously for entry grace → degraded; recovered continuously for exit grace → normal; fewer than min providers forces normal; transitions reset the opposite streak. While degraded, router exploration is disabled.

## 9. Quirks

Tables: `quirks(slug UNIQUE, title, body, severity, created_at_ms, updated_at_ms)`; `quirk_targets(quirk_id FK CASCADE, platform NULLABLE, model_glob NULLABLE)`. Severity order blocker(0) < warning(1) < info(2). Match: `(target.platform IS NULL OR target.platform = model.platform) AND (target.model_glob IS NULL OR model.model_id GLOB target.model_glob)`.

Seed set (11): keyless-anonymous (info; kilo,llm7,ovh), ovh-anon-trickle (warning; ovh), pollinations-degraded (warning; pollinations), or-free-cap-account-wide (info; openrouter `*:free`), zen-promo-roster (warning; opencode), cloudflare-key-format (info; cloudflare), nvidia-rate-limited (info; nvidia), nim-gemma-hung (blocker; nvidia `*gemma*`), or-ultra-hangs (warning; openrouter `*nemotron-3-ultra*`), zen-serves-ultra-fast (info; opencode `*nemotron-3-ultra*`), zhipu-shared-key (info; zhipu `*glm-4.6v*`). Migration seeds upsert-by-slug and resets targets for curated slugs only. Catalog sync REPLACES the quirk set wholesale.

## 10. Weight overrides + model state

**Routing multipliers**: env `MODEL_ROUTING_OVERRIDES` = JSON object model_id → multiplier; valid range [0.0, 2.0], finite; any malformed entry discards the ENTIRE map; parsed once, cached. Applied AFTER `combineScore`. Matches bare `model_id`, exact case-sensitive. `0.0` = unpickable by the bandit but still servable via priority chains.

**Operator field overrides**: `model_overrides(platform, model_id, overrides_json)`, merged upserts; known patch keys mapped to snake_case columns and applied via `UPDATE models … WHERE platform=? AND model_id=?`; `modelsWithOverriddenField(field)` → set of "platform:model_id" pinning that specific field (protects it from catalog sync).

**Tombstones** (`catalog_model_tombstones(kind chat|media, platform, model_id, source 'user'|'upstream_eol', reason)`): user deletions delete the row (and its overrides) on next sync; upstream-EOL tombstones only DISABLE chain memberships and are lifted if a later catalog lists the model again.

## 11. Sticky sessions, reasoning replay, listing, quotas

**Sticky sessions** (routes/proxy.ts): key = `x-session-id` header (`hdr:<id>`, suffixed `::<strategyKey>` for group pins) else sha1 of first user message text (same suffix rule). `getStickyModel` requires an assistant turn already in history; TTL 30 min with lazy delete; `setStickyModel` sweeps expired entries past 500 and hard-evicts oldest past 1000.

**Reasoning replay**: per-session memory (30-min TTL, 500-entry sweep/1000 hard cap) of the last assistant `reasoning_content`, tagged with producing `platform:model`; replayed only to the SAME platform:model; restoration builds a NEW message array.

**Model listing**: Unify ON → one entry per group (availability/enabled/tools = ANY member, context = MAX, intel = MIN rank, platforms distinct, ownedBy 'freellmapi'); OFF → `ROW_NUMBER() OVER (PARTITION BY model_id ORDER BY available DESC, intelligence_rank ASC, id ASC)` pick. Global sort: available desc, enabled desc, intel asc, name. `autoContextWindow` = max context among available entries.

**Profile backfill** (profile-models.ts): only profiles with `auto_include_new_models = 1`; a model must exist in fallback_config to be appended; appended at `max(priority)+1` inheriting the fallback_config enabled flag.

**Quota observations** (provider-quota.ts): pool keys — openrouter splits `::free` (":free" suffix) vs `::account`; ~30 fixed platform pools (google::project, groq::account, cerebras::shared, …); default `${platform}::${modelId}`, or `${platform}::account` for keyless/shared platforms. Confidence: header/quota_api 1.0, error_body 0.75, probe 0.6, local_usage 0.45, documentation 0.35; source priority header/quota_api(5) > error_body(4) > probe(3) > local_usage(2) > documentation(1). Header specs: groq/openrouter x-ratelimit-requests/-tokens; cerebras requests-day (provider_reported) + tokens-minute (token_bucket). `parseResetAtFromHeader`: >1e12 = epoch-ms, >1e9 = epoch-s, else relative seconds. Upsert merges with COALESCE keeping MAX confidence; expired-reset fix-up restores remaining=limit. `getKeyQuotaHeadroom`: requires confidence ≥ 0.7, expired → 1, ratio = min(1, remaining/limit), WORST metric wins per key, 5-s TTL cache.

**Retryable-error classification** (lib/error-classify.ts): timeout markers `['timeout','stalled','etimedout','aborted']` (case-insensitive substrings); retryable statuses 408/409/425/429 + 5xx; network codes ECONNRESET/ECONNREFUSED/ENOTFOUND/ETIMEDOUT/EAI_AGAIN; 400/401/402/403/404/413 are terminal classes.

**Budget parsing** (lib/budget.ts): monthly_token_budget labels ("50k", "1.5M", "unlimited"/blank → null) → numeric tokens/month used for monthly headroom.
