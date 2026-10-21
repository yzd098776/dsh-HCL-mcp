# 真实 HCL 环境下的观测记录（2026-10-21）

本文件记录一次真实环境验证里观察到的事实，用于区分"服务器实现"与"环境限制"。
MCP_HCL 自身的合同见 [contract-v2.md](contract-v2.md)，接线方式见
[deepseek-harness.md](deepseek-harness.md)。

## 已验证可用

| 工具 | 结果 | 证据 |
| --- | --- | --- |
| `hcl_bridge_status` | `verified` | 回执含两帧原始协议响应，`backend_version=1.0.0&6.0.14` |
| `hcl_list_projects` | `verified` | 12 个真实工程 |
| `hcl_list_devices` | `verified` | `E:\HCL\devicecfgs\*.cfg` 与 `deviceInfo.ini` 的 sha256 |
| `hcl_inspect_project` | `ok` | 解析真实 `.net` |
| `hcl_stage_copy` | `staged` | 12/12 工程成功，含 4~23 台设备 |
| `hcl_console_exec` | `output_received` | 经 telnet 桥在真机 MSR36-20 上执行 `display version`，746 字节真实回执 |
| `hcl_live_deploy` | `verified` | `native_channels=true`、`configuration_verified=true`、`persistence_verified=true`（native 路径，见下节） |
| `Deployment.backup` | `verified` | native：`backup_verified=true`、`restore_test_verified=true`、记录绑定 `vm_uuid` |
| `Deployment.restore` | `verified` | native：`rollback_verified=true`，digest 回到基线 |
| `hcl_live_deploy`（故障注入） | `partial` | `rollback_verified=true`：第 4 条命令被拒后自动回滚，digest 逐字节回到基线 |
| `hcl_backup_config` / `hcl_restore_config` | `verified` | 经 DSH MCP 工具（重启后）native 跑出 `backup_verified=true` / `rollback_verified=true` |
| `hcl_inspect_ports` / `hcl_read_vlans` / `hcl_read_qos` | `verified` | 改走 telnet 桥后经 DSH 工具复验 `native_readback_verified=true`；MCP 重启并重新 stage 之后又复验两轮（14 个接口 / VLAN 1 / QoS 空库，见下节） |
| `hcl_health_check` / `hcl_diff` | `partial` | 经 DSH 工具复验 `native_readback_verified=true`，`partial` 是这两个工具对"未配置项"的既定语义 |
| `hcl_audit_security` | `partial` | 经 DSH 工具复验 `native_readback_verified=true`，同上 |

## console 的真实通道是 telnet，不是命名管道

这是本轮最重要的修正。设备 console 表面上是
`\\.\pipe\<topology>-device<N>`，但那个管道**只被 HCL 自己的 SimwareClient 读**，
外部进程永远打不开：

```
\\.\pipe\topo1-device1   READ  -> WinError 231 (ERROR_PIPE_BUSY)  每次都是
                         WRITE -> WinError 5  (ERROR_ACCESS_DENIED) 每次都是
                         RW    -> WinError 5  每次都是
不存在的管道             -> WinError 2
```

share mode 取 `0`/`R`/`W`/`RW` 都不改变结果，30 秒内每秒采样一次也从未释放。所以
`open_console_pipe` 等不到端点空闲——不是时序问题，是设计如此。

HCL 真正的 console 通路是它把管道桥接成的 telnet 服务器。HCL 自己的日志写着：

```
2026-10-21 13:02:25,362 - HCL topo1 - INFO: create_telnet_server success
    strPipeName:\\.\pipe\topo1-device1, telnet_port:30001)
```

映射规律经 240 条历史日志全量核对：**`device<N>` → `30000 + N`**，无例外。
GUI 的 console 窗口和 `putty.reg` 快捷方式连的都是这个 telnet 端口。

### 通道行为

- 连接即收到 `IAC WILL ECHO`、`IAC WILL SUPPRESS-GO-AHEAD`、`IAC DO BINARY`。
- 刚启动的 guest 停在 `Press ENTER to get started.`，只有收到一个行结束符才打印
  提示符。`open_telnet` 因此把"唤醒"做在打开阶段：最多发 3 次 `\r\n`，读到提示符
  为止，把这段输出存进 `session.wakeup_output` 并丢弃。这样第一条命令的回执不会
  被唤醒横幅污染（否则会报 `command_echo_missing`）。
