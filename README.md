<!-- mcp-name: io.github.vmware-skills/vmware-aria -->
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

### Offline / Air-Gapped Install (from source)

This project uses the modern PEP 517 build system (hatchling), so there is **no
`setup.py`** by design — that is expected, not a missing file. If you cloned the
source and hit `ERROR: File "setup.py" or "setup.cfg" not found ... editable mode
currently requires a setuptools-based build`, your `pip` is older than 21.3 and
cannot do an *editable* (`-e`) install with a non-setuptools backend. Editable
mode is a developer convenience, not needed to run the tool — do one of:

```bash
# From the source tree — a normal (non-editable) install builds a wheel:
pip install .              # NOT  pip install -e .

# ...or upgrade pip first, and editable works too:
pip install --upgrade pip && pip install -e .
```

For a **truly air-gapped host**, build the wheels on a connected machine and copy
them over — the target then needs no network:

```bash
# On a connected machine, collect this package + its dependencies as wheels:
pip wheel . -w dist        # → dist/*.whl   (or: uv build, for just this package)

# Copy dist/ to the air-gapped host, then install offline:
pip install --no-index --find-links dist vmware-aria
```

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
| **[vmware-aiops](https://github.com/vmware-skills/VMware-AIops)** ⭐ entry point | VM lifecycle, deployment, guest ops, clusters | 49 | `uv tool install vmware-aiops` |
| **[vmware-monitor](https://github.com/vmware-skills/VMware-Monitor)** | Read-only monitoring, alarms, events, VM info | 27 | `uv tool install vmware-monitor` |
| **[vmware-nsx](https://github.com/vmware-skills/VMware-NSX)** | NSX networking: segments, gateways, NAT, IPAM | 33 | `uv tool install vmware-nsx-mgmt` |
| **[vmware-nsx-security](https://github.com/vmware-skills/VMware-NSX-Security)** | DFW microsegmentation, security groups, Traceflow | 21 | `uv tool install vmware-nsx-security` |
| **[vmware-avi](https://github.com/vmware-skills/VMware-AVI)** | AVI / NSX ALB load balancing, AKO K8s operations | 28 | `uv tool install vmware-avi` |
| **[vmware-storage](https://github.com/vmware-skills/VMware-Storage)** | Datastores, iSCSI, vSAN | 11 | `uv tool install vmware-storage` |
| **[vmware-vks](https://github.com/vmware-skills/VMware-VKS)** | Tanzu Namespaces, TKC cluster lifecycle | 20 | `uv tool install vmware-vks` |
| **[vmware-harden](https://github.com/vmware-skills/VMware-Harden)** | Compliance baselines, drift detection | 6 | `uv tool install vmware-harden` |

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
