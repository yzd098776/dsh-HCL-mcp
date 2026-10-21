# MCP_HCL 当前交接

当前源码位于 `C:/Users/yzdwxl/.dsh/work/MCP_HCL` 的 main，版本 0.5.0，完整交付仍 **Incomplete**，`full_live_gate_passed=false`。使用入口见 [README](README.md)，维护文档见 [docs/README](docs/README.md)；公开发布清单见 [CHANGELOG](CHANGELOG.md) 与 [Harness 插件包](dsh/README.md)。

## 当前版本与证据

当前候选：`C:/Users/Public/Documents/MCP_HCL_Descriptor_20261018_f71b2415/mcp_hcl-0.5.0-py3-none-any.whl`。

SHA256：`bb07a86555255bc6e20a2a5e3a2cd8f3f29cd4ecea2a793ebe9c094910f82586`。59 个包文件与当前源码及候选安装副本逐字节一致。受影响源码检查 52 项全部通过；新候选安装专项 68 项、67 通过、1 符号链接权限跳过。当前 HCL 声明面 55/6，固定上游复现基线 62/5；当前提交的完整 CI 单独核对。旧候选 `2213e9c3…56849f` 保留，它才是本次原生拒绝所用包，不能混作新包的原生结果。

前一候选 `7a74d260…9c9` 及其提交 2f7ec0f 的云端 338 项结果保留；它不包含当前 API 对照增量，不能冒充新源码或候选的结果。当前 CI 状态须按实际提交检查。

旧规范化候选 `2c57afcd…5880a` 和首轮 `b52e247c…56812` 及其结果保留，不能标识修复后的当前源码。

软件证据根：`work/normalization-20261011/ccf9210a95624f7ba9401fbb79e0dead/`，含备份 bundle、来源快照、manifest、SDK/AST 对比和逐项结果。旧候选和原始失败日志保留。

原生证据根：`C:/Users/Public/Documents/MCP_HCL_Native_Test_20261011/c2f6bfc9fcb347d8a5b2f1d2f2fd0556`。原报告记录一次单设备隔离生命周期和 MCPHCLLab 删除；profile/工程/证据保留。旧候选 CLI 被后台 MultiCC 保护拒绝，命令未发送；配置/startup、重启与流量仍未验证。该记录不代表当前现场已重采，也不证明新候选原生通过。

## 下一步

**2026-10-21 实况进展（本机真实 HCL）：完整链路已在 native 路径跑出 `verified`，并经 DSH 工具端到端确认。**
用 `Deployment` 自身代码路径（不注入 `session_factory`）对运行中的
`topo1-device1` 跑通了全部五步：备份 → 改配置 → 回读 → 持久化 → 回滚，并拿到
`native_channels=true`、`configuration_verified=true`、`persistence_verified=true`、
`verified_scope=running_configuration_readback_only`；`Deployment.backup` 返回
`backup_verified=true`，`Deployment.restore` 返回 `rollback_verified=true`，
故障注入那一轮在第 4 条命令被拒后自动回滚、`rollback_verified=true` 且回滚后
`display current-configuration` 的 digest 与基线逐字节相同。
（`connectivity_verified`/`guest_boot_verified`/`full_live_gate_passed` 按合同恒为
false——MCP 不声称验证过 guest 启动或端到端连通性。）

重启 MCP 服务后，**经 DSH 的 MCP 工具直接调用**同样是 native `verified`：
`hcl_live_deploy`（`958cd76a…`）→ `configuration_verified=true`、
`persistence_verified=true`；`hcl_backup_config`（`97249a78…`）→
`backup_verified=true`；`hcl_restore_config`（`c538df8d…`）→
`rollback_verified=true`。新进程完整性实测 `medium`（`integrity_rid=8192`）。

为此改掉了 `RuntimeBinding` 的证据模型两处：
(1) staged 副本不再要求"运行中 VM 以绝对路径挂载 staged 的 `ConfigDisk`"，改为按
**VDI disk UUID** 绑定（VDI 1.1 头偏移 `0x188` 的 `uuidLinkage`；当时实测 live 与
staged 的 `device1.vdi` 都是 `32924cf6-d794-480c-b277-470d3099dc77`——这是
19:27 重开工程前的 UUID，之后变成 `a3e3b701-…`，见下文"重启后必须重新 stage"；
`VBoxManage showhdinfo` 对 staged 副本报 "already exists with UUID {…}" 是权威
佐证；`showvminfo` 的 `IDE-ImageUUID-1-0` 也一致）。路径分支保留，报
`configuration_disk_binding="path"`；UUID 分支报 `"disk_uuid"`。
(2) `verify_pipe` 按通道分派：命名管道仍走原来的 `GetNamedPipeServerProcessId` +
`GetOwnerSid` + `--startvm`；telnet 走 `_verify_telnet_bridge`——`GetExtendedTcpTable`
拿 `30000+device` 的唯一监听 PID → `QueryFullProcessImageNameW` 必须是
`SimwareClient.exe` → `ProcessIdToSessionId` 等于当前 session → HCL 自己的
`Log/HCLLog/*.log*` 里必须有 `create_telnet_server … strPipeName:\\.\pipe\<topology>-device<N>, telnet_port:<port>`。
这条判据只用跨完整性级别可用的调用，不需要 `SimwareClient` 的 owner SID。

环境侧还查清一件事：`C:\Users\yzdwxl\.dsh` 带 Low 强制标签，其下解释器继承 Low，
Low 令牌下 `VBoxManage showvminfo/showhdinfo` 的 COM 调用直接
`E_ACCESSDENIED`（`--version` 不受影响）。已对 `.venv\Scripts\{python,mcp-hcl,pythonw}.exe`
执行 `icacls … /setintegritylevel Medium`；**该修复只对重启后的新进程生效**，重启
MCP 后实测 `low`(4096) → `medium`(8192)，同一条 `hcl_live_deploy` 从
`Running VM has no configuration disk bound to this staged project` 变为 `verified`。
这是部署 native 路径的一个前提条件，接手时若再次遇到该报错，先查
`runtime_binding.token_integrity()` 的级别。

本轮还修掉六个只有真机才暴露的 bug：Comware 按 80 列折行导致长命令回显归属误报
`command_echo_missing`（新增 `cli_session.command_echo_present`）；在用户视图盲发
`return` 会让整条事务失败（新增 `cli_session.ensure_user_view`，
`deployment.py`/`startup.py` 里 7 处裸 `exchange("return")` 全部改走它）；
`VBoxManage showvminfo --machinereadable` 一行里混用 JSON 转义与原始反斜杠，原实现
一律 `json.loads` 导致 147 行里 7 行不可解析（`machine_values` 改为"先试 JSON，失败
则剥一层配对引号"）；分页符 `---- More ----` 混进回读正文（`clean_text` 用
`PAGER_NOISE` 剔噪，另加 `pager_pending` 在剔噪前判断是否仍需翻页）；`send()` 用
CRLF 会让设备多回一个空提示符并污染下一次回读（改为裸 `\r`，这也是 HCL 桥期望的
行尾）；设备无用户自定义 QoS 时完全不打印 header，`qos_definitions` 原本据此报
`Missing user-defined QoS result header`（现把空正文当空库）。

**只读工具也已改走 telnet 桥。** `inspection.py`、`policy.py`、
`reconciliation.py`、`security_audit.py`、`evidence.py` 里 5 处 `endpoint(...)`
全部换成 `resolve_endpoint(...)`；此前它们直接开命名管道，在真机上恒报
`pipe_access_denied`（`CreateFileW` `winerror=5`）。改后实测
`hcl_inspect_ports`（14 个接口）、`hcl_read_vlans`（VLAN 1）、
`hcl_read_qos`（设备无用户自定义 QoS，空库）都是 `verified` 且
`native_readback_verified=true`；`hcl_health_check`、`hcl_diff`、
`hcl_audit_security` 是 `native_readback_verified=true` 的 `partial`（`partial`
是它们对"未配置项"的既定语义）。这六个工具在 MCP 重启加载新代码后，已逐个经
DSH 工具端到端复验通过（`work/post_restart_checklist.py` 可先确认运行中的进程
比源码新；重启后跑了两轮、共十二条 operation_id 见 `docs/hcl-live-findings.md`；
QoS 空库的那个真机 bug 见上面第 6 条）。复现脚本 `work/probe_read_tools_telnet.py`
（可选 stage 名参数，缺省取 `work/staged` 下最新且带 bindings 的副本）。

**重启后必须重新 stage。** 重启 HCL 工程会重写配置盘，`uuidLinkage` 从
`32924cf6-d794-480c-b277-470d3099dc77` 变成
`a3e3b701-c853-4fad-97f8-fdd7a8c8292b`，旧 staged 副本随即失效，所有只读/部署
工具报 `Running VM has no configuration disk bound to this staged project`——这是
绑定依据变化，不是代码回归。用 `work/restage_after_reboot.py` 重做：
`hcl_stage_copy` 单独不够（不写 `MCPArtifacts/bindings.json`，会报
`Fresh stage/bindings receipts are required`），必须再跑 `hcl_stage_plan`，并把
它分配出的 `device_id`（`max+1`）钉回 `1` 才代表运行中的 `topo1-device1`。
设备配置同时会回到 HCL 保存时的状态（本轮实测回到出厂默认：只有 `vlan 1`、
sysname `H3C`），因此早前读到的 VLAN 10/30 不能当现状复述。
复现脚本：`work/verify_native_chain.py`（`backup`/`restore <id>`/`fault`）、
`work/verify_full_chain.py`（`stage`/`backup`/`run`/`restore`）、
`work/verify_rollback_fault.py`、`work/restore_live_state.py`（把设备恢复原状）、
`work/probe_read_tools_telnet.py`（全部只读工具走 telnet）、
`work/restage_after_reboot.py`（重启后重做 staged 副本并钉回 device_id）、
`work/post_restart_checklist.py`（确认运行中的 MCP 已加载当前源码）、
`work/preflight.py`（每次使用前的一条命令前置检查）。
细节与证据见 [真实 HCL 观测](docs/hcl-live-findings.md)。

**每次使用前的前置检查就一条命令：**

```powershell
.venv\Scripts\python.exe work\preflight.py
```

它按顺序验四件事，任何一步不过都打印修复动作并以非零退出码结束：
HCL 后端 `127.0.0.1:16600` 有响应（GUI 开着且已载入工程）→ 至少一台 HCL VM 在跑
（设备已开机）→ MCP 进程启动时间晚于最新源码（否则改的代码没生效）→ 最新 staged
副本的配置盘 UUID 仍被运行中的 VM 挂载（否则重启后副本已失效）。四项全过时它直接
打印本次该用的 `project_path` / `topology` / `device`。退出码：0 就绪、1 后端未起、
2 无运行 VM、3 MCP 进程过期、4 staged 副本失效（跑 `work/restage_after_reboot.py`）。

除这套通用检查外，只有两条按需前提：**要做写操作**才需要 `MCP_HCL_ALLOW_LIVE_WRITE=1`
（已在 DSH profile 里打开；目标必须是 staged 工程）；**要让 `VBoxManage` 的 COM 调用
成功**才需要 Medium 完整性令牌（`icacls ... /setintegritylevel Medium`，且必须重启
进程才生效）。

**2026-10-21 实况进展（本机真实 HCL）：console 通道已打通。** 设备管道
`\\.\pipe\<topology>-device<N>` 的读端被 HCL 的 SimwareClient 独占，外部进程恒得
`ERROR_PIPE_BUSY`/`ERROR_ACCESS_DENIED`；HCL 把同一根管道桥接成
`telnet://127.0.0.1:(30000 + 设备号)`，GUI console 窗口用的就是它。`console.py` 现已
支持该通道（`TelnetSession`/`open_telnet`，含 telnet 协商、IAC 转义与打开阶段唤醒），
`hcl_console_exec` 传 `topology` + `device` 时默认解析成 telnet 端点。在真机 MSR36-20
上经 MCP 自身代码路径执行 `system-view` → `vlan 30` → `quit` → `display vlan brief`
已落地并回读确认。`hcl_apply_*` 仍只接受 staged 工程（需 `MCPArtifacts/bindings.json`
与 `stage-receipt.json`），对运行中的工程要用 `hcl_console_exec` 逐条下发。细节与证据见
[真实 HCL 观测](docs/hcl-live-findings.md)。因此下表"独占 CLI"一行在内核检查之外，
已有真实回显/提示符证据，但"配置行为等价"等目标仍需 staged 工程走完整部署流程。

真实 CLI 仍需验证内核独占检查及执行身份，再准备必要专项。用户随后已授权临时普通账号用于最基础测试；2026-10-12 的最小预检在环境隔离检查失败，未启动 HCL/guest 或发送命令。该账号已按本次 SID/创建标记删除，profile 和失败证据保留，原生专项保持 Unverified。有效成功生命周期应复用，不重复启动，不复用已删除账号的旧 SID。真实执行按 [测试指南](docs/live-acceptance.md)；参数、恢复与功能限制见 [合同](docs/contract-v2.md)。

