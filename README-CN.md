# VMware Aria Operations MCP Skill

> **作者**: Wei Zhou, VMware by Broadcom — wei-wz.zhou@broadcom.com
> 本项目由 VMware 工程师维护的社区项目，非 VMware 官方产品。
> VMware 官方开发者工具请访问 [developer.broadcom.com](https://developer.broadcom.com)。

通过 Model Context Protocol (MCP) 为 VMware Aria Operations（原 vRealize Operations）提供 AI 辅助的监控与容量规划能力。

## 概述

`vmware-aria` 提供 28 个 MCP 工具，支持通过自然语言 AI Agent（Claude Code、Cursor、Goose 等）与 Aria Operations 交互：

| 类别 | 工具 | 类型 |
|------|------|------|
| **资源** | 列表、详情、指标、健康评分、高消耗排名 | 只读 (5) |
| **告警** | 列表、详情、溯源（告警→资源）、确认、取消、告警定义 | 读+2写 (6) |
| **告警定义** | 症状定义、创建、启用/禁用、删除 | 读+3写 (4) |
| **容量** | 概览、剩余容量、时间预测、虚拟机调整 | 只读 (4) |
| **报表** | 模板、生成、列表、状态、删除 | 读+2写 (5) |
| **异常** | 异常列表、风险评分 | 只读 (2) |
| **健康** | 平台健康、采集器状态 | 只读 (2) |

**共 28 个工具** — 21 只读、7 写操作

## 快速开始

```bash
# 安装
uv tool install vmware-aria

# 配置
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

# 设置密码（绝不存入 config.yaml）
echo "VMWARE_ARIA_PROD_PASSWORD=your_password" > ~/.vmware-aria/.env
chmod 600 ~/.vmware-aria/.env

# 验证配置
vmware-aria doctor
```

### 离线 / 气隙环境安装（从源码）

本项目采用现代 PEP 517 构建系统（hatchling），因此**没有 `setup.py`**——这是
有意为之，不是缺失文件。如果你克隆源码后遇到 `ERROR: File "setup.py" or
"setup.cfg" not found ... editable mode currently requires a setuptools-based
build`，说明你的 `pip` 早于 21.3，无法对非 setuptools 后端做*可编辑*（`-e`）
安装。可编辑模式只是开发便利，运行本工具并不需要——任选其一：

```bash
# 从源码树安装——普通（非可编辑）安装会构建 wheel：
pip install .              # 不是  pip install -e .

# ……或先升级 pip，之后可编辑安装也能用：
pip install --upgrade pip && pip install -e .
```

对于**真正的气隙主机**，在联网机器上构建 wheel 再拷贝过去——目标主机无需联网：

```bash
# 在联网机器上，把本包及其依赖收集为 wheel：
pip wheel . -w dist        # → dist/*.whl   （或：uv build，仅构建本包）

# 把 dist/ 拷到气隙主机，然后离线安装：
pip install --no-index --find-links dist vmware-aria
```

## 常用 CLI 示例

```bash
# 查看 CPU 消耗排名前 10 的虚拟机
vmware-aria resource top --metric cpu|usage_average --top 10

# 查看所有严重告警
vmware-aria alert list --criticality CRITICAL

# 确认告警
vmware-aria alert acknowledge <alert-id>

# 查询虚拟机 CPU 和内存指标（最近 4 小时）
vmware-aria resource metrics <vm-id> --metrics cpu|usage_average,mem|usage_average --hours 4

# 集群容量规划
vmware-aria capacity remaining <cluster-id>
vmware-aria capacity time-remaining <cluster-id>

# 查找虚拟机资源调整建议
vmware-aria capacity rightsizing

# Aria 平台自身健康检查
vmware-aria health status
```

## MCP 配置（Claude Code）

完成 `uv tool install vmware-aria` 后，在 `~/.claude.json` 中添加：

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

> **v1.5.15+** 推荐使用单命令 `vmware-aria mcp`。旧的 `vmware-aria-mcp` 入口仍然
> 保留以向后兼容。如必须使用 `uvx --from vmware-aria vmware-aria mcp`（不安装）
> 且公司 TLS 代理报 `invalid peer certificate: UnknownIssuer`，请设置
> `UV_NATIVE_TLS=true`，或直接使用上面的 `vmware-aria mcp` 形式。

然后即可使用自然语言：
- *"显示当前 CPU 占用最高的 10 台虚拟机"*
- *"列出所有严重告警并逐一确认"*
- *"生产集群的内存还够用多少天？"*
- *"哪些虚拟机资源配置过多？给出调整建议"*
- *"vm-web-01 上有没有异常检测结果？"*

## 认证机制

Aria Operations 使用 **vRealizeOpsToken** 认证：

```
POST /suite-api/api/auth/token/acquire
Body: {"username": "admin", "password": "...", "authSource": "LOCAL"}
响应: {"token": "abc123", "validity": 1765182896000}

后续请求 Header: Authorization: vRealizeOpsToken abc123
```

Token 为 6 小时滑动有效期（每次调用自动延长，官方规范行为），客户端在到期前 60 秒自动重新获取。响应中的 `validity` 字段是到期时间戳（epoch 毫秒），不是时长。

## 密码命名规则

| 目标名 | 环境变量 |
|--------|---------|
| `prod` | `VMWARE_ARIA_PROD_PASSWORD` |
| `aria-lab` | `VMWARE_ARIA_ARIA_LAB_PASSWORD` |
| `staging-01` | `VMWARE_ARIA_STAGING_01_PASSWORD` |

规则：`VMWARE_ARIA_<TARGET大写>_PASSWORD`，连字符转为下划线。

### 配套 Skill

| Skill | 功能范围 | 工具数 | 安装 |
|-------|---------|:-----:|------|
| **[vmware-aiops](https://github.com/zw008/VMware-AIops)** ⭐ 入口 | VM 生命周期、部署、Guest 操作、集群管理 | 49 | `uv tool install vmware-aiops` |
| **[vmware-monitor](https://github.com/zw008/VMware-Monitor)** | 只读监控：告警、事件、VM 信息 | 27 | `uv tool install vmware-monitor` |
| **[vmware-nsx](https://github.com/zw008/VMware-NSX)** | NSX 网络：Segment、网关、NAT、IPAM | 33 | `uv tool install vmware-nsx-mgmt` |
| **[vmware-nsx-security](https://github.com/zw008/VMware-NSX-Security)** | DFW 微分段、安全组、Traceflow | 21 | `uv tool install vmware-nsx-security` |
| **[vmware-avi](https://github.com/zw008/VMware-AVI)** | AVI / NSX ALB 负载均衡、AKO K8s 运维 | 28 | `uv tool install vmware-avi` |
| **[vmware-storage](https://github.com/zw008/VMware-Storage)** | 数据存储、iSCSI、vSAN | 11 | `uv tool install vmware-storage` |
| **[vmware-vks](https://github.com/zw008/VMware-VKS)** | Tanzu 命名空间、TKC 集群生命周期 | 20 | `uv tool install vmware-vks` |
| **[vmware-harden](https://github.com/zw008/VMware-Harden)** | 合规基线、Drift 检测 | 6 | `uv tool install vmware-harden` |

## 安全性

- 密码仅从环境变量或 `.env` 文件加载，不存入 `config.yaml`
- 写操作（告警确认/取消、告警定义管理、报表生成/删除）记录审计日志至 `~/.vmware/audit.db`（MCP，经 vmware-policy）和 `~/.vmware-aria/audit.log`（CLI）
- API 响应经过净化处理（去除控制字符，截断至 500 字符），防止提示注入攻击
- 支持自签名证书（`verify_ssl: false`），适用于实验环境

## 许可证

MIT
