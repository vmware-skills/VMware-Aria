## v1.8.11 — two wrong numbers: the server's own version, and the advertised tool count

Both defects were invisible to the test suites and both were user-facing.

- **The MCP server reported the SDK's version as its own.** `FastMCP` accepts no
  `version` argument and leaves the lowlevel server's at `None`; with it `None`
  the SDK answers `initialize` with its OWN version. Every skill in the family
  therefore told its client it was mcp 1.29.1 — a number that exists for no
  package here, and one that would change with an SDK bump and no code change of
  ours. Verified end to end rather than by reading: unset the field and a probe
  server reports the installed SDK's version; set it and it reports ours.
- **server.json advertised a stale tool count.** That number is what MCP Registry
  publishes and what the plugin manifest and marketplace copy, so one stale
  integer was wrong in three public places. Corrected against the registered
  tools: 18 advertised, 33 real. README and SKILL.md were already right.

Also new: this repo is installable as a Claude Code plugin
(`/plugin install vmware-aria@vmware-skills`). The skill and its MCP server arrive in
one step; nothing is duplicated, the manifest points at the existing `skills/`
tree. family_smoke gained three gates — the server's reported version, the plugin
manifest's agreement with pyproject, and the advertised tool count against the
live registration.

## v1.8.10 (2026-08-06) — VCF Operations 9.1 fleet, diagnostics, and real-time PromQL (5 read tools, path-verified but not yet run on a live 9.1 appliance)

Five read-only tools for VCF Operations 9.1 (the VCF 9 rebrand of Aria Operations),
taking the skill from **28 → 33 MCP tools (26 read + 7 write)**. All five are
read-only — no rotation, no writes:

| Tool | CLI | What it reads |
|---|---|---|
| `fleet_certificate_list` | `vmware-aria fleet certificates` | certificate status/expiry across the VCF fleet |
| `fleet_password_account_list` | `vmware-aria fleet passwords` | managed password-account status (never rotates/sets) |
| `fleet_domain_list` | `vmware-aria fleet domains <integration-id>` | SDDC/workload domains behind one registered VCF integration |
| `findings_list` | `vmware-aria fleet findings` | operational diagnostic findings (NOT compliance — use vmware-harden) |
| `promql_query` | `vmware-aria fleet promql <expr>` | real-time (~2s) Prometheus instant query via the VODAP service |

### Honest status: paths VERIFIED, live 9.1 appliance NOT yet exercised

