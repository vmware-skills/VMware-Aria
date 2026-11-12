<!-- mcp-name: io.github.vmware-skills/vmware-aria -->

# VMware-Aria

<a id="readme-overview"></a>

Investigate performance, alerts and capacity with Aria Operations evidence.

English · [简体中文](README-CN.md)

Community-maintained · MIT · Not an official VMware/Broadcom product.

[![18-second illustrative investigation demo](https://raw.githubusercontent.com/vmware-skills/marketplace/main/assets/demos/vmware-skills-en.gif)](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-en.mp4)

Synthetic-data animation, not a live recording. [Watch the AI-narrated family introduction (~2 min)](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-en.mp4)

<a id="readme-capabilities-and-workflows"></a>

## Why use it?

- Read metrics and percentile summaries.
- Explore alerts, anomalies, capacity and rightsizing.
- Preserve missing samples and uncertain conclusions.

<a id="readme-quick-start"></a>

## Get started

Install the published CLI and inspect its commands:

```sh
uv tool install vmware-aria==2.4.6
vmware-aria --help
```

Requires uv and Python >=3.10. Configure targets and credentials locally before remote queries. [Setup guide](skills/vmware-aria/references/setup-guide.md).

For Claude Code and other host installation paths, see the [family portal](https://github.com/vmware-skills/marketplace).

<a id="readme-safety-and-audit"></a>

## Know the boundaries

Includes alert, definition, report and maintenance writes; it is not an exclusively read-only server. Target permissions and local Policy apply.

<a id="readme-mcp-configuration"></a> <a id="readme-troubleshooting"></a> <a id="readme-reference"></a>

## Go deeper

- [Capabilities](skills/vmware-aria/references/capabilities.md)
- [CLI reference](skills/vmware-aria/references/cli-reference.md)
- [Agent guardrails](skills/vmware-aria/references/agent-guardrails.md)
- [Detailed usage and architecture](skills/vmware-aria/references/detailed-guide.md)
- [MCP launch configuration](.mcp.json)
- [Canonical skill instructions](skills/vmware-aria/SKILL.md)

<a id="readme-companion-skills"></a>

Part of [VMware Skills](https://github.com/vmware-skills/marketplace): 15 skills, 14 MCP servers and one shared Policy library. If it helps your work, star the family portal.

<a id="readme-license"></a>

[Issues and feedback](https://github.com/vmware-skills/VMware-Aria/issues) · [MIT](https://opensource.org/license/mit/)
