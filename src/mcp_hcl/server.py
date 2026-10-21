"""MCP stdio façade for evidence-backed HCL project staging."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

from mcp_hcl import __version__, dsh_compat
from mcp_hcl import console as hcl_console
from mcp_hcl.api_compatibility import api_compatibility_report
from mcp_hcl.artifact_store import ArtifactRetries
from mcp_hcl.backend import BackendError, HclBackend
from mcp_hcl.build_pipeline import register_build_tool
from mcp_hcl.catalog import CatalogError, HclCatalog
from mcp_hcl.contracts import StrictFastMCP
from mcp_hcl.deployment import Deployment, register_deployment_tools
from mcp_hcl.gui_relay import HclGuiRelay, register_gui_tools
from mcp_hcl.inspection import Inspection, register_inspection_tools
from mcp_hcl.offline_tools import register_offline
from mcp_hcl.parity import REGISTERED_RESOURCES, REGISTERED_TOOLS, parity_report
from mcp_hcl.policy import Policy, register_policy_tools
from mcp_hcl.project import ProjectError, ProjectStore
from mcp_hcl.project_edit import register_edit_tools
from mcp_hcl.reconciliation import Reconciliation, register_reconciliation_tools
from mcp_hcl.security_audit import SecurityAudit, register_security_audit_tool
from mcp_hcl.topology import validate_plan

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def log_level() -> str:
    """SDK log level for the stdio server, defaulting to WARNING.

    FastMCP logs one ``Processing request of type ...`` line per request at
    INFO. Under a stdio host such as DeepSeek Harness that noise lands in the
    host log for every call, so the default is WARNING; set
    ``MCP_HCL_LOG_LEVEL=INFO`` or ``DEBUG`` when troubleshooting.
    """
    level = os.environ.get("MCP_HCL_LOG_LEVEL", "WARNING").strip().upper()
    return level if level in _LOG_LEVELS else "WARNING"


def live_writes_enabled() -> bool:
    """Single master switch for every action that changes HCL or device state."""
    return os.environ.get("MCP_HCL_ALLOW_LIVE_WRITE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def console_lease_root() -> Path:
    """Directory holding the per-pipe console leases.

    Native console sessions take a per-user endpoint lease so two MCP_HCL
    processes cannot drive the same device console at once. The default lives
    under the user profile, which is unreachable for a host that runs the
    server in a restricted or low-integrity token: there only the host's own
    workspace is writable, and the ``mkdir`` fails with ``WinError 5``.
    ``MCP_HCL_CONSOLE_LEASE_ROOT`` moves the leases somewhere the process can
    actually write; relative paths resolve against the current directory.
    """
    configured = os.environ.get("MCP_HCL_CONSOLE_LEASE_ROOT", "").strip()
    if configured:
        return Path(configured)
    return Path.home() / ".mcp_hcl" / "console-leases"


def create_store() -> ProjectStore:
    output_root = Path(os.environ.get("MCP_HCL_OUTPUT_ROOT", "work/staged"))
    configured = os.environ.get("MCP_HCL_PROJECT_ROOTS", "")
    project_roots = [Path(item) for item in configured.split(";") if item.strip()]
    if not project_roots:
        project_roots = [Path.home() / "HCL" / "Projects"]
    return ProjectStore(project_roots=project_roots, output_root=output_root)


def create_server(
    store: ProjectStore | None = None,
    backend: HclBackend | None = None,
    catalog: HclCatalog | None = None,
    console: hcl_console.HclConsole | None = None,
) -> FastMCP:
    store = store or create_store()
    writes = live_writes_enabled()
    backend = backend or HclBackend(
        port=int(os.environ.get("MCP_HCL_BACKEND_PORT", "16600")), allow_writes=writes
    )
    catalog = catalog or HclCatalog(Path(os.environ.get("MCP_HCL_INSTALL_ROOT", "D:/HCL")))
    console = console or hcl_console.HclConsole(
        writes_enabled=writes,
        lease_root=store.output_root / ".console-leases",
        native_lease_root=console_lease_root(),
    )
    # The wrapper keys a topology to the TCP connection, so at most one backend
    # session is held at a time: the one hcl_backend_topology opened.
    session_lock = threading.Lock()
    session_state: dict[str, Any] = {
        "session": None,
        "topology": None,
        "project": None,
        "commands": [],
    }
    mcp = StrictFastMCP(
        name="mcp-hcl",
        instructions=(
            "HCL topology changes are staged as separate project files. "
            "After staging, the user must open the returned .net file in HCL. "
            "A staged result is not a live-canvas result."
        ),
        # The SDK logs one "Processing request of type ..." line per request at
        # INFO. Under a stdio host such as DeepSeek Harness that lands in the
        # host log for every call, so default to WARNING and keep DEBUG/INFO
        # available through the environment for troubleshooting.
        log_level=log_level(),
    )
    # FastMCP 1.x has no version constructor argument. Its underlying Server
    # exposes the version used by initialize; keep the handshake project-specific.
    mcp._mcp_server.version = __version__
    mcp.artifact_retries = ArtifactRetries(store.output_root, store.project_roots)

    @mcp.tool()
    def hcl_capabilities() -> dict[str, Any]:
        """Describe verified capabilities and the activation boundary."""
        return {
            "version": __version__,
            "project_inspection": "available",
            "stage_device_and_link": "available",
            "gui_activation": "requires opening the staged .net project in HCL",
            "live_canvas_api": "not verified and not exposed",
            "device_start_and_console": "gated primitives; guest and CLI effects unverified",
            "console_transport": (
                "telnet bridge on 127.0.0.1:(30000 + device number); the device pipe itself "
                "is held read-only by HCL's SimwareClient and rejects a second opener"
            ),
            "output_root": str(store.output_root),
            "live_gate": {"state": "not_passed", "full_live_gate_passed": False},
            "registered_tools": sorted(REGISTERED_TOOLS),
            "registered_resources": sorted(REGISTERED_RESOURCES),
            "upstream_parity": "incomplete; call hcl_parity_report for the pinned 62-tool inventory",
            "backend_probe": "read-only version and memory; does not prove live canvas control",
            "control_plane": (
                "observed SimwareWrapper verbs plus the telnet device console HCL bridges "
                "from each device pipe; every state-changing action is disabled unless "
                "MCP_HCL_ALLOW_LIVE_WRITE=1"
            ),
            "catalog": "local model defaults and port families; runtime firmware unverified",
            "offline_layer": {
                "typed_service_generators": [
                    "dhcp",
                    "ospf",
                    "rip",
                    "vlan",
                    "acl",
                    "nat",
                    "security",
                    "wireless",
                    "qos",
                    "netstream",
                ],
                "file_executor": "hcl_stage_plan",
                "operation_manifests_executable": False,
                "full_p1_acceptance": "incomplete: AC/AP default CLI mappings and per-model NIC mode compatibility are unverified",
            },
        }

    @mcp.resource("hcl://capabilities")
    def capabilities_resource() -> str:
        """Read the same capability boundaries exposed by hcl_capabilities."""
        return json.dumps(hcl_capabilities(), ensure_ascii=False)

    def catalog_read(action: Any) -> dict[str, Any]:
        try:
            return action()
        except (CatalogError, OSError, UnicodeError) as exc:
            return {
                "status": "partial",
                "operation_id": uuid4().hex,
                "target_project": None,
                "error": str(exc),
                "runtime_verified": False,
                "evidence": [],
            }

    @mcp.tool()
    def hcl_list_devices() -> dict[str, Any]:
        """List installed HCL model defaults, with file evidence and firmware boundaries."""
        return catalog_read(catalog.devices)

    @mcp.tool()
    def hcl_get_device_details(model: str) -> dict[str, Any]:
        """Read one installed model's default interfaces; never claim guest CLI verification."""
        return catalog_read(lambda: catalog.device_details(model))

    @mcp.tool()
    def hcl_validate_plan(
        plan: dict[str, Any], validation_level: Literal["topology", "configuration"] = "topology"
    ) -> dict[str, Any]:
        """Validate intent and, by default, require a connected declared wired topology.

        Use Comware interface names, not .net scene port keys. Validation does
        not prove firmware support, deployment or actual network connectivity.
        configuration checks individual intents without requiring one wired component.
        """
        return validate_plan(plan, catalog, validation_level=validation_level)

    @mcp.resource("hcl://catalog/devices")
    def devices_resource() -> str:
        return json.dumps(hcl_list_devices(), ensure_ascii=False)

    @mcp.resource("hcl://catalog/ports")
    def ports_resource() -> str:
        return json.dumps(catalog_read(catalog.ports), ensure_ascii=False)

    # The schema resources exist to compensate for the DeepSeek Harness
    # projection, so they are only advertised when that projection is active.
    # In the default ``raw`` mode the discovery surface stays exactly the six
    # pinned catalog resources.
    if dsh_compat.schema_mode() == "dsh":

        @mcp.resource("hcl://schema/tools")
        def tool_schemas_resource() -> str:
            """Full pydantic input schemas, before DeepSeek Harness projection.

            The advertised `tools/list` schemas are projected into the DeepSeek
            Harness keyword subset, which drops `$defs`/`$ref`/bounds. Read this
            resource when the exact accepted shape of a payload matters.
            """
            raw = mcp.raw_parameters_all()
            return json.dumps(
                {
                    "contract_version": 2,
                    "schema_mode": dsh_compat.schema_mode(),
                    "note": (
                        "Advertised tools/list schemas are projected for DeepSeek Harness; "
                        "these are the unprojected pydantic schemas used for validation."
                    ),
                    "tools": raw,
                },
                ensure_ascii=False,
            )

        @mcp.resource("hcl://schema/tools/{name}")
        def tool_schema_resource(name: str) -> str:
            """Unprojected input schema for one tool, addressed by tool name."""
            raw = mcp.raw_parameters(name)
            if raw is None:
                raise ValueError(f"unknown tool: {name}")
            return json.dumps(raw, ensure_ascii=False)

    @mcp.tool()
    async def hcl_parity_report() -> dict[str, Any]:
        """Read the pinned mapping and API differences against current MCP discovery."""
        report = parity_report()
        # Compare against the unprojected schemas: the parity claim is about the
        # real argument contract, not the DeepSeek Harness projection of it.
        raw = mcp.raw_parameters_all()
        report["api_compatibility"] = api_compatibility_report(
            report,
            {tool.name: raw.get(tool.name, tool.inputSchema) for tool in await mcp.list_tools()},
            {str(resource.uri) for resource in await mcp.list_resources()},
        )
        return report

    @mcp.tool()
    def hcl_bridge_status() -> dict[str, Any]:
        """Probe the local HCL backend; never infer GUI or CLI control from it."""
        return backend.status()

    @mcp.tool()
    def hcl_stage_copy(
        project_path: str, stage_name: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """Create an isolated HCL project for safe live-gate verification."""
        try:
            result = store.stage_copy(project_path, stage_name)
            return {
                **result,
                "operation_id": uuid4().hex,
                "target_project": result["project_file"],
                "evidence": [{"source": result["project_file"], "layer": "saved_project"}],
                "full_live_gate_passed": False,
            }
        except (ProjectError, OSError) as exc:
            return {
                "status": "rejected",
                "error": str(exc),
                "operation_id": uuid4().hex,
                "target_project": project_path,
                "evidence": [],
            }

    @mcp.tool()
    def hcl_list_projects() -> dict[str, Any]:
        """List HCL projects immediately under configured project roots."""
        return {"projects": store.list_projects()}

    @mcp.tool()
    def hcl_inspect_project(project_path: str) -> dict[str, Any]:
        """Read devices and links from one saved HCL project without changing it."""
        try:
            return {"status": "ok", **store.inspect(project_path)}
        except (ProjectError, OSError) as exc:
            return {"status": "rejected", "error": str(exc)}

    @mcp.tool()
    def hcl_stage_add_device(
        project_path: str,
        stage_name: str,
        template_device: str,
        new_name: str,
        x: float,
        y: float,
        connect_to: str | None = None,
        connect_to_port: str | None = None,
        new_port: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Copy a project, add a device cloned from a known model, and optionally link it."""
        try:
            return store.stage_add_device(
                project_path,
                stage_name,
                template_device,
                new_name,
                x,
                y,
                connect_to,
                connect_to_port,
                new_port,
            )
        except (ProjectError, OSError) as exc:
            return {"status": "rejected", "error": str(exc)}

    @mcp.tool()
    def hcl_stage_link(
        project_path: str,
        stage_name: str,
        a: str,
        a_port: str,
        b: str,
        b_port: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Copy a project and add a reciprocal link between two free ports."""
        try:
            return store.stage_link(project_path, stage_name, a, a_port, b, b_port)
        except (ProjectError, OSError) as exc:
            return {"status": "rejected", "error": str(exc)}

    # ---------------------------------------------------------- control plane
    # Everything below talks to HCL's own backend or to a device console.  It is
    # NOT upstream parity: no parity row becomes "implemented" because a tool
    # exists here.  All state-changing actions need MCP_HCL_ALLOW_LIVE_WRITE=1.

    def _rejected(code: str, message: str, operation: str) -> dict[str, Any]:
        return {
            "status": "rejected",
            "operation": operation,
            "operation_id": uuid4().hex,
            "target_project": None,
            "evidence": [],
            "errors": [{"code": code, "message": message}],
            "runtime_verified": False,
            "full_live_gate_passed": False,
        }

    @mcp.tool()
    def hcl_backend_capabilities() -> dict[str, Any]:
        """List the observed backend verbs, their evidence level and gating state.

        Read-only and offline: it never opens a socket. The verb list comes from
        HCL's own logs, not from vendor documentation.
        """
        verbs = backend.verbs()
        return {
            "status": "verified",
            "operation": "backend_verb_registry",
            "operation_id": uuid4().hex,
            "target_project": None,
            "verified_scope": "observed_verb_registry_only",
            "evidence": [{"source": "HANDOFF.md section 6", "layer": "documentation"}],
            "endpoint": {"host": backend.host, "port": backend.port},
            "write_verbs_enabled": backend.allow_writes,
            "console_exec_enabled": console.writes_enabled,
            "console_transport": (
                "telnet bridge on 127.0.0.1:(30000 + device number); the device pipe itself "
                "is held read-only by HCL's SimwareClient and rejects a second opener"
            ),
            "topology_session_open": session_state["session"] is not None,
            "invocable_count": sum(1 for row in verbs if row["enabled"]),
            "verbs": verbs,
            "runtime_verified": False,
            "full_live_gate_passed": False,
            "limitations": [
                "The verb list is observed behaviour, not a vendor specification.",
                "An accepted command proves the backend accepted it; it does not prove that a "
                "guest booted, that a command reached a device, or that anything is reachable.",
                "Verbs marked invocable=false were observed but are refused because their field "
                "semantics are not understood well enough to reproduce safely.",
            ],
        }

    @mcp.tool()
    def hcl_backend_topology(
        action: Literal["open", "close", "status"],
        topology: str | None = None,
        working_directory: str | None = None,
        project_name: str | None = None,
        udp_port: int = 10000,
    ) -> dict[str, Any]:
        """Open, inspect or close the backend topology session (state-changing).

        The wrapper keys its topology to the **connection**, so opening keeps one
        connection alive and reuses it for ``hcl_device_power``. At most one
        session is held at a time.

        ``action`` is ``open``, ``close`` or ``status``. ``open`` needs
        ``topology`` and a ``working_directory`` inside a configured project root.
        """
        with session_lock:
            if action in {"close", "status"} and any(
                v is not None for v in (topology, working_directory, project_name)
            ):
                return _rejected(
                    "invalid_arguments",
                    "close/status do not accept topology, working_directory or project_name",
                    "backend_topology",
                )
            if action in {"close", "status"} and udp_port != 10000:
                return _rejected(
                    "invalid_arguments", "udp_port belongs to action=open", "backend_topology"
                )
            if action == "status":
                return {
                    "status": "verified" if session_state["session"] else "unverified",
                    "operation": "backend_topology_status",
                    "operation_id": uuid4().hex,
                    "target_project": session_state["project"],
                    "topology": session_state["topology"],
                    "session_open": session_state["session"] is not None,
                    "commands_sent": list(session_state.get("commands", [])),
                    "evidence": [],
                    "runtime_verified": False,
                    "full_live_gate_passed": False,
                }
            if not backend.allow_writes:
                # The master switch is checked before any state, so a disabled
                # server can never look like it merely lacks a session.
                return _rejected(
                    "writes_disabled",
                    "This verb changes HCL state. Start the server with "
                    "MCP_HCL_ALLOW_LIVE_WRITE=1 after an explicit decision.",
                    "backend_topology",
                )
            if action == "close":
                session = session_state["session"]
                if session is None:
                    return _rejected(
                        "no_open_session", "No backend topology session is open", "backend_topology"
                    )
                try:
                    result = session.close_topology()
                finally:
                    session.close()
                    session_state.update(session=None, topology=None, project=None, commands=[])
                return result
            if action != "open":
                return _rejected(
                    "unknown_action",
                    "action must be 'open', 'close' or 'status'",
                    "backend_topology",
                )
            if session_state["session"] is not None:
                return _rejected(
                    "session_already_open",
                    "Close the current backend topology session first",
                    "backend_topology",
                )
            if not topology or not working_directory:
                return _rejected(
                    "missing_argument",
                    "action='open' needs topology and working_directory",
                    "backend_topology",
                )
            try:
                resolved = Path(working_directory).resolve()
            except (OSError, ValueError) as exc:
                return _rejected(
                    "invalid_argument",
                    f"Cannot resolve working_directory: {exc}",
                    "backend_topology",
                )
            if not any(resolved.is_relative_to(root) for root in store.project_roots):
                return _rejected(
                    "outside_configured_roots",
                    "working_directory must be inside a configured project root",
                    "backend_topology",
                )
            try:
                session = backend.connect()
            except BackendError as exc:
                return _rejected("backend_unreachable", str(exc), "backend_topology")
            result = session.open_topology(
                topology, str(resolved), project_name or resolved.name, udp_port
            )
            if result.get("status") != "accepted":
                session.close()
                return result
            session_state.update(
                session=session,
                topology=topology,
                project=str(resolved),
                commands=list(session.commands_sent),
            )
            result["target_project"] = str(resolved)
            result["session_open"] = True
            result["next_step"] = (
                "Topology verbs must use this session; the wrapper keys the "
                "topology to the connection."
            )
            return result

    @mcp.tool()
    def hcl_device_power(device: int, on: bool = True) -> dict[str, Any]:
        """Power one device on through the observed backend verb (gated).

        Requires an open session from ``hcl_backend_topology``: the wrapper keys
        the topology to the connection, so a one-shot request is answered with
        ``errorCode=228``. Only power-on was observed - no stop verb exists in
        the logs, so ``on=False`` is reported as unsupported rather than guessed.
        """
        if not backend.allow_writes:
            return _rejected("writes_disabled", "Live writes are disabled", "device_power")
        if on is not True:
            return _rejected(
                "unsupported",
                "No power-off verb was observed in HCL's logs. Only power-on is implemented; "
                "guessing a stop command could leave a VM in an unknown state.",
                "device_power",
            )
        if not backend.allow_writes:
            return _rejected(
                "writes_disabled",
                "This verb changes HCL state. Start the server with "
                "MCP_HCL_ALLOW_LIVE_WRITE=1 after an explicit decision.",
                "device_power",
            )
        with session_lock:
            session = session_state["session"]
            if session is None:
                return _rejected(
                    "no_open_session",
                    "Open a backend topology session first: hcl_backend_topology "
                    "action='open' (the topology context is per connection)",
                    "device_power",
                )
            result = session.invoke("start", {"device": device})
            result["target_project"] = session.project_directory
            session_state["commands"] = list(session.commands_sent)
            result["topology"] = session.topology
            return result

    @mcp.tool()
    def hcl_console_exec(
        command: str,
        pipe: str | None = None,
        topology: str | None = None,
        device: int | None = None,
        expect: str | None = None,
    ) -> dict[str, Any]:
        """Send one line to a device console over HCL's telnet bridge (gated).

        Supply either ``pipe`` (``\\\\.\\pipe\\<topology>-device<N>`` or
        ``telnet://127.0.0.1:<30001..31024>``) or ``topology`` plus ``device``;
        the default endpoint for a topology is the telnet bridge HCL publishes
        for that device. Output that arrived before the command is drained and
        reported as ``discarded_stale_bytes`` so stale console text cannot be
        mistaken for this request's answer.
        """
        if pipe is not None and (topology is not None or device is not None):
            return _rejected(
                "invalid_arguments",
                "pipe and topology/device are mutually exclusive",
                "console_exec",
            )
        if not console.writes_enabled:
            return _rejected("writes_disabled", "Live writes are disabled", "console_exec")
        if pipe is None:
            if topology is None or device is None:
                return _rejected(
                    "missing_argument", "Supply either pipe, or topology and device", "console_exec"
                )
            try:
                pipe = hcl_console.resolve_endpoint(topology, device)
            except hcl_console.ConsoleError as exc:
                return _rejected("invalid_argument", str(exc), "console_exec")
        return console.run(pipe, command, expect=expect, topology_hint=topology)

    register_offline(mcp, catalog, store, catalog_read)
    relay = HclGuiRelay(store, writes_enabled=writes, installation_root=catalog.installation)
    register_gui_tools(mcp, relay)
    register_build_tool(mcp, store, catalog, relay)
    register_edit_tools(mcp, store, catalog)
    register_deployment_tools(mcp, Deployment(store, catalog=catalog, writes_enabled=writes))
    register_inspection_tools(mcp, Inspection(store, writes_enabled=writes))
    register_reconciliation_tools(mcp, Reconciliation(store, catalog, writes_enabled=writes))
    register_security_audit_tool(mcp, SecurityAudit(store, writes_enabled=writes))
    register_policy_tools(mcp, Policy(store, catalog, writes_enabled=writes))
    return mcp


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
