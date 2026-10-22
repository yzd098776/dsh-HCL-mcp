# DeepSeek Harness 适配

本仓库默认保持客户端无关：`tools/list` 直接暴露 pydantic 生成的原始 schema，
发现面固定为上游的 6 个 catalog 资源。设置 `MCP_HCL_SCHEMA_MODE=dsh` 后，服务会
把 input schema 投影进 DeepSeek Harness（DSH）接受的 JSON Schema 子集，并额外
暴露两个 schema 资源；本文其余部分描述该模式，以及一份可直接粘贴到 DSH profile
的 MCP 客户端配置。

## 为什么要投影

DSH 的 `assertSupportedJsonSchema` 只接受这些关键字：

```
type / oneOf / properties / required / additionalProperties / items / enum / const
+ 注解 description / title / default / examples
```

它不接受 `$defs`、`$ref`、`anyOf`、`allOf`、`if`、`not`、`discriminator`、
`propertyName`、`minLength`、`maxLength`、`pattern`、`minimum`、`maximum`、
`minItems`、`maxItems`、`format`、`writeOnly`，也不接受 `type` 数组和非布尔的
`additionalProperties`。

pydantic v2 生成的 schema 正好大量使用这些关键字：改造前 55 个工具里 48 个含
不支持的键，`$ref` 386 次、`anyOf` 395 次、`maximum` 358 次。DSH 仍会注册这些
工具，但会把 code-mode（`run_code`）下生成的 SDK 声明降级成 `unknown`
（TypeScript）或 `Any`（Python），模型看不到参数结构。

更实际的问题是体积：8 个 plan 类工具各自内嵌一份完整的 `TopologyPlan`
`$defs`（每份约 21.5 KB，重复 8 次），55 个工具的 schema 合计 **254 KB**。
这段 schema 是每轮请求的固定前缀成本。

投影后：**55 个工具全部通过 DSH 校验器，0 降级，合计约 61 KB**（默认
`MCP_HCL_PLAN_SCHEMA_DEPTH=3`）。

## 投影做了什么

`src/mcp_hcl/dsh_compat.py` 提供两个投影：

| 函数 | 用途 |
|---|---|
| `project` | 解析 `$ref`、内联整个子树、丢弃不支持的键。用于手写的小参数模型。 |
| `outline` | 只保留 `depth` 层结构，更深的节点塌成类型。用于超大 `plan`/`request`。 |

关键取舍：

- `$ref` 递归展开，自引用折叠成 `{"type": "object", "additionalProperties": true}`。
- `anyOf` 只有一个非 `null` 分支时直接内联；多个分支折叠成宽松类型。
- pydantic 判别联合（`discriminator.propertyName = "feature"`）无法用该子集表达，
  改写成 `{type: object, properties: {feature: {type: string, enum: [...10 个 tag]},
  <tag>: {...}, ...}, required: ["feature"], additionalProperties: true}`，
  保住 payload 形状的可发现性，而不是塌成 "any object"。
- 数值/字符串边界（`minimum`/`maxLength`/`pattern` 等）从**展示**层移除，
  但**校验层不受影响**：`StrictFastMCP.call_tool` 始终用 pydantic 模型
  （`extra="forbid"`）校验原始参数，投影只改 `tool.parameters`。

`outline` 的 `depth` 由 `MCP_HCL_PLAN_SCHEMA_DEPTH` 控制（默认 3，范围 1–12）。
深度 3 保留每个顶层 section 及其元素字段名，足够写出 payload；更深的嵌套
（例如 `plan.services` 各 feature 分支内部）会塌成空 object。

## 完整 schema 的出口

投影会让 `tools/list` 丢失精确形状。为此在 `dsh` 模式下额外暴露两个只读资源
（`raw` 模式下不暴露，发现面与上游完全一致）：

| 资源 | 内容 |
|---|---|
| `hcl://schema/tools` | 全部 55 个工具的**未投影** pydantic schema |
| `hcl://schema/tools/{name}` | 单个工具的未投影 schema，未知工具名返回错误 |

