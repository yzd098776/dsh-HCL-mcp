# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-10-21

First public release: the packaging, Harness plugin bundle and tool safety
metadata are new; the behaviour below was developed and verified against a real
Windows HCL installation before the repository was published.

Published as `mcp-hcl==0.5.0` on PyPI (wheel and sdist) with a matching
`v0.5.0` GitHub Release; `pipx install mcp-hcl` installs the `mcp-hcl` server,
and the bundle in `dsh/` is installed from the repository checkout.

### Added

- **DeepSeek Harness plugin bundle**: root `package.json` plus
  `dsh/cordis.patch.yml` registering `@deepseek-ai/dsh-mcp-client` as the `hcl`
  server (`dsh/package.json` keeps the same bundle installable when only the
  subdirectory is shipped). Config-only: no Host module, no lifecycle script, no Harness core
  patch.
- **Tool safety annotations**: all 55 tools now advertise
  `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`
  (24 read-only, 14 destructive device-mutating, 17 other), validated against
  the advertised inventory by `tests/test_mcp_contract.py`.
- **Harness schema projection** (`MCP_HCL_SCHEMA_MODE=dsh`): tools/list is
  projected to the schema keywords the Harness validator accepts (254 KB →
  61 KB, 55/55 accepted), with the unprojected schemas still available from the
  `hcl://schema/tools` resource.
- Release automation (`.github/workflows/release.yml`): tag → wheel/sdist,
  SHA-256 checksums, GitHub Release, PyPI trusted publishing.

### Fixed

Real-machine bugs found only by running against HCL, not by the offline suite:

- Read-only tools opened the HCL named pipe directly, which always fails on a
  live host (`pipe_access_denied`/`WinError 5`); they now resolve the telnet
  bridge like the write path.
- Pager pages leaked into console output (`---- More ----` glued to the final
  prompt), corrupting VLAN port parsing; pager noise is now stripped and a
  pending page is detected *before* stripping.
- `_PolledSession.send` used CRLF, so the device answered with one extra
  prompt that landed in the next read; it now sends a bare `\r`.
- A device with no user-defined QoS prints no header at all;
  `qos_definitions` treated that as a missing result and failed. Empty bodies
  are now an empty database, and the header is still required for non-empty
  output.
- Long commands wrapped at 80 columns were misread as a missing echo; the echo
  check now tolerates wrapping.
- A blind `return` after configuration was sent before the device had finished
  rendering the previous prompt; the session now re-enters the user view
  explicitly.
- `machine_values` mixed JSON and raw escaping, producing unparsable values for
  some displays.

- npm bundle publishing: root `package.json` is publishable as `dsh-mcp-hcl`
  (the packed tarball contains only `package.json`, `LICENSE`, `README.md`
  and `dsh/`), so the plugin installs with
  `dsh plugin --profile desktop add dsh-mcp-hcl`; the release workflow has
  an opt-in `npm` job (`NPM_PUBLISH=true` + `NPM_TOKEN`).

### Documentation

- New live-lab evidence document (`docs/hcl-live-findings.md`) covering the
  telnet-vs-pipe channel, VDI config-disk UUID binding, the restage-after-reboot
  procedure, the identity evidence chain and the six real-machine bugs.
- Harness adaptation guide (`docs/deepseek-harness.md`) with the projection
  rationale, environment table and bundle install steps.

### Known limitations

- Windows is required for live-lab tools; a restart of HCL invalidates a staged
  copy and the copy must be re-staged.
- `connectivity_verified`, `guest_boot_verified` and `full_live_gate_passed`
  stay false: the server does not claim what it cannot prove.
- Upstream MCP-Packet-Tracer parity remains incomplete (12 implemented / 47
  planned / 3 unverified tools).
- Some tests execute signed PowerShell helpers and therefore need an execution
  policy that permits them.
