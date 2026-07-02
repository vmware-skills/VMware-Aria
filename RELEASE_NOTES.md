## v1.7.2 (2026-07-02) — anomaly/rightsizing N+1 + complete pagination

### Fixed
- **Per-resource stats N+1.** `list_anomalies` and `list_rightsizing_recommendations`
  issued one `GET /resources/{id}/stats/latest` per VM (up to ~101 sequential
  round-trips). Both now use a single bulk `POST /resources/stats/query`.
- **Silent first-page truncation.** `get_top_consumers` ranked only a single page
  of candidates (so the true top consumer could be missed), and alert/symptom/
  report definition listings applied name filters after one capped page. These now
  paginate the candidate set. Output shape unchanged; the top-N HTTP-414 guard
  (100 ids) is retained with a clear warning to narrow the query.

## v1.7.1 (2026-07-02) — family version alignment

No code changes. Version bump to stay aligned with the v1.7.1 family release
(VMware-AIops + VMware-Monitor large-inventory scale fix — PropertyCollector
batching to stop per-object lazy SOAP round-trips, GitHub issue #31).

## v1.7.0 (2026-06-27) — guided onboarding + teaching auth errors

### Added
- **`vmware-aria init` — interactive first-run setup wizard.** Prompts for host /
  username / password and writes `config.yaml` + `.env` for you. The password is
  stored grep-safe (`b64:`, never plaintext on disk) and `.env` is locked to
  0600, then the connection is verified. Replaces the manual "mkdir + cp
  config.example.yaml + edit YAML + chmod 600" dance.
- `.env.example` added (was missing) documenting the per-target password var.

### Changed
- `doctor` now points to `vmware-aria init` when config/credentials are missing
  (previously suggested a command that did not exist), keeping the manual steps
  as a fallback.
- Authentication and TLS failures now print a teaching message naming the exact
  file and env var to fix (`~/.vmware-aria/.env` password var, `config.yaml`
  username) plus a `verify_ssl: false` hint for self-signed labs.

## v1.6.1 (2026-06-24)

### Added
- **`.env` passwords are auto-obfuscated to a grep-safe `b64:` form** on first
  load and decoded transparently at runtime — plaintext no longer sits in
  `~/.<skill>/.env` for a casual `grep` to find. Values are read/written through
  python-dotenv's own parser, so the stored secret never drifts from the
  configured one (handles quotes, inline comments, trailing whitespace, and a
  password that literally starts with `b64:`). **Obfuscation, not encryption** —
  for real at-rest secrecy, inject the password from a secret manager instead of
  storing `.env`. New regression suite (10 cases) covers dotenv parity, the
  `b64:`-prefixed edge case, idempotency, and 0600 preservation.

## v1.6.0 (2026-06-22) — family alignment + harness trust architecture

No skill code changes. Aligns to the v1.6.0 family release and automatically picks up the
vmware-policy 1.6.0 governance upgrades (token/runaway budget guard, audit accountability fields,
graduated-autonomy risk tiers) on next install.

## v1.5.39 (2026-06-22) — family version alignment

No code changes. Version bump to stay aligned with the v1.5.39 family release
(AIops snapshot-delete async + honest-timeout token-burn fix; Storage datastore-browse timeout fix).

## v1.5.38 (2026-06-12) — backlog finish: pagination bug fix, server split

### Fixed
- **Resource listing was capped at ~500 results.** `list_resources` now follows suite-api pagination
  (page/pageSize loop with totalCount termination), so large environments return all resources, not a
  truncated page. (#7)

### Changed
- Split the oversized MCP server into `mcp_server/tools/*` modules under the 800-line cap
  (behavior-preserving; 27 tools unchanged). (#9)

## v1.5.37 (2026-06-12) — backlog: liveness caching, error-hint completeness

### Fixed
- `is_alive()` is now cached with a short TTL instead of a full HTTP round-trip on every MCP tool call. (#8)
- MCP `_safe_error` passes `ConnectionError` through; the CLI now closes per-command `ConnectionManager`s
  (no token accumulation). (#10)

## v1.5.36 (2026-06-12) — error-translation completeness + parity tests

### Fixed
- **`AriaApiError` now passes through MCP `_safe_error`** — the v1.5.34 teaching hints
  (404 "list the parent collection first", 503 "platform booting") reach agents again.
- **Token acquisition errors are translated** — a wrong password / booting node at connect or
  mid-session refresh now yields a teaching AriaApiError instead of a raw httpx traceback.
- **Non-idempotent POSTs no longer auto-retry on 502/504** (`post()` defaults to `retries=0`);
  only idempotent `/query` reads opt into one retry — prevents duplicate report/alert-def creation.
- CLI catches AriaApiError → one teaching line + exit 1 (no rich traceback).

### Added
- MCP parity regression test (27 tools, 20 read / 7 write) and a spec-conformance AST fix that
  no longer skips the f-string auth endpoints.

## v1.5.35 (2026-06-10) — security hardening: safe errors, tighter audit-file perms

### Fixed
- **MCP tools route errors through `_safe_error()`** — full detail to the server log,
  a sanitized message to the agent (no raw response bodies / host:port leakage).
- **Audit** directory created 0700 and `audit.log` 0600 on creation.

This release aligns the whole family back to a single version (1.5.35); vmware-policy and vmware-pilot return to the shared number after sitting at 1.5.22.

## v1.5.34 (2026-06-09) — teaching errors instead of tracebacks for every API call

Generalizes the v1.5.33 health fix: the 503 crash was one instance of a
systemic problem — every by-id call (`resource get`, `alert get`, `report
get`, capacity/anomaly lookups, alert acknowledge/cancel/delete) raised a raw
`httpx` traceback when given a bad UUID (404) or when the server returned 5xx.

### Fixed
- **All API errors are now actionable**: the connection layer translates every
  non-2xx status and transport failure into an `AriaApiError` carrying the
  status, path, and a remediation hint (e.g. a 404 says "verify the id — list
  the parent collection first"). End users and agents see a clear line instead
  of an httpx stack trace.
- **Lightweight recovery**: transient gateway statuses (502/503/504) and
  transport/timeout errors are retried once before giving up; a 401/403 still
  triggers a single token re-acquisition. 4xx client errors are surfaced
  immediately (not retried). The token re-acquire retry is now covered by the
  same transport-error handling, so a connection drop during re-auth can no
  longer leak a raw error.
- **`is_alive()`**: a node returning 503 while booting is treated as "alive but
  not ready" (the client + token still work), so `connect()` no longer
  needlessly re-authenticates on every call during a cluster restart.
- **`health status`** reads the 503 with no retry back-off (the 503 *is* the
  OFFLINE answer), so it stays fast on an unhealthy platform.

### Tests
- New connection-layer regressions in `test_aria_specific.py`: 404→teaching
  error, 503 retried exactly once, 4xx not retried, transport-error-after-reauth
  wrapped, `is_alive` 503=alive / 401=dead. 82 tests pass; bandit 0 Medium+.

## v1.5.33 (2026-06-09) — health status survives a 503 from an offline node

### Fixed
- **Health**: `vmware-aria health status` no longer aborts with an httpx
  `HTTPStatusError` traceback when `GET /deployment/node/status` returns HTTP
  503. That 503 is the documented "node not ONLINE" signal (the suite-api
  gateway runs on the same node it reports on), so the health check now
  surfaces it as a structured `OFFLINE` result with a remediation hint
  ("platform is starting up … retry") instead of crashing — a health check
  must work precisely when the platform is unhealthy. External user report
  ([#6](https://github.com/zw008/VMware-Aria/issues/6)). Non-503 errors still
  propagate.

### Tests
- `tests/eval/regression/test_aria_spec_shapes.py`: H12 pins the 503→OFFLINE
  shape and asserts non-503 statuses are re-raised.

## v1.5.32 (2026-06-08) — Second-pass spec audit: auth header, Alert model, capacity statKeys

Follow-up to v1.5.31: a full-detail review against official sources found the
response-field layer (untouched by v1.5.31's endpoint fixes) also contained
invented fields, plus an auth-header compatibility bug.

### Fixed
- **Auth**: `Authorization: vRealizeOpsToken <token>` — the previous `OpsToken`
  spelling is only documented in Aria-branded (8.16+) guides and 401s on 8.6.
- **Alerts**: criticality read from `alertLevel`, name from `alertDefinitionName`
  (the previously parsed `alertName`/`criticality`/`resourceName`/`info` fields
  don't exist in the Alert model); triggered symptoms now fetched from
  `GET /alerts/contributingsymptoms`; alert-definition severity derived from
  `states[].severity`; symptom filter param corrected to `resourceKind`.
- **Resources**: `badges[]` array parsing in list_resources/get_resource
  (`badge` singular never existed); Top-N values read from the nested
  `resourceStats[].stat` object; topn candidates capped at 100 (URL length).
- **Capacity**: remaining-capacity percentage exists only at group level
  (`OnlineCapacityAnalytics|capacityRemainingPercentage`); VM rightsizing keys
  have no `demand` segment; anomaly count metric is `System Attributes|total_alarms`.
- **Collector groups**: parse `collectorId` int array + enrich via `GET /collectors`
  (the previously parsed `collectors[]` objects don't exist on CollectorGroup).
- **Reports**: timestamps from `completionTime`; ReportDefinition `subject` is an
  array of strings (old object access raised AttributeError); list limit applied
  client-side (no pageSize param on GET /reports).
- **Robustness**: empty `resourceStatusStates` no longer raises IndexError;
  token release sends no body; create_alert_definition uses doc-verified
  `aggregation: ALL` + `symptomSetOperator: OR`.

### Safety
- MCP `delete_alert_definition` / `delete_report` gained `confirmed=False`
  preview gates (matching acknowledge/cancel).
- Safety test rewritten to assert the real three-layer guard architecture
  (ops audit / MCP confirmed / CLI typer.confirm).

### Tests & docs
- +23 shape regression tests (74 total green); README/SKILL/references synced.

## v1.5.31 (2026-06-08) — API layer rewritten against the official suite-api spec

An external user ran the MCP against a real Aria Operations instance and reported
that roughly half the API calls returned 404. They were right. Every claim was
verified against the official Broadcom/VMware suite-api specification (vROps 8.6
operation index + official sample payloads + VMware's own client code); 12 of
their 14 findings were confirmed, 2 were already spec-correct, and our own audit
found 6 more invented endpoints they hadn't hit yet. Sincere thanks to the reporter.

### Fixed — confirmed user findings (one line per reported bug)
1. `get_resource_metrics`: `statKey` now sent as an array of plain strings (was `[{key: ...}]` objects).
2. `get_resource_metrics`: response parsing now traverses `values[].stat-list.stat[]` (was reading non-existent top-level fields, so results were always empty).
3. `get_resource_metrics`: request field renamed `intervalQuantity` → `intervalQuantifier` per StatQuery model.
4. `get_top_consumers`: now uses real `GET /api/resources/stats/topn` (was POSTing to invented `/resources/query/topn`); resolves resource IDs by kind first since the endpoint has no resourceKind param.
5. `list_alerts`: filtering now goes through `POST /api/alerts/query` (AlertQuery: `activeOnly`, `alertCriticality`, `resource-query`) — `status`/`criticality` were never valid GET params and were silently ignored.
6. `acknowledge_alert`: now `POST /api/alerts?action=takeownership` with `{uuids:[...]}` — the spec has no acknowledge action; takeownership (control state ASSIGNED) is the semantic equivalent and is documented as such.
7. `cancel_alert`: now `POST /api/alerts?action=cancel` with `{uuids:[...]}` (was DELETE `/alerts/{id}`, which doesn't exist).
8. `set_alert_definition_state`: now PUT `/api/alertdefinitions/{id}/enable|disable` (was POST).
10. `generate_report`: correct flat creation body `{id, resourceId, reportDefinitionId, subject:[]}` (was invented `{"reportDefinition":{"id"}}` nesting); a root `resource_id` is now required with a teaching error when missing.
11. `list_reports`: `reportDefinitionId` is not a valid GET param — definition filtering is now client-side.
13. `get_aria_health`: reads the real NodeStatus `status` field, returns ONLINE/OFFLINE + healthy bool (was reporting `clusterVipAddress` — an IP address — as the status; the `services[]` array it also "parsed" doesn't exist).
14. Token handling: `validity` in the acquire response is an epoch-ms expiry timestamp with 6-hour sliding validity — the old code treated it as a duration, scheduling refresh ~56 years out, so sessions idle past 6h died with 401. README's "30 minutes" claim fixed too.

### Not changed — user findings that were already spec-correct
9. `create_alert_definition` keeps the `base-symptom-set` wire key — the Broadcom portal's model page names the property "symptoms", but the live server JSON uses `base-symptom-set` (verified against VMware's own build-tools client and official sample payloads). The invalid `relation: "ANY"` value WAS fixed (→ `SELF`, with `aggregation`/`symptomSetOperator` expressing any-of semantics).
12. `get_report` download URLs keep the `format` param — it is documented (PDF/CSV, default PDF); literals upper-cased to match the doc.

### Fixed — additional invented endpoints found during the audit (would also 404)
- `get_resource_health` / `get_resource_riskbadge`: `/resources/{id}/badge/health|risk` don't exist — badges now read from the `badges[]` array on `GET /resources/{id}`.
- `get_capacity_overview` / `get_remaining_capacity` / `get_time_remaining`: `/resources/{id}/recommendations|remainingcapacity|timeremaining` don't exist — reimplemented on the real `OnlineCapacityAnalytics|*` metrics via `GET /resources/{id}/stats/latest`.
- `list_rightsizing_recommendations`: `/recommendations/rightsizing` doesn't exist (`/api/recommendations` is alert-recommendation text CRUD) — reimplemented on `OnlineCapacityAnalytics|{cpu,mem}|demand|recommendedSize` metrics.
- `list_anomalies`: `/anomalies` and `/resources/{id}/anomalies` don't exist (the UI's anomalous-metrics view is not in the public API) — reimplemented on the `System Attributes|anomaly` metric (per-resource anomaly counts).

### Tests
- New `tests/eval/regression/test_aria_spec_conformance.py`: AST-scans every API call in the codebase and asserts it exists in the official vROps 8.6 operation index (315 operations, stored at `tests/eval/spec/vrops86_operations.json`). Invented endpoints now fail CI instead of 404-ing in production.
- New `tests/eval/regression/test_aria_specific.py`: 13 per-bug regression tests pinning correct request/response shapes with a mocked client.

### Known limitation
- Return shapes of the reimplemented capacity/anomaly/health tools changed (they previously parsed fields that never existed). Capacity analytics metrics need the product's analytics cycle to warm up; values are None until then.

## v1.5.30 (2026-06-07) — Tool description quality (Glama TDQS)

### Improved
- Rewrote MCP tool descriptions flagged by Glama's Tool Description Quality Score review:
  per-parameter semantics (format, defaults, valid values), return-field documentation,
  sibling-tool routing guidance, and behavioral transparency (side effects, audit logging,
  async semantics). Corrected descriptions that overstated or misstated actual behavior.
- No functional changes; descriptions only.

## v1.5.29 (2026-05-29) — Family Version Alignment

No Aria-specific changes since v1.5.28. Bumped for family-wide v1.5.29 alignment.

## v1.5.28 (2026-05-20)

**Fix `subclass() arg 1 must be a class` in goose/old mcp environments** —
v1.5.25–1.5.27 replaced `X | None` with `Optional[X]` but kept
`from __future__ import annotations` at the top of `mcp_server/server.py`.
Under mcp 1.10–1.13 (which Goose and some sandboxes pin), `Tool.from_function`
calls `issubclass(param.annotation, Context)` without resolving forward refs,
so string annotations crash the entire server load. Removed
`from __future__ import annotations` from `mcp_server/server.py` so annotations
are real classes; verified all tools load under mcp 1.10 and 1.14.

Traceback location: `mcp/server/fastmcp/tools/base.py:67`. CLAUDE.md 踩坑 #33
updated. family_smoke.sh Check 4b now installs `mcp==1.10.0` to catch this
regression class.

## v1.5.27 (2026-05-20)

**Loosen Python requirement: now supports Python >= 3.10** — v1.5.25/26 fixed
the PEP 604 root cause in MCP tool signatures (Optional[X] instead of X | None),
but kept `requires-python = ">=3.11"` and a 3.11 hard guard in `mcp_cmd`. Both
relaxed to 3.10 so users on Python 3.10 (e.g. Goose default sandbox, Ubuntu
22.04 system python) can install and run directly without a Python upgrade.

- `pyproject.toml`: `requires-python = ">=3.10"` (was `>=3.11`; VMware-VKS
  was `>=3.12`, now also `>=3.10` for family alignment).
- `<pkg>/cli.py` `mcp_cmd()`: version guard now triggers on `< (3, 10)`.
- Behavior on Python 3.10 matches 3.11/3.12 — the Optional[X] fix from v1.5.25
  is what actually enables this; this release just stops blocking installs.

---

## v1.5.26

**Family-wide MCP server fix — Python 3.10 compatibility (踩坑 #33)** — `vmware-aria mcp`
crashed at decorator time on Python 3.10 with `subclass() arg 1 must be a class`.
Root cause: `mcp_server/server.py` used PEP 604 `X | None` in tool signatures
plus `from __future__ import annotations`; on Python 3.10 + older mcp/pydantic
combos, `typing.get_type_hints()` evaluates `"str | None"` to a
`types.UnionType` instance, which FastMCP/Pydantic then feeds to `issubclass()`.
Reported by a goose user (qwen3.6:27, Python 3.10).

- `mcp_server/server.py`: all `X | None` → `Optional[X]`; ops layer untouched.
- `<pkg>/cli.py` `mcp_cmd()`: hard guard — exits with installation fix command
  if Python < 3.11 (defense in depth, our actual lower bound).
- `pyproject.toml`: `mcp[cli]>=1.10,<2.0` (was `>=1.0`) so uv doesn't pick
  an ancient version that has the same issubclass bug.

**Tooling — family smoke gains MCP schema-build check** — `scripts/family_smoke.sh`
new Check 4b runs `asyncio.run(mcp.list_tools())` per skill, forcing FastMCP to
build Pydantic models for every declared tool. Supports both module-level `mcp`
and `build_server()` factory patterns.

**Docs — CLAUDE.md gains 踩坑 #33 (PEP 604 / Python 3.10) and #34 (CLI/MCP exposure parity).**

---

## v1.5.24 (2026-05-19)

**Family version alignment** — no code changes in this skill. Bumped together
with VMware-AIops and VMware-VKS, which received a pyVmomi 8.x `ManagedObject`
setattr fix (踩坑 #32). `family_smoke.sh` now enforces the no-setattr rule
across all 9 skills.

## v1.5.23 (2026-05-19)

**VCF 9.0 / 9.1 ("VCF Operations") compatibility declared.**

- **docs:** README and `references/capabilities.md` now note that in VCF 9.0+, "VMware Aria Operations" has been rebranded to **VCF Operations**. The suite-api REST endpoints (`/suite-api/api/auth/token/acquire`, `/resources`, `/alerts`, etc.) are unchanged — this skill continues to work against VCF Operations 9.x without code changes.
- **docs:** Added `Official Broadcom References` pointer to [VCF Operations API docs](https://developer.broadcom.com/xapis) and the [VCF Python SDK](https://developer.broadcom.com/sdks).
- **chore:** `.trae/` and `skills-lock.json` added to `.gitignore` (local IDE/tool artifacts).
- **align:** Family v1.5.23 — all 9 skills tracking VCF 9.0 / 9.1 compatibility declaration.

## v1.5.22 (2026-05-08)

**Family alignment** — no source changes in this skill.

- **align:** Tracks v1.5.22 family bump driven by Smithery onboarding for vmware-avi / vmware-harden / vmware-pilot.

## v1.5.21 (2026-05-08)

**Family alignment** — no source changes in this skill.

- **deps:** Bumped `python-multipart` 0.0.26 → 0.0.27 (transitive, fixes GHSA HIGH DoS via unbounded multipart headers).
- **align:** Tracks v1.5.21 family bump driven by vmware-monitor folder_path feature (community PR #11).

## v1.5.20 (2026-05-08)

**Fix:** Added `<!-- mcp-name: io.github.zw008/vmware-aria -->` marker to README.md so MCP Registry ownership validation passes. Without this marker the registry refused publish (HTTP 400, "PyPI package ownership validation failed"), leaving this skill missing from the official registry from v1.3.0 through v1.5.19.

- **registry:** First-time publish of `vmware-aria` to registry.modelcontextprotocol.io.
- **align:** Family bumped 1.5.19 → 1.5.20 in lockstep.

## v1.5.19 (2026-05-06)

**Family alignment** — no source changes in this skill.

- **build:** Bumped `requires-python` from `>=3.10` to `>=3.11` (regression eval uses `tomllib`).
- **smoke:** Family `scripts/family_smoke.sh` adds Check 3b — recursive `--help` on every subcommand to surface broken lazy imports (yjs review 2026-05-06; 踩坑 #27).
- **align:** Tracks v1.5.19 fixes in vmware-nsx (CRITICAL CLI imports), vmware-vks (ApiClient leak), vmware-harden (Twin indexes + LEFT JOIN), vmware-policy (approval gate + singleton lock).

## v1.5.18 (2026-05-02)

**Family alignment + tooling normalization** — no source changes in this skill.

- **dev:** Added `[dependency-groups] dev` block (PEP 735) so `uv sync --group dev` works. Canonical set: `pytest>=8.0,<10.0`, `pytest-cov`, `ruff`.
- **test:** New `tests/eval/regression/test_release_blockers.py` (5 evals) catches the v1.5.x release blockers — missing `mcp_server` in wheel, AST-detected unimported runtime names, Typer app load failure, module import errors. Run via `pytest tests/eval/regression/`.
- **align:** Family version bump to v1.5.18.

## v1.5.17 (2026-05-01)

**Family alignment** — no source changes in this skill.

This release tracks vmware-pilot v1.5.17 (new `investigate_alert` template + `review_workflow` MCP tool + `parallel_group` step type) and vmware-policy v1.5.17 (L5 pattern matcher integrated into `@vmware_tool`). Both work with the existing skill MCP surface unchanged.

- **align:** Family version bump to v1.5.17.

## v1.5.16 (2026-04-30)

**Enterprise Harness Engineering alignment** — adapted from the Linkloud × addxai framework articles ([part 1](https://mp.weixin.qq.com/s/hz4W7ILHJ1yz_pG0Z1xP-A), [part 2](https://mp.weixin.qq.com/s/F3qYbyB3S8oIqx-Y4BrWNQ)).

- **docs:** New `references/investigation-protocol.md` — causal-chain root cause analysis protocol with 4 completeness criteria, shared with monitor/aiops. Aria is the primary L1/L2 metrics data source.
- **docs:** Added Broadcom/VMware brand disclaimer to `references/setup-guide.md` Security Notes (clears Snyk E005 brand-misuse flag on next clawhub Rescan).
- **docs:** "Automation Level Reference" section in `references/capabilities.md` — clarifies that aria is heavily L1/L2 (21 read / 6 write).
- **docs:** Common Workflows enriched with contention-vs-consumption judgment and investigation-protocol cross-reference.
- **align:** Family version bump to v1.5.16.

## v1.5.15 (2026-04-29)

**UX improvements from real user feedback**

- **feat:** New top-level CLI subcommand `vmware-aria mcp` starts the MCP server. Single command after `uv tool install vmware-aria` — no more `uvx --from`, no PyPI re-resolve, no TLS-proxy issues.
- **feat:** Default `verify_ssl: true` on new targets (already True in code). Aria Operations with default self-signed certs requires explicit `verify_ssl: false` in `config.yaml`.
- **docs:** README, SKILL.md, setup-guide.md, and `examples/mcp-configs/*.json` switched to `command: "vmware-aria"`, `args: ["mcp"]`. uvx form moved to fallback with TLS-proxy troubleshooting note.
- **compat:** Legacy `vmware-aria-mcp` console script kept — existing user configs continue to work.

## v1.5.14 (2026-04-21)

- Align with VMware skill family v1.5.14 (code review follow-up fixes by @yjs-2026)

## v1.5.13 (2026-04-21)

- Align with VMware skill family v1.5.13 (code review bug fixes)

## v1.5.12 (2026-04-17)

- Align with VMware skill family v1.5.12 (security & bug fixes from code review by @yjs-2026)

## v1.5.11 (2026-04-17)

- Align with VMware skill family v1.5.11 (AVI 22.x fixes from @timwangbc)

## v1.5.10 (2026-04-16)

- Security: bump python-multipart 0.0.22→0.0.26 (DoS via large multipart preamble/epilogue)
- Align with VMware skill family v1.5.10

## v1.5.8 (2026-04-15)

- Fix: MCP destructive tools `acknowledge_alert` and `cancel_alert` bypassed confirmation gates in MCP mode (CLI used interactive `double_confirm` which cannot run via stdio). Added `confirmed: bool = False` parameter with preview-by-default response; callers must pass `confirmed=True` to actually execute.
- Fix: SSL warning suppression scope — replaced `warnings.filterwarnings()` with class-targeted `urllib3.disable_warnings(InsecureRequestWarning)`.
- Align with VMware skill family v1.5.8

## v1.5.7 (2026-04-15)

- Align with VMware skill family v1.5.7 (Pilot `__from_step_N__` fix + VKS SSL/timeout fix)

## v1.5.6 (2026-04-15)

- Align with VMware skill family v1.5.6 (AVI bugfixes + packaging hotfix)

## v1.5.5 (2026-04-15)

- Align with VMware skill family v1.5.5

## v1.5.4 (2026-04-14)

- Security: bump pytest 9.0.2→9.0.3 (CVE-2025-71176, insecure tmpdir handling)

## v1.5.0 (2026-04-12)

### Anthropic Best Practices Integration

- **[READ]/[WRITE] tool prefixes**: All MCP tool descriptions now start with [READ] or [WRITE] to clearly indicate operation type
- **Read/write split counts**: SKILL.md MCP Tools section header shows exact read vs write tool counts
- **Negative routing**: Description frontmatter includes "Do NOT use when..." clause to prevent misrouting
- **Broadcom author attestation**: README.md, README-CN.md, and pyproject.toml include VMware by Broadcom author identity (wei-wz.zhou@broadcom.com) to resolve Snyk E005 brand warnings

### Aria-specific

- **Documentation fix**: capabilities.md corrected — create_alert_definition is supported (was incorrectly listed as "Not supported, UI required")

## v1.4.9 (2026-04-11)

- Fix: require explicit VMware/vSphere context in skill routing triggers (prevent false triggers on generic "clone", "deploy", "alarms" etc.)
- Fix: clarify vmware-policy compatibility field (Python transitive dep, not a required standalone binary)

## v1.4.8 (2026-04-09)

- Security: bump cryptography 46.0.6→46.0.7 (CVE-2026-39892, buffer overflow)
- Security: bump urllib3 2.3.0→2.6.3 (multiple CVEs) [VMware-VKS]
- Security: bump requests 2.32.5→2.33.0 (medium CVE) [VMware-VKS]

## v1.4.7 (2026-04-08)

- Fix: align openclaw metadata with actual runtime requirements
- Fix: standardize audit log path to ~/.vmware/audit.db across all docs
- Fix: update credential env var docs to correct VMWARE_<TARGET>_PASSWORD convention
- Fix: declare .env config and vmware-policy optional dependency in metadata

# Release Notes

## v1.4.5 — 2026-04-03

- **Security**: bump pygments 2.19.2 → 2.20.0 (fix ReDoS CVE in GUID matching regex)
- **Infrastructure**: add uv.lock for reproducible builds and Dependabot security tracking


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.4.0 — 2026-03-29

### Architecture: Unified Audit & Policy

- **vmware-policy integration**: All MCP tools now wrapped with `@vmware_tool` decorator
- **Unified audit logging**: Operations logged to `~/.vmware/audit.db` (SQLite WAL), replacing per-skill JSON Lines logs
- **Policy enforcement**: `check_allowed()` with rules.yaml, maintenance windows, risk-level gating
- **Sanitize consolidation**: Replaced local `_sanitize()` with shared `vmware_policy.sanitize()`
- **Risk classification**: Each tool tagged with risk_level (low/medium/high) for confirmation gating
- **Agent detection**: Audit logs identify calling agent (Claude/Codex/local)
- **New family members**: vmware-policy (audit/policy infrastructure) + vmware-pilot (workflow orchestration)


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.3.1 (2026-03-27)

### Documentation

- Updated README.md and README-CN.md companion skills table: expanded to full 6-skill family with tool counts and install commands, added vmware-nsx-security entry


## v1.4.6 — 2026-04-06

- fix: remove suspicious content from SKILL.md for ClawHub clean scan

---

## v1.3.0 (2026-03-27)

Initial release of `vmware-aria` — VMware Aria Operations AI monitoring skill.

### Features

**Resource Monitoring (5 tools)**
- `list_resources` — List VMs, hosts, clusters, datastores by resource kind with health badge
- `get_resource` — Full resource details including health, risk, and efficiency badge scores
- `get_resource_metrics` — Time-series metric stats with configurable window and rollup type
- `get_resource_health` — Health badge score (0–100) with color and description
- `get_top_consumers` — Rank VMs or hosts by CPU, memory, disk, or network usage

**Alert Management (5 tools)**
- `list_alerts` — Active or all alerts with criticality and resource scope filtering
- `get_alert` — Full alert details: symptom list, recommendations, timeline
- `acknowledge_alert` — Mark alert as acknowledged (write, audit-logged)
- `cancel_alert` — Dismiss an active alert (write, audit-logged)
- `list_alert_definitions` — Browse alert definition templates

**Capacity Planning (4 tools)**
- `get_capacity_overview` — Cluster capacity recommendations
- `get_remaining_capacity` — Remaining CPU, memory, disk headroom
- `get_time_remaining` — Days until capacity dimensions are exhausted
- `list_rightsizing_recommendations` — Over/under-provisioned VM recommendations

**Anomaly Detection (2 tools)**
- `list_anomalies` — ML-detected metric anomalies, per-resource or global
- `get_resource_riskbadge` — Risk score with contributing causes

**Platform Health (2 tools)**
- `get_aria_health` — All Aria internal service states
- `list_collector_groups` — Remote collector agent status

### Authentication
- OpsToken authentication: `POST /suite-api/api/auth/token/acquire`
- Auto-refresh: token refreshed 60s before 30-minute expiry
- Supports LOCAL, LDAP, and AD authentication sources

### Security
- Passwords loaded from env vars / `.env` file only
- All API text sanitized (control chars stripped, 500-char max)
- Write operations (acknowledge/cancel) audit-logged to `~/.vmware-aria/audit.log`

### CLI
- Full CLI with 5 command groups: `resource`, `alert`, `capacity`, `anomaly`, `health`
- `vmware-aria doctor` — pre-flight diagnostics for config, network, auth, MCP import
- Rich table output for list commands, JSON output for detail commands

### Compatibility
- Python 3.10+
- Aria Operations 8.x (vRealize Operations 8.x)
- Suite API v2 (`/suite-api/api/`)