# 文档入口

仓库仅维护以下使用、开发与验收文档：

| 文档 | 用途 |
|---|---|
| [README](../README.md) | 安装、运行与能力概览 |
| [HANDOFF](../HANDOFF.md) | 当前版本、验证结果与下一步 |
| [开发说明](development.md) | 模块职责、代码风格和统一检查 |
| [合同 v2](contract-v2.md) | 严格参数、结果、恢复和功能边界 |
| [API 指南](offline-api.md) | 工具分组、规划和调用方式 |
| [DeepSeek Harness 适配](deepseek-harness.md) | schema 投影、完整 schema 资源与 DSH 注册配置 |
| [真实 HCL 观测](hcl-live-findings.md) | 已验证工具（含全部只读工具走 telnet 桥及重启后端到端复验）、console 真实通道（telnet 桥）、完整链路实测（含 native `verified`）、VDI disk UUID 绑定与重启后重新 stage、telnet 桥身份判据、Low 完整性标签、六个真机 bug、写开关与 staged 工程边界 |
| [参数 Schema](offline-contracts.json) | 机器可读字段、类型和范围 |
| [测试指南](live-acceptance.md) | 软件与原生验收的必要步骤 |
| [Harness 插件包](../dsh/README.md) | 以 DSH bundle 形式安装（前置条件、环境变量、健康检查、卸载、权限） |
| [变更日志](../CHANGELOG.md) | 版本历史与首次公开发布内容 |
| [安全策略](../SECURITY.md) | 漏洞报告渠道、范围与运行加固建议 |

日期化报告、重复交接、旧决策和脚本快照已移出维护文档集合；历史仍在 Git 与仓库外备份中。原始验证日志、恢复材料和失败回执继续保存在各自私有证据目录，当前定位见 HANDOFF。