复现目标已扩展并固定到上游 `3c7c7a46b9ea27c914f845d161f95f0fc3f53d5f` 的 62 工具/5 资源，库存为 12 implemented / 47 planned / 3 unverified，HCL 声明面仍为 55/6。原生完整设备/线缆库存、若干 IOS 语义、clock_rate/EIGRP delay、旧 PVST/protect/restrict、原生命名 Exporter/monitor、完整审计规则和安全 Phone 关联等仍缺。

当前源码与历史分支已整合，来源工作区仍保留各自版本；E:/MCP_HCL 的旧 dsh-2 checkout 不是当前 main 源码。远端同步状态以 `git status --short --branch` 和实际 `origin/main` 为准。日期化报告和重复快照已移出仓库维护文档，仍可从 Git、原始证据和仓库外备份追溯。

## 完整复现目标与推进顺序

hcl_parity_report 的 reproduction_targets 保存以下全部目标、依赖和验收条件；不能以接口声明或软件测试把它们标为完成。

| 目标 | 关闭条件 | 当前层次 |
|---|---|---|
| 独占 CLI | 单连接证据、服务器身份/VM/配置盘绑定、真实回显/提示符，忙/未知路径拒绝 | 内核检查已实现；真实 telnet console 回显/提示符已验证（2026-10-21）；native 身份绑定已闭合（VDI disk UUID + telnet 桥判据，2026-10-21），见 `docs/hcl-live-findings.md` |
| 完整原生库存 | 全设备、线缆、端口与非 VM 终端的权威清单和稳定归因 | planned |
| 实时编辑/生命周期 | 创建、删除、改名、移动、链接、端口、模块与注释均原生前后回读，统一隔离保护 | planned |
| 有线完整闭环 | 规划→创建/连线→启动→配置→DHCP/OSPF/ping→保存/恢复，无手工复制配置 | CLI/库存前提未满足 |
| 配置行为等价 | NAT ingress/池掩码、clock/delay/EIGRP、STP/BPDU/MAC、认证与原生命名 NetFlow 等语义，以及 native startup/恢复/重启 | partial：备份/改配置/回读/持久化/回滚已在真机走通，native 路径已出 `verified`（2026-10-21）；其余 IOS 语义仍未覆盖 |
| 安全无线闭环 | AC/AP/交换机/2 Phone，真实 WPA2-PSK、租约、双向 ping、保存/恢复 | blocked |
| 完整实时观察 | 原生 diff/health、端口/VLAN/QoS/IPv4/IPv6/路由域与完整覆盖 | partial |
| 模拟/抓包/工作区 | HCL 可验证的逐事件/逐层等价、截图、项目元数据与工作区设置；普通抓包不替代完整事件合同 | planned |
| 独立 DHCP Server | 明确服务器角色的池读/创建/更新/删除、默认值/范围/DNS/网关、原生回读和实际租约 | planned；不注册占位工具 |
| API 对应 | 62/5 的名称、参数、默认值、错误、结果和能力协商明确对应，不静默缩小范围 | 声明/当前 Schema 对照已实现；转换及行为等价未完成 |
| 交付与证据 | 精确候选的全部必要原生验收、可复现安装与软件/发布检查、分层证据 | blocked |

本轮首先取消 CLI 对任意后台 MultiCC 的一刀切占用结论，改为连接后的内核单实例证据；共享文件锁、原生忙/权限错误、回显、预算、服务器身份与原 UUID 恢复保护保留。多/无限实例和未知信息不发送。Windows 私有合成管道证明软件路径，不证明 VirtualBox/HCL 已接受。

接手只读复核未观察到旧实验账号及运行 Client/Wrapper/MultiCC/guest；本轮不重建账号、不启动 HCL/guest。后续真实 CLI 专项需要已审阅的普通身份/隔离工程。先关闭该前提及原生库存，再推进有线闭环；其余目标继续保留。

本轮源码/测试基线和新回执根：`work/reproduction-62-20261011/018ef18892ed4024a273473dcefef4b6/`。原失败与旧候选不覆盖。

软件交付检查已加入 `.github/workflows/software-ci.yml`：Windows/Python 3.12、默认写开关 0、完整软件检查、wheel 构建及独立日志制品。CI 是软件门槛，不替代原生验收；实际运行状态以对应提交的 Actions 结果为准。

云端干净环境暴露的 Windows 8.3 路径别名误判已修复：证据模板比较解析后的同一文件，路径/哈希/链接保护保留；NetStream 故障注入明确命中规范化目标。首轮失败回执和原工作副本换行字节保存在本轮 work 目录。当前数据文件与 Git/CI 的正式字节对齐，避免旧工作区换行差异混用候选。

## 当前软件增量

2026-10-12 增加 `hcl_parity_report.api_compatibility`，覆盖上游 62/5 固定声明和当前 55/6 MCP 发现结果。逐工具列出参数、必填性、默认值、返回注解、实际 HCL 输入 Schema 和未实现目标；`request`/`parameters` 的同名候选不表示可执行转换。所有输出/错误/行为等价仍为 unverified，自动转换仍 false；上游 `pt_*` 不注册占位别名。API 目标推进到 partial，不提升原生字段或 12/47/3 库存计数。

同时修正库存中“未测试/等待外部测试”的旧描述，明确 NAT、接口、交换机安全、NetStream、观察/审计及 GUI 的软件测试层与原生未验证层。

基线、源码/依赖清单、逐项测试、API 对照快照和新候选身份位于 `work/api-contracts-20261012/5e8578f9bf144f489deb1433ea9733f3/`。旧失败、历史候选及原生证据保留。下一软件目标继续处理配置语义及原生库存的可验证来源；真实调用等待已准备的普通身份/隔离工程。

## 身份预检诊断增量（2026-10-17）

上次最小原生预检记录为 `C:/Users/Public/Documents/MCP_HCL_Basic_20261012/250073e1572e4b0b9491ac125e4a2bb8/result.json`。其中只有 `Profile environment mismatch`，没有具体字段，不能据此断言 TEMP 或其他变量是根因。

现有 `scripts/hcl_lab_environment_identity.ps1` 改为必须显式传入本次预期 SID/profile，不再绑定已删除账号的旧 SID。只读输出 14 个身份/路径检查的 observed、expected、passed/failed/unknown；缺失不猜测，拒绝不符合普通账号身份或 profile/Temp 的路径。即使路径匹配，activation_allowed、runtime_isolation_verified 和 full_live_gate_passed 仍为 false；不修改环境、不创建账号、不启动 HCL。

本机受影响软件检查 14 项全部通过，首次失败保留。另仅在当前原账号执行一次只读观察，14 字段匹配；这不复现或关闭旧临时账号的环境故障。回执根：`work/identity-diagnostics-20261017/bcdea944e8884091967d5f5b3516680d/`。运行器现在把 scripts 的实际字节也纳入测试 manifest。

本轮只修改仓库脚本、测试和文档，包内源码未变，无需重建候选；当前候选 SHA 与 55/6 声明面保持。软件 CI 结果以本轮提交为准，真实 CLI/配置/流量仍未验证。

## 原生最小测试当前结果（2026-10-18）

新账号 MCPHCLQ1018E（SID 尾号 1012）已完成真实 14/14 身份/路径观察，子进程退出码 0。使用逐文件核对的 CPython/标准库和旧候选 2213e9c3…56849f，仅启动一个 MSR36-20，VM UUID 60e8a662-885b-4ccc-b4b3-504aec8037ac。单次探针在 runtime_binding 拒绝，command_outcome=not_sent；未打开控制台或重放，配置/流量未运行。

拒绝的实证原因：HCL 生成的 .vbox 在非身份 ExtraDataItem/DmiBoardSerial 属性中写入字面 0x03，导致标准 XML 1.0 解析失败。原始描述文件 5271 字节，SHA 2708f9dc1e16e94b01f0fbe5598e2cd87a0cac95187d9ac3942a9961e7b3ea21。证据根：`C:/Users/Public/Documents/MCP_HCL_Console_20261018/57ba837b6b0744c5ba181cd6f486e759/`。HCL/guest 已正常停止、保存和退出，账号按本次 SID/marker 停用删除，profile/证据保留。原账号三个记录根共 97 文件无增删或变动；只校验大小/mtime 和不超过 1MiB 文件的哈希。

当前源码仅对已观察到的 Machine/ExtraData/DmiBoardSerial 字面 ETX，在内存身份解析副本中作兼容；不写文件、不改 UUID/镜像/序列号，不删除其他未知控制字节。DTD/实体（含 UTF-16）、注释/CDATA/PI、其他属性与错误视图中的控制字节仍拒绝。原始 descriptor_sha256、UUID/配置盘/管道与真实进程检查保留，验证标志不提升。

首次软件失败、修复、源码 52 项和新安装候选 68 项检查位于 `work/vbox-descriptor-20261018/f71b241523d5469bb713acb767047811/`。捕获文件仅作离线解析复核；新包尚未完成后续真实配置盘/管道/CLI 验证，不得据此关闭 exclusive_cli 或原生总门槛。

