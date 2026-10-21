# MCP_HCL

**A Model Context Protocol server for HCL (H3C Cloud Lab): plan and stage topologies offline, inspect a live lab through its real telnet bridge, and change device configuration only through gated, receipt-backed operations.**

[![CI](https://github.com/yzd098776/dsh-HCL-mcp/actions/workflows/software-ci.yml/badge.svg)](https://github.com/yzd098776/dsh-HCL-mcp/actions/workflows/software-ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mcp-hcl.svg)](https://pypi.org/project/mcp-hcl/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Ships a DeepSeek Harness bundle ([`dsh/`](dsh/README.md)) and works with any
other MCP client over stdio.

---

## What it does

| Area | Tools |
|---|---|
| Topology planning and staging | `hcl_plan_topology`, `hcl_stage_plan`, `hcl_stage_copy`, `hcl_stage_add_device`, `hcl_stage_link`, `hcl_move/rename/delete`, `hcl_export_artifacts`, `hcl_load_plan`, `hcl_fix_plan` |
| Offline validation and generation | `hcl_validate_plan`, `hcl_explain_plan`, `hcl_estimate_plan`, `hcl_generate_configs`, `hcl_generate_operations` |
| Live lab read-only inspection | `hcl_read_vlans`, `hcl_read_qos`, `hcl_inspect_ports`, `hcl_health_check`, `hcl_diff`, `hcl_audit_security`, `hcl_inspect_project`, `hcl_capabilities`, … |
| Gated device configuration | `hcl_apply_vlan`, `hcl_apply_acl`, `hcl_apply_nat`, `hcl_apply_stp`, `hcl_apply_port_security`, `hcl_apply_netstream`, `hcl_apply_interface_tuning`, `hcl_remove_acl`, `hcl_remove_nat`, `hcl_backup_config`, `hcl_restore_config`, `hcl_live_deploy`, `hcl_device_power`, `hcl_console_exec` |

Full surface: **55 tools / 6 resources**, discoverable via `tools/list` and
`resources/list`. Every tool carries MCP safety annotations (`readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`) so a client can decide
what to confirm before calling: 24 read-only, 14 destructive device-mutating,
17 staging/GUI/other.

## What it changes in the Harness, and what it does not

- **Changes**: one MCP client-connector row in the active DSH profile, which
  exposes the server's tools as `mcp__hcl__<tool>`.
- **Does not change**: no Harness core file is patched, no module is injected
  into the Harness process, no lifecycle or install script runs, and no GUI is
  automated by default.
- **Does not do**: create HCL virtual machines, draw cables on the HCL canvas,
  power devices on, or prove end-to-end connectivity. HCL itself owns VM
  lifecycle; this server plans, validates, opens and configures.

## Requirements

| Requirement | Notes |
|---|---|
| Python | 3.11+ |
| MCP client | Any stdio client; DeepSeek Harness 0.1.0-rc.7+ (no upper bound: the bundle composes on the 0.1.0-rc.7 CLI and runs in the 0.2.1-rc.2 desktop app) |
| Windows | Required for live-lab and GUI tools; offline planning/staging tools also run on Linux/macOS |
| HCL (H3C Cloud Lab) | Required for native tools; without it the server starts and offline tools still work |

## Install

```powershell
pipx install mcp-hcl                 # or: uv tool install mcp-hcl
```

DeepSeek Harness plugin (adds the `hcl` MCP server row):

```powershell
dsh plugin --profile desktop add .       # from the repository root, or a release tarball
```

Then set the HCL paths in the composed profile patch — see
[`dsh/README.md`](dsh/README.md) for the full env table, health check and
uninstall steps.

Generic MCP client (`claude_desktop_config.json` style):

```json
{
  "mcpServers": {
    "hcl": {
      "command": "mcp-hcl",
      "env": {
        "MCP_HCL_INSTALL_ROOT": "E:\\HCL",
        "MCP_HCL_PROJECT_ROOTS": "C:\\Users\\me\\HCL\\Projects",
        "MCP_HCL_OUTPUT_ROOT": "D:\\mcp-hcl\\staged",
        "MCP_HCL_ALLOW_LIVE_WRITE": "0"
      }
    }
  }
}
```

## Quick start

```text
# offline: turn a declarative request into a real, staged HCL project
hcl_estimate_plan  {template: "router_on_a_stick", routers: 1, pcs_per_lan: 3}
hcl_stage_plan     {project_path: "C:\\Users\\me\\HCL\\Projects\\lab",
                    stage_name: "demo", request: {...}, port_map: {...}}

# live: read the running lab through the telnet bridge
hcl_read_vlans     {project_path: "...\\work\\staged\\demo\\lab", topology: "topo1",
                    device: "MSR36-20_1"}
```

## Safety and permissions

- **Local only**: talks to `127.0.0.1:16600` (HCL backend) and
  `127.0.0.1:30001-31024` (HCL telnet bridge). No outbound network access.
- **Writes are opt-in**: `MCP_HCL_ALLOW_LIVE_WRITE` defaults to `0`; with the
  default, device-mutating tools return a refusal instead of touching a device.
- **Staged-only deployments**: destructive tools refuse the original HCL
  project and require a stage receipt plus VDI/VM identity binding; every
  operation writes a receipt under the configured output root.
- **Evidence over claims**: read tools report `native_readback_verified`, and
  the contract keeps `connectivity_verified` / `guest_boot_verified` /
  `full_live_gate_passed` false because the server cannot prove those.
- **No secrets**: stores no credentials; lab device logins stay inside the
  emulated network.

## Status and evidence

- Verified on a real Windows HCL install: read-only tools return
  `native_readback_verified: true` over the telnet bridge (14 ports, VLAN 1,
  empty QoS DB, security audit, health and diff), after a restart and a fresh
  staging round.
- Boundary documented in [真实 HCL 观测](docs/hcl-live-findings.md): restart
  invalidates a staged copy's config-disk UUID, so re-run the restage helper;
  HCL's GUI cannot be automated from a non-elevated process.
- Upstream MCP-Packet-Tracer parity is **incomplete** (12 implemented / 47
  planned / 3 unverified). This project does not claim that parity.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe scripts\check_all.py     # ruff + full software verification
```

Module responsibilities and code style: [docs/development.md](docs/development.md).
Contracts and schemas: [docs/contract-v2.md](docs/contract-v2.md),
[docs/offline-contracts.json](docs/offline-contracts.json).
A handful of tests exercise signed PowerShell helpers and need an execution
policy that permits them.

## Documentation

| Document | Purpose |
|---|---|
| [docs/README.md](docs/README.md) | Documentation index |
| [docs/offline-api.md](docs/offline-api.md) | Tool groups and call patterns |
| [docs/contract-v2.md](docs/contract-v2.md) | Strict argument/result/recovery contract |
| [docs/deepseek-harness.md](docs/deepseek-harness.md) | Schema projection and Harness registration |
| [docs/hcl-live-findings.md](docs/hcl-live-findings.md) | Real HCL observations and known issues |
| [docs/live-acceptance.md](docs/live-acceptance.md) | Software and native acceptance steps |
| [HANDOFF.md](HANDOFF.md) | Current version, verification results, next steps |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [SECURITY.md](SECURITY.md) | Reporting a vulnerability |

## 中文摘要

MCP_HCL 把 HCL（H3C Cloud Lab）的规划、staging、只读回读与受控配置封装成 MCP
工具，既可接入任意 MCP 客户端，也自带 DeepSeek Harness 插件包（`dsh/`）。

安装：`pipx install mcp-hcl`，再 `dsh plugin --profile desktop add .`（仓库根目录），并在
profile 的 patch 里填好 `MCP_HCL_INSTALL_ROOT` / `MCP_HCL_PROJECT_ROOTS` /
`MCP_HCL_OUTPUT_ROOT`。写设备默认关闭（`MCP_HCL_ALLOW_LIVE_WRITE=0`），只读与
离线工具可直接使用。边界：本服务不创建虚拟机、不代替 HCL 画布，也不声称已证明
业务连通性；真实设备结论见 [docs/hcl-live-findings.md](docs/hcl-live-findings.md)。

## License

MIT — see [LICENSE](LICENSE).

本仓库是基于 [cheese-sansan/MCP_HCL](https://github.com/cheese-sansan/MCP_HCL)（MIT）
面向 DeepSeek Harness 插件市场的适配版本：新增插件包、工具安全注解与发布面文档，
未改变服务的功能边界与验收口径。