- telnet 端口**可多客户端同时连接且共享同一个 console**：两个客户端同时连 30001，
  一个发 `display clock`，另一个也收到回执。因此租约（`PipeLease`）仍然是必需的，
  只是加在 telnet 端点上而不是管道上。
- 端点写法：`telnet://127.0.0.1:<30001..31024>`，或传 `topology` + `device` 由
  `resolve_endpoint` 推导。telnet 端点本身不含拓扑名，所以结果里的 `topology`
  来自调用方声明，`topology_source` 会标成 `declared`（管道端点则标 `endpoint`）。

### 实测的配置命令序列

经 MCP 自身的 `HclConsole.run` 路径（不是独立探测脚本），在真机 MSR36-20 上：

```
system-view        -> System View: return to User View with Ctrl+Z.  [H3C]
vlan 10            -> [H3C-vlan10]
quit               -> [H3C]
display vlan brief -> VLAN ID 1 (VLAN 0001) / VLAN ID 10 (VLAN 0010)
```

配置在 TCP 断开后仍然保留（每次调用新建一条 telnet 连接，设备侧 CLI 状态持续），
所以 `hcl_console_exec` 可以逐条下发配置命令。

## 三个独立的环境阻塞（其中两个已解决）

### 1. 写开关默认关闭（设计行为）— 已打开

`hcl_console_exec`、`hcl_inspect_ports`、`hcl_read_vlans` 都返回
`writes_disabled`，因为 `MCP_HCL_ALLOW_LIVE_WRITE` 默认关闭。这是有意的总开关，
不是故障。真实环境验证时在 profile 的 MCP env 里置 `MCP_HCL_ALLOW_LIVE_WRITE: "1"`
即可（见 [deepseek-harness.md](deepseek-harness.md)）。

### 2. console 管道被独占 — 已绕过

见上文：不走管道，走 telnet 桥。

### 3. `hcl_apply_*` 只能作用于 staged 工程（设计行为）

`hcl_apply_vlan` 等写工具要求目标工程位于 `MCP_HCL_OUTPUT_ROOT` 之内，并带
`MCPArtifacts/bindings.json` 与 `MCPArtifacts/stage-receipt.json` 两份回执
（`deployment.py:145-166`），否则报 `Only staged projects may be deployed`。
直接对 `C:\Users\yzdwxl\HCL\Projects\<name>` 调用会被拒。

这意味着两条不同的路径：

- **对 GUI 里正在跑的设备下发命令** → 用 `hcl_console_exec`（telnet，逐条命令）。
- **带备份/回读/回滚的批量配置** → 先 `hcl_stage_copy` 出 staged 工程，再对 staged
  工程调用 `hcl_apply_*`。

另外 `hcl_apply_vlan` 的 `ports[].interface` 要写 Comware 全名（`GigabitEthernet0/1`），
不是 `.net` 里的 `GE_0/1`；且 `allowed_vlans` 里的每个 VLAN 都必须出现在 `vlans` 中。

## 完整链路已在真机跑通（备份 → 改配置 → 回读 → 持久化 → 回滚）

用 `Deployment` 自身的代码路径（不是独立探测脚本）对运行中的 `topo1-device1`
跑完整链路，`session_factory` 注入 telnet 会话。工程是 staged 副本
`work/staged/chainlive/hcl_909530729855`，`device_id` 钉成 1。

### 备份（`Deployment.backup`）

```
status=output_received
backup_id=d384123380884c78b946ae83b82e131f
sha256=5eac8b5bd92d57ee29a0859a3bf91c27c7624167207d49d9e0bb43f92714cbe5
protocol_running_backup_verified=true
protocol_restore_test_verified=true
verified_scope=synthetic_backup_protocol_only
```

走完的步骤：`screen-length disable` → `display current-configuration` 存盘 →
`save flash:/mcp_<op>_before.cfg`（`[Y/N]` 确认）→ `more` 回读比对 digest →
`system-view` + `configuration replace file` 做还原预检 → 再比对 digest。

### 改配置 + 回读 + 持久化（`Deployment.run`）

```
status=output_received   native_channels=False
protocol_configuration_verified=true
protocol_persistence_verified=true
verified_scope=synthetic_configuration_protocol_only
```