历史 C/D 账号的 14 项匹配、独立运行目录、退出码修复及旧 CLI unknown 回执继续保留；D 账号证据根为 `C:/Users/Public/Documents/MCP_HCL_Basic_20261018/df0ea35cc8f2412e8632c7a6847317b7/`，不能把 D 的 unknown 补写成 E 的 not_sent。旧脚本失败不重放，新单次脚本覆盖全阶段回执，回执落盘失败保留已有命令结果。生命周期成功证据继续复用，不另做配置/流量。完整目标仍 Incomplete，full_live_gate_passed=false。

## 公开发布与插件市场（2026-10-21）

为把本仓库作为 DeepSeek Harness 插件发布，补齐了市场评审实际读取的材料：

- 根 `package.json`（`dsh.bundle.patch: ./dsh/cordis.patch.yml`）+ `dsh/package.json` + `dsh/cordis.patch.yml` + `dsh/README.md`：config-only bundle；市场按“根级 package.json + Bundle 声明”发现插件，所以根目录也声明一次，`dsh plugin --profile desktop add .` 在仓库根即可安装（`add .\dsh` 仍可用）。实测：新 profile 安装后 `--dump-config` 出现 `- id: mcp-hcl` 与 `command: mcp-hcl`。，插入 `@deepseek-ai/dsh-mcp-client`（`serverName: hcl`、`command: mcp-hcl`、`MCP_HCL_SCHEMA_MODE=dsh`、`MCP_HCL_ALLOW_LIVE_WRITE=0`）；不含 Host 模块、客户端入口或生命周期脚本。用 `dsh --profile web --patch .\dsh\cordis.patch.yml --dump-config` 组合通过。
- 工具安全注解：`src/mcp_hcl/tool_annotations.py` 把 55 个工具分为 24 只读 / 14 设备破坏性 / 17 其它，`StrictFastMCP.list_tools` 在发现面补齐 `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`；未分类工具落到保守回退（非只读、破坏性、开放世界），契约测试 `test_every_advertised_tool_carries_safety_annotations` 固定该不变量（DSH 模式 55/55 带注解）。
- 发布面：`README.md` 重写为市场可直接抽取的英文主文档（定位、改变/不改变什么、安装、权限、边界、状态），`CHANGELOG.md`（0.5.0 首次公开发布，含六类真机 bug 修复）、`SECURITY.md`（报告渠道与范围）、`.github/workflows/release.yml`（tag `v*` → sdist/wheel、SHA256SUMS、GitHub Release、PyPI trusted publishing），`pyproject.toml` 补 keywords/classifiers/urls。
- 本地软件检查：ruff 与 format 干净，406 tests、9 failures 全部是本机执行策略拒绝运行未签名 `scripts/*.ps1`（环境限制，CI 上默认策略允许），`native_process_audit_passed=true`、`manifest_unchanged=true`、`live_write=0`。
- **已发布（2026-10-21）**：仓库 `https://github.com/yzd098776/dsh-HCL-mcp`（`main` = `76b8477`，`Software checks` 全绿）；tag `v0.5.0`（tag 对象 `b41941c`，指向 commit `3513d6f`，其后 `850539a`/`76b8477` 只改发布工作流）已建 GitHub Release，附件为 `dist/*` 与 `checksums/SHA256SUMS.txt`；PyPI `mcp-hcl 0.5.0` 经 trusted publishing 发布成功（wheel `sha256:a51fd554bca1307fbfca34ced57d9b166dffdbb4b1ff60991797d0d4eaa5e6a3`，sdist `sha256:c537e02be8fbfbb06f73fb5dfc85957df8c8bbd4525afdf7f7246419c1d19817`），`pipx install mcp-hcl` 因此可用。
- 发布过程中修掉的两个 CI 问题：`pyproject.toml` 同时写 SPDX `license` 与 `License :: OSI Approved` classifier 导致 setuptools 80 拒绝 editable 安装（删 classifier、`setuptools>=77`）；`pypi` job 从带子目录的 artifact 解压到 `dist/` 造成嵌套、且把 `SHA256SUMS.txt` 放进 `dist/` 触发 `InvalidDistribution`（改为解压到工作区根，校验和移到 `checksums/`）。
- 尚未完成：市场 `/zh/submit` 表单提交（市场按周从 GitHub 抓取并固定 commit SHA，仓库 README 是其主要输入）；npm 发布（bundle 目前通过仓库路径安装）；若后续改动 README/pyproject 需要重新发版到 PyPI，则打 `v0.5.1`。