This is a beta surface. The endpoint **paths** are verified against the VCF
Operations 9.1 OpenAPI (`vcf-operations-openapi.json` / `realtime-metrics-openapi.json`,
9.1.0.0) and seeded into `tests/eval/spec/vcf91_fleet_operations.json`, where the
whole-tree phantom-endpoint scan (踩坑 #36) now enforces that every HTTP call the new
ops modules make resolves to a listed path. What that guard does **not** cover, and
what has **not** been confirmed on a live 9.1 appliance:

- **Response field names** are read defensively (`.get` / degrade-to-empty), so a
  field absent or renamed on a real appliance yields an empty value rather than a
  crash — but that also means the exact wire schema is unverified. Every summarizer
  tries several plausible container/field names.
- **`promql_query` reaches a sibling service whose base path `/data-query-service`
  is INFERRED** from the Swagger UI location, not a wire capture. It is marked
  `INFERRED` in the spec, and every PromQL result envelope carries
  `base_path_confirmed: False` to surface the caveat to the caller.
- PromQL is a **2-hop token exchange** — locate the `VCF_VODAP` integration service,
  then exchange the suite-api OpsToken for a service-scoped Bearer JWT — and requires
  the real-time metrics (VODAP) integration to be registered; if it is not, the tool
  returns an actionable teaching error, not historical data.

### Fable5-review fixes applied this release

- **Empty result is no longer read as a confident "none" (踩坑 形态 #1).** Because the
  9.1 response schemas are not pinned, a drifted shape returning no rows would let an
  agent report "no expiring certificates" or "no findings" — a dangerous false
  all-clear. Each fleet/findings tool now distinguishes a *recognised* empty container
  (genuine "none", no note) from an *unrecognised* non-empty shape it could not parse,
  and attaches an `note` marking the empty result as **unconfirmed** in the latter case.
- **PromQL `/integrations/services` in an unrecognised shape no longer claims "VODAP not
  registered — enable it"** — it may in fact be registered; the error now says the shape
  could not be determined and asks the operator to verify, instead of a misleading
  "enable it" instruction.
- **LOW-1**: a `VCF_VODAP` service registered but exposing no `serviceKeys` now raises an
  authored teaching error about integration health, instead of posting
  `{"serviceKeys": null}` and surfacing a generic HTTP 400 that teaches nothing.
- **LOW-2**: a 401/403 from the data-query service is re-worded to point at the exchanged
  VODAP Bearer token / integration health — **not** the suite-api
  `VMWARE_ARIA_<TARGET>_PASSWORD` env var, which the generic connection-layer 401 hint
  would wrongly name for this path.
- **Phantom-endpoint guard hardened (踩坑 #36 / #41).** `client.raw_request()` (the new
  absolute-URL helper the PromQL path uses to reach the sibling service with a Bearer
  header) bypasses the `(method, path)` AST scanner, so a new `raw_request` call site
  could ship an unverified URL with every scanner green. A regression now confines the
  name to `connection.py` (its definition) and `ops/promql.py` (the one gated call site),
  and asserts it actually *saw* both known sites so a broken glob can't pass while
  checking nothing.

The `test_mcp_parity.py` expected counts were bumped to 33/26/7 and the capability
grader's non-entity set now excludes the operator-supplied `integration_id`.

## v1.8.9 — moved to vmware-skills org + MCP Registry namespace io.github.vmware-skills/vmware-aria

Repo transferred from github.com/zw008 to github.com/vmware-skills (redirects preserve old links).
MCP Registry server renamed to `io.github.vmware-skills/*`; the old `io.github.zw008/*` entry is deprecated.
All in-repo links updated. No functional code change on this line beyond the org move.

## v1.8.8 — CLI writes now route through policy + audit, exactly like the MCP tools

Every state-changing CLI command is now wrapped by `@guarded`, the CLI counterpart
to the MCP `@vmware_tool` decorator: it runs the same vmware-policy `guard()`
authorization and writes the same `audit_call()` row to `~/.vmware/audit.db`. A
`delete`/`disable`/destructive command run through a shell is now authorized and
recorded exactly like the equivalent MCP tool — closing the gap where CLI writes
bypassed policy and landed only in the legacy per-skill log (HLD I-1/I-8).

- a policy `deny` rule now refuses the operation on the CLI with a teaching line
  naming the rule that fired, not a traceback
- the legacy per-skill audit log is still written this release (dual-write); it is
  removed at 2.0
- **requires vmware-policy >= 1.8.8** (the release that adds the shared `guarded` core)
- a regression test derives the write-command set from the MCP `[WRITE]` markers and
  asserts every one is `@guarded`, so a new write command cannot ship unguarded

Also carries the environment-field docstring correction (an optional label a `deny`
rule may scope to — there is no "warn now / refuse next major" gate).

## v1.8.7 (2026-07-21) — the skill-level read-only switch is removed; read/write authorization is the vCenter account's job (RBAC)

### Removed: `VMWARE_READ_ONLY` / `read_only:` — give the agent a read-only service account instead

The skill-level read-only switch is gone. It was enforced only on the MCP tool
registry, and any agent with a shell (every SKILL.md grants `allowed-tools: Bash`)
could reach the same change one CLI command away — so it withheld the *tool*, not
the *capability*. It was never a real boundary.

To run an agent read-only, give it a **read-only vCenter/NSX service account
(RBAC)**. Writes are then refused at the platform, un-bypassably, regardless of
surface or shell — the one place read/write control cannot be stepped around. A
config still carrying `read_only: true` is ignored, with a one-time warning that
names the replacement (no silent behavior change).

### Removed: approval tiers and the declared-environment gate (via vmware-policy)

The graduated-autonomy approval tiers (`confirm`/`dual`/`review`) and the "declare
an environment or be refused" baseline are removed — they only ever fired on the
rarest configuration while carrying the family's most complex machinery. Opt-in
`deny` rules and the maintenance window remain, and apply identically wherever a
tool runs.

### Added: offline / air-gapped install docs

The README now covers installing from source without editable mode (for older
`pip`) and building wheels to carry onto an air-gapped host — the modern PEP 517
layout has no `setup.py` by design, which is expected, not a missing file.

This release also carries the accumulated fixes staged since 1.8.5.

## v1.8.5 (2026-07-20) — the two fixes v1.8.4 announced now actually work

Four adversarial reviews of v1.8.4 found that both of its headline fixes were
incomplete in ways the release notes did not reflect. This release makes them
real. If you are on 1.8.4, this is the one to take.

### Fixed — a failure that was *returned* was still audited as a success

vmware-policy 1.8.4 added `report_tool_failure()` for tools that catch an
exception and return an error payload instead of raising. **No skill called it.**

Every string-returning tool therefore kept doing exactly what 1.8.4 said it had
stopped doing: writing `status=ok` to `~/.vmware/audit.db` for an operation that
failed, recording an undo token for a change that never happened, and telling the
circuit breaker the call succeeded so repeated failures never tripped it.

The surface this covered is not marginal:

| Skill | What was mis-audited |
|---|---|
| vmware-aiops | 25 of 49 tools, including **every undo-bearing write** — a failed `vm_power_on` left an undo token saying "power it back off" |
| vmware-avi | all 28 tools, including `vs_toggle` and `ako_restart` |
| vmware-storage | all 4 write tools |
| vmware-nsx | the 5 delete tools |

vmware-avi is worth calling out: before 1.8.4 its exceptions propagated and the
audit was correct. 1.8.4 caught them and returned a string, so **that release made
its audit trail worse than it had been.**

Skills whose tools already return dict payloads (vmware-monitor, vmware-vks,
vmware-aria, vmware-log-insight, vmware-harden, vmware-debug, vmware-pilot) were
already detected correctly. They gained a test proving it rather than a redundant
call.

### Fixed — narrowing `OSError` did not close the leak it was meant to close

1.8.4 narrowed the `_safe_error` passthrough because bare `OSError` let TLS and
DNS failures reach the agent with hostnames and certificate subjects in them.
That narrowing had no effect on the error it was written for:

```
ssl.SSLCertVerificationError → ssl.SSLError → OSError, ValueError
```

`ValueError` has been on every allowlist since long before 1.8.4, so a
certificate failure kept passing through — the commonest self-signed-certificate
failure in this family, carrying the hostname it was checked against. An
allowlist structurally cannot express "not this one".

Where `ssl.SSLError` can actually surface — the pyVmomi skills — it is now
reduced *ahead* of the allowlist. In the httpx skills TLS arrives wrapped as
`httpx.ConnectError`, and in vmware-avi as `requests.exceptions.SSLError`, so the
guard cannot fire there; in those skills the leak was the raw exception
interpolated into an already-allowlisted `*ApiError`, and that is now authored
text naming the config target and `verify_ssl` instead of the exception.

The missing-password error — this family's most common first-run failure, whose
entire remedy is the environment variable name it carries — keeps its message
through a narrow `ConfigError(OSError)` rather than the base class. Connection
failures are translated at the connection layer into an authored remedy that
names the target and the setting to change, with the raw detail left on
`__cause__` for the server log.

### Also fixed

- **vmware-vks**: the quickstart documented a password variable the code never
  reads — following `README.md` verbatim produced "Password not found". Five
  places, plus six references to a `doctor` command this CLI has never had, two
  descriptions promising fields the tools do not return, and eight teaching
  messages that `RuntimeError` was masking.
- **vmware-nsx**: an error cited `--route-advertisement`; the flag is `--advertise`.
- **vmware-pilot**: `get_workflow_status` told the model to call `approve` — a
  tool the read-only gate withholds — as the required next step; and a hint
  pointed at a filename that could never appear in that message.
- **vmware-aiops**: `vm_task_status` polling a *failed task* returned
  `{"state": "error", "error": ...}` from a successful read, which the new
  detection read as the call itself failing. The field is now `task_error`.
  **This is a breaking change for anything parsing that payload.**
- Several remedies that were still being cut by the 300-character cap the 1.8.4
  notes claimed to have addressed.

### Known and not fixed

`ConnectionError` remains one type from two sources in several skills — a
skill's own authored message and urllib3's `HTTPSConnectionPool(host=..., port=...)`
share it, and an allowlist cannot separate them. vmware-vks is converted; the
rest need their own domain type and are deferred rather than half-done.

## v1.8.4 (2026-07-20) — errors that teach, and tool descriptions a small model can route from

A capability eval was rolled out across the family and asked two open questions:
when a call fails, is the model told enough to fix it, and can it pick the right
tool from the description alone? Both answers were worse than anyone thought, and
in several places the reason was that the measurement was looking somewhere other
than where the model reads.

### Fixed — teaching messages were being discarded on the way to the agent

`_safe_error` reduces unrecognised exceptions to `"<Class>: operation failed."`
so raw API text, credentials in URLs and internal paths cannot reach an agent.
Its allowlist held only the builtin validation errors — so this skill's **own**
domain exceptions, the ones that exist precisely to carry a corrected next step,
had their messages replaced by their class names.

The effect was invisible from the CLI, which prints those messages in full.

The worst case was shared by nine skills: `config.py` raises exactly one
`OSError`, the missing-password error, whose entire remedy is the environment
variable name it names. An agent hitting an unconfigured target received
`OSError: operation failed.` and had nothing to act on. That is the family's most
common first-run failure, and it landed one release after the documented variable
names were corrected — so the message that would have unstuck the operator was
the one being thrown away.

The rule is now the property it always meant: **every exception this skill raises
on purpose passes through**, and only genuinely unplanned ones are reduced.
`RuntimeError` stays reduced — it is the generic catch-all and in several skills
carries raw upstream text.

### Fixed — error messages now carry the correction

Every message that reported a failure without saying how to recover was
rewritten: it names the offending value, gives an imperative remedy, and names
something concrete to act on — a tool that exists, a real CLI command, a config
file, an environment variable. Recovery becomes an instruction-following problem
rather than an inference one, which is what a weak model can still do.

Three classes of defect surfaced while doing it:

- **Remedies that were never delivered.** `_safe_error` truncates with no
  ellipsis, so a message longer than the cap loses its closing sentence
  silently. One message had been shipping at 396 characters against a 300-char
  cap — its remedy had never once reached an agent. Messages now lead with the
  remedy so a long interpolated value truncates the expendable detail instead.
- **Commands that do not exist.** One skill's error hints named a `doctor`
  subcommand it does not have.
- **Tools that do not exist.** A tool description pointed at two sibling-skill
  tools that had been renamed, and another named a tool that had moved to a
  different skill entirely.

### Improved — tool descriptions state when to use them and what to call next

The description is the API for a small model: an unstated routing rule is a
routing rule that does not exist, and a tool with no stated next hop is one the
model stops at. Descriptions now say when to prefer this tool over a sibling,
what shape comes back, the caveat that bites, and which tool to call after.

**Manifest size did not grow.** Descriptions load into every session, so the
routing clauses were paid for by cutting duplicated reference material —
repeated boilerplate, examples that restated the parameter list, and prose
copies of the pagination contract.

### Note

Every tool and CLI command named anywhere in this release was verified against
the live MCP registry and the live command tree, not against documentation.

## v1.8.3 (2026-07-20) — credentials resolve as a pair; documented env vars now exist

### Added — the per-target username can come from the environment

Adapted from [VMware-AIops#33](https://github.com/vmware-skills/VMware-AIops/pull/33) by
@wright-bench, with thanks. The password already resolved from an env var; the
username did not, so a deployment injecting credentials from a secret store
(systemd `EnvironmentFile`, container secrets, a vault sidecar) could externalise
only half of the pair — and a config-file username paired with an env password
from a different account logs in as nobody.

`<PASSWORD-KEY-PREFIX>_USERNAME` now overrides the `username:` in config.yaml,
using that skill's own password-key convention. Absent, config.yaml still wins;
nothing changes for anyone not setting it.

**Resolved on every access, like the password.** The contributed version read the
username once at load time while the password stayed a property, which
reintroduces exactly the split the override exists to prevent: a sidecar rotating
both halves mid-process moves the password and leaves the username behind. A test
pins that both halves resolve at the same moment.

### Fixed — documented credential variables that the code never read

Rolling the above across the family surfaced a separate defect: four skills
documented a password variable their own loader does not look up. An operator
following the documentation exactly — correct file, correct place, correct-looking
name — got "Password not found".

| Skill | Documented | Actually read |
|---|---|---|
| vmware-nsx | `VMWARE_NSX_<TARGET>_PASSWORD` for target `nsx-prod` → `VMWARE_NSX_PROD_PASSWORD` | `VMWARE_NSX_NSX_PROD_PASSWORD` |
| vmware-nsx-security | `VMWARE_<TARGET>_PASSWORD` | `VMWARE_NSX_SECURITY_<TARGET>_PASSWORD` |
| vmware-aria | `VMWARE_<TARGET>_PASSWORD` | `VMWARE_ARIA_<TARGET>_PASSWORD` |
| vmware-vks | `VMWARE_<TARGET>_PASSWORD` | `VMWARE_VKS_<TARGET>_PASSWORD` |
| vmware-avi | three different forms across three files | `<CONTROLLER>_PASSWORD` |

The prefixes genuinely differ per skill, so nothing could be fixed by
standardising a pattern — each repo's docs were corrected against its own code.
The code was left alone: changing a key would break every existing deployment.

`family_smoke.sh` now compares the credential variables named in each repo's docs
against the ones that repo's code builds, so the two cannot drift apart again.

## v1.8.2 (2026-07-20) — the MCP server moves into the package namespace

### Fixed — co-installing two skills broke all but the last one

Every skill shipped its MCP server as a **top-level `mcp_server` package**. Python
has one top-level namespace, so installing any two of them into one environment let
the second overwrite the first — silently, with no error and no warning.

    uv tool install vmware-aiops   ->  49 tools   (correct)
    uv pip  install vmware-aiops   ->  27 tools   (Monitor's read-only server)

vmware-aiops depends on vmware-monitor, so this was not an edge case: **every pip
install hit it**, and the operator got 27 read-only tools where 49 were expected,
with all 35 write tools missing. Docker images, shared MCP hosts and CI runners that
install more than one skill were affected the same way.

The server now lives at `vmware_<skill>/mcp_server/`, a name only this package can
claim. Introduced 2026-02-26; it survived 70 releases because every test ran against
a single package in its own repo, where the local directory shadows site-packages —
the conflict was invisible by construction.

**Migration.** Console scripts are unchanged: `vmware-<skill>` and
`vmware-<skill>-mcp` work exactly as before, as does `"command": "vmware-<skill>",
"args": ["mcp"]` in an MCP client config. Only a direct `python -m mcp_server`
breaks; use `python -m vmware_<skill>.mcp_server`.

### Added — `references/agent-guardrails.md` in every skill

The operating rules for local and small models (Llama 3.3 70B, Qwen, Mistral via
Goose / Ollama / OpenShift AI) existed in two skills. They now ship in all 13, each
with its own tool counts and failure modes, and are linked from every SKILL.md.

## v1.8.1 (2026-07-19) — read-only mode reaches the surfaces that teach it

v1.8.0 put read-only mode in the code and documented it in the README only.
Every other layer was empty, and each serves a different reader: SKILL.md is what
the agent loads, setup-guide is what an operator reads while configuring, `doctor`
is where they verify it took. The gap had two concrete costs.

An agent read SKILL.md, called a write tool the gate had withheld, and got nothing
back — with no way to learn that the absence was a deliberate lockdown rather than
a fault. It reads as a broken tool, so the model retries or hunts for a workaround.

An operator who set the switch had no way to confirm it. The only signal was a line
in the MCP server's start-up log.

### Added — the feature is now documented where each reader looks

- **SKILL.md** — a short section telling the agent that a missing write tool is a
  lockdown, not a fault: name the blocked operation, do not retry, do not route
  around it.
- **references/setup-guide.md** — the operator's view: how to enable it, the
  precedence chain, and how to verify.
- **references/capabilities.md** — which tools the gate withholds.

### Added — `doctor` reports the read-only state

`vmware-aria doctor` now shows whether read-only mode is on, **which** of the three
switches decided it, and the value as written. A typo'd value (`ture`) is called
out as a typo rather than reported as a confident ON — it resolves to on, which is
fail-closed but almost never what was meant.

The resolution runs through `vmware_policy.read_only_status()` rather than a local
copy of the precedence chain: a doctor that disagrees with the gate it reports on is
worse than no doctor. Requires `vmware-policy>=1.8.1`.

## v1.8.0 (2026-07-18) — read-only mode, working policy defaults, declared environments

Family release driven by [VMware-AIops#31](https://github.com/vmware-skills/VMware-AIops/issues/31),
where an operator running Llama 3.3 70B (Goose / OpenShift AI, on-prem H100) had to
hand-write 17 prompt guardrails to make tool calling reliable. A prompt is advisory — a
model can ignore it. Every guardrail that could move into the harness has.

### Added
- **Read-only mode.** Set `VMWARE_READ_ONLY=true` (or `VMWARE_<SKILL>_READ_ONLY`, or
  `read_only: true` in config.yaml) and every write tool is removed from the MCP registry
  at start-up. `list_tools()` never offers them, so the model cannot call what it cannot
  see. **Off by default** — nothing changes unless you turn it on. Fail-closed: if the
  mode is requested but cannot be guaranteed, the server refuses to start rather than
  running open.
- **`environment:` on each config target**, declaring which environment it is
  (production / staging / lab). Policy rules scope by this value.

### Added — list results now state whether they are complete

Every `[READ]` list tool returns the family envelope instead of a bare array:

    {"items": [...], "returned": 50, "limit": 50, "total": 213,
     "truncated": true, "hint": "Showing 50 of 213. Raise limit or narrow the query..."}

This closes the reported failure where long responses were summarised as "no data
returned": a bare list gives a model no way to tell a complete answer from page one, so
it guessed. `truncated: false` now positively states completeness — including when
`items` is empty, which means "checked, found none", not "the call failed".

- **10 tool(s) converted** across ops, MCP and CLI. Real totals come from suite-api's `pageInfo.totalCount`. Suppressed on purpose where
  it would mislead: with `name_filter` set, `totalCount` counts the *unfiltered*
  collection, so reporting it beside filtered rows would answer a question nobody asked.
  `list_anomalies` returns only flagged VMs, so it reports a total only when the scan
  stopped short — a short list is otherwise not evidence of completeness.

### Changed — migration, read this
- **Approval tiers now actually run.** They shipped in v1.6.0 but the engine only ever
  read `~/.vmware/rules.yaml`, and a fresh install has no such file — so every deny rule,
  maintenance window and approval tier had been inert on every install that never
  hand-authored one. A packaged baseline now loads when you have written no rules of your
  own. Writes at medium risk and above are stamped with their tier in the audit log;
  irreversible work and guest execution against a target declared `production` require a
  named approver via `VMWARE_AUDIT_APPROVED_BY`.
- **`environment:` will become required for writes.** Today a state-changing operation
  against a target that declares none still runs and logs a warning. **The next major
  release refuses it.** Declare it now and that upgrade is a no-op:

      targets:
        prod-vc01:
          host: vc01.corp.local
          environment: production

  Read-only operations are never affected, in this release or the next. Check what applies
  to your targets before upgrading: `vmware-audit policy --operation vm_delete --env <env>`.

### Fixed
- **Policy glob patterns with a leading wildcard silently matched nothing.** A rule written
  `operations: ["*_delete"]` parsed fine, read correctly, and never fired — only a trailing
  `*` was honoured. Now full glob matching, for operations and environments alike.
- Config-path overrides (`VMWARE_<SKILL>_CONFIG`) are honoured when reading `read_only`
  and `environment`, so a setting in a custom config file is no longer silently ignored.

### Notes
- Requires `vmware-policy>=1.8.0`; publish that package first.
- `vmware-audit policy` reports which rules are in force and where they came from —
  including the case where your rules file exists but failed to parse, which previously
  looked identical to "policy is working".

### Added
- **`investigate_alert`** (27 → 28 tools, 21 read / 7 write). Resolves an alert to
  its affected resource in one call: fetches the alert, reads `resourceId`, fetches
  that resource, confirms name and kind, and returns both UUIDs *explicitly
  labelled* plus a ready-to-use handoff naming the exact vmware-monitor tool and
  argument. Replaces the three prompt guardrails the reporter had to hand-write to
  stop a small model swapping the alert UUID for the resource UUID. An unresolvable
  resource degrades to a warning plus explicit nulls rather than losing the alert.
- `references/agent-guardrails.md` — operating this skill with a local/small model.

## v1.7.5 (2026-07-13) — internal dead-code cleanup + family version alignment

### Internal
- Removed an unused `CONFIG_DIR` import (cli) and unused `import os` (doctor).
  No behavior change; MCP tool surface unchanged (27).

## v1.7.4 (2026-07-13) — family version alignment

## v1.7.3 (2026-07-03) — family version alignment

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
  ([#6](https://github.com/vmware-skills/VMware-Aria/issues/6)). Non-503 errors still
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