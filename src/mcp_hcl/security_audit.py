"""Bound Comware posture observations; credential values never leave memory."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated
from uuid import uuid4

from pydantic import Field

from mcp_hcl.cli_session import PersistentConsole
from mcp_hcl.console import ConsoleError, resolve_endpoint
from mcp_hcl.deployment import Deployment
from mcp_hcl.gui_relay import rejected
from mcp_hcl.inspection import reply_body
from mcp_hcl.readback import ReadbackError
from mcp_hcl.reconciliation import Deadline
from mcp_hcl.runtime_binding import RuntimeBinding, unlinked

QUERY = "display current-configuration"
AuditTimeout = Annotated[int, Field(ge=1, le=600)]
UNSUPPORTED_RULES = (
    "NO_ENABLE_SECRET",
    "ENABLE_SECRET_REVERSIBLE",
    "ENABLE_SECRET_WEAK_ALGO",
    "ENABLE_PASSWORD_PRESENT",
    "NO_SERVICE_PASSWORD_ENCRYPTION",
    "CONFIG_REGISTER_BYPASS",
)


def credential_storage(command):
    """Inspect the keyword only; cipher is not assigned a Cisco algorithm."""
    parts = command.split()
    if len(parts) > 2 and parts[1] == "hash":
        return "hash_algorithm_unknown"
    if len(parts) > 2 and parts[1] == "simple":
        return "plaintext"
    if len(parts) > 2 and parts[1] == "cipher":
        return "cipher_algorithm_unknown"
    return "unknown"


def comware_posture(text):
    lines = text.splitlines()
    if not lines or lines[-1].strip() != "return":
        raise ReadbackError("Configuration footer is missing")
    users = []
    user_names = set()
    access_lines = []
    versions = 0
    banner_motd = False
    banner_delimiter = None
    banner_kind = None
    banner_content = False
    context = None
    global_posture = {
        "telnet_server_explicit": None,
        "ssh_server_explicit": None,
        "role_switch_password_storage": [],
    }
    for raw in lines:
        value = raw.strip()
        if banner_delimiter is not None:
            if banner_delimiter in value:
                if not value.endswith(banner_delimiter) or value.count(banner_delimiter) != 1:
                    raise ReadbackError("Unsupported banner framing")
                banner_content |= bool(value[:-1])
                if banner_kind == "motd":
                    banner_motd |= banner_content
                banner_delimiter = None
            else:
                banner_content |= bool(value)
            continue
        if value in {"", "#", "return"}:
            if value == "#":
                context = None
            continue
        root = not raw[:1].isspace() or context is None
        if root and value.startswith("header "):
            header = re.fullmatch(r"header (motd|login|shell|incoming|legal) (.+)", value)
            if header is None or header[2][0].isalnum() or not header[2][0].isprintable():
                raise ReadbackError("Unsupported banner declaration")
            delimiter, content = header[2][0], header[2][1:]
            if delimiter in content:
                if not content.endswith(delimiter) or content.count(delimiter) != 1:
                    raise ReadbackError("Unsupported banner framing")
                if header[1] == "motd":
                    banner_motd |= bool(content[:-1])
            else:
                banner_delimiter, banner_kind, banner_content = delimiter, header[1], bool(content)
            continue
        if root and value.startswith("local-user "):
            match = re.fullmatch(r"local-user (\S+)(?: class (manage|network))?", value)
            if (
                match is None
                or len(match[1]) > 55
                or not all(char.isprintable() for char in match[1])
                or match[1] in user_names
            ):
                raise ReadbackError("Unsupported or duplicate local-user declaration")
            user_names.add(match[1])
            users.append(
                {
                    "name": match[1],
                    "class": match[2] or "unspecified",
                    "credential_storage": "not_observed",
                    "service_types": [],
                    "roles": [],
                }
            )
            context = ("user", users[-1])
            continue
        if root and re.match(r"^(?:line|user-interface) (?:vty|console|con|aux)\b", value):
            match = re.fullmatch(
                r"(?:line|user-interface) (vty|console|con|aux) (\d+)(?: (\d+))?", value
            )
            if (
                match is None
                or int(match[2]) > 255
                or match[3]
                and not int(match[2]) <= int(match[3]) <= 255
            ):
                raise ReadbackError("Unsupported access-line declaration")
            access_lines.append(
                {
                    "kind": match[1],
                    "first": int(match[2]),
                    "last": int(match[3] or match[2]),
                    "authentication": "not_observed",
                    "protocols": [],
                    "credential_storage": "not_observed",
                }
            )
            context = ("line", access_lines[-1])
            continue
        if context is not None and context[0] in {"user", "line"} and raw[:1].isspace():
            row = context[1]
            password = value
            if context[0] == "line" and value.startswith("set authentication password "):
                password = "password " + value[len("set authentication password ") :]
            if password.startswith("password "):
                if row["credential_storage"] != "not_observed":
                    raise ReadbackError("Conflicting credential storage declarations")
                row["credential_storage"] = credential_storage(password)
            elif context[0] == "user" and value.startswith("service-type "):
                tokens = value.split()[1:]
                row["service_types"] = [
                    token
                    for token in tokens
                    if token
                    in {"ssh", "telnet", "terminal", "ftp", "http", "https", "ppp", "lan-access"}
                ]
            elif context[0] == "user" and value.startswith("authorization-attribute user-role "):
                role = value.split()[-1]
                if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", role):
                    row["roles"].append(role)
            elif context[0] == "line" and value.startswith("authentication-mode "):
                mode = value[20:]
                if (
                    mode not in {"none", "password", "scheme"}
                    or row["authentication"] != "not_observed"
                ):
                    raise ReadbackError("Unsupported access authentication declaration")
                row["authentication"] = mode
            elif context[0] == "line" and value.startswith("protocol inbound "):
                protocols = value[17:].split()
                if any(item not in {"all", "ssh", "telnet"} for item in protocols):
                    raise ReadbackError("Unsupported inbound protocol declaration")
                row["protocols"] = protocols
            continue
        if root:
            context = None
            if value.startswith("version "):
                versions += 1
            elif value in {"telnet server enable", "undo telnet server enable"}:
                global_posture["telnet_server_explicit"] = not value.startswith("undo ")
            elif value in {
                "ssh server enable",
                "stelnet server enable",
                "undo ssh server enable",
                "undo stelnet server enable",
            }:
                global_posture["ssh_server_explicit"] = not value.startswith("undo ")
            elif value.startswith("super password "):
                # Only storage mode observations, never role-switch secrets.
                match = re.fullmatch(
                    r"super password(?: (?:level \d+|role \S+))? "
                    r"(hash(?: (?:simple|cipher))?|simple|cipher) .+",
                    value,
                )
                mode = match[1] if match is not None else "unknown"
                global_posture["role_switch_password_storage"].append(
                    "hash_algorithm_unknown"
                    if mode.startswith("hash")
                    else "plaintext"
                    if mode == "simple"
                    else "cipher_algorithm_unknown"
                    if mode == "cipher"
                    else "unknown"
                )
            elif not raw[:1].isspace():
                context = ("opaque", None)
    if banner_delimiter is not None or versions != 1:
        raise ReadbackError("Configuration/banners are incomplete or ambiguous")
    return {
        "local_users": users,
        "access_lines": access_lines,
        "banner_motd_set": banner_motd,
        **global_posture,
        "credential_algorithms_verified": False,
        "authentication_behavior_verified": False,
        "boot_bypass_verified": False,
    }


def audit_findings(name, posture):
    findings = []

    def add(code, severity, message, suggestion):
        findings.append(
            {
                "device": name,
                "code": code,
                "severity": severity,
                "message": message,
                "suggestion": suggestion,
            }
        )

    for user in posture["local_users"]:
        if user["credential_storage"] == "plaintext":
            add(
                "USER_CREDENTIAL_REVERSIBLE",
                "high",
                "A local-user credential is explicitly stored as simple text.",
                "Review the firmware-supported hashed credential format for this local-user class.",
            )
    if not posture["local_users"]:
        add(
            "NO_LOCAL_USERS",
            "low",
            "No local-user declarations were observed.",
            "Verify the intended local or remote AAA policy; absence alone does not prove SSH is unavailable.",
        )
    if not posture["banner_motd_set"]:
        add(
            "NO_BANNER_MOTD",
            "low",
            "No nonempty MOTD banner was observed.",
            "Review the intended login notice policy.",
        )
    if "plaintext" in posture["role_switch_password_storage"]:
        add(
            "COMWARE_ROLE_PASSWORD_PLAINTEXT",
            "high",
            "A role-switch credential is explicitly stored as simple text.",
            "Review the supported hashed role-switch password command; do not apply Cisco enable commands.",
        )
    if posture["telnet_server_explicit"] is True:
        add(
            "COMWARE_TELNET_ENABLED",
            "medium",
            "The Telnet server is explicitly enabled.",
            "Verify whether cleartext management is required and whether SSH can replace it.",
        )
    for line in posture["access_lines"]:
        if line["authentication"] == "none":
            add(
                "COMWARE_LINE_NO_AUTHENTICATION",
                "high",
                "An access line explicitly uses no authentication.",
                "Review the configured authentication method for that access-line range.",
            )
        if line["credential_storage"] == "plaintext":
            add(
                "COMWARE_LINE_PASSWORD_PLAINTEXT",
                "high",
                "An access-line credential is explicitly stored as simple text.",
                "Review the firmware-supported credential representation and authentication policy.",
            )
    findings.sort(
        key=lambda row: (
            {"high": 0, "medium": 1, "low": 2}[row["severity"]],
            row["device"],
            row["code"],
        )
    )
    return findings


class SecurityAudit:
    def __init__(self, store, *, writes_enabled=False, runtime_binding=None, session_factory=None):
        self.store = store
        self.writes_enabled = writes_enabled
        self.runtime = runtime_binding
        self.session_factory = session_factory
        self.native_dependencies = runtime_binding is None and session_factory is None
        self.staged = Deployment(store)

    def read(self, project_path, topology, device="", timeout_seconds=120):
        if not self.writes_enabled:
            return rejected(
                "writes_disabled", "Native audit queries require the explicit live command switch"
            )
        if (
            type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= 600
            or not isinstance(device, str)
        ):
            return rejected(
                "invalid_audit_arguments",
                "Use a device string and an integer timeout from 1 to 600",
            )
        resolve_endpoint(topology, 1)
        budget = Deadline(timeout_seconds)
        runtime = self.runtime or RuntimeBinding(runner=budget.run)
        operation = uuid4().hex
        result = {
            "status": "unverified",
            "operation": "audit_security",
            "operation_id": operation,
            "target_project": project_path,
            "devices": [],
            "findings": [],
            "errors": [],
            "evidence": [],
            "secure": None,
            "devices_audited": 0,
            "counts": {"high": 0, "medium": 0, "low": 0},
            "native_readback_verified": False,
            "live_device_inventory_verified": False,
            "runtime_verified": False,
            "configuration_verified": False,
            "connectivity_verified": False,
            "upstream_behavior_equivalent": False,
            "full_live_gate_passed": False,
            "warnings": [
                "Only staged bound-device posture is observed; full native inventory and complete rule mapping are unavailable."
            ],
            "rule_mapping": {code: "unsupported_comware_equivalence" for code in UNSUPPORTED_RULES},
            "credential_values_exported": False,
            "raw_configuration_evidence_exported": False,
        }
        result["rule_mapping"].update(
            USER_CREDENTIAL_REVERSIBLE="explicit_simple_storage_only",
            USER_CREDENTIAL_WEAK_ALGO="unverified_algorithm",
            NO_LOCAL_USERS="local_declarations_only_remote_AAA_unverified",
            NO_BANNER_MOTD="nonempty_MOTD_observation",
        )
        directory = None
        channel = None
        safe_history = []
        try:
            document, bindings = self.staged.bindings(project_path)
            result["target_project"] = str(document.net_path)
            selected = [
                (identifier, binding)
                for identifier, binding in bindings.items()
                if not device or identifier == device or binding["name"] == device
            ]
            if device and len(selected) != 1:
                return rejected(
                    "unknown_or_ambiguous_device", "Select one exact staged ID or display name"
                )
            result["skipped_hosts"] = [
                identifier for identifier, binding in selected if binding["model"] == "PC"
            ]
            selected = [
                (identifier, binding)
                for identifier, binding in selected
                if binding["model"] != "PC"
            ]
            if not selected or len(selected) > 256:
                raise ReadbackError("No supported bound configuration devices or too many targets")
            sources = [
                document.net_path,
                document.directory / "MCPArtifacts/bindings.json",
                document.directory / "MCPArtifacts/stage-receipt.json",
            ]
            # Stage hashes are checked privately: an engineering file might
            # itself contain embedded credentials in a vendor-specific field.
            before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
            targets = {}
            for identifier, binding in selected:
                budget.remaining()
                target = runtime.resolve(document, binding, topology)
                if target.get("project_sha256") != before[str(document.net_path)]:
                    raise ReadbackError("Staged project changed during preflight")
                targets[identifier] = target
            destination = self.store.output_root / ".security-audit-receipts" / operation
            unlinked(destination)
            destination.mkdir(parents=True, exist_ok=False)
            directory = destination
            result["receipt_directory"] = str(directory)
            for identifier, binding in selected:
                safe_event = {
                    "device": identifier,
                    "command": QUERY,
                    "complete": False,
                    "raw_fields_omitted": True,
                }
                safe_history.append(safe_event)
                try:
                    remaining = budget.remaining()
                    # HCL republishes the device pipe as a telnet console; the pipe
                    # itself is held read-exclusively by SimwareClient.
                    pipe = resolve_endpoint(topology, binding["device_id"])
                    channel = (
                        self.session_factory(pipe)
                        if self.session_factory
                        else PersistentConsole(
                            pipe,
                            self.store.output_root / ".console-leases",
                            timeout=min(12, remaining),
                            open_timeout=min(2, remaining),
                        )
                    )
                    identity = runtime.verify_pipe(channel, targets[identifier])
                    channel.timeout = min(12, budget.remaining())
                    reply = channel.exchange(QUERY)
                    budget.remaining()
                    body = reply_body(reply, QUERY)
                    posture = comware_posture(body)
                    native = (
                        self.native_dependencies
                        and getattr(channel, "native_channel", False) is True
                    )
                    findings = audit_findings(binding["name"], posture)
                    safe_identity = {
                        key: identity[key]
                        for key in (
                            "vm_uuid",
                            "vm_name",
                            "owner_sid",
                            "console_server_pid",
                            "device_id",
                            "topology",
                            "project_file",
                            "configuration_disk",
                            "verified_scope",
                        )
                        if key in identity
                    }
                    result["devices"].append(
                        {
                            "device": identifier,
                            "name": binding["name"],
                            "model": binding["model"],
                            "identity": safe_identity,
                            "posture": posture,
                            "native_readback_verified": native,
                        }
                    )
                    result["findings"].extend(findings)
                    safe_event["complete"] = True
                    # Do not retain raw replies, body, hashes or base64 in receipts.
                    reply = None
                    body = None
                except (ConsoleError, OSError, ValueError):
                    result["errors"].append(
                        {
                            "code": "security_audit_observation_unverified",
                            "device": identifier,
                            "message": "Bound configuration posture could not be observed completely.",
                        }
                    )
                finally:
                    if channel is not None:
                        channel.history = []
                        channel.partial_output = b""
                        try:
                            channel.close()
                        except (OSError, ValueError, ConsoleError):
                            result["errors"].append(
                                {
                                    "code": "audit_channel_close_failed",
                                    "device": identifier,
                                    "message": "The audit channel did not close cleanly.",
                                }
                            )
                        channel = None
            after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
            if after != before:
                result["errors"].append(
                    {
                        "code": "audit_stage_changed",
                        "message": "Stage files changed during the audit.",
                    }
                )
                for row in result["devices"]:
                    row["native_readback_verified"] = False
            result["evidence"].extend(
                {
                    "source": path,
                    "hashes_exported": False,
                    "layer": "staged_identity_checked_privately",
                }
                for path in before
            )
        except (ConsoleError, OSError, ValueError, KeyError, TypeError):
            result["errors"].append(
                {
                    "code": "security_audit_preflight_unverified",
                    "message": "Staged/runtime identity or observation prerequisites were not established.",
                }
            )
        finally:
            result["devices_audited"] = len(result["devices"])
            result["findings"].sort(
                key=lambda row: (
                    {"high": 0, "medium": 1, "low": 2}[row["severity"]],
                    row["device"],
                    row["code"],
                )
            )
            for row in result["findings"]:
                result["counts"][row["severity"]] += 1
            result["status"] = "partial" if result["devices"] else "unverified"
            result["native_readback_verified"] = (
                bool(result["devices"])
                and not result["errors"]
                and all(row["native_readback_verified"] for row in result["devices"])
            )
            native_names = {
                row["name"] for row in result["devices"] if row["native_readback_verified"]
            }
            if any(
                row["device"] in native_names and row["severity"] in {"high", "medium"}
                for row in result["findings"]
            ):
                result["secure"] = False
            result["verified_scope"] = (
                "bound_native_posture_observations_only"
                if result["native_readback_verified"]
                else "incomplete_or_synthetic_posture_only"
            )
            if directory is not None:
                try:
                    trace = directory / "sanitized-command-summary.json"
                    trace.write_text(json.dumps(safe_history, indent=2), encoding="utf-8")
                    result["evidence"].append(
                        {
                            "source": str(trace),
                            "sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
                            "layer": "sanitized_command_metadata_only",
                        }
                    )
                    (directory / "result.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except (OSError, TypeError, ValueError):
                    result["errors"].append(
                        {
                            "code": "audit_receipt_write_failed",
                            "message": "Sanitized audit evidence could not be saved.",
                        }
                    )
                    result["native_readback_verified"] = False
                    result["secure"] = None
        return result


def register_security_audit_tool(mcp, audit):
    @mcp.tool()
    def hcl_audit_security(
        project_path: str, topology: str, device: str = "", timeout_seconds: AuditTimeout = 120
    ) -> dict:
        """Audit bound running Comware configuration posture without exporting credentials.

        Empty device selects staged non-PC bindings, not a complete native canvas inventory.
        Unsupported Cisco rules and unknown storage algorithms prevent a secure=true verdict.
        """
        return audit.read(project_path, topology, device, timeout_seconds)