`hcl_parity_report` 也改用未投影 schema 做对比，因为对齐声明针对的是真实参数
契约，不是 DSH 投影后的形态。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MCP_HCL_SCHEMA_MODE` | `raw` | `dsh` 开启投影与 schema 资源；`raw` 保持上游 schema 与 6 资源发现面 |
| `MCP_HCL_PLAN_SCHEMA_DEPTH` | `3` | `plan`/`request` 保留的结构层数（1–12），仅在 `dsh` 模式生效 |
| `MCP_HCL_LOG_LEVEL` | `WARNING` | SDK 日志级别。设为 `INFO` 会为每个请求打一行 stderr |
| `MCP_HCL_CONSOLE_LEASE_ROOT` | `<用户主目录>\.mcp_hcl\console-leases` | 原生 console 会话的端点租约目录；受限令牌宿主需指到工作区内 |
| `MCP_HCL_ALLOW_LIVE_WRITE` | 关闭 | 写操作总开关；不开时 `hcl_console_exec` 与 `hcl_apply_*` 一律返回 `writes_disabled` |

其余变量（`MCP_HCL_INSTALL_ROOT`、`MCP_HCL_PROJECT_ROOTS`、
`MCP_HCL_OUTPUT_ROOT`、`MCP_HCL_BACKEND_PORT`、`MCP_HCL_PROFILE_ROOT`）
与上游一致。

`MCP_HCL_LOG_LEVEL` 默认 `WARNING` 的原因：FastMCP 在 `INFO` 下为每个请求打一行
`Processing request of type ...`。stdio 宿主会把这些行收进自己的日志，正常使用
时属于噪声。排查连接问题时再调到 `INFO`。

## 在 DeepSeek Harness 中注册

DSH 用 `@deepseek-ai/dsh-mcp-client` 连接 MCP 服务，工具在模型侧显示为
`mcp__<serverName>__<toolName>`。把下面这段插进 profile 的
`cordis.patch.yml`（`<DSH_HOME>/profiles/<name>/cordis.patch.yml`）：

```yaml
- insert:
    - id: mcp-hcl
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: hcl
        transport: stdio
        command: "C:\\Users\\YOUR_NAME\\.dsh\\work\\MCP_HCL\\.venv\\Scripts\\mcp-hcl.exe"
        args: []
        cwd: "C:\\Users\\YOUR_NAME\\.dsh\\work\\MCP_HCL"
        failOnStartupError: false
        toolCallTimeoutMs: 120000
        env:
          MCP_HCL_INSTALL_ROOT: "E:\\HCL"
          MCP_HCL_PROJECT_ROOTS: "C:\\Users\\YOUR_NAME\\HCL\\Projects"
          MCP_HCL_OUTPUT_ROOT: "C:\\Users\\YOUR_NAME\\.dsh\\work\\MCP_HCL\\work\\staged"
          MCP_HCL_BACKEND_PORT: "16600"
          MCP_HCL_SCHEMA_MODE: "dsh"
          MCP_HCL_PLAN_SCHEMA_DEPTH: "3"
          MCP_HCL_CONSOLE_LEASE_ROOT: "C:\\Users\\YOUR_NAME\\.dsh\\work\\MCP_HCL\\work\\console-leases"
          MCP_HCL_ALLOW_LIVE_WRITE: "1"
