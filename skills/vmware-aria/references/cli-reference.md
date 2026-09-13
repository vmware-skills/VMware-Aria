# CLI Reference

Complete reference for `vmware-aria` command-line interface.

## Global Options

All commands accept:
- `--target / -t <name>` — Target name from config (uses default if omitted)
- `--config / -c <path>` — Custom config file path

---

## `vmware-aria doctor`

Run pre-flight diagnostics.

```
vmware-aria doctor [OPTIONS]

Options:
  --skip-auth    Skip authentication check (only tests config + network)
  --config -c    Path to config file
```

**Checks performed**:
1. Config file exists at `~/.vmware-aria/config.yaml`
2. `.env` file permissions (warns if wider than 600)
3. Config parse succeeds (validates YAML and target structure)
4. Password env vars are set for each target
5. Network TCP connectivity to port 443 for each target
6. Aria Operations token acquisition (unless `--skip-auth`)
7. Aria version from `GET /versions/current`, e.g. `VMware Aria Operations 8.18.7 (8.x line, build 25423534)` (**WARN** `Not read: …` when it cannot be read — an unreadable version says nothing about whether the target works), and Aria platform health: PASS when HEALTHY, **WARN** when DEGRADED or UNKNOWN (the failed services are named), FAIL when DOWN. Only a failure to connect is an auth FAIL: an error after the token was acquired is a FAIL on the "Aria platform" row (`Checks did not complete: …`). The doctor disconnects from each target either way
8. MCP server module importable

Error details in the doctor table do not tell you to run the doctor again.

---

## Resource Commands

### `vmware-aria resource list`

List resources by kind.

```
vmware-aria resource list [OPTIONS]

Options:
  --kind -k TEXT    Resource kind [default: VirtualMachine]
                    Values: VirtualMachine, HostSystem, ClusterComputeResource,
                            Datastore, Datacenter, ResourcePool
  --limit -n INT    Max results [default: 50]
  --name TEXT       Filter by name substring (case-insensitive)
  --target -t TEXT  Target name
```

**Output**: Table with Name, ID, Health (color + score), Status.

### `vmware-aria resource get`

Get full resource details.

```
vmware-aria resource get <resource-id> [OPTIONS]

Arguments:
  resource-id  Resource UUID (required)

Options:
  --target -t TEXT  Target name
```

**Output**: JSON with all resource fields including health, risk, efficiency badges and identifiers.

### `vmware-aria resource metrics`

Fetch time-series metrics for a resource.

```
vmware-aria resource metrics <resource-id> [OPTIONS]

Arguments:
  resource-id  Resource UUID (required)

Options:
  --metrics -m TEXT    Comma-separated metric keys
                       [default: cpu|usage_average,mem|usage_average]
  --hours INT          History window in hours [default: 1]
  --target -t TEXT     Target name
```

**Common metric keys** (VirtualMachine names and units as Aria Operations 8.18.7 defines them; other resource kinds differ):
- `cpu|usage_average` — CPU|Usage (%)
- `mem|usage_average` — Memory|Usage (%)
- `cpu|demandmhz` — CPU|Demand (MHz)
- `mem|workload` — Memory|Workload (%)
- `disk|usage_average` — Physical Disk|Total Throughput (KBps) — a throughput, not a utilization percentage
- `net|usage_average` — Network|Usage Rate (KBps)

**Output**: JSON object:

```json
{
  "resource_id": "<uuid>",
  "window_begin_ms": 1757700000000,
  "window_end_ms": 1757703600000,
  "metrics": {"cpu|usage_average": [{"timestamp_ms": 1757700300000, "value": 3.2}]},
  "missing": [
    {"metric_key": "mem|usage_avg", "reason": "not_collected_for_resource",
     "detail": "...", "similar_keys": ["mem|usage_average", "..."]}
  ],
  "stat_keys_on_resource": 170
}
```

