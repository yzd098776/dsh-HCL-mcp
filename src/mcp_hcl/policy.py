"""Scoped ACL/VLAN configuration transactions with native backup and readback."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from mcp_hcl.acl_semantics import (
    acl_view,
    compile_acl_commands,
    ensure_acl_target,
    ipv4_binding,
    verify_acl,
)
from mcp_hcl.acl_values import acl_header
from mcp_hcl.cli_session import PersistentConsole
from mcp_hcl.console import ConsoleError, resolve_endpoint
from mcp_hcl.contracts import validation_errors
from mcp_hcl.deployment import Deployment, config_text, verification_commands
from mcp_hcl.generation import render_acl, render_vlan
from mcp_hcl.gui_relay import rejected
from mcp_hcl.runtime_binding import RuntimeBinding
from mcp_hcl.services import Acl, Vlan
from mcp_hcl.topology import validate_plan
from mcp_hcl.vlan_semantics import exact_vlan_commands, verify_vlan


def scopes(text):
    """A normalized command set per Comware view, including its existence."""
    result = {(): set()}
    context = ()
    for line in text.splitlines():
        value = line.strip()
        if not value or value in {"#", "return"}:
            continue
        if not line.startswith(" "):
            normalized, acl_name = acl_view(value)
            value = normalized
            if value.startswith(
                (
                    "interface ",
                    "vlan ",
                    "acl ",
                    "nat address-group ",
                    "ospf ",
                    "rip ",
                    "dhcp server ip-pool ",
                    "local-user ",
                    "traffic classifier ",
                    "traffic behavior ",
                    "qos policy ",
                    "wlan ",
                )
            ):
                context = (value,)
                result.setdefault(context, set())
                if acl_name is not None:
                    result[context].add("@acl-name " + acl_name)
                header = acl_header(line.strip())
                if header is not None and header["order"] == "auto":
                    result[context].add("@acl-order auto")
            else:
                context = ()
                result[context].add(value)
        elif value.startswith(("area ", "radio ")):
            context = context[:1] + (value,)
            result.setdefault(context, set())
        else:
            result.setdefault(context, set()).add(value)
    return result


def patch_preserves_unrequested(before, after, feature, parameters):
    old = scopes(before)
    new = scopes(after)
    targets = set()
    if feature == "acl":
        try:
            for config in (before, after):
                targets.update(
                    entry["header"]["key"] for entry in ensure_acl_target(config, parameters)
                )
        except ValueError:
            return False
    interfaces = {
        item.interface for item in (parameters.bindings if feature == "acl" else parameters.ports)
    }
    svi_interfaces = (
        {f"Vlan-interface{item.vlan}" for item in parameters.svis} if feature == "vlan" else set()
    )
    for context in old.keys() | new.keys():
        existed = context in old
        exists = context in new
        changed = old.get(context, set()) ^ new.get(context, set())
        if context == ():
            if changed:
                return False
            continue
        view = context[0]
        if feature == "acl" and view in targets:
            wanted = {rule.id for rule in parameters.rules}
            permitted_name = (
                "@acl-name " + parameters.name
                if parameters.name is not None and not existed
                else None
            )
            if any(
                line != permitted_name
                and (
                    not re.match(r"^rule (\d+)\b", line)
                    or int(re.match(r"^rule (\d+)\b", line)[1]) not in wanted
                )
                for line in changed
            ):
                return False
            continue
        if feature == "vlan" and view in {f"vlan {number}" for number in parameters.vlans}:
            named = {f"vlan {name.vlan}" for name in parameters.names}
            if any(not line.startswith("name ") or view not in named for line in changed):
                return False
            continue
        if view.startswith("interface "):
            interface = view[10:]
            if interface in svi_interfaces:
                if any(not line.startswith("ip address ") for line in changed):
                    return False
                continue
            if interface in interfaces:
                if feature == "acl":
                    directions = {
                        binding.direction
                        for binding in parameters.bindings
                        if binding.interface == interface
                    }
                    for line in changed:
                        match = ipv4_binding(line)
                        if not match or match[1] not in directions:
                            return False
                elif any(
                    not line.startswith(
                        (
                            "port link-type ",
                            "port access vlan ",
                            "port trunk permit vlan ",
                            "port trunk pvid vlan ",
                        )
                    )
                    for line in changed
                ):
                    return False
                # A scoped patch must not remove an existing interface.
                if existed and not exists:
                    return False
                continue
        if changed or existed != exists:
            return False
    return True


class Policy:
    def __init__(
        self, store, catalog, *, writes_enabled=False, runtime_binding=None, session_factory=None
    ):
        self.store = store
        self.catalog = catalog
        self.writes_enabled = writes_enabled
        self.native_dependencies = runtime_binding is None and session_factory is None
        self.runtime = runtime_binding or RuntimeBinding()
        self.session_factory = session_factory or (
            lambda pipe: PersistentConsole(pipe, store.output_root / ".console-leases")
        )
        self.deployment = Deployment(store, catalog=catalog)
        self.deployment.native_mode = self.native_dependencies

    def open(self, document, binding, topology, expected_uuid=None):
        target = self.runtime.resolve(document, binding, topology)
        if expected_uuid is not None and target["vm_uuid"] != expected_uuid:
            raise ConsoleError(
                "Recovery target VM identity changed", code="recovery_target_changed"
            )
        # The device pipe is read-exclusively by HCL's SimwareClient, which
        # republishes it as the telnet console HCL's own GUI uses.
        channel = self.session_factory(resolve_endpoint(topology, binding["device_id"]))
        try:
            identity = self.runtime.verify_pipe(channel, target)
        except Exception:
            channel.close()
            raise
        channel.target_identity = identity
        return channel, identity

    def apply(self, feature, project_path, topology, device, parameters, dry_run=False):
        if not dry_run and not self.writes_enabled:
            return rejected(
                "writes_disabled", "Native policy changes require the explicit live write switch"
            )
        if feature not in {"acl", "vlan"}:
            return rejected("unsupported_policy", "Only ACL/VLAN changes are registered here")
        try:
            typed = (Acl if feature == "acl" else Vlan).model_validate(parameters)
        except ValidationError as exc:
            return {
                "status": "rejected",
                "errors": validation_errors(exc),
                "full_live_gate_passed": False,
            }
        document, bindings = self.deployment.bindings(project_path)
        matches = [
            (identifier, binding)
            for identifier, binding in bindings.items()
            if identifier == device or binding["name"] == device
        ]
        if len(matches) != 1:
            return rejected(
                "unknown_or_ambiguous_device", "Select one exact staged plan ID or display name"
            )
        identifier, binding = matches[0]
        plan = json.loads(
            (document.directory / "MCPArtifacts/plan.json").read_text(encoding="utf-8")
        )
        preview = copy.deepcopy(plan)
        if feature == "vlan":
            preview["services"] = [
                service
                for service in preview["services"]
                if not (service["device"] == identifier and service["feature"] == "vlan")
            ]
        else:
            preview["services"] = [
                service
                for service in preview["services"]
                if not (
                    service["device"] == identifier
                    and service["feature"] == "acl"
                    and Acl.model_validate(service["parameters"]).identity == typed.identity
                )
            ]
            directions = {(binding.interface, binding.direction) for binding in typed.bindings}
            for service in preview["services"]:
                if service["device"] == identifier and service["feature"] == "acl":
                    service["parameters"]["bindings"] = [
                        binding
                        for binding in service["parameters"].get("bindings", [])
                        if (binding["interface"], binding.get("direction", "inbound"))
                        not in directions
                    ]
        preview["services"].append(
            {"device": identifier, "feature": feature, "parameters": typed.model_dump(mode="json")}
        )
        checked = validate_plan(preview, self.catalog, validation_level="configuration")
        if not checked.get("valid"):
            return checked
        intent = render_acl(typed) if feature == "acl" else render_vlan(typed)
        commands = exact_vlan_commands(intent) if feature == "vlan" else intent
        if dry_run:
            return {
                "status": "verified",
                "verified_scope": "offline_scoped_policy_command_intent_only",
                "device": identifier,
                "feature": feature,
                "commands": ["system-view", *commands, "return"],
                "runtime_verified": False,
                "configuration_verified": False,
                "full_live_gate_passed": False,
            }
        operation = uuid4().hex
        directory = self.store.output_root / ".policy-receipts" / operation
        directory.mkdir(parents=True, exist_ok=False)
        result = {
            "status": "unverified",
            "operation_id": operation,
            "operation": "apply_" + feature,
            "target_project": str(document.net_path),
            "device": identifier,
            "receipt_directory": str(directory),
            "steps": [],
            "errors": [],
            "configuration_verified": False,
            "runtime_verified": False,
            "connectivity_verified": False,
            "full_live_gate_passed": False,
            "persistence_verified": False,
            "protocol_persistence_verified": False,
            "protocol_effect_verified": False,
            "rollback_verified": False,
            "protocol_rollback_verified": False,
        }
        channel = None
        identity = None
        backup = None
        dirty = False
        archived = []
        try:
            channel, identity = self.open(document, binding, topology)
            result["target_identity"] = identity

            def saved(record):
                nonlocal backup, dirty
                backup = record
                dirty = True

            backup = self.deployment._backup(
                channel, identifier, directory, operation, on_saved=saved
            )
            dirty = False
            baseline = Path(backup["local_file"]).read_text(encoding="utf-8")
            actual_interfaces = {
                context[0][10:]
                for context in scopes(baseline)
                if context and context[0].startswith("interface ")
            }
            required = {
                item.interface for item in (typed.bindings if feature == "acl" else typed.ports)
            }
            if not required <= actual_interfaces:
                raise ConsoleError(
                    "Policy references interfaces absent from the current configuration",
                    code="policy_interface_unverified",
                )
            result["backup"] = backup
            result["steps"].append({"stage": "backup_restore_preflight", "status": "verified"})
            if feature == "acl":
                commands, effective = compile_acl_commands(commands, baseline, typed)
            else:
                effective = typed
            dirty = True
            for command in ["system-view", *commands, "return"]:
                channel.exchange(command)
                result["steps"].append(
                    {"stage": "command", "command": command, "status": "accepted"}
                )
            current = config_text(channel.exchange("display current-configuration")["output"])
            if not (
                verify_acl(current, typed) if feature == "acl" else verify_vlan(current, typed)
            ):
                raise ConsoleError(
                    "Policy readback differs from requested intent", code="policy_effect_unverified"
                )
            if not patch_preserves_unrequested(baseline, current, feature, effective):
                raise ConsoleError(
                    "Policy altered configuration outside its requested scope",
                    code="policy_scope_changed",
                )
            native = self.native_dependencies and getattr(channel, "native_channel", False)
            result["persistence"] = self.deployment.startup.persist(
                channel,
                backup["startup"],
                current,
                f"flash:/mcp_{operation}_{identifier}_after.cfg",
            )
            result["protocol_effect_verified"] = True
            result["protocol_persistence_verified"] = True
            result["persistence_verified"] = native
            result.update(
                status="verified" if native else "output_received",
                configuration_verified=native,
                verified_scope="bound_native_scoped_configuration_readback_only"
                if native
                else "synthetic_policy_transaction_only",
            )
        except (ConsoleError, OSError, ValueError) as exc:
            result["errors"] = (
                [exc.as_error()]
                if isinstance(exc, ConsoleError)
                else [{"code": "policy_failed", "message": str(exc)}]
            )
            if dirty and backup is not None:
                try:
                    # Reconnect for recovery only; do not reuse a failed CLI
                    # session or replay the original policy command sequence.
                    if channel is not None:
                        archived.extend(getattr(channel, "history", []))
                        channel.close()
                        channel = None
                    channel, recovered_identity = self.open(
                        document, binding, topology, identity["vm_uuid"]
                    )
                    result["recovery_identity"] = recovered_identity
                    result["rollback"] = self.deployment._restore(channel, backup)
                    result["protocol_rollback_verified"] = True
                    result["rollback_verified"] = self.native_dependencies and getattr(
                        channel, "native_channel", False
                    )
                except (ConsoleError, OSError, ValueError) as recovery:
                    result["rollback"] = {
                        "status": "unverified",
                        "error": recovery.as_error()
                        if isinstance(recovery, ConsoleError)
                        else str(recovery),
                        "manual_restore_command": "configuration replace file "
                        + backup["guest_file"]
                        if backup.get("protocol_running_backup_verified")
                        else None,
                        "running_snapshot": backup["local_file"],
                        "startup_snapshot": backup.get("startup"),
                    }
        finally:
            history = archived + (getattr(channel, "history", []) if channel is not None else [])
            (directory / "transcript.json").write_text(
                json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if channel is not None:
                channel.close()
            (directory / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return result


def register_policy_tools(mcp, policy):
    @mcp.tool()
    def hcl_apply_netstream(
        project_path: str, topology: str, parameters: dict, dry_run: bool = False
    ) -> dict:
        """Apply/remove a scoped NetStream destination with local exporter-name identity.

        Named NetFlow monitor/object equivalence and actual collector delivery remain unverified.
        """
        from mcp_hcl.netstream_policy import NetstreamOperation

        return NetstreamOperation(policy).apply(project_path, topology, parameters, dry_run)

    @mcp.tool()
    def hcl_apply_stp(
        project_path: str, topology: str, parameters: dict, dry_run: bool = False
    ) -> dict:
        """Apply per-VLAN rapid spanning-tree priority and selected edge/guard intent.

        Native PVST, root-selection and global BPDU protection are not full IOS equivalence claims.
        """
        from mcp_hcl.switch_security import SwitchSecurityOperation

        return SwitchSecurityOperation(policy).apply(
            "stp", project_path, topology, parameters, dry_run
        )

    @mcp.tool()
    def hcl_apply_port_security(
        project_path: str, topology: str, parameters: dict, dry_run: bool = False
    ) -> dict:
        """Apply bounded secure-MAC/shutdown intent on one existing access port.

        Refuses unproved IOS protect/restrict mappings and unrequested authentication or learned-state conversion.
        """
        from mcp_hcl.switch_security import SwitchSecurityOperation

        return SwitchSecurityOperation(policy).apply(
            "port_security", project_path, topology, parameters, dry_run
        )

    @mcp.tool()
    def hcl_apply_interface_tuning(
        project_path: str, topology: str, parameters: dict, dry_run: bool = False
    ) -> dict:
        """Tune requested interface bandwidth and OSPF fields with scoped readback and saved recovery.

        Serial clock and IOS delay mappings remain unavailable; encrypted authentication values are partial.
        Secrets are omitted from operation receipts and previews.
        """
        from mcp_hcl.interface_tuning import InterfaceTuningOperation

        return InterfaceTuningOperation(policy).apply(project_path, topology, parameters, dry_run)

    @mcp.tool()
    def hcl_apply_nat(
        project_path: str, topology: str, parameters: dict, dry_run: bool = False
    ) -> dict:
        """Apply static, dynamic or interface/pool PAT intent to one staged router.

        Uses recorded ACL/pool identities, bounded backup/readback/persistence and same-VM recovery.
        Comware outbound semantics and pool ranges do not prove IOS ingress-role/netmask equivalence.
        """
        from mcp_hcl.nat_policy import NatOperations

        return NatOperations(policy).run(project_path, topology, parameters, dry_run=dry_run)

    @mcp.tool()
    def hcl_remove_nat(
        project_path: str, topology: str, parameters: dict, dry_run: bool = False
    ) -> dict:
        """Remove exact NAT mappings, binding and optional unshared ACL/pool resources.

        Reused operation keys return recorded receipts without replaying device commands.
        """
        from mcp_hcl.nat_policy import NatOperations

        return NatOperations(policy).run(
            project_path, topology, parameters, removal=True, dry_run=dry_run
        )

    @mcp.tool()
    def hcl_remove_acl(
        project_path: str, topology: str, device: str, parameters: dict, dry_run: bool = False
    ) -> dict:
        """Remove one exact numbered/named ACL and optional IPv4 binding with backup, scope and startup readback.

        Named types may be resolved from the actual config; unrelated definitions and bindings remain protected.
        """
        from mcp_hcl.acl_removal import RemoveAcl

        return RemoveAcl(policy).apply(project_path, topology, device, parameters, dry_run)

    @mcp.tool()
    def hcl_apply_vlan_network(
        project_path: str, topology: str, parameters: dict, dry_run: bool = False
    ) -> dict:
        """Apply scoped VLAN/access/trunk/router subinterface changes across staged devices.

        All targets and backups precede changes; persistence and recovery stay bound to each VM.
        """
        from mcp_hcl.vlan_network import VlanNetwork

        return VlanNetwork(policy).apply(project_path, topology, parameters, dry_run)

    @mcp.tool()
    def hcl_apply_acl(
        project_path: str, topology: str, device: str, parameters: dict, dry_run: bool = False
    ) -> dict:
        """Apply one strict Comware numbered or named ACL after identity, backup and restore preflight.

        Changes only requested rules/bindings; readback and unrelated scope checks are mandatory.
        """
        return policy.apply("acl", project_path, topology, device, parameters, dry_run)

    @mcp.tool()
    def hcl_apply_vlan(
        project_path: str, topology: str, device: str, parameters: dict, dry_run: bool = False
    ) -> dict:
        """Apply strict single-device VLAN/port/SVI intent with backup, readback and recovery.

        Native trunk membership is replaced with the exact requested allowed VLAN set.
        """
        return policy.apply("vlan", project_path, topology, device, parameters, dry_run)
