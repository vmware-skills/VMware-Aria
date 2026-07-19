<!-- mcp-name: io.github.zw008/vmware-aria -->
# VMware Aria Operations MCP Skill

> **Note**: In VCF 9.0 and later, **VMware Aria Operations** has been rebranded as **VCF Operations**. This skill works against both names — the `/suite-api/` REST endpoints are unchanged.

> **Author**: Wei Zhou, VMware by Broadcom — wei-wz.zhou@broadcom.com
> This is a community-driven project by a VMware engineer, not an official VMware product.
> For official VMware developer tools see [developer.broadcom.com](https://developer.broadcom.com).

AI-assisted monitoring and capacity planning for VMware Aria Operations (vRealize Operations) via the Model Context Protocol (MCP).

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Overview

`vmware-aria` exposes 28 MCP tools for interacting with Aria Operations through natural language AI agents (Claude Code, Cursor, Goose, etc.):

| Category | Tools | Type |
|----------|-------|------|
| **Resources** | list, get, metrics, health badge, top consumers | Read-only (5) |
| **Alerts** | list, get, investigate (alert→resource), acknowledge, cancel, definitions | Read + 2 Write (6) |
| **Alert Definitions** | symptom definitions, create, enable/disable, delete | Read + 3 Write (4) |
| **Capacity** | overview, remaining, time-remaining, rightsizing | Read-only (4) |
| **Reports** | definitions, generate, list, get, delete | Read + 2 Write (5) |
| **Anomaly** | list anomalies, risk badge | Read-only (2) |
| **Health** | platform health, collector groups | Read-only (2) |

**Total**: 28 tools — 21 read-only, 7 write

- **Read-only mode** — one env var strips every write tool from the MCP registry; ideal for audits, PoCs, and untrusted/local models — see [Read-Only Mode](#read-only-mode)

## Quick Start

```bash
# Install
uv tool install vmware-aria

# Configure
mkdir -p ~/.vmware-aria
cat > ~/.vmware-aria/config.yaml << 'EOF'
targets:
  prod:
    host: aria-ops.example.com
    username: admin
    port: 443
    verify_ssl: true
    auth_source: LOCAL
default_target: prod
EOF

# Set password (never in config.yaml)
echo "VMWARE_ARIA_PROD_PASSWORD=your_password" > ~/.vmware-aria/.env
chmod 600 ~/.vmware-aria/.env

# Verify setup
vmware-aria doctor
```

## Read-Only Mode

A prompt instruction is advisory — a model can ignore it. Read-only mode is structural: set `VMWARE_READ_ONLY=true` and all 7 write tools (alert acknowledge/cancel, alert definition create/enable-disable/delete, report generate/delete) are removed from the MCP registry at startup. `list_tools()` never offers them, so the model cannot call what it cannot see. Off by default, and fail-closed: if the mode is requested but cannot be guaranteed, the server refuses to start.

Three ways to enable:

```json
{
  "mcpServers": {
    "vmware-aria": {
      "command": "vmware-aria",
      "args": ["mcp"],
      "env": { "VMWARE_READ_ONLY": "true" }
    }
  }
}
```

- Per-skill override: `VMWARE_ARIA_READ_ONLY=true` (takes precedence over the family-wide `VMWARE_READ_ONLY`)
- Config alternative: `read_only: true` in `~/.vmware-aria/config.yaml`

Precedence: per-skill env → family env → config → off. Startup logs list exactly which tools were withheld.

Running with local or small models? See [`skills/vmware-aria/references/agent-guardrails.md`](skills/vmware-aria/references/agent-guardrails.md).

## CLI Examples

```bash
# List top CPU consumers
vmware-aria resource top --metric cpu|usage_average --top 10

# Check active CRITICAL alerts
vmware-aria alert list --criticality CRITICAL

# Acknowledge an alert
vmware-aria alert acknowledge <alert-id>

# Fetch 4-hour CPU + memory metrics for a VM
vmware-aria resource metrics <vm-id> --metrics cpu|usage_average,mem|usage_average --hours 4

# Check cluster capacity
vmware-aria capacity remaining <cluster-id>
vmware-aria capacity time-remaining <cluster-id>

# Find rightsizing opportunities
vmware-aria capacity rightsizing

# Check Aria platform health
vmware-aria health status
vmware-aria health collectors
```

## MCP Setup (Claude Code)

After `uv tool install vmware-aria`, add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "vmware-aria": {
      "command": "vmware-aria",
      "args": ["mcp"],
      "env": {
        "VMWARE_ARIA_CONFIG": "~/.vmware-aria/config.yaml"
      }
    }
  }
}
```

> **v1.5.15+** uses the single-command form `vmware-aria mcp`. The legacy
> `vmware-aria-mcp` console script is still kept for backward compatibility.
> If you must use `uvx --from vmware-aria vmware-aria mcp` (no install) and hit
> `invalid peer certificate: UnknownIssuer` behind a corporate TLS proxy, set
> `UV_NATIVE_TLS=true` or use the recommended `vmware-aria mcp` form above.

Then use natural language:
- *"Show me the top 10 CPU consumers right now"*
- *"List all CRITICAL alerts and acknowledge them"*
- *"How long until the prod cluster runs out of memory?"*
- *"Which VMs are over-provisioned? Show rightsizing recommendations"*
- *"Are there any anomalies on vm-web-01?"*

## Authentication

Aria Operations uses **vRealizeOpsToken** authentication:

```
POST /suite-api/api/auth/token/acquire
{"username": "admin", "password": "...", "authSource": "LOCAL"}
→ {"token": "abc123", "validity": 1765182896000}  # validity = expiry epoch ms

