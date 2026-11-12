<!-- mcp-name: io.github.vmware-skills/vmware-aria -->

# VMware-Aria

用 Aria Operations 的证据调查性能、告警与容量。

[English](README.md) · 简体中文

社区维护 · MIT 开源 · 非 VMware/Broadcom 官方产品。

[![18 秒调查示意演示](https://raw.githubusercontent.com/vmware-skills/marketplace/main/assets/demos/vmware-skills-zh.gif)](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-zh.mp4)

使用合成数据的示意动画，非实时录屏。[观看家族 AI 配音介绍（约 2 分钟）](https://github.com/vmware-skills/marketplace/blob/main/assets/demos/vmware-skills-zh.mp4)

## 能解决什么问题？

- 读取指标与分位数摘要。
- 调查告警、异常、容量和规格建议。
- 保留缺失采样与不确定结论。

## 快速开始

安装已发布 CLI，并查看可用命令：

```sh
uv tool install vmware-aria==2.4.6
vmware-aria --help
```

需要 uv 与 Python >=3.10。远程查询前，在本地配置目标与凭据。[配置指南](skills/vmware-aria/references/setup-guide.md)。

Claude Code 及其他宿主安装方式见[家族门户](https://github.com/vmware-skills/marketplace/blob/main/README-CN.md)。

## 使用边界

包含告警、定义、报告和维护写操作，并非纯只读服务；受目标权限与本地 Policy 约束。

## 详细文档

- [能力范围](skills/vmware-aria/references/capabilities.md)
- [CLI 用法](skills/vmware-aria/references/cli-reference.md)
- [Agent 使用约束](skills/vmware-aria/references/agent-guardrails.md)
- [详细用法与架构](skills/vmware-aria/references/detailed-guide-CN.md)
- [唯一技能指令正本](skills/vmware-aria/SKILL.md)

属于 [VMware Skills 家族](https://github.com/vmware-skills/marketplace/blob/main/README-CN.md)：15 个 skill、14 个 MCP server 与一个共享 Policy 库。如果对你有用，欢迎 Star 家族门户。

[问题与反馈](https://github.com/vmware-skills/VMware-Aria/issues) · [MIT](https://opensource.org/license/mit/)