`metrics` holds only keys that returned at least one point. Every other requested key is in `missing`, with `reason`:
`not_collected_for_resource` (the resource never reports it; `similar_keys` lists up to 10 of its keys in the same group,
only keys that `sanitize()` leaves unchanged and that are at most 200 characters — when some were dropped, `detail` says
`N key(s) in the same group were omitted: they contain control characters or exceed 200 characters.`),
`no_data_in_window` (reported, but no points in the window), `resource_reports_no_stat_keys`, or `undetermined`
(the resource's stat-key list could not be read, or some of its rows are in an unrecognised form — `detail` then says
`N of M 'stat-key' rows in an unrecognised form`). `metric_key` is sanitized. `stat_keys_on_resource` is `null` unless something is missing.
A missing key is not a zero. (Before this change the output was a bare object keyed by metric.)

### `vmware-aria resource health`

Get health badge for a resource.

```
vmware-aria resource health <resource-id> [OPTIONS]
```

**Output**: JSON with health score (0–100), color, description.

### `vmware-aria resource top`

List top resource consumers by metric.

```
vmware-aria resource top [OPTIONS]

Options:
  --metric TEXT     Metric key to rank by [default: cpu|usage_average]
  --kind -k TEXT    Resource kind [default: VirtualMachine]
  --top -n INT      Number of top consumers [default: 10]
  --target -t TEXT  Target name
```

**Output**: Table with rank, name, value, unit. `value` is the average of the metric's points over the last hour (5-minute AVG rollup) — the number Aria Operations ranks by — and rows keep Aria's order, descending by `value`. The MCP tool `get_top_consumers` also returns `latest_value`, the most recent point. Resources with no data for the metric in the last hour are left out, not ranked at zero; `excluded_no_data` (MCP) counts the ones the ranking listed with no points. A yellow hint under the table says when that made the list shorter (or when no resources of that kind exist). When no-data rows took slots of a full `--top`, the result is marked truncated and the hint says to raise top_n.

---

## Alert Commands

### `vmware-aria alert list`

List alerts.

```
vmware-aria alert list [OPTIONS]

Options:
  --active / --all          Active alerts only vs all [default: active]
  --criticality TEXT        Filter: INFORMATION, WARNING, IMMEDIATE, CRITICAL
  --limit -n INT            Max results [default: 50]
  --target -t TEXT          Target name
```

**Output**: Table with ID, Name, Criticality, Status, Resource (name), Resource ID. Names and kinds come from one batched `GET /resources` lookup per page. A resource whose name could not be resolved prints as `?` — unknown, not "no resource" — and a yellow note under the table says how many could not be retrieved, were not returned (deleted or stale), or were returned with no name in Aria Operations. If the lookup answers with rows that were not requested (the appliance ignored the id filter), requested ids it left out count as could not be retrieved — retry — not as deleted.

### `vmware-aria alert get`

Get full alert details with contributing (triggered) symptoms. Recommendations are attached to the alert definition, not the alert. `get_alert` carries the resource ID only — resolve the name via `vmware-aria resource get <id>` or `alert list`.

Symptoms that carry no name or severity themselves (all of them on Aria Operations 8.18.7) take both from their symptom definition, fetched in one batched `GET /symptomdefinitions` lookup. Each symptom has `definition_lookup`: `resolved`, `not_needed`, `not_found`, `failed`, or `no_definition_id`. A `symptom_definitions_note` key appears when some did not resolve, or when symptoms carry no definition id to look a missing name or severity up by; an empty name there means unknown.

```
vmware-aria alert get <alert-id> [OPTIONS]
```

### `vmware-aria alert acknowledge`

Acknowledge an alert (marks as seen, does not close it).

```
vmware-aria alert acknowledge <alert-id> [OPTIONS]

Options:
  --yes -y          Skip confirmation prompt
  --target -t TEXT  Target name
```

**Audit logged**: yes.

### `vmware-aria alert cancel`

Cancel (dismiss) an alert. **Asks twice** unless `--yes` is given.