Subsequent requests: Authorization: vRealizeOpsToken abc123
```

Tokens have a 6-hour sliding validity (extended on each call, per the official spec); the client re-acquires automatically 60 seconds before expiry. The `validity` field is the expiry timestamp in epoch milliseconds, not a duration.

## Architecture

```
User (natural language)
  ↓
AI Agent (Claude Code / Goose / Cursor)
  ↓  [reads SKILL.md]
vmware-aria MCP server (stdio transport)
  ↓  [HTTPS + vRealizeOpsToken]
Aria Operations Suite API
  ↓
VMs / Hosts / Clusters / Alerts / Capacity
```

### Companion Skills

| Skill | Scope | Tools | Install |
|-------|-------|:-----:|---------|
| **[vmware-aiops](https://github.com/zw008/VMware-AIops)** ⭐ entry point | VM lifecycle, deployment, guest ops, clusters | 49 | `uv tool install vmware-aiops` |
| **[vmware-monitor](https://github.com/zw008/VMware-Monitor)** | Read-only monitoring, alarms, events, VM info | 27 | `uv tool install vmware-monitor` |
| **[vmware-nsx](https://github.com/zw008/VMware-NSX)** | NSX networking: segments, gateways, NAT, IPAM | 33 | `uv tool install vmware-nsx-mgmt` |
| **[vmware-nsx-security](https://github.com/zw008/VMware-NSX-Security)** | DFW microsegmentation, security groups, Traceflow | 21 | `uv tool install vmware-nsx-security` |
| **[vmware-avi](https://github.com/zw008/VMware-AVI)** | AVI / NSX ALB load balancing, AKO K8s operations | 28 | `uv tool install vmware-avi` |
| **[vmware-storage](https://github.com/zw008/VMware-Storage)** | Datastores, iSCSI, vSAN | 11 | `uv tool install vmware-storage` |
| **[vmware-vks](https://github.com/zw008/VMware-VKS)** | Tanzu Namespaces, TKC cluster lifecycle | 20 | `uv tool install vmware-vks` |
| **[vmware-harden](https://github.com/zw008/VMware-Harden)** | Compliance baselines, drift detection | 6 | `uv tool install vmware-harden` |

## Security

- Passwords loaded from env vars or `.env` file, never from `config.yaml`
- Write operations (alert acknowledge/cancel, alert definition management, report generate/delete) audit-logged to `~/.vmware/audit.db` (MCP, via vmware-policy) and `~/.vmware-aria/audit.log` (CLI)
- API responses sanitized (control chars stripped, 500-char limit) to prevent prompt injection
- Supports self-signed certificates (`verify_ssl: false`) for lab environments

#### Official Broadcom References

- **REST APIs**: <https://developer.broadcom.com/xapis> — VCF Operations API (formerly Aria Operations suite-api)
- **SDKs**: <https://developer.broadcom.com/sdks> — VCF Python SDK
- **CLI Tools**: <https://developer.broadcom.com/tools> — PowerCLI 9.1 includes "VCF Operations (formerly vRealize Operations Manager)" cmdlets

## License

MIT — see [LICENSE](LICENSE)