```

要点：

- **`env` 必须写全。** DSH 启动 stdio 子进程时以"清洗过的父环境"为基座：删除名字
  匹配 `KEY|PASSWORD|SECRET|TOKEN` 的变量和所有 `DSH_*` 变量，再合并 `config.env`。
  `MCP_HCL_*` 不匹配清洗正则，但父进程里本来也没有它们，所以只能在这里显式给出。
- `command` 指向虚拟环境里的 `mcp-hcl.exe`，不需要 `args` 传模块名。
- `cwd` 决定相对路径 `MCP_HCL_OUTPUT_ROOT` 的落点，建议显式给出绝对路径。
- `serverName: hcl` 决定公开工具名前缀，且必须匹配 `^[A-Za-z0-9_-]{1,32}$`、
  在同一次加载里唯一。
- `MCP_HCL_BACKEND_PORT` 必须是 `16600`：`gui_identity` 校验该端口，改成别的值会
  直接抛 `gui_backend_port_unverified`。它对应 SimwareWrapper 的监听端口。
- **`MCP_HCL_ALLOW_LIVE_WRITE` 是写操作的总开关。** 不写它（默认）时
  `hcl_console_exec` 与 `hcl_apply_*` 一律返回 `writes_disabled`；写上 `"1"` 才会
  真正下发命令、改 ACL/VLAN/NAT、部署、上下电。上面示例是打开的写法；只想读环境
  就把它删掉。

- **`MCP_HCL_OUTPUT_ROOT` 必须落在 DSH 的工作区里面。** 这是本机实测出来的约束：
  DSH 把 stdio 子进程放进受限令牌（低完整性级别）里运行，该令牌只能写工作区
  （`$DSH_HOME`，本机为 `C:\Users\YOUR_NAME\.dsh`）内部。写工作区之外会得到
  `WinError 5 拒绝访问`，读操作不受影响。把输出根指到
  `C:\Users\YOUR_NAME\HCL\Projects\_mcp_staged` 这类外部目录时，
  `tempfile.mkdtemp` 会因为这行判定而空转：

  ```python
  except PermissionError:
      if _os.name == 'nt' and _os.path.isdir(dir) and _os.access(dir, _os.W_OK):
          continue          # 名字冲突才该重试，权限被拒也走到了这里
  ```

  `os.access` 对当前用户报告的仍是可写，于是它认为"只是重名"，一直换名字重试，
  直到调用方超时——表现为 `hcl_stage_copy` 无响应，而不是报权限错误。现在
  `_stage` 会把 `PermissionError` 转成带解释的 `ProjectError`，不会再无响应。
  若确实要把 staged 产物放在 DSH 工作区之外，就得把 MCP 服务器移出受限令牌
  （例如手工运行、或换一个不经受限子进程的宿主）。

- **`MCP_HCL_CONSOLE_LEASE_ROOT` 出于同一原因要指到工作区内。** 原生 console 会话
  会为一根管道取一个独占租约文件，默认落在 `<用户主目录>\.mcp_hcl\console-leases`。
  受限令牌下这个 `mkdir` 直接得到 `WinError 5`，表现为
  `hcl_console_exec` 返回 `invalid_operation: [WinError 5] ...'.mcp_hcl'`。
  该变量把租约目录挪到进程真正能写的地方。

修改 profile 后需要重启 DSH 宿主（Electron 应用）才会重新加载插件。

### 用插件包安装（推荐）

仓库自带 `dsh/` 插件包（config-only bundle：`package.json` + `cordis.patch.yml`，
没有 Host 模块、没有生命周期脚本、不改 Harness 内核），它插入的就是上面这一行：

```powershell
pipx install mcp-hcl                        # 提供 mcp-hcl 控制台脚本（PyPI）
dsh plugin --profile desktop add dsh-mcp-hcl  # 从 npm registry 装插件包
```

插件包已发布到 npm（`dsh-mcp-hcl@0.5.0`），也可以从仓库根目录
`dsh plugin --profile desktop add .`、或 `add .\dsh` 装子目录那份；三条路径装出来的
都是同一行配置。

包里的 `command` 写的是 `mcp-hcl`（依赖 PATH），`env` 只给了
`MCP_HCL_SCHEMA_MODE=dsh` 和默认关闭的 `MCP_HCL_ALLOW_LIVE_WRITE=0`；HCL 路径这类
本机事实必须由使用者补进 profile 的 `cordis.patch.yml`。完整的环境变量表、健康检查
（`dsh --profile <name> --dump-config`）与卸载步骤见 `dsh/README.md`。

安装包不会替使用者装 Python 服务、不会创建工程、不会开机或打开 GUI；`failOnStartupError`
为 `false`，因此没装 HCL 时服务仍能启动，只是原生工具不可用。

## 校验结果

离线（不连设备、不启动 HCL GUI）：

- `MCP_HCL_SCHEMA_MODE=dsh` 下 `tools=55`，用 DSH 的
  `assertSupportedJsonSchema` 逐个断言：`failing=0`、`ts_degraded=0`。
- 原始模式对照：`failing=36`、`ts_degraded=36`。
- 体积：raw 232 KB → `depth=3` 时 61 KB。
- stdio 端到端（`dsh` 模式）：握手 `mcp-hcl 0.5.0`、55 工具、7 资源 + 1 资源模板、
  `contract_version=2` envelope、未知字段被严格拒绝、`hcl_list_devices` 正常返回。
  `raw` 模式下同样 55 工具，但资源回到 6 个、无资源模板。
- 仓库自带测试：`python -m unittest discover -s tests -p "test_*.py"` 在默认 `raw`
  模式下与改造前基线逐项一致（372 tests，10 个预先存在的 Windows lab-environment
  failure，0 新增）。

已知与仓库自带验收清单的差异：`tests/contract_inventory.py` 把发现面钉成
55 工具 / 6 资源。`dsh` 模式会额外暴露 `hcl://schema/tools` 与
`hcl://schema/tools/{name}`，因此该模式下
`test_discovery_surface_matches_pinned_inventory` 等 6 个断言钉死清单的测试会失败。
这是把投影设为 opt-in 的原因：默认路径不改变暴露面，验收基线只在显式选择
`dsh` 模式时才需要随之更新。