```
vmware-aria alert cancel <alert-id> [OPTIONS]

Options:
  --yes -y          Skip both confirmation prompts
  --target -t TEXT  Target name
```

**Audit logged**: yes. Cancelled alerts will not re-trigger unless the underlying condition recurs.

### `vmware-aria alert definitions`

List alert definition templates.

```
vmware-aria alert definitions [OPTIONS]

Options:
  --name TEXT       Filter by name substring
  --limit -n INT    Max results [default: 50]
  --target -t TEXT  Target name
```

**Output**: Table with Name, Criticality (max severity across the definition's states), Resource Kind, Impact. Creating, enabling/disabling, and deleting alert definitions are MCP-only tools.

---

## Capacity Commands

### `vmware-aria capacity overview`

Get a capacity overview for a cluster.

```
vmware-aria capacity overview <cluster-id> [OPTIONS]
```

**Output**: JSON with group-level `capacity_remaining_pct` plus per-dimension (cpu/mem/diskspace) `capacity_remaining` and `time_remaining_days`. The percentage metric exists only at group level. Values are None while capacity analytics warm up.

### `vmware-aria capacity remaining`

Get remaining capacity headroom for a cluster or host.

```
vmware-aria capacity remaining <resource-id> [OPTIONS]
```

**Output**: JSON with group-level `capacity_remaining_pct` and per-dimension `remaining_value` (absolute, unit per dimension e.g. MHz/KB).

### `vmware-aria capacity time-remaining`

Predict how many days until capacity is exhausted.

```
vmware-aria capacity time-remaining <resource-id> [OPTIONS]
```

**Output**: JSON with projected days per capacity dimension (None while capacity analytics have no data).

### `vmware-aria capacity rightsizing`

List VM rightsizing recommendations.

```
vmware-aria capacity rightsizing [OPTIONS]

Options:
  --resource-id TEXT   Scope to a specific VM UUID
  --limit -n INT       Max results [default: 20]
  --target -t TEXT     Target name
```

**Output**: Table with VM name, power state (`template` for templates), sizing status, `vCPU now→rec`, `Mem GiB now→rec`, `Disk GB`, and `Act.` (actionable), from the `OnlineCapacityAnalytics|{cpu,mem,diskspace}|recommendedSize` metrics. Status `reclaimable` means the engine publishes 0 for the VM — that is not a recommendation of zero; `none published` means the VM either needs no resizing or was never scored, which the appliance does not distinguish.

The raw recommendations are MHz (cpu), KB (memory) and GB (disk) — verified on Aria Operations 8.18.7. The table converts them: CPU MHz is divided by the VM's own MHz per vCPU (`cpu|speed` / `numCpu`) and rounded up — except that a result at most 0.01 vCPU (1% of one core's MHz) above a whole number of cores counts as that number, absorbing float noise between the engine's MHz and `cpu|speed`; a real fraction above that still rounds up (1.5 → 2). Memory is shown in GiB. Arrows mark direction against the current configuration: `↓` oversized, `↑` undersized, `=` right-sized. Memory within 1% of the configured size is right-sized. Disk has no direction.

`Act.` is `yes` only when the power state was read as `Powered On`, the template flag was read as false, and CPU or memory is off its recommendation; an unknown power state or template flag is never taken as running. Per-VM caveats print under the table: powered off, template, power state / template flag not published, a power state other than `Powered On`, current size not published, disagreement between `recommendedSize` and the engine's own `summary|oversized|*` / `summary|undersized|*` statistics, and — for every reduction — check the vendor minimum size first (appliances cannot be identified reliably from the API).

The MCP tool returns the same rows as JSON: `recommended_cpu` / `recommended_memory` / `recommended_diskspace` (raw), `recommended_units`, `sizing_status`, `current_vcpus`, `cpu_mhz_per_vcpu`, `recommended_vcpus`, `cpu_direction`, `current_memory_kb`, `memory_direction`, `power_state`, `is_template`, `product_name` (only when the VM publishes a vApp product), `aria_verdict`, `actionable`, `caveats`, plus the top-level `properties_note`.

