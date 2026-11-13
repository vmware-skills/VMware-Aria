<!-- mcp-name: io.github.vmware-skills/vmware-aria -->

# VMware-Aria

<a id="readme-overview"></a>

Investigate performance, alerts and capacity with Aria Operations evidence.

English · [简体中文](https://github.com/vmware-skills/VMware-Aria/blob/main/README-CN.md)

Community-maintained · MIT · Not an official VMware/Broadcom product.

[![24-second illustrative investigation demo](https://raw.githubusercontent.com/vmware-skills/marketplace/main/assets/demos/vmware-skills-en.gif)](https://github.com/vmware-skills/marketplace/releases/download/demos-2026-11-13-operations/vmware-skills-en.mp4)

Synthetic-data GIF; video includes redacted historical screenshots. [Download the AI-narrated family introduction (~3 min)](https://github.com/vmware-skills/marketplace/releases/download/demos-2026-11-13-operations/vmware-skills-en.mp4)

<a id="readme-capabilities-and-workflows"></a>

## Why use it?

- Read metrics and percentile summaries.
- Explore alerts, anomalies, capacity and rightsizing.
- Preserve missing samples and uncertain conclusions.

<a id="readme-quick-start"></a>

## Get started

Install the published CLI and inspect its commands:

```sh
uv tool install vmware-aria==2.4.7
vmware-aria --help
```

Requires uv and Python >=3.10. Configure targets and credentials locally before remote queries. [Setup guide](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/references/setup-guide.md).

For agent-host installation, use the [setup guide](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/references/setup-guide.md) and [Cursor, Codex, Gemini CLI, Cline and local-model examples](https://github.com/vmware-skills/marketplace/blob/main/docs/agent-hosts.md).

Choose a model with the [shared model-fit workflow](https://github.com/vmware-skills/marketplace/blob/main/model-fit/RUNNER.md): compare the same tasks, review quality and safety, then select the smallest tested passing configuration per task group; this is not a production guarantee.

VCF Operations 9.x, including 9.1.1.0, is the renamed product family; judge each operation by its API evidence rather than treating version examples as a whitelist. Recognition is not full live-compatibility certification.

<a id="readme-safety-and-audit"></a>

## Know the boundaries

Includes alert, definition, report and maintenance writes; it is not an exclusively read-only server. Target permissions and local Policy apply.

<a id="readme-mcp-configuration"></a> <a id="readme-troubleshooting"></a> <a id="readme-reference"></a>

## Go deeper

- [Capabilities](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/references/capabilities.md)
- [CLI reference](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/references/cli-reference.md)
- [Agent guardrails](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/references/agent-guardrails.md)
- [Detailed usage and architecture](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/references/detailed-guide.md)
- [MCP launch configuration](https://github.com/vmware-skills/VMware-Aria/blob/main/.mcp.json)
- [Canonical skill instructions](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/SKILL.md)

<a id="readme-companion-skills"></a>

Part of [VMware Skills](https://github.com/vmware-skills/marketplace): 15 skills, 14 MCP servers and one shared Policy library. If it helps your work, star the family portal.

<a id="readme-license"></a>

[Issues and feedback](https://github.com/vmware-skills/VMware-Aria/issues) · [MIT](https://opensource.org/license/mit/)