## 在真实 HCL 上的验证

DSH 宿主加载本服务器后，工具以 `mcp__hcl__<name>` 出现在会话里。以下结果来自
HCL 正在运行的机器，非离线模拟：

已验证（`status: verified`）：

- `hcl_bridge_status` —— 与 SimwareWrapper 的真实 TCP 会话，`backend_version`
  返回 `1.0.0&6.0.14`，回执里带两帧原始协议响应。
- `hcl_list_projects` —— 读到 12 个真实工程。
- `hcl_list_devices` —— 9 个已装模型（F1060、F1090、MSR36-20、PC、S5820V2-54QS-GE、
  S6850、VPC、VSR-88、AC、AP、Phone），evidence 是 `E:\HCL\devicecfgs\*.cfg` 与
  `deviceInfo.ini` 的 sha256。
- `hcl_inspect_project` —— 解析真实 `.net`，得到工程名、设备与连线。
- `hcl_stage_copy` —— 12 个真实工程全部 staging 成功（4~23 设备不等）。

打开 `MCP_HCL_ALLOW_LIVE_WRITE=1` 之后：

- `hcl_console_exec` —— 在真机 MSR36-20（`topo1` device 1）上返回
  `status=output_received`、`output_bytes=746`、`read_termination=prompt`、
  `command_outcome=response_received`，内容是真实的 `display version` 回执
  （`H3C Comware Software, Version 7.1.064, Release 0427P22`）。
  走的是 HCL 桥接出来的 telnet 端口 `telnet://127.0.0.1:30001`，
  `evidence[0].layer == "console_telnet"`。
- 配置命令序列已实测落地：`system-view` → `vlan 10` → `quit` →
  `display vlan brief` 显示 `VLAN ID 10 (VLAN 0010)`，且配置在连接断开后保留。
- 完整链路（备份 → 改配置 → 回读 → 持久化 → 回滚）已在真机跑通：备份侧
  `protocol_running_backup_verified=true`、`protocol_restore_test_verified=true`；
  部署侧 `protocol_configuration_verified=true`、
  `protocol_persistence_verified=true`；回滚侧显式回滚与故障注入自动回滚都
  `protocol_rollback_verified=true`，回滚后 `display current-configuration`
  的 digest 与基线逐字节相同。复现脚本 `work/verify_full_chain.py`、
  `work/verify_rollback_fault.py`。
