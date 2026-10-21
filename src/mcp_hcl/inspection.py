"""Bound staged-device inspection with native identity and request-local evidence."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from mcp_hcl.cli_session import PROMPT, PersistentConsole, clean_text
from mcp_hcl.console import ConsoleError, resolve_endpoint
from mcp_hcl.deployment import Deployment
from mcp_hcl.gui_relay import rejected
from mcp_hcl.readback import ReadbackError, interface_states, qos_definitions, vlan_database
from mcp_hcl.runtime_binding import RuntimeBinding

QUERIES = {
    "ports": ["display interface"],
    "vlans": ["display vlan all"],
    "qos": [
        "display traffic classifier user-defined",
        "display traffic behavior user-defined",
        "display qos policy user-defined",
    ],
}


def reply_body(reply, command):
    try:
        raw = base64.b64decode(reply["raw_output_b64"], validate=True)
        if (
            reply.get("complete") is not True
            or reply.get("command") != command
            or reply.get("request_sha256") != hashlib.sha256(command.encode("ascii")).hexdigest()
            or reply.get("output_sha256") != hashlib.sha256(raw).hexdigest()
        ):
            raise ReadbackError("Readback is incomplete or not bound to its command")
        text = clean_text(raw)
        prompt = PROMPT.search(text)
        lines = text[: prompt.start()].splitlines() if prompt else []
        if not lines or not (
            lines[0].strip() == command
            or lines[0].strip().endswith(">" + command)
            or lines[0].strip().endswith("]" + command)
        ):
            raise ReadbackError("Readback has no initial command echo and ending prompt")
        return "\n".join(lines[1:]).strip()
    except (KeyError, ValueError) as exc:
        raise ReadbackError("Readback transcript failed its integrity checks") from exc


class Inspection:
    def __init__(self, store, *, writes_enabled=False, runtime_binding=None, session_factory=None):
        self.store = store
        self.writes_enabled = writes_enabled
        self.native_dependencies = runtime_binding is None and session_factory is None
        self.runtime = runtime_binding or RuntimeBinding()
        self.session_factory = session_factory or (
            lambda pipe: PersistentConsole(pipe, store.output_root / ".console-leases")
        )
        self.staged = Deployment(store)

    def read(self, kind, project_path, topology, device="", only_linked=False):
        if not self.writes_enabled:
            return rejected(
                "writes_disabled", "Native console queries require the explicit live write switch"
            )
        if kind not in QUERIES:
            return rejected("invalid_inspection", "Unknown inspection kind")
        if kind != "ports" and not device:
            return rejected(
                "missing_device", "VLAN and QoS inspection require one exact staged device"
            )
        if only_linked:
            return rejected(
                "live_cable_inventory_unverified",
                "CLI interface state does not prove the native HCL cable inventory; linked-only filtering is unavailable",
            )
        document, bindings = self.staged.bindings(project_path)
        if device:
            selected = [
                (identifier, binding)
                for identifier, binding in bindings.items()
                if identifier == device or binding["name"] == device
            ]
            if len(selected) != 1:
                return rejected(
                    "unknown_or_ambiguous_device", "Select one exact staged plan ID or display name"
                )
        else:
            selected = list(bindings.items())
        operation = uuid4().hex
        directory = self.store.output_root / ".inspection-receipts" / operation
        directory.mkdir(parents=True, exist_ok=False)
        result = {
            "status": "unverified",
            "operation": "inspect_" + kind,
            "operation_id": operation,
            "target_project": str(document.net_path),
            "receipt_directory": str(directory),
            "devices": [],
            "errors": [],
            "evidence": [],
            "native_readback_verified": False,
            "runtime_verified": False,
            "guest_boot_verified": False,
            "configuration_verified": False,
            "connectivity_verified": False,
            "full_live_gate_passed": False,
        }
        channels = []
        try:
            # Validate every target before sending the first command. A stage
            # binding alone does not identify the currently running guest.
            targets = {
                identifier: self.runtime.resolve(document, binding, topology)
                for identifier, binding in selected
            }
            for identifier, binding in selected:
                # The device pipe is read-exclusively by HCL's SimwareClient, which
                # republishes it as the telnet console HCL's own GUI uses.
                pipe = resolve_endpoint(topology, binding["device_id"])
                channel = self.session_factory(pipe)
                channels.append((identifier, channel))
                with channel:
                    identity = self.runtime.verify_pipe(channel, targets[identifier])
                    replies = [channel.exchange(command) for command in QUERIES[kind]]
                    bodies = [
                        reply_body(reply, command) for reply, command in zip(replies, QUERIES[kind])
                    ]
                    if kind == "ports":
                        parsed = {"ports": interface_states(bodies[0])}
                    elif kind == "vlans":
                        parsed = {"vlans": vlan_database(bodies[0])}
                    else:
                        parsed = {
                            key: qos_definitions(body, key)
                            for key, body in zip(("classifiers", "behaviors", "policies"), bodies)
                        }
                        classifiers = {row["name"] for row in parsed["classifiers"]}
                        behaviors = {row["name"] for row in parsed["behaviors"]}
                        if any(
                            item["classifier"] not in classifiers
                            or item["behavior"] not in behaviors
                            for policy in parsed["policies"]
                            for item in policy["classes"]
                        ):
                            raise ReadbackError(
                                "QoS policy refers to a missing classifier or behavior in this snapshot"
                            )
                    native = self.native_dependencies and getattr(channel, "native_channel", False)
                    result["devices"].append(
                        {
                            "device": identifier,
                            "name": binding["name"],
                            "model": binding["model"],
                            "identity": identity,
                            "native_readback_verified": native,
                            **parsed,
                        }
                    )
            native = bool(result["devices"]) and all(
                row["native_readback_verified"] for row in result["devices"]
            )
            result.update(
                status="verified" if native else "output_received",
                native_readback_verified=native,
                verified_scope="bound_native_comware_readback_only"
                if native
                else "synthetic_inspection_protocol_only",
            )
            if kind == "ports":
                ports = [
                    (row["device"], port) for row in result["devices"] for port in row["ports"]
                ]
                result["summary"] = {
                    "devices_inspected": len(result["devices"]),
                    "ports_total": len(ports),
                    "ports_up": sum(port["physical_state"] == "up" for _, port in ports),
                    "ports_linked": None,
                    "anomalies": [
                        {"device": device, "interface": port["interface"], "issue": issue}
                        for device, port in ports
                        for issue in port["anomalies"]
                    ],
                    "live_cable_inventory_verified": False,
                }
        except (ConsoleError, ReadbackError, OSError, ValueError) as exc:
            result["status"] = "partial" if result["devices"] else "unverified"
            result["errors"] = (
                [exc.as_error()]
                if isinstance(exc, ConsoleError)
                else [{"code": "inspection_readback_unverified", "message": str(exc)}]
            )
        finally:
            for identifier, channel in channels:
                transcript = directory / (identifier + "-transcript.json")
                transcript.write_text(
                    json.dumps(getattr(channel, "history", []), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                result["evidence"].append(
                    {
                        "source": str(transcript),
                        "sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
                        "layer": "request_console_transcript",
                    }
                )
            (directory / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return result


def register_inspection_tools(mcp, inspection):
    @mcp.tool()
    def hcl_inspect_ports(
        project_path: str, topology: str, device: str = "", only_linked: bool = False
    ) -> dict:
        """Read bound running-device interface state; empty device inspects all staged devices.

        Link/cable inventory remains unverified, so only_linked=true is rejected.
        """
        return inspection.read("ports", project_path, topology, device, only_linked)

    @mcp.tool()
    def hcl_read_vlans(project_path: str, topology: str, device: str) -> dict:
        """Read detailed native VLAN state from one bound staged device; no default or capacity guesses."""
        return inspection.read("vlans", project_path, topology, device)

    @mcp.tool()
    def hcl_read_qos(project_path: str, topology: str, device: str) -> dict:
        """Read native user-defined traffic classes, behaviors and policies from one bound device."""
        return inspection.read("qos", project_path, topology, device)
