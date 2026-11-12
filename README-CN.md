<!-- mcp-name: io.github.vmware-skills/vmware-aria -->

# VMware-Aria

<a id="readme-overview"></a>

用 Aria Operations 的证据调查性能、告警与容量。

[English](https://github.com/vmware-skills/VMware-Aria/blob/main/README.md) · 简体中文

社区维护 · MIT 开源 · 非 VMware/Broadcom 官方产品。

[![18 秒调查示意演示](https://raw.githubusercontent.com/vmware-skills/marketplace/main/assets/demos/vmware-skills-zh.gif)](https://github.com/vmware-skills/marketplace/releases/download/demos-2026-11-12/vmware-skills-zh.mp4)

GIF 使用合成数据；视频包含脱敏的历史截图。[下载家族 AI 配音介绍（约 2 分钟）](https://github.com/vmware-skills/marketplace/releases/download/demos-2026-11-12/vmware-skills-zh.mp4)

<a id="readme-capabilities-and-workflows"></a>

## 能解决什么问题？

- 读取指标与分位数摘要。
- 调查告警、异常、容量和规格建议。
- 保留缺失采样与不确定结论。

<a id="readme-quick-start"></a>

## 快速开始

安装已发布 CLI，并查看可用命令：

```sh
uv tool install vmware-aria==2.4.6
vmware-aria --help
```

需要 uv 与 Python >=3.10。远程查询前，在本地配置目标与凭据。[配置指南](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/references/setup-guide.md)。

Agent 宿主安装见本模块的[配置指南](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/references/setup-guide.md)，以及 [Cursor、Codex、Gemini CLI、Cline 与本地模型示例](https://github.com/vmware-skills/marketplace/blob/main/docs/agent-hosts-CN.md)。

<a id="readme-safety-and-audit"></a>

## 使用边界

包含告警、定义、报告和维护写操作，并非纯只读服务；受目标权限与本地 Policy 约束。

<a id="readme-mcp-configuration"></a> <a id="readme-troubleshooting"></a> <a id="readme-reference"></a>

## 详细文档

- [能力范围](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/references/capabilities.md)
- [CLI 用法](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/references/cli-reference.md)
- [Agent 使用约束](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/references/agent-guardrails.md)
- [详细用法与架构](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/references/detailed-guide-CN.md)
- [MCP 启动配置](https://github.com/vmware-skills/VMware-Aria/blob/main/.mcp.json)
- [唯一技能指令正本](https://github.com/vmware-skills/VMware-Aria/blob/main/skills/vmware-aria/SKILL.md)

<a id="readme-companion-skills"></a>

属于 [VMware Skills 家族](https://github.com/vmware-skills/marketplace/blob/main/README-CN.md)：15 个 skill、14 个 MCP server 与一个共享 Policy 库。如果对你有用，欢迎 Star 家族门户。

<a id="readme-license"></a>

[问题与反馈](https://github.com/vmware-skills/VMware-Aria/issues) · [MIT](https://opensource.org/license/mit/)