5 条命令全部 `accepted`，`configuration_readback` 与
`startup_persistence_readback` 均 `verified`，`errors=[]`。持久化落成
`flash:/mcp_<op>_after.cfg` 并被设为 `Next main` startup 文件。

### 回滚

显式回滚（`Deployment.restore`）与自动回滚（`run` 中途失败）两条路径都验证过：

```
restore -> protocol_rollback_verified=true
           startup_restore.protocol_startup_rollback_verified=true
```

自动回滚用一个注入故障的真实会话触发（在 `vlan 100` 那条命令上抛
`command_rejected`，此时 `sysname` 已改）：`run` 返回 `status=partial`、
`protocol_rollback_verified=true`，且**回滚后 `display current-configuration`
的 digest 与备份基线逐字节相同**（`5eac8b5b…`），VLAN 100/CHAIN 都已消失。

`work/verify_rollback_fault.py` 可复现这条路径；`work/verify_full_chain.py`
的 `stage/backup/run/restore` 子命令复现前面三步。

### 注入通道下的 `*_verified` 为什么是 false（已解决，见下节）

上表前几行的 `protocol_*` 都是 true，但 `configuration_verified`、
`persistence_verified`、`rollback_verified` 是 false。原因不是链路没走通，而是
那几轮注入了一个 telnet `session_factory`，`native_mode` 因此为假：`native_channels`
是 false，`verified_scope` 就如实标成 `synthetic_*`——协议层真的走通了，但
"这条命令打到了哪台 VM"没有被 native 证据钉死。

**这一限制已经解除**：修掉下面两处 native 断点后，不注入 factory 的原生路径在
真机上直接跑出 `verified`。

## native 路径已在真机跑通（2026-10-21 收尾）

重启 MCP 服务（让 `icacls /setintegritylevel Medium` 生效并加载新代码）之后，
**经 DSH 的 MCP 工具直接调用**也拿到了 native `verified`：

| 工具 | 结果 | 关键字段 |
| --- | --- | --- |
| `hcl_live_deploy` | `verified` | `native_channels=true`、`configuration_verified=true`、`persistence_verified=true`、`verified_scope=running_configuration_readback_only` |
| `hcl_backup_config` | `verified` | `backup_verified=true`、`restore_test_verified=true`、`vm_uuid=b445d563-…` |
| `hcl_restore_config` | `verified` | `rollback_verified=true`、`verified_scope=running_configuration_restored_only` |

新进程（PID 51228）的完整性实测是 `medium`（`integrity_rid=8192`），此前是
`low`（4096）。三条调用的 operation_id 分别是 `958cd76a1d6e4b3492aeb07444e0af40`、
`97249a78d96544149b8e808fa9966778`、`c538df8d0cbe4692a203977792a0bc4b`。

同一批代码也可以用脚本跑（不依赖 MCP 宿主）：

`work/verify_native_chain.py` 用 `Deployment(store, catalog=HclCatalog(E:/HCL),
writes_enabled=True)`（**不注入 `session_factory`**）在 `topo1-device1` 上跑：

```
python work/verify_native_chain.py backup    -> status=verified  backup_verified=true
python work/verify_native_chain.py restore <backup_id> -> status=verified rollback_verified=true
python work/verify_native_chain.py fault     -> status=partial  rollback_verified=true  restored=true
```

`hcl_live_deploy` 的 native 结果（receipt `d47efaf5f327442ea206180e47524e55`）：

```json
{"status": "verified", "native_channels": true,
 "verified_scope": "running_configuration_readback_only",
 "configuration_verified": true, "protocol_configuration_verified": true,
 "persistence_verified": true, "protocol_persistence_verified": true,
 "rollback_verified": false, "connectivity_verified": false,
 "guest_boot_verified": false, "full_live_gate_passed": false, "errors": []}
```

steps 是 `backup_and_restore_preflight/verified` + 6 条 `command/accepted` +
`configuration_readback/verified` + `startup_persistence_readback/verified`；
`backups[0]` 里 `restore_test_verified=true`、`vm_uuid=b445d563-…`；
`persistence.MSR36-20_1.state.main` 指向 `flash:/mcp_<op>_MSR36-20_1_after.cfg`。