If the bulk property read (`POST /resources/properties/latest/query`) fails, the rows still come back from the stats, but `power_state`, `is_template`, `current_vcpus`, `current_memory_kb`, `recommended_vcpus`, both directions and `product_name` are null — unknown, not unpublished — no row is actionable, and each row carries one caveat starting `VM properties could not be read`. `properties_note` (null when the read succeeded) names the failure — the HTTP status, or no HTTP response — and the CLI prints it in yellow under the table.

---

## Anomaly Commands

### `vmware-aria anomaly list`

List per-resource anomaly counts (`System Attributes|total_alarms` Total Anomalies metric — the public API does not expose the UI's anomalous-metrics list).

```
vmware-aria anomaly list [OPTIONS]

Options:
  --resource-id TEXT   Scope to a specific resource
  --limit -n INT       Max VMs to scan when listing [default: 20]
  --target -t TEXT     Target name
```

**Output**: Table with resource name (or ID) and anomaly count; without `--resource-id`, only resources with non-zero counts are shown, sorted descending.

### `vmware-aria anomaly risk`

Get risk badge score for a resource.

```
vmware-aria anomaly risk <resource-id> [OPTIONS]
```

**Output**: JSON with risk score (0–100) and color (from the resource's `badges[]` array). For contributing causes, inspect the resource's active alerts.

---

## Health Commands

### `vmware-aria health status`

Check Aria Operations platform health: node, each service, and the release.

```
vmware-aria health status [OPTIONS]
```

**Output**: Console summary with the assessment, the node status, the version (e.g. `VMware Aria Operations 8.18.7 — 8.x line`, or `unknown (<version_error>)`), a per-service table (Service / Health / Details, from `GET /deployment/node/services/info`) or `Services: not read (<reason>)`, and details (always printed).

| Assessment | Meaning |
|------------|---------|
| `HEALTHY` | Node reports ONLINE and every listed service reports OK — or node reports ONLINE and the per-service breakdown was not read, in which case details say no service was checked individually |
| `DEGRADED` | At least one service OK and at least one ERROR — the platform still answers |
| `DOWN` | Every service reports ERROR |
| `UNKNOWN` | The observations do not settle it: node not ONLINE and the breakdown not read; a service reports a state other than OK or ERROR (and it is not DEGRADED); or node not ONLINE while every service reports OK |

A services reply with no service objects in it counts as not read.

The node status is OFFLINE (and `/deployment/node/status` answers HTTP 503) whenever any one service is not running, so OFFLINE alone is not an outage. On Aria Operations 8.18.7 a node with only `LOCATOR` not OK reads DEGRADED. The MCP tool `get_aria_health` returns `assessment`, `overall_status`, `healthy` (assessment is HEALTHY), `system_time_ms`, `services` (null when unreadable, with `services_error`), `services_not_ok`, `services_unrecognized`, `release_name`, `product_name`, `product_version`, `product_line`, `build_number`, `version_error`, and `details`. `details` is composed: the node status (with `(HTTP 503 at /deployment/node/status)` and the node's own details, up to 300 characters), the assessment in words, `Not OK: …` naming services in ERROR, and `services_error`. A version or service list that cannot be read — including a 2xx body that is not JSON — is reported in `version_error` / `services_error`, not raised; `release_name` and `product_name` are sanitized. A node-status failure other than HTTP 503 still raises.

### `vmware-aria health collectors`

List collector groups and member status.

```
vmware-aria health collectors [OPTIONS]
```

**Output**: Per-group tables listing collector ID, name, state (UP/DOWN), and local flag (marks the built-in collector on the Aria node).

---

## Report Commands

### `vmware-aria report definitions`

List available report definition templates.

```
vmware-aria report definitions [OPTIONS]

Options:
  --name TEXT       Filter by name substring
  --limit -n INT    Max results [default: 50]
  --target -t TEXT  Target name
```

**Output**: Table with Name, ID, Subject Type (resource kinds the template applies to), Owner.

### `vmware-aria report generate`

Trigger report generation from a definition template (async).

```
vmware-aria report generate <definition-id> --resources <id1,id2> [OPTIONS]

Options:
  --resources TEXT  Comma-separated resource UUIDs — at least one is required
                    (the Report API generates against a resource)
  --target -t TEXT  Target name
```

**Audit logged**: yes. Returns the queued `report_id`; poll with `report get`.

### `vmware-aria report list`

List generated reports.

```
vmware-aria report list [OPTIONS]

Options:
  --definition-id TEXT  Filter by definition UUID (applied client-side)
  --limit -n INT        Max results [default: 20]
  --target -t TEXT      Target name
```

### `vmware-aria report get`

Get status and download URLs for a generated report.

```
vmware-aria report get <report-id> [OPTIONS]
```

**Output**: Status; when `COMPLETED`, the PDF `download_url` and `csv_url`.

### `vmware-aria report delete`

Delete a generated report (the definition and schedules remain intact).
**Irreversible — asks twice** unless `--yes` is given.

```
vmware-aria report delete <report-id> [OPTIONS]

Options:
  --yes -y          Skip both confirmation prompts
  --target -t TEXT  Target name
```

**Audit logged**: yes.

---

## Fleet Commands (VCF Operations 9.1)

Read-only fleet / diagnostics queries added for VCF Operations 9.1. The suite-api
*paths* are verified against the VCF 9.1 OpenAPI; the response *schemas* are read
defensively, and an unrecognised shape returns an empty result carrying a `note`
that the empty result is unconfirmed (never a silent "none"). The `promql`
sub-command reaches the real-time metrics (VODAP) service on a separate base
(`/data-query-service`) whose prefix is **INFERRED and not yet confirmed on real
hardware** — every result carries `base_path_confirmed: false`.

### `vmware-aria fleet certificates`

List certificate status/expiry across the VCF fleet.

```
vmware-aria fleet certificates [OPTIONS]

Options:
  --limit -n INT    Max rows (default 50)
  --target -t TEXT  Target name
```

### `vmware-aria fleet passwords`

List managed password-account status across the VCF fleet (read-only; does not rotate).

```
vmware-aria fleet passwords [OPTIONS]

Options:
  --limit -n INT    Max rows (default 50)
  --target -t TEXT  Target name
```

### `vmware-aria fleet domains`

List SDDC/workload domains behind one registered VCF integration. The integration
UUID comes from the Operations Integrations page.

```
vmware-aria fleet domains <integration-id> [OPTIONS]

Options:
  --limit -n INT    Max rows (default 50)
  --target -t TEXT  Target name
```

### `vmware-aria fleet findings`

List Operations diagnostic findings (not compliance — use vmware-harden for that).

```
vmware-aria fleet findings [OPTIONS]

Options:
  --severities TEXT  Comma-separated, e.g. CRITICAL,WARNING
  --categories TEXT  Comma-separated category filter
  --types TEXT       Comma-separated findingType filter
  --limit -n INT     Max rows (default 50)
  --target -t TEXT   Target name
```

### `vmware-aria fleet promql`

Run a real-time PromQL instant query against the VCF 9.1 VODAP service. Base path
INFERRED — confirm against a live appliance (`base_path_confirmed: false`).

```
vmware-aria fleet promql <query> [OPTIONS]

Options:
  --time TEXT       Evaluation timestamp (RFC3339 or Unix seconds)
  --source TEXT     Data-source id to scope the query
  --limit -n INT    Max result series (default 50)
  --target -t TEXT  Target name
```

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (API failure, auth failure, validation error) |
| 2 | Usage error (invalid arguments) |

`vmware-aria doctor` exits 0 if all checks pass, 1 if any fail. WARN rows do not fail.
