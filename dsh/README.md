# dsh-mcp-hcl — DeepSeek Harness bundle

This directory is a **config-only Harness bundle**. It adds one client
connector row to the active DSH profile; the actual work happens in the
`mcp-hcl` Python process that the connector spawns.

- Harness side: registers MCP server `hcl` (plugin `@deepseek-ai/dsh-mcp-client`),
  so every tool is exposed as `mcp__hcl__<tool>`.
- No Harness core file is patched, and nothing is injected into the Harness
  process: no host module, no client module, no lifecycle script.
- The bundle does **not** install the Python server, create HCL projects, start
  VMs, or open a GUI window by itself.

## Prerequisites

| Requirement | Notes |
|---|---|
| DeepSeek Harness | 0.1.0-rc.7 or newer, no upper bound; compose verified with the 0.1.0-rc.7 CLI and the 0.2.1-rc.2 desktop app, and `dsh plugin` requires `--profile <name>` |
| Python | 3.11+ |
| `mcp-hcl` on `PATH` | `pipx install mcp-hcl`, or `uv tool install mcp-hcl`, or point `command:` at an absolute `.exe` |
| HCL / H3C Cloud Lab | Windows only, for the native (live-lab) tools; offline planning and staging work without it |

## Install

From a checkout of this repository:

```powershell
pipx install mcp-hcl                    # provides the `mcp-hcl` console script
dsh plugin --profile desktop add .\dsh   # register the bundle in a profile
```

From a published tarball: `dsh plugin --profile desktop add dsh-mcp-hcl-0.5.0.tgz`.

Then edit the generated row in
`<DSH_HOME>\profiles\<profile>\cordis.patch.yml` (or the bundle copy before
install) and fill in the HCL paths that no bundle can guess:

```yaml
        env:
          MCP_HCL_SCHEMA_MODE: dsh
          MCP_HCL_ALLOW_LIVE_WRITE: "0"
          MCP_HCL_INSTALL_ROOT: "E:\\HCL"                    # HCL install dir
          MCP_HCL_PROJECT_ROOTS: "C:\\Users\\me\\HCL\\Projects"
          MCP_HCL_OUTPUT_ROOT: "D:\\mcp-hcl\\staged"          # writable
```

Restart the Harness (or re-apply the profile). Tool calls are then available as
`mcp__hcl__hcl_plan_topology`, `mcp__hcl__hcl_read_vlans`, and so on.

If `mcp-hcl` is not on the Harness PATH, set `command:` to the absolute
console-script path instead, for example
`C:\Users\me\.local\bin\mcp-hcl.exe` (pipx) or
`C:\...\venv\Scripts\mcp-hcl.exe` (venv install).

## Configuration reference

| Env var | Default | Meaning |
|---|---|---|
| `MCP_HCL_SCHEMA_MODE` | `full` | `dsh` projects tool schemas to the shapes the Harness accepts; `full` returns the raw JSON Schema |
| `MCP_HCL_ALLOW_LIVE_WRITE` | `0` | `1` enables device-mutating tools (apply/deploy/restore/power/console); default refuses them |
| `MCP_HCL_INSTALL_ROOT` | unset | HCL install directory (device templates, `deviceInfo.ini`, logs) |
| `MCP_HCL_PROJECT_ROOTS` | unset | Colon/semicolon-separated roots a project may be read from |
| `MCP_HCL_OUTPUT_ROOT` | unset | Where staged project copies are written |
| `MCP_HCL_BACKEND_PORT` | `16600` | HCL simware backend port (loopback only) |
| `MCP_HCL_CONSOLE_LEASE_ROOT` | unset | Console lease/audit directory for telnet sessions |
| `MCP_HCL_PLAN_SCHEMA_DEPTH` | `0` | Schema depth used by plan validation (`3` matches the Harness projection) |

## Health check

```powershell
dsh --profile desktop --dump-config     # the composed tree shows the `mcp-hcl` row
```

or, with `MCP_HCL_SCHEMA_MODE=dsh`, verify 55 tools are advertised; the
read-only ones carry `readOnlyHint: true` and device-mutating ones
`destructiveHint: true`. If the Harness reports a connection error, run
`mcp-hcl` manually — a missing HCL install only disables the native tools, it
does not prevent startup.

## Uninstall

```powershell
dsh plugin --profile desktop remove dsh-mcp-hcl
pipx uninstall mcp-hcl
```

Removing the bundle only deletes the connector row. Staged project copies and
receipts under `MCP_HCL_OUTPUT_ROOT` are ordinary files: delete them yourself
if you no longer need the evidence trail.

## Permissions

- **Network**: loopback only — `127.0.0.1:16600` (HCL backend) and
  `127.0.0.1:30001-31024` (HCL telnet bridge). No outbound internet access.
- **Files**: reads `MCP_HCL_INSTALL_ROOT` and `MCP_HCL_PROJECT_ROOTS`; writes
  only under `MCP_HCL_OUTPUT_ROOT`, `MCP_HCL_CONSOLE_LEASE_ROOT`, and the
  project directory named by a staging/deploy request.
- **Processes**: launches no Windows UI automation and no `powershell.exe`
  child by default. Two opt-in helpers (`hcl_open_project`, `hcl_save_project`)
  drive an already-running HCL GUI and require
  `MCP_HCL_ALLOW_LIVE_WRITE=1` plus a matching stage receipt.
- **Tokens/credentials**: none. Device logins are the lab's own accounts inside
  the emulated network; the server stores no secrets.
- **Lifecycle scripts**: none in this bundle (`package.json` has no `scripts`,
  no `postinstall`).
