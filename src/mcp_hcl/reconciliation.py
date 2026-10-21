"""Read-only reconciliation of bound guests, with explicit inventory gaps."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from ipaddress import ip_interface
from typing import Annotated
from uuid import uuid4

from pydantic import Field, ValidationError

from mcp_hcl.address_observation import interface_domains, ipv6_interfaces
from mcp_hcl.cli_session import PersistentConsole
from mcp_hcl.console import ConsoleError, resolve_endpoint
from mcp_hcl.contracts import validation_errors
from mcp_hcl.deployment import Deployment
from mcp_hcl.gui_relay import rejected
from mcp_hcl.inspection import reply_body
from mcp_hcl.readback import INTERFACE, ReadbackError, interface_states
from mcp_hcl.runtime_binding import RuntimeBinding, unlinked
from mcp_hcl.topology import TopologyPlan, validate_plan

QUERY = "display interface"
ObservationTimeout = Annotated[int, Field(ge=1, le=600)]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def reconciliation_ports(text: str) -> list[dict]:
    """Distinguish an explicit absent IPv4 address from unread address state.

    Interface status alone does not reveal a cable or its peer. The parser
    keeps those facts unknown, including on an interface whose carrier is up.
    """
    ports = interface_states(text)
    headers = list(
        re.finditer(
            r"^(" + INTERFACE + r")(?:\s+current state\s*:\s*([^\n]+))?\s*$", text, re.M | re.I
        )
    )
    for index, port in enumerate(ports):
        block = text[
            headers[index].end() : headers[index + 1].start()
            if index + 1 < len(headers)
            else len(text)
        ]
        candidates = [
            line.strip()
            for line in block.splitlines()
            if re.match(r"\s*Internet\s+(?:address|protocol processing)\b", line, re.I)
        ]
        primary = []
        secondary = []
        absent = False
        processing_enabled = False
        for line in candidates:
            if re.fullmatch(r"Internet protocol processing\s*:\s*disabled", line, re.I):
                absent = True
                continue
            if re.fullmatch(r"Internet protocol processing\s*:\s*enabled", line, re.I):
                processing_enabled = True
                continue
            if re.fullmatch(r"Internet address\s*:\s*(?:unassigned|none)", line, re.I):
                absent = True
                continue
            match = re.fullmatch(
                r"Internet address\s*:\s*([0-9.]+/\d+)(?:\s*\((Primary|Secondary)\))?", line, re.I
            )
            if match is None:
                raise ReadbackError("Unsupported reported IPv4 address field")
            address = ip_interface(match[1])
            if address.version != 4 or address.ip.is_unspecified:
                raise ReadbackError("Unsupported reported IPv4 address")
            target = secondary if (match[2] or "").lower() == "secondary" else primary
            target.append(
                {
                    "address": str(address.ip),
                    "prefix_length": address.network.prefixlen,
                    "netmask": str(address.netmask),
                }
            )
        if len(primary) > 1 or absent and (primary or secondary or processing_enabled):
            raise ReadbackError("Conflicting interface IPv4 observations")
        if secondary and not primary:
            raise ReadbackError("Secondary address without an observed primary address")
        addresses = primary + secondary
        if len({item["address"] for item in addresses}) != len(addresses):
            raise ReadbackError("Duplicate address in one interface observation")
        port.update(
            ipv4=primary[0] if primary else None,
            ipv4_addresses=addresses,
            ipv4_observation="present" if primary else "absent" if absent else "unknown",
            ipv6_observation="unknown",
            ipv6_addresses=[],
            ipv6_link_local=None,
            routing_domain_observation="unknown",
            routing_domain=None,
        )
    return ports


def diff_observations(plan: TopologyPlan, devices: list[dict]) -> dict:
    """Compare observations only; this function does not establish their origin."""
    actual = {row["name"]: row for row in devices}
    ip_mismatches = []
    prefix_mismatches = []
    unavailable = []
    unconfigured = []
    planned = {row.id: row for row in plan.devices}
    for address in plan.addresses:
        device = planned[address.device]
        desired = ip_interface(address.cidr)
        row = actual.get(device.name)
        port = (
            next((item for item in row["ports"] if item["interface"] == address.interface), None)
            if row
            else None
        )
        finding = {
            "device": device.name,
            "interface": address.interface,
            "planned": str(desired.ip),
        }
        if port is None:
            unavailable.append({**finding, "reason": "device_or_interface_not_observed"})
        elif desired.version == 6:
            if port["ipv6_observation"] == "unknown":
                unavailable.append({**finding, "reason": "ipv6_readback_unavailable"})
            else:
                addresses = (
                    ([port["ipv6_link_local"]] if port["ipv6_link_local"] else [])
                    if desired.ip.is_link_local
                    else port["ipv6_addresses"]
                )
                matches = [item for item in addresses if item["address"] == str(desired.ip)]
                if not matches:
                    if addresses:
                        ip_mismatches.append(
                            {
                                **finding,
                                "actual": sorted(item["address"] for item in addresses),
                                "address_family": 6,
                            }
                        )
                    else:
                        unconfigured.append({**finding, "address_family": 6})
                elif not matches[0]["usable"]:
                    unavailable.append(
                        {
                            **finding,
                            "reason": "ipv6_address_not_usable",
                            "flags": matches[0]["flags"],
                        }
                    )
                elif "prefix_length" not in matches[0]:
                    unavailable.append({**finding, "reason": "link_local_prefix_not_observed"})
                elif matches[0]["prefix_length"] != desired.network.prefixlen:
                    prefix_mismatches.append(
                        {
                            **finding,
                            "planned_prefix_length": desired.network.prefixlen,
                            "actual_prefix_length": matches[0]["prefix_length"],
                            "address_family": 6,
                        }
                    )
        elif port["ipv4_observation"] == "unknown":
            unavailable.append({**finding, "reason": "ipv4_field_not_observed"})
        elif port["ipv4_observation"] == "absent":
            unconfigured.append(finding)
        else:
            observed = port["ipv4"]
            if observed["address"] != str(desired.ip):
                ip_mismatches.append({**finding, "actual": observed["address"]})
            if observed["prefix_length"] != desired.network.prefixlen:
                prefix_mismatches.append(
                    {
                        **finding,
                        "planned_prefix_length": desired.network.prefixlen,
                        "actual_prefix_length": observed["prefix_length"],
                    }
                )
    return {
        "ip_mismatches": ip_mismatches,
        "prefix_mismatches": prefix_mismatches,
        "unconfigured_planned_addresses": unconfigured,
        "unavailable_addresses": unavailable,
        "not_observed_devices": sorted({row.name for row in plan.devices} - actual.keys()),
    }


def health_observations(devices: list[dict]) -> dict:
    """Retain all observed address owners; never interpret unknown links as down."""
    owners: dict[str, list[str]] = {}
    unknown_addresses = []
    unknown_links = []
    unknown_ipv6 = []
    ipv6_owners = {}
    unusable_ipv6 = []
    domain_owners = {}
    for device in devices:
        for port in device["ports"]:
            label = {"device": device["name"], "port": port["interface"]}
            if port["ipv4_observation"] == "unknown":
                unknown_addresses.append(label)
            for address in port["ipv4_addresses"]:
                owners.setdefault(address["address"], []).append(
                    device["name"] + ":" + port["interface"]
                )
                if port["routing_domain_observation"] == "known":
                    domain_key = json.dumps(port["routing_domain"], sort_keys=True)
                    domain_owners.setdefault((domain_key, address["address"]), []).append(
                        device["name"] + ":" + port["interface"]
                    )
            if port["ipv6_observation"] == "unknown":
                unknown_ipv6.append(label)
            for address in port["ipv6_addresses"]:
                if not address["usable"]:
                    unusable_ipv6.append(
                        {**label, "address": address["address"], "flags": address["flags"]}
                    )
                elif "ANYCAST" not in address["flags"]:
                    ipv6_owners.setdefault(address["address"], []).append(
                        device["name"] + ":" + port["interface"]
                    )
            unknown_links.append(label)
    duplicates = {
        address: sorted(set(labels))
        for address, labels in sorted(owners.items())
        if len(set(labels)) > 1
    }
    return {
        "duplicate_ips": duplicates,
        "down_links": None,
        "cabled_without_ip": None,
        "ports_with_unknown_ipv4": unknown_addresses,
        "ports_with_unknown_link_inventory": unknown_links,
        "duplicate_ip_scope": "observed_ipv4_interfaces; use the per-domain findings where available",
        "duplicate_ips_by_routing_domain": [
            {
                "routing_domain": json.loads(domain),
                "address": address,
                "owners": sorted(set(labels)),
            }
            for (domain, address), labels in sorted(domain_owners.items())
            if len(set(labels)) > 1
        ],
        "ports_with_unknown_ipv6": unknown_ipv6,
        "ipv6_duplicate_addresses": {
            address: sorted(set(labels))
            for address, labels in sorted(ipv6_owners.items())
            if len(set(labels)) > 1
        },
        "ipv6_duplicate_scope": "observed_usable_global_addresses; IPv6 routing domains remain unknown; anycast excluded",
        "ipv6_unusable_addresses": unusable_ipv6,
    }


class Deadline:
    def __init__(self, seconds: int):
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise ConsoleError(
                "Reconciliation observation budget expired", code="observation_timeout"
            )
        return remaining

    def run(self, *args, **kwargs):
        kwargs["timeout"] = min(kwargs.get("timeout", 12), self.remaining())
        return subprocess.run(*args, **kwargs)


class Reconciliation:
    def __init__(
        self, store, catalog, *, writes_enabled=False, runtime_binding=None, session_factory=None
    ):
        self.store = store
        self.catalog = catalog
        self.writes_enabled = writes_enabled
        self.runtime = runtime_binding
        self.session_factory = session_factory
        self.native_dependencies = runtime_binding is None and session_factory is None
        self.staged = Deployment(store)

    def observe(
        self, kind, project_path, topology, plan=None, timeout_seconds=120, include_ipv6=False
    ):
        # Console reads still send commands and consume an exclusive stream.
        # Preserve the existing global live-command gate before filesystem I/O.
        if not self.writes_enabled:
            return rejected(
                "writes_disabled", "Native reconciliation requires the explicit live command switch"
            )
        if kind not in {"diff", "health"}:
            return rejected("invalid_reconciliation", "Use diff or health")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
            return rejected("invalid_timeout", "timeout_seconds must be an integer from 1 to 600")
        if type(include_ipv6) is not bool:
            return rejected("invalid_ipv6_option", "include_ipv6 must be a boolean")
        try:
            resolve_endpoint(topology, 1)
            typed = TopologyPlan.model_validate(plan) if kind == "diff" else None
        except ValidationError as exc:
            return {
                "status": "rejected",
                "errors": validation_errors(exc),
                "full_live_gate_passed": False,
            }
        except ConsoleError as exc:
            return {
                "status": "rejected",
                "errors": [exc.as_error()],
                "full_live_gate_passed": False,
            }
        budget = Deadline(timeout_seconds)
        ipv6_devices = set()
        if typed is not None:
            validation = validate_plan(typed.model_dump(), self.catalog)
            if validation.get("valid") is not True:
                validation.update(operation=kind, target_project=project_path)
                return validation
            device_names = {device.id: device.name for device in typed.devices}
            ipv6_devices = {
                device_names[address.device]
                for address in typed.addresses
                if ip_interface(address.cidr).version == 6
            }
        operation = uuid4().hex
        result = {
            "status": "unverified",
            "operation": kind,
            "operation_id": operation,
            "target_project": project_path,
            "devices": [],
            "errors": [],
            "evidence": [],
            "warnings": [
                "Native device/cable inventory is not implemented; staged bindings are not a live inventory."
            ],
            "native_readback_verified": False,
            "snapshot_complete_for_staged_bindings": False,
            "live_device_inventory_verified": False,
            "live_cable_inventory_verified": False,
            "runtime_verified": False,
            "guest_boot_verified": False,
            "configuration_verified": False,
            "connectivity_verified": False,
            "runtime_isolation_verified": False,
            "upstream_behavior_equivalent": False,
            "full_live_gate_passed": False,
            "atomic_topology_snapshot_verified": False,
            "observation_started_utc": utc_now(),
        }
        result["in_sync" if kind == "diff" else "healthy"] = None
        directory = None
        active = None
        histories = []
        runtime = self.runtime or RuntimeBinding(runner=budget.run)
        try:
            budget.remaining()
            document, bindings = self.staged.bindings(project_path)
            result["target_project"] = str(document.net_path)
            declaration = document.summary()
            if not bindings or len(bindings) > 256:
                raise ReadbackError("A reconciliation needs 1 to 256 staged device bindings")
            if len({binding["name"] for binding in bindings.values()}) != len(bindings):
                raise ReadbackError("Staged display names are ambiguous")
            tracked = [
                document.net_path,
                document.directory / "MCPArtifacts/bindings.json",
                document.directory / "MCPArtifacts/stage-receipt.json",
            ]
            before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked}
            result["staged_device_count"] = len(bindings)
            result["staged_declaration"] = declaration
            result["unbound_project_devices"] = sorted(
                {row["name"] for row in declaration["devices"]}
                - {row["name"] for row in bindings.values()}
            )
            destination = self.store.output_root / ".reconciliation-receipts" / operation
            unlinked(destination)
            destination.mkdir(parents=True, exist_ok=False)
            directory = destination
            result["receipt_directory"] = str(directory)
            targets = {}
            # All staged identities are checked before the first CLI command.
            for identifier, binding in bindings.items():
                try:
                    budget.remaining()
                    target = runtime.resolve(document, binding, topology)
                    budget.remaining()
                    if target.get("project_sha256") != before[str(document.net_path)]:
                        raise ReadbackError(
                            "Runtime binding observed a different staged project version"
                        )
                    targets[identifier] = target
                except (ConsoleError, OSError, ValueError) as exc:
                    result["errors"].append(self._error(exc, identifier))
            for identifier, binding in bindings.items():
                if identifier not in targets:
                    continue
                try:
                    remaining = budget.remaining()
                    # HCL republishes the device pipe as a telnet console; the pipe
                    # itself is held read-exclusively by SimwareClient.
                    pipe = resolve_endpoint(topology, binding["device_id"])
                    active = (
                        self.session_factory(pipe)
                        if self.session_factory
                        else PersistentConsole(
                            pipe,
                            self.store.output_root / ".console-leases",
                            timeout=min(12, remaining),
                            open_timeout=min(2, remaining),
                        )
                    )
                    histories.append((identifier, active))
                    identity = runtime.verify_pipe(active, targets[identifier])
                    active.timeout = min(12, budget.remaining())
                    reply = active.exchange(QUERY)
                    budget.remaining()
                    ports = reconciliation_ports(reply_body(reply, QUERY))
                    native = (
                        self.native_dependencies
                        and getattr(active, "native_channel", False) is True
                    )
                    row = {
                        "device": identifier,
                        "name": binding["name"],
                        "model": binding["model"],
                        "name_source": "stage_binding",
                        "identity": identity,
                        "ports": ports,
                        "observed_at_utc": utc_now(),
                        "queries": [QUERY],
                        "native_readback_verified": native,
                        "ipv6_readback_verified": False,
                        "ipv6_interface_coverage_complete": False,
                        "routing_domain_readback_verified": False,
                    }
                    result["devices"].append(row)
                    if binding["name"] in ipv6_devices or kind == "health" and include_ipv6:
                        command = "display ipv6 interface"
                        active.timeout = min(12, budget.remaining())
                        row["queries"].append(command)
                        try:
                            reply = active.exchange(command)
                            budget.remaining()
                            additional = ipv6_interfaces(reply_body(reply, command))
                            if not additional.keys() <= {port["interface"] for port in ports}:
                                raise ReadbackError(
                                    "IPv6 interface inventory changed or differs from the initial observation"
                                )
                            for port in ports:
                                if port["interface"] in additional:
                                    port.update(additional[port["interface"]])
                            row["ipv6_readback_verified"] = native
                            row["ipv6_interface_coverage_complete"] = {
                                port["interface"] for port in ports
                            } <= additional.keys()
                            row["ipv6_observed_at_utc"] = utc_now()
                        except (ConsoleError, OSError, ValueError) as exc:
                            row["native_readback_verified"] = False
                            result["errors"].append(
                                self._error(exc, identifier, "ipv6_readback_unverified")
                            )
                except (ConsoleError, OSError, ValueError) as exc:
                    result["errors"].append(self._error(exc, identifier))
                finally:
                    if active is not None:
                        try:
                            active.close()
                        except (OSError, ValueError, ConsoleError) as exc:
                            result["errors"].append(
                                self._error(exc, identifier, "console_close_failed")
                            )
                        active = None
            if kind == "health":
                # A second read is needed only for devices owning repeated
                # IPv4 addresses. It never replays configuration commands.
                duplicate_addresses = set(health_observations(result["devices"])["duplicate_ips"])
                for row in result["devices"]:
                    if not any(
                        address["address"] in duplicate_addresses
                        for port in row["ports"]
                        for address in port["ipv4_addresses"]
                    ):
                        continue
                    identifier = row["device"]
                    try:
                        budget.remaining()
                        target = runtime.resolve(document, bindings[identifier], topology)
                        if (
                            target.get("vm_uuid") != targets[identifier].get("vm_uuid")
                            or target.get("project_sha256") != before[str(document.net_path)]
                        ):
                            raise ReadbackError(
                                "Identity changed before the routing-domain observation"
                            )
                        pipe = resolve_endpoint(topology, bindings[identifier]["device_id"])
                        remaining = budget.remaining()
                        active = (
                            self.session_factory(pipe)
                            if self.session_factory
                            else PersistentConsole(
                                pipe,
                                self.store.output_root / ".console-leases",
                                timeout=min(12, remaining),
                                open_timeout=min(2, remaining),
                            )
                        )
                        histories.append((identifier + "-routing", active))
                        row["routing_domain_identity"] = runtime.verify_pipe(active, target)
                        command = "display ip interface brief"
                        active.timeout = min(12, budget.remaining())
                        row["queries"].append(command)
                        reply = active.exchange(command)
                        budget.remaining()
                        domain_ports = [
                            port
                            for port in row["ports"]
                            if any(
                                address["address"] in duplicate_addresses
                                for address in port["ipv4_addresses"]
                            )
                        ]
                        domains = interface_domains(reply_body(reply, command), domain_ports)
                        for port in row["ports"]:
                            if port["interface"] in domains["interfaces"]:
                                port.update(domains["interfaces"][port["interface"]])
                        row["routing_domain_observed_at_utc"] = utc_now()
                        row["routing_domain_readback_verified"] = (
                            domains["status"] == "observed"
                            and bool(domains["interfaces"])
                            and self.native_dependencies
                            and getattr(active, "native_channel", False) is True
                        )
                        if domains["status"] == "unavailable":
                            result["warnings"].append(
                                "VPN column absent for "
                                + identifier
                                + "; routing domains remain unknown."
                            )
                    except (ConsoleError, OSError, ValueError) as exc:
                        result["errors"].append(
                            self._error(exc, identifier, "routing_domain_readback_unverified")
                        )
                    finally:
                        if active is not None:
                            try:
                                active.close()
                            except (OSError, ValueError, ConsoleError) as exc:
                                result["errors"].append(
                                    self._error(exc, identifier, "console_close_failed")
                                )
                            active = None
            after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in tracked}
            if after != before:
                result["errors"].append(
                    {
                        "code": "staged_snapshot_changed",
                        "message": "Project or bindings changed during observation",
                    }
                )
                # An altered stage cannot authorize device/name comparisons.
                for row in result["devices"]:
                    row["native_readback_verified"] = False
                    row["ipv6_readback_verified"] = False
                    row["routing_domain_readback_verified"] = False
            result["evidence"].extend(
                {"source": path, "sha256": digest, "layer": "staged_file_only"}
                for path, digest in before.items()
            )
            result["snapshot_complete_for_staged_bindings"] = (
                len(result["devices"]) == len(bindings) and not result["errors"]
            )
            result["native_readback_verified"] = result[
                "snapshot_complete_for_staged_bindings"
            ] and all(row["native_readback_verified"] for row in result["devices"])
            result["status"] = "partial" if result["devices"] else "unverified"
            result["verified_scope"] = (
                "bound_native_interface_observations_only"
                if result["native_readback_verified"]
                else "incomplete_or_synthetic_interface_observations_only"
            )
        except (ConsoleError, OSError, ValueError, KeyError) as exc:
            result["errors"].append(self._error(exc))
            result["status"] = "partial" if result["devices"] else "unverified"
        finally:
            result["observation_finished_utc"] = utc_now()
            if directory is not None:
                for identifier, channel in histories:
                    path = directory / (identifier + "-transcript.json")
                    try:
                        path.write_text(
                            json.dumps(
                                getattr(channel, "history", []), ensure_ascii=False, indent=2
                            ),
                            encoding="utf-8",
                        )
                        result["evidence"].append(
                            {
                                "source": str(path),
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                "layer": "request_console_transcript",
                            }
                        )
                    except (OSError, ValueError, TypeError) as exc:
                        result["errors"].append(
                            self._error(exc, identifier, "receipt_write_failed")
                        )
                # A partial observation still yields diagnostics, never a
                # fabricated complete inventory or an empty healthy pass.
            if typed is not None:
                result.update(
                    diff_observations(typed, result["devices"]),
                    missing_devices=None,
                    extra_devices=None,
                    missing_devices_status="unknown",
                    extra_devices_status="unknown",
                    plan_device_count=len(typed.devices),
                    live_device_count=None,
                )
                declared = {
                    row["name"] for row in result.get("staged_declaration", {}).get("devices", [])
                }
                wanted = {row.name for row in typed.devices}
                declaration_available = bool(result.get("staged_declaration"))
                result["staged_missing_devices"] = (
                    sorted(wanted - declared) if declaration_available else None
                )
                result["staged_extra_devices"] = (
                    sorted(declared - wanted) if declaration_available else None
                )
                result["staged_declaration_available"] = declaration_available
                # Only a fresh native observation can establish a mismatch.
                native_names = {
                    row["name"] for row in result["devices"] if row["native_readback_verified"]
                }
                if any(
                    row["device"] in native_names
                    for row in result["ip_mismatches"] + result["prefix_mismatches"]
                ):
                    result["in_sync"] = False
            else:
                result.update(health_observations(result["devices"]))
                # Repeated addresses are findings; separate routing domains
                # can legitimately reuse them, so they do not prove unhealthy.
                result["healthy"] = None
            if result["errors"]:
                result["native_readback_verified"] = False
                result["snapshot_complete_for_staged_bindings"] = False
                if any(row["code"] == "receipt_write_failed" for row in result["errors"]):
                    if kind == "diff":
                        result["in_sync"] = None
            if directory is not None:
                try:
                    (directory / "result.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except (OSError, ValueError, TypeError) as exc:
                    result["errors"].append(self._error(exc, code="receipt_write_failed"))
                    result["native_readback_verified"] = False
                    result["snapshot_complete_for_staged_bindings"] = False
                    if kind == "diff":
                        result["in_sync"] = None
        return result

    @staticmethod
    def _error(exc, device=None, code="reconciliation_observation_unverified"):
        row = (
            exc.as_error() if isinstance(exc, ConsoleError) else {"code": code, "message": str(exc)}
        )
        return {**row, **({"device": device} if device is not None else {})}


def register_reconciliation_tools(mcp, reconciliation):
    @mcp.tool()
    def hcl_diff(
        project_path: str, topology: str, plan: dict, timeout_seconds: ObservationTimeout = 120
    ) -> dict:
        """Compare planned addresses with freshly bound guest observations.

        Missing/extra live devices and a positive in_sync remain unknown until
        an authoritative native topology inventory is available.
        """
        return reconciliation.observe("diff", project_path, topology, plan, timeout_seconds)

    @mcp.tool()
    def hcl_health_check(
        project_path: str,
        topology: str,
        timeout_seconds: ObservationTimeout = 120,
        include_ipv6: bool = False,
    ) -> dict:
        """Read interface anomalies and repeated addresses from staged running guests.

        Carrier state does not prove cabling; healthy remains unknown.
        """
        return reconciliation.observe(
            "health",
            project_path,
            topology,
            timeout_seconds=timeout_seconds,
            include_ipv6=include_ipv6,
        )