- **native 路径同样跑出 `verified`**，而且已经过 DSH 的 MCP 工具端到端确认
  （重启 MCP 让 `icacls /setintegritylevel Medium` 生效后）：
  `hcl_live_deploy` → `native_channels=true`、`configuration_verified=true`、
  `persistence_verified=true`、`verified_scope=running_configuration_readback_only`；
  `hcl_backup_config` → `backup_verified=true`；
  `hcl_restore_config` → `rollback_verified=true`。
  同一批代码也能用脚本跑（`work/verify_native_chain.py`，不注入
  `session_factory`），故障注入那一轮 `rollback_verified=true` 且回滚后 digest
  与基线逐字节相同。
- **只读工具也已改走同一条 telnet 桥**，`native_readback_verified=true`：
  `hcl_inspect_ports`（14 个接口）、`hcl_read_vlans`（VLAN 1）、
  `hcl_read_qos`（设备无用户自定义 QoS，空库）都是 `verified`；
  `hcl_health_check`、`hcl_diff`、`hcl_audit_security` 是 `partial` ——
  这是它们对"未配置项"的既定语义，不是通道问题。此前它们直接开命名管道，
  在真机上恒报 `pipe_access_denied`。这六个工具在 MCP 重启加载新代码后已逐个
  经 DSH 工具端到端复验（重启后跑了两轮，共十二条 operation_id 与实读结果见
  [hcl-live-findings.md](hcl-live-findings.md)）；`work/post_restart_checklist.py`
  可先确认运行中的进程比源码新（MCP 只在进程启动时加载代码，改了源码不重启会
  继续跑旧实现）。重启还会换掉 HCL 配置盘的 UUID，使旧 staged 副本失效，此时用
  `work/restage_after_reboot.py` 重新 stage 即可。

仍未验证：

- `hcl_apply_*` 只接受 staged 工程（需 `MCPArtifacts/bindings.json` 与
  `stage-receipt.json`），对 GUI 里正在跑的工程会报
  `Only staged projects may be deployed`。要带备份与回滚地批量下发，先
  `hcl_stage_copy`。
- `connectivity_verified`、`guest_boot_verified`、`full_live_gate_passed` 恒为
  `false`，这是合同写死的：MCP 从不声称验证过 guest 启动或端到端连通性。

### 每次使用前的一条命令

```powershell
.venv\Scripts\python.exe work\preflight.py
```

它按顺序验四件事，任一步不过就打印修复动作并以非零退出码结束：HCL 后端
`127.0.0.1:16600` 有响应 → 至少一台 HCL VM 在跑 → MCP 进程启动时间晚于最新源码 →
最新 staged 副本的配置盘 UUID 仍被运行中的 VM 挂载。全过时直接打印本次该用的
`project_path` / `topology` / `device`（退出码 0）。

### 一个部署前提：解释器不能带 Low 强制标签

如果 MCP 服务器是从带 `Mandatory Label\Low Mandatory Level` 的目录（例如
`C:\Users\yzdwxl\.dsh`）里启动的，`VBoxManage showvminfo/showhdinfo` 的 COM 调用会
直接 `E_ACCESSDENIED`，native 部署随之报
`Running VM has no configuration disk bound to this staged project`。修法是给 venv
里的可执行文件改标签并**重启** MCP 服务：

```
icacls .venv\Scripts\python.exe  /setintegritylevel Medium
icacls .venv\Scripts\mcp-hcl.exe /setintegritylevel Medium
icacls .venv\Scripts\pythonw.exe /setintegritylevel Medium
```

`runtime_binding.token_integrity()` 会报出当前进程的级别，`_observation_failure`
会把 COM 拒绝翻译成 "VBoxManage cannot reach VirtualBox from this process (token
integrity: low); …"。详见 [hcl-live-findings.md](hcl-live-findings.md)。

## console 通道为什么是 telnet

详见 [hcl-live-findings.md](hcl-live-findings.md)。一句话：设备管道
`\\.\pipe\<topology>-device<N>` 的读端被 HCL 的 SimwareClient 独占，外部进程拿不到；
HCL 把同一根管道桥接成 `telnet://127.0.0.1:(30000 + 设备号)`，那才是 GUI console
窗口用的通道，也是 MCP 现在用的通道。`hcl_console_exec` 的 `pipe` 参数两种写法都收，
传 `topology` + `device` 时默认解析成 telnet 端点。
