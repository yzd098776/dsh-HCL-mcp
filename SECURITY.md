# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.5.x | yes |
| < 0.5 | no (pre-release history) |

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

- Preferred: GitHub private advisory —
  <https://github.com/cheese-sansan/MCP_HCL/security/advisories/new>
- Include: affected version, Windows version, the tool names involved, whether
  `MCP_HCL_ALLOW_LIVE_WRITE` was `1`, and a minimal reproduction (command or
  tool call plus the receipt directory).
- Expect an acknowledgement within 3 business days. Please give us 90 days
  before public disclosure, or less if a fix ships earlier.

## Scope

This server runs on the same machine as HCL. The security-relevant surfaces are:

1. **Live write gate.** `MCP_HCL_ALLOW_LIVE_WRITE` defaults to `0`. A bug that
   lets a device-mutating tool run while the gate is closed is in scope.
2. **Staged-project protection.** Destructive tools must refuse the original
   HCL project and require a stage receipt plus VM/config-disk identity
   binding. Escaping that check is in scope.
3. **Path handling.** Reads are limited to `MCP_HCL_INSTALL_ROOT` and
   `MCP_HCL_PROJECT_ROOTS`; writes to `MCP_HCL_OUTPUT_ROOT`,
   `MCP_HCL_CONSOLE_LEASE_ROOT` and the project named in the request. Path
   traversal or writing outside those roots is in scope.
4. **Console sessions.** The telnet bridge listens on loopback only. A way to
   make the server connect somewhere else, or to leak one console lease into
   another device, is in scope.
5. **GUI relay (opt-in).** `hcl_open_project` / `hcl_save_project` drive an
   already-running HCL GUI and require `MCP_HCL_ALLOW_LIVE_WRITE=1` plus a
   matching stage receipt. Identity-check bypasses are in scope.

## Not in scope

- HCL, VirtualBox or H3C emulated images and their own vulnerabilities.
- A user who sets `MCP_HCL_ALLOW_LIVE_WRITE=1` and then points the server at a
  production network. This is a lab tool; the gate is a guard rail, not an
  authorization system.
- Denial of service against the local machine by a user who can already run
  arbitrary commands as that user.
- Missing vendor feature parity (see the limitations in
  [CHANGELOG.md](CHANGELOG.md)).

## Hardening notes for operators

- Keep `MCP_HCL_ALLOW_LIVE_WRITE=0` unless a task truly needs device changes.
- Point `MCP_HCL_PROJECT_ROOTS` at the HCL project directory only.
- Treat receipts under `MCP_HCL_OUTPUT_ROOT` as an audit trail: they record
  identity and scope, and are the fastest way to explain a change.
- HCL itself may need an elevated GUI; this server never requires elevation and
  does not automate an elevated window.