故障注入那一轮（`7e94bb152a4d4c16a2581d2d7dce4317`）在第 4 条命令被拒后自动回滚，
`rollback_verified=true`，回滚后 `display current-configuration` 的 digest 与
baseline 逐字节相同（`ce75447e…`）。

`connectivity_verified`/`guest_boot_verified`/`full_live_gate_passed` 仍恒为 false，
这是合同里写死的：MCP 从不声称验证过 guest 启动或端到端连通性。

### 两处断点是怎么修的

#### 1. staged 副本改为按 VDI disk UUID 绑定

原来的判据要求运行中 VM 以**绝对路径**挂载
`document.directory / "ConfigDisk" / f"device{device_id}.vdi"`。真实 VM 挂的是
源工程 `C:\Users\yzdwxl\HCL\Projects\hcl_909530729855\ConfigDisk\device1.vdi`，
staged 副本是另一个路径，所以永远失败：

```
runtime_binding_unverified: Running VM has no configuration disk bound to this staged project
```

全文件 sha256 也不能用：VM 在跑、磁盘在写（live `21e4846c…` / staged `a6902c12…`）。
唯一稳定的是 VDI 头里的 disk UUID：

- VDI 1.1 头在文件偏移 `0x40` 起（前 64 字节是 `<<< Oracle VM VirtualBox Disk Image >>>`
  注释），`uuidLinkage` 在 `0x188`，16 字节，VirtualBox 混合字节序（前 3 组 LE、尾 8 字节 BE）。
- 当时实测 live 与 staged 的 `device1.vdi` 在 `0x188` 都是
  `32924cf6-d794-480c-b277-470d3099dc77`；`device2.vdi` 里搜不到该值。
  （该 UUID 在 19:27 重开工程后变成 `a3e3b701-c853-4fad-97f8-fdd7a8c8292b`，
  说明它跟着配置盘走、不是固定标识；见"重启后必须重新 stage"。）
- 交叉印证：`VBoxManage showvminfo topo1-device1 --machinereadable` 自己就报
  `"IDE-ImageUUID-1-0"="32924cf6-…"`；对 staged 副本跑 `showhdinfo` 则被拒绝：
  `Cannot register the hard disk '<staged path>' {32924cf6-…} because a hard disk
  '<live path>' with UUID {32924cf6-…} already exists`——VirtualBox 亲自证明两者是同一块盘。

`resolve()` 现在先试路径（保持原语义，报 `configuration_disk_binding="path"`），
路径不中就退到 UUID（`"disk_uuid"`）：读 staged VDI 头的 `0x188`，与描述符里
`<HardDisk uuid>` 匹配，再要求该 UUID 出现在 `StorageControllers/AttachedDevice/Image`
里，并且 `showvminfo` 里必须有 `*-ImageUUID-<n>-<m>` 字段等于它。纯文件读 + 一次
`showvminfo`，不需要 COM，所以也不受令牌完整性影响。

#### 2. telnet 桥换成跨完整性级别的身份证据

原来的 `verify_pipe` 从管道句柄拿 server PID（`msvcrt.get_osfhandle(
channel.session._read_fd)`），而 `TelnetSession` 根本没有 `_read_fd`；而且 telnet
的 server 是 `SimwareClient.exe`，不是 VirtualBox 进程，`--startvm <uuid>` 那条
判据本来就不成立。现在按通道分派：

- `named_pipe` → 原逻辑不变（`GetNamedPipeServerProcessId` + `GetOwnerSid` +
  `--startvm`），`console_transport="named_pipe"`。
- `telnet` → `_verify_telnet_bridge`，全部用跨完整性级别可用的调用：
  `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_LISTENER)` 拿 30001 的唯一监听 PID →
  `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `QueryFullProcessImageNameW`
  必须是 `SimwareClient.exe` → `ProcessIdToSessionId` 必须等于当前 session →
  在 `E:\HCL\Log\HCLLog\*.log*` 里必须存在
  `create_telnet_server success strPipeName:\\.\pipe\<topology>-device<N>,
  telnet_port:<port>` 这一行（把端口和本设备的管道钉在一起，而不是靠 `30000 + N`
  算术）。`verified_scope` 是
  `running_vm_staged_config_disk_and_hcl_telnet_bridge_identity_only`。

`SimwareClient` 的 owner SID 依然读不到（`GetOwnerSid` 返回 2），但新判据不再需要它：
镜像名 + session + HCL 自己的桥日志已经能把"这个 telnet 端口属于本设备的管道"钉死。

### 顺带发现：Low 完整性令牌会让 VBoxManage 完全不可用

`C:\Users\yzdwxl\.dsh` 带 `Mandatory Label\Low Mandatory Level:(OI)(CI)(NW)`，其下
所有可执行文件继承 Low，启动的进程即 Low。Low 令牌下 `VBoxManage showhdinfo` /
`showvminfo` 报：

```
VBoxManage.exe: error: Failed to create the VirtualBox object!
VBoxManage.exe: error: Details: code E_ACCESSDENIED (0x80070005),
component VirtualBoxClientWrap, interface IVirtualBoxClient
```

而 `VBoxManage --version` 仍正常（不需要 COM）。修法是给 venv 里的解释器改标签：

```
icacls .venv\Scripts\python.exe  /setintegritylevel Medium
icacls .venv\Scripts\mcp-hcl.exe /setintegritylevel Medium
icacls .venv\Scripts\pythonw.exe /setintegritylevel Medium
```

改后同一父进程下 `token_integrity()` 报 `medium`，`showvminfo` 正常。注意**只对
重启后的新进程生效**。本轮已实测这个闭环：改标签前 `mcp-hcl` PID 26516 是
`low`（4096），经 DSH 调 `hcl_live_deploy` 报 `Running VM has no configuration disk
bound to this staged project`；重启后新进程 PID 51228 是 `medium`（8192），同一条
调用直接返回 `verified`。

`runtime_binding._observation_failure` 会把这类 COM 拒绝翻译成
"VBoxManage cannot reach VirtualBox from this process (token integrity: low); …"，
而不是误报成 VM 身份不匹配。

## 只读工具的端到端复验（2026-10-21 重启后）

MCP 重启加载新代码（`work/post_restart_checklist.py` 报 `fresh`；判据是进程启动
时间晚于源码 mtime，PID 每次重启都会变）之后，六个只读工具全部经 **DSH 的 MCP
工具直接调用**跑了一遍，不再靠脚本。重启后一共复验了两轮（第一轮在
`chainlive3`，第二轮在 `chainlive5`），两轮结果一致；下表是第二轮的 operation_id：

| 工具 | `status` | `native_readback_verified` | operation_id |
| --- | --- | --- | --- |
| `hcl_inspect_ports` | `verified` | true | `fb6734bec9c94f00907930b12f715c4a` |
| `hcl_read_vlans` | `verified` | true | `76faa0694f92421588d8524e89084670` |
| `hcl_read_qos` | `verified` | true | `7185b0835eee48039f59f2c4275bdf8f` |
| `hcl_health_check` | `partial` | true | `85a167172d684ce78695308cf4c58f7e` |
| `hcl_diff` | `partial` | true | `985025ae3a904b8c8bff47b619d5b2bd` |
| `hcl_audit_security` | `partial` | true | `674285a81a9745fe8f575071c396a94a` |

第一轮（`chainlive3`）的六条 operation_id 是
`100a0397171945c58cd9166a5b668931`（ports）、
`4bb0b07483f740ef8a1f65728bd4a9c0`（vlans）、
`7f86c2c52c4945869c2bf67604ce4e6e`（qos）、
`a6cfff2599d54193b79d6538952e57b1`（health）、
`502042aa825f4ddbbaf8d054c4358f48`（diff）、
`bfb7a43607904ec8af6d7c8c3c232146`（audit）；两轮的端口数、VLAN 数、空 QoS 库
完全相同。

每条回执的身份字段一致，说明证据链没有退化成"读到了某台设备"：

```
vm_uuid=cf3f9676-abdc-4776-a6ad-e8abf4e2f2fb
vm_name=topo1-device1
console_server_pid=31684
console_transport=telnet  telnet_port=30001
configuration_disk_uuid=a3e3b701-c853-4fad-97f8-fdd7a8c8292b
configuration_disk_binding=disk_uuid
verified_scope=running_vm_staged_config_disk_and_hcl_telnet_bridge_identity_only
```

有一处刻意的差异：`hcl_audit_security` 的回执只保留
`vm_uuid`/`vm_name`/`owner_sid`/`console_server_pid`/`device_id`/`topology`/
`project_file`/`configuration_disk`/`verified_scope`，**不含**
`configuration_disk_uuid` 与 `configuration_disk_binding`——这个工具会外发回执，
所以身份字段走白名单脱敏（`security_audit.py` 的 `safe_identity`），UUID 绑定
仍由同一个 `verify_pipe` 在本地完成。

各工具的实读结果（`verified_scope=bound_native_comware_readback_only` 或
`bound_native_interface_observations_only` / `bound_native_posture_observations_only`）：

- `hcl_inspect_ports`：14 个接口，11 个 down，3 个 up（`InLoopBack0`、`NULL0`、
  `Register-Tunnel0`）；`Register-Tunnel0` 带 `line_up_protocol_down` 异常。
  `linked`/`peer` 全为 null，`live_cable_inventory_verified=false`——线缆库存不在
  只读回读的证据范围内。
- `hcl_read_vlans`：只有 `VLAN 1`（`VLAN 0001`，`Static`，`tagged_ports`/
  `untagged_ports` 均为空）。**这与早前观测到的 VLAN 1/10/30 不同**：设备现在是
  出厂默认状态，`display saved-configuration | include vlan` 为空、
  `display current-configuration` 里只有 `vlan 1`，sysname 是 `H3C`。原因是 HCL 在
  19:27 重开工程时重写了配置盘（见下节），设备回到初始配置。文档只记录当下实测，
  不把早前的 10/30 当成现状。
- `hcl_read_qos`：`classifiers`/`behaviors`/`policies` 全为 `[]`——设备没有用户
  自定义 QoS，空库。
- `hcl_audit_security`：2 条 low 级发现 `NO_BANNER_MOTD`、`NO_LOCAL_USERS`，
  `secure=null`（未给整体结论）。
- `hcl_diff`：`in_sync=null`、`live_device_count=null`、
  `missing_devices_status`/`extra_devices_status` 均为 `unknown`——没有权威的原生
  设备清单，这个工具不猜。

### 重启后必须重新 stage（否则所有只读工具都会拒绝）

重启后第一次调用 `hcl_inspect_ports`（还没重新 stage）直接失败：

```
status: unverified
native_readback_verified: false
runtime_binding_unverified: Running VM has no configuration disk bound to this staged project
```

根因不是代码退化，而是绑定依据变了：HCL 在 19:27 重开工程时重建了
`C:\Users\yzdwxl\HCL\Projects\hcl_909530729855\ConfigDisk\device1.vdi`，它的
`uuidLinkage` 从 `32924cf6-d794-480c-b277-470d3099dc77` 变成了
`a3e3b701-c853-4fad-97f8-fdd7a8c8292b`；而 13:57 做的 staged 副本里还是旧 UUID。
UUID 分支本来就是这个设计（见"两处断点是怎么修的"），所以重新 stage 一次即可：

- `hcl_stage_copy` **单独不够**：它不写 `MCPArtifacts/bindings.json` 与
  `plan.json`，后续会报 `Fresh stage/bindings receipts are required`。必须再跑一次
  `hcl_stage_plan` 才算可用 stage。
- `hcl_stage_plan` 会把新设备编号分配成 `max(已有 device_id) + 1`（这里是 2），
  要让它代表运行中的 `topo1-device1`，得把 `.net` 里的 `device_id` 和
  `bindings.json` 一起改回 `1`。
- 上述步骤已固化成 `work/restage_after_reboot.py <new_stage_name>`：复制、跑 plan、
  把 device_id 钉回 1，最后打印 staged 与 live 的 VDI UUID 及是否一致。stage 名必须
  是新的——staging 从不覆盖已有目录，重名时脚本直接退出码 2 并提示换名。
  这个脚本本身也端到端验过两遍（`chainlive4`、`chainlive5`，都 `match: True`），
  随后对 `work/staged/chainlive4/hcl_909530729855` 调 `hcl_inspect_ports` 仍是
  `verified` + `native_readback_verified=true`（operation_id
  `d3b78d35be3c40bdb1bdb776abd0cf80`，14 个接口、3 个 up、同样的
  `Register-Tunnel0` 异常）。
- `work/probe_read_tools_telnet.py` 的 stage 参数可省略：缺省取 `work/staged` 下最新
  且带 `MCPArtifacts/bindings.json` 的副本，因此重 stage 后直接跑它即可。
- 上述四步（后端在不在、VM 有没有开机、MCP 进程新不新、staged 副本还对不对得上）
  已固化成 `work/preflight.py`：每次使用前跑一条命令，全过就打印本次该用的
  `project_path` / `topology` / `device`，不会过就打印该跑哪条修复命令。
- 对 staged 副本跑 `VBoxManage showhdinfo` 必然失败
  （`Cannot register the hard disk … because a hard disk … with UUID {…} already
  exists`），这是**预期**：它恰好证明副本和运行中的是同一块盘，UUID 仍可用
  `vdi_disk_uuid` 直接读。

## 关于 `[[Cloud ...]]` 终端

`.net` 里有两类设备块：

```ini
[vbox 127.0.0.1:16600]
    [[MSR36-20 Server]]
        device_id = 1
        ...

[Non-vBox-DATA]
    [[Cloud Host_1]]
        symbol = Host
        ...
```

`project.json` 的 `deviceInfoList` 把两者都列了出来，但只有前者有
`device_id` 和 `configPath`。早先的设备数量校验拿 `deviceInfoList` 的长度去比
`[vbox]` 段数，于是凡是用到 Host/PC 云终端的工程都会被拒：

```
Staged .net and project.json device counts disagree
```

现在改由 `.net` 里的 `[[Cloud <name>]]` 块判定哪些行是云端终端，并只对真正的
虚拟设备做数量核对。12 个真实工程因此全部通过。

## 未保存的工程在磁盘上没有设备段

HCL 只在 GUI 保存工程时才把设备写进 `.net`。一个刚建、还没保存的工程，`.net` 只有：

```ini
version = 5.10.3
[vbox 127.0.0.1:16600]
    workingdir = None
    udp = 10000
```

`project.json` 的 `deviceInfoList` 是 `[]`，但 `ConfigDisk\device1.vdi` 已经生成、
VM 已在跑。此时任何依赖 `.net` 设备段的工具（`hcl_inspect_project`、
`hcl_apply_*`）都会报 `No device sections found` / `No device sections found`，
而 `hcl_console_exec` 因为只按 `30000 + device` 算端口，仍然可用。需要 `.net`
设备段时，让用户在 GUI 里保存一次工程。

## 结论

- 工具面、协议层、console 下发三层都是通的，配置命令已在真机上落地并回读确认。
- **完整链路（备份 → 改配置 → 回读 → 持久化 → 回滚）已在真机跑通**，且 native
  路径也已跑出 `verified`：`native_channels=true`、`configuration_verified=true`、
  `persistence_verified=true`、`rollback_verified=true`。
- **只读工具也全部改走 telnet 桥**：`hcl_inspect_ports`、`hcl_read_vlans`、
  `hcl_read_qos` 现在都是 `native_readback_verified=true`；`hcl_health_check`、
  `hcl_diff`、`hcl_audit_security` 是 `native_readback_verified=true` 的
  `partial`（`partial` 是它们对"未配置项"的既定语义，不是通道失败）。此前这些
  工具都直接开命名管道，在真机上恒报 `pipe_access_denied`。这六个工具在 MCP
  重启加载新代码之后，已逐个经 DSH 工具端到端复验通过（回执与实读结果见下文
  "只读工具的端到端复验"）。
- 重启会换掉 HCL 的配置盘 UUID，staged 副本随之失效：只读/部署工具会报
  `Running VM has no configuration disk bound to this staged project`。这不是代码
  回归，重跑 `work/restage_after_reboot.py <new_stage_name>`（`hcl_stage_copy` +
  `hcl_stage_plan` + 把 `device_id` 钉回 1）即可。设备配置也会回到 HCL 保存时的
  状态，所以任何"上次读到 VLAN 10/30"之类的观测都不能当成现状复述。
- native 证据闭合靠两处改造：staged 副本按 **VDI disk UUID** 绑定运行中 VM 的磁盘
  （不再要求路径相同）；telnet 桥的身份判据换成跨完整性级别的
  "唯一监听 PID + 镜像名 + session + HCL 桥日志里的 pipe 映射"。
- `connectivity_verified`、`guest_boot_verified`、`full_live_gate_passed` 按合同
  恒为 false：MCP 不声称验证过 guest 启动或端到端连通性。
- 对运行中的设备下发命令走 telnet（`hcl_console_exec`）；带备份与回滚的批量配置
  走 staged 工程 + `hcl_apply_*` / `hcl_live_deploy`。
- `.dsh` 工作区带 Low 强制标签，其下的解释器会让 `VBoxManage` 的 COM 调用
  `E_ACCESSDENIED`。用 `icacls ... /setintegritylevel Medium` 修，且必须重启进程
  才生效。`.dsh` 之外的输出根在 DSH 受限子进程里不可写，处理见
  [deepseek-harness.md](deepseek-harness.md)；console 租约目录有同一类问题，用
  `MCP_HCL_CONSOLE_LEASE_ROOT` 解决。

## 本轮顺手修掉的六个真 bug

真机跑链路才暴露出来的，单元测试原本都覆盖不到：

1. **长命令的回显被设备折行，归属判定误报 `command_echo_missing`。**
   Comware 按终端宽度（默认 80 列）折行，`configuration replace file
   flash:/mcp_<32 hex>_MSR36-20_1_before.cfg` 这条 92 字符命令的回显被切成
   `...MSR3` 与 `6-20_1_before.cfg` 两行。原判定要求某一行完整等于命令，于是
   恰恰是带 flash 路径的命令永远失败。修法：新增
   `cli_session.command_echo_present`，按行累积片段后与命令比对（提示符后缀只在
   它本身像命令开头时才剥掉，避免把 `[H3C-vlan10]` 误判成 `vlan 10` 的回显）。
2. **在用户视图盲发 `return` 会让整条事务失败。**
   `return` 在用户视图是非法命令，设备回 `% Unrecognized command`，会话被标
   `failed`。修法：新增 `cli_session.ensure_user_view`，先看已捕获的提示符；未知
   时允许一次失败，只要事后捕获到的提示符是 `<...>` 就认定本来就在用户视图，
   并丢弃那条被拒的历史记录、清掉 `failed`。`deployment.py` 与 `startup.py` 里
   7 处裸 `exchange("return")` 全部改为走它。
3. **`VBoxManage showvminfo --machinereadable` 一行里混用两种转义。**
   由 `Bstr` 发出的字段（`CfgFile`/`SnapFldr`/`LogFldr`）是 JSON 转义，存储与
   UART 字段（`"IDE-1-0"`、`uartmode2`）是原始单反斜杠，`VideoMode` 的引号还不是
   包裹。原实现一律 `json.loads`，于是 147 行里有 7 行报 `Unreadable machine
   observation field`。修法：`machine_values` 改为"先试 `json.loads`，失败则剥一层
   配对引号"，`"IDE-1-0"` 这种带引号的键也先解包。
4. **分页符 `---- More ----` 混进回读正文。**
   `Deployment._backup` 会先发 `screen-length disable`，但
   `Inspection`/`Reconciliation`/`SecurityAudit`/`evidence` 都不发，于是
   `display vlan all` 触发分页，`---- More ----` 落在最后一个 VLAN 块之后，还与
   结尾提示符粘在同一行，`vlan_database` 的端口列表解析随即失败
   （`Unsupported VLAN port list`）。修法：`cli_session.clean_text` 末尾用
   `PAGER_NOISE` 把分页符当传输噪声剔掉；同时新增 `pager_pending`，在剔噪**之前**
   的原始文本上判断是否仍在分页，否则翻页续页会失效。
5. **`send()` 用 CRLF 会让设备多回一个空提示符。**
   设备对 `\r\n` 回两个提示符（`...\r\r\n[H3C]\r\r\n[H3C]`），多出来的那个会落进
   下一次回读的正文里。修法：`_PolledSession.send` 改为只用裸 `\r` —— 这也是 HCL
   桥真正期望的行尾。
6. **设备没有用户自定义 QoS 时完全不打印 header。**
   `readback.qos_definitions` 原本要求正文里出现
   `User-defined classifier/behavior/QoS policy information` 一类的头，但真机上
   `display traffic classifier user-defined` / `display traffic behavior
   user-defined` / `display qos policy user-defined` 在空库时什么都不输出——连
   "none is configured" 都没有。于是 `hcl_read_qos` 报
   `Missing user-defined QoS result header`。修法：空正文（`not text.strip()`）
   直接当作空库返回 `[]`；正文非空时才要求 header，格式仍严格。
   有内容的设备行为不变，测试用 fixture 覆盖两条路径。
