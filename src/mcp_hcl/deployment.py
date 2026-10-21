"""Bounded staged-device deployment with tested native backups and rollback."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from mcp_hcl.acl_semantics import (
    ensure_acl_target,
    prepare_acl_commands,
    verify_acl,
    without_acl_definitions,
)
from mcp_hcl.cli_session import PROMPT, PersistentConsole, ensure_user_view
from mcp_hcl.console import ConsoleError, resolve_endpoint
from mcp_hcl.gui_relay import rejected
from mcp_hcl.runtime_binding import RuntimeBinding
from mcp_hcl.services import Acl
from mcp_hcl.startup import StartupConfig


def config_text(reply: str) -> str:
    lines = reply.replace("\r", "").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == "#"), None)
    end = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip() == "return"), None)
    if start is None or end is None or end <= start:
        raise ConsoleError("Complete Comware configuration delimiters are missing")
    return "\n".join(line.rstrip() for line in lines[start : end + 1]) + "\n"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verification_commands(commands: list[str], current: str) -> bool:
    """Check commands within their view, not merely anywhere in a config."""
    scopes = {}
    context = ()
    for line in current.splitlines():
        stripped = line.strip()
        if not stripped or stripped in {"#", "return"}:
            continue
        if not line.startswith(" "):
            if stripped.startswith(
                (
                    "interface ",
                    "ospf ",
                    "rip ",
                    "vlan ",
                    "acl ",
                    "nat address-group ",
                    "dhcp server ip-pool ",
                    "local-user ",
                    "traffic classifier ",
                    "traffic behavior ",
                    "qos policy ",
                    "wlan ",
                )
            ):
                context = (stripped.casefold(),)
                scopes.setdefault(context, set())
            else:
                context = ()
                scopes.setdefault(context, set()).add(stripped.casefold())
        elif stripped.startswith(("area ", "radio ")):
            context = context[:1] + (stripped.casefold(),)
            scopes.setdefault(context, set())
        else:
            scopes.setdefault(context, set()).add(stripped.casefold())
    context = ()
    for command in commands:
        c = command.strip().casefold()
        if c in {"system-view", "return"}:
            context = ()
            continue
        if c == "quit":
            context = context[:-1]
            continue
        if c.startswith(
            (
                "interface ",
                "ospf ",
                "rip ",
                "vlan ",
                "acl number ",
                "nat address-group ",
                "dhcp server ip-pool ",
                "local-user ",
                "traffic classifier ",
                "traffic behavior ",
                "qos policy ",
                "wlan ",
            )
        ):
            context = (c,)
            if context not in scopes:
                return False
        elif c.startswith(("area ", "radio ")):
            context = context[:1] + (c,)
            if context not in scopes:
                return False
        elif c not in scopes.get(context, set()):
            return False
    return True


class Deployment:
    def __init__(self, store, *, catalog=None, writes_enabled=False, session_factory=None):
        self.store = store
        self.writes_enabled = writes_enabled
        self.catalog = catalog
        self.native_mode = session_factory is None
        self.session_factory = session_factory or (
            lambda pipe: PersistentConsole(pipe, store.output_root / ".console-leases")
        )
        self.runtime = RuntimeBinding(
            log_root=(Path(self.catalog.installation) / "Log" / "HCLLog" if self.catalog else None)
        )
        self.startup = StartupConfig(config_text)

    def open_channel(self, document, binding, topology, expected_uuid=None):
        target = self.runtime.resolve(document, binding, topology) if self.native_mode else None
        if target is not None and expected_uuid is not None and target["vm_uuid"] != expected_uuid:
            raise ConsoleError(
                "Recovery target VM identity changed", code="recovery_target_changed"
            )
        # HCL's SimwareClient holds the device pipe's only read handle and
        # republishes the console as telnet 30000 + device, so the live console is
        # the telnet endpoint; the pipe name itself is never openable here.
        channel = self.session_factory(resolve_endpoint(topology, binding["device_id"]))
        if target is not None:
            try:
                channel.target_identity = self.runtime.verify_pipe(channel, target)
            except Exception:
                channel.close()
                raise
        return channel

    def is_native(self, channel):
        return (
            self.native_mode
            and bool(getattr(channel, "native_channel", False))
            and bool(getattr(channel, "target_identity", {}).get("vm_uuid"))
        )

    def bindings(self, project_path):
        doc = self.store._open(project_path)
        if not doc.directory.is_relative_to(self.store.output_root):
            raise ValueError("Only staged projects may be deployed")
        path = doc.directory / "MCPArtifacts/bindings.json"
        stage = doc.directory / "MCPArtifacts/stage-receipt.json"
        for p in (path, stage):
            if not p.is_file() or p.is_symlink():
                raise ValueError("Fresh stage/bindings receipts are required")
        metadata = json.loads(stage.read_text(encoding="utf-8"))
        if Path(metadata["project_file"]).resolve() != doc.net_path:
            raise ValueError("Stage identity mismatch")
        data = json.loads(path.read_text(encoding="utf-8"))["devices"]
        actual = {d["name"]: d for d in doc.summary()["devices"]}
        if len({d["plan_id"] for d in data}) != len(data):
            raise ValueError("Duplicate staged device binding")
        for binding in data:
            if actual.get(binding["name"], {}).get("device_id") != binding["device_id"]:
                raise ValueError("Device binding does not match the staged .net")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", binding["plan_id"]):
                raise ValueError("Invalid plan ID")
        return doc, {d["plan_id"]: d for d in data}

    def _directory(self, operation):
        directory = self.store.output_root / ".deployment-receipts" / operation
        directory.mkdir(parents=True, exist_ok=False)
        return directory

    def _backup(self, channel, device, directory, operation, on_saved=None):
        ensure_user_view(channel)
        channel.exchange("screen-length disable")
        startup = self.startup.capture(channel, directory, device)
        before = config_text(channel.exchange("display current-configuration")["output"])
        guest = f"flash:/mcp_{operation}_{device}_before.cfg"
        local = directory / (device + ".cfg")
        local.write_text(before, encoding="utf-8")
        record = {
            "device": device,
            "guest_file": guest,
            "local_file": str(local),
            "sha256": digest(before),
            "restore_test_verified": False,
            "protocol_restore_test_verified": False,
            "protocol_running_backup_verified": False,
            "startup": startup,
        }
        identity = getattr(channel, "target_identity", None)
        if identity is not None:
            record["vm_uuid"] = identity["vm_uuid"]
        # Capture the startup checkpoint before any save, including a save
        # whose outcome becomes unknown before a usable guest backup exists.
        if on_saved:
            on_saved(record)
        channel.exchange(f"save {guest}", confirmation=True)
        # Native save output alone is not proof that a usable complete file exists.
        saved = config_text(channel.exchange(f"more {guest}")["output"])
        if digest(before) != digest(saved):
            raise ConsoleError("Guest backup differs from the complete running configuration")
        record["protocol_running_backup_verified"] = True
        channel.exchange("system-view")
        channel.exchange(f"configuration replace file {guest}", confirmation="N")
        ensure_user_view(channel)
        restored = config_text(channel.exchange("display current-configuration")["output"])
        if digest(before) != digest(restored):
            raise ConsoleError("Native restore preflight failed to reproduce the baseline")
        self.startup.verify(channel, startup)
        record["protocol_restore_test_verified"] = True
        record["restore_test_verified"] = self.is_native(channel)
        return record

    def _restore(self, channel, backup):
        ensure_user_view(channel)
        if backup.get(
            "protocol_running_backup_verified", backup.get("restore_test_verified", False)
        ):
            channel.exchange("system-view")
            channel.exchange(f"configuration replace file {backup['guest_file']}", confirmation="N")
        ensure_user_view(channel)
        current = config_text(channel.exchange("display current-configuration")["output"])
        if digest(current) != backup["sha256"]:
            raise ConsoleError("Restored running configuration does not match the baseline")
        startup_result = (
            self.startup.restore(channel, backup["startup"], Path(backup["local_file"]).parent)
            if "startup" in backup
            else None
        )
        native = self.is_native(channel)
        return {
            "device": backup["device"],
            "status": "verified" if native else "output_received",
            "sha256": digest(current),
            "rollback_verified": native,
            "protocol_rollback_verified": True,
            "startup_restore": startup_result,
        }

    def run(self, project_path, topology, selected=None):
        if not self.writes_enabled:
            return rejected("writes_disabled", "CLI deployment requires the live write switch")
        doc, bindings = self.bindings(project_path)
        selected = selected or list(bindings)
        if len(set(selected)) != len(selected) or not set(selected) <= set(bindings):
            raise ValueError("Deployment device selection is invalid")
        commands = {}
        from mcp_hcl.generation import generate_configs

        if self.catalog is None:
            raise ValueError("A catalog is required to validate the staged command artifacts")
        plan = json.loads((doc.directory / "MCPArtifacts/plan.json").read_text(encoding="utf-8"))
        generated = generate_configs(plan, self.catalog)
        if generated["status"] != "verified":
            raise ValueError("Staged plan cannot produce verified offline command templates")
        for device in selected:
            p = doc.directory / "MCPArtifacts/commands" / (device + ".txt")
            if p.is_symlink() or not p.is_file():
                raise ValueError("Command artifacts are missing or linked")
            commands[device] = p.read_text(encoding="utf-8").splitlines()
            if commands[device] != generated["configs"][device].splitlines():
                raise ValueError("Staged commands differ from the validated plan")
            if any(
                line.lower().startswith(("save", "reboot", "reset", "configuration replace"))
                for line in commands[device]
            ):
                raise ValueError(
                    "Command artifacts cannot contain lifecycle/persistence/restore operations"
                )
        operation = uuid4().hex
        directory = self._directory(operation)
        backups = {}
        modified = []
        rollback = []
        steps = []
        channels = {}
        error = None
        complete = False
        persisted = {}
        currents = {}
        archived_histories = {}
        failure_detail = None
        with ExitStack() as stack:
            try:
                for device in selected:
                    channel = self.open_channel(doc, bindings[device], topology)
                    channels[device] = channel
                    stack.callback(channel.close)
                for device in selected:

                    def saved(record):
                        backups[device] = record
                        modified.append(device)

                    backups[device] = self._backup(
                        channels[device], device, directory, operation, on_saved=saved
                    )
                    modified.remove(device)
                    steps.append(
                        {
                            "device": device,
                            "stage": "backup_and_restore_preflight",
                            "status": "verified",
                        }
                    )
                for device in selected:
                    baseline = Path(backups[device]["local_file"]).read_text(encoding="utf-8")
                    expected_acls = [
                        Acl.model_validate(service["parameters"])
                        for service in generated["plan"]["services"]
                        if service["device"] == device and service["feature"] == "acl"
                    ]
                    for acl in expected_acls:
                        ensure_acl_target(baseline, acl)
                    modified.append(device)
                    actual_commands = commands[device]
                    for acl in expected_acls:
                        actual_commands = prepare_acl_commands(actual_commands, baseline, acl)
                    for index, command in enumerate(actual_commands):
                        channels[device].exchange(command)
                        steps.append(
                            {
                                "device": device,
                                "stage": "command",
                                "index": index,
                                "status": "accepted",
                            }
                        )
                    current = config_text(
                        channels[device].exchange("display current-configuration")["output"]
                    )
                    if not verification_commands(
                        without_acl_definitions(commands[device]), current
                    ) or any(not verify_acl(current, acl) for acl in expected_acls):
                        raise ConsoleError("Configuration readback does not match generated intent")
                    steps.append(
                        {"device": device, "stage": "configuration_readback", "status": "verified"}
                    )
                    currents[device] = current
                for device in selected:
                    persisted[device] = self.startup.persist(
                        channels[device],
                        backups[device]["startup"],
                        currents[device],
                        f"flash:/mcp_{operation}_{device}_after.cfg",
                    )
                    steps.append(
                        {
                            "device": device,
                            "stage": "startup_persistence_readback",
                            "status": "verified",
                        }
                    )
                complete = True
            except (ConsoleError, OSError, ValueError) as exc:
                error = str(exc)
                if isinstance(exc, ConsoleError):
                    failure_detail = exc.as_error()
                for device in reversed(modified):
                    try:
                        rollback.append(self._restore(channels[device], backups[device]))
                    except (ConsoleError, OSError, ValueError) as restore_error:
                        # Reconnect once for recovery only. Never replay the
                        # original configuration commands after a disconnect.
                        try:
                            previous_channel = channels[device]
                            channels[device].close()
                            expected_uuid = getattr(previous_channel, "target_identity", {}).get(
                                "vm_uuid"
                            )
                            fresh = self.open_channel(
                                doc, bindings[device], topology, expected_uuid
                            )
                            archived_histories.setdefault(device, []).extend(
                                getattr(previous_channel, "history", [])
                            )
                            channels[device] = fresh
                            stack.callback(fresh.close)
                            rollback.append(self._restore(fresh, backups[device]))
                        except (ConsoleError, OSError, ValueError) as retry_error:
                            rollback.append(
                                {
                                    "device": device,
                                    "status": "unverified",
                                    "error": str(retry_error),
                                    "initial_error": str(restore_error),
                                    "manual_restore_command": f"configuration replace file {backups[device]['guest_file']}"
                                    if backups[device].get("protocol_running_backup_verified")
                                    else None,
                                    "running_snapshot": backups[device]["local_file"],
                                    "startup_snapshot": backups[device].get("startup"),
                                }
                            )
            finally:
                for device, channel in channels.items():
                    transcript = archived_histories.get(device, []) + getattr(
                        channel, "history", []
                    )
                    (directory / (device + "-transcript.json")).write_text(
                        json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
        native = bool(channels) and all(self.is_native(channel) for channel in channels.values())
        recovered = (
            bool(modified)
            and len(rollback) == len(modified)
            and all(row.get("protocol_rollback_verified") for row in rollback)
        )
        result = {
            "status": ("verified" if native else "output_received") if complete else "partial",
            "operation_id": operation,
            "target_project": str(doc.net_path),
            "native_channels": native,
            "verified_scope": (
                "running_configuration_readback_only"
                if native
                else "synthetic_configuration_protocol_only"
            )
            if complete
            else "deployment_and_recovery_receipts_only",
            "configuration_verified": complete and native,
            "protocol_configuration_verified": complete,
            "persistence_verified": complete and native,
            "protocol_persistence_verified": complete,
            "connectivity_verified": False,
            "guest_boot_verified": False,
            "full_live_gate_passed": False,
            "steps": steps,
            "backups": list(backups.values()),
            "persistence": persisted,
            "rollback": rollback,
            "receipt_directory": str(directory),
            "rollback_verified": recovered and native,
            "protocol_rollback_verified": recovered,
            "errors": []
            if complete
            else [{"code": "deployment_failed", "message": error or "No verified deployment"}],
        }
        if failure_detail is not None:
            result["errors"][0]["detail"] = failure_detail
        (directory / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    def backup(self, project_path, topology, device):
        if not self.writes_enabled:
            return rejected(
                "writes_disabled", "Native configuration backup requires the live write switch"
            )
        doc, bindings = self.bindings(project_path)
        if device not in bindings:
            raise ValueError("Unknown staged device")
        operation = uuid4().hex
        directory = self._directory(operation)
        with self.open_channel(doc, bindings[device], topology) as channel:
            result = self._backup(channel, device, directory, operation)
            result.update(
                project_file=str(doc.net_path),
                topology=topology,
                device_id=bindings[device]["device_id"],
            )
            if self.native_mode:
                result["vm_uuid"] = channel.target_identity["vm_uuid"]
            (directory / "backup.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        native = self.is_native(channel)
        return {
            "status": "verified" if native else "output_received",
            "backup_id": operation,
            "backup": result,
            "backup_verified": native,
            "protocol_backup_verified": True,
            "verified_scope": "native_backup_and_restore_preflight_only"
            if native
            else "synthetic_backup_protocol_only",
            "full_live_gate_passed": False,
        }

    def restore(self, project_path, topology, backup_id):
        if not self.writes_enabled:
            return rejected("writes_disabled", "Native restore requires the live write switch")
        if not re.fullmatch(r"[0-9a-f]{32}", backup_id):
            raise ValueError("Invalid backup ID")
        doc, bindings = self.bindings(project_path)
        path = self.store.output_root / ".deployment-receipts" / backup_id / "backup.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        device = record["device"]
        preflight = (
            "restore_test_verified" if self.native_mode else "protocol_restore_test_verified"
        )
        if device not in bindings or not record.get(preflight):
            raise ValueError("Backup has no verified restore preflight")
        local = Path(record["local_file"]).resolve()
        if (
            Path(record["project_file"]).resolve() != doc.net_path
            or record["topology"] != topology
            or record["device_id"] != bindings[device]["device_id"]
            or not local.is_relative_to(path.parent.resolve())
            or local.is_symlink()
        ):
            raise ValueError("Backup is not bound to this staged device and topology")
        if digest(local.read_text(encoding="utf-8")) != record["sha256"]:
            raise ValueError("Backup content was modified")
        if self.native_mode and not record.get("vm_uuid"):
            raise ValueError("Native backup has no verified runtime VM UUID")
        with self.open_channel(doc, bindings[device], topology, record.get("vm_uuid")) as channel:
            result = self._restore(channel, record)
        native = self.is_native(channel)
        return {
            "status": "verified" if native else "output_received",
            "restore": result,
            "rollback_verified": native,
            "protocol_rollback_verified": True,
            "verified_scope": "running_configuration_restored_only"
            if native
            else "synthetic_restore_protocol_only",
            "full_live_gate_passed": False,
        }

    def connectivity(self, project_path, topology, device, destination, count=5):
        if not self.writes_enabled:
            return rejected("writes_disabled", "Sending ping requires the live write switch")
        from ipaddress import ip_address

        destination = str(ip_address(destination))
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
            raise ValueError("Ping count must be 1-10")
        doc, bindings = self.bindings(project_path)
        if device not in bindings:
            raise ValueError("Unknown staged device")
        operation = uuid4().hex
        directory = self._directory(operation)
        with self.open_channel(doc, bindings[device], topology) as channel:
            response = channel.exchange(f"ping -c {count} {destination}")
            (directory / "ping.json").write_text(
                json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        match = re.search(
            r"(\d+)\s+packet(?:\(s\)|s)?\s+transmitted,?\s*(\d+)\s+packet(?:\(s\)|s)?\s+received,?\s*([\d.]+)%\s+packet loss",
            response["output"],
            re.I,
        )
        verified = bool(
            match and int(match[1]) == count and int(match[2]) == count and float(match[3]) == 0
        )
        native = self.is_native(channel)
        return {
            "status": ("verified" if native else "output_received") if verified else "unverified",
            "operation_id": operation,
            "verified_scope": "measured_ping_statistics_only"
            if native
            else "synthetic_ping_statistics_only",
            "connectivity_verified": verified and native,
            "protocol_ping_verified": verified,
            "statistics": None
            if not match
            else {
                "transmitted": int(match[1]),
                "received": int(match[2]),
                "loss_percent": float(match[3]),
            },
            "receipt_directory": str(directory),
            "full_live_gate_passed": False,
        }


def register_deployment_tools(mcp, deployment):
    @mcp.tool()
    def hcl_backup_config(project_path: str, topology: str, device: str) -> dict:
        """Create and test a native running-config backup for a bound staged device (gated)."""
        return deployment.backup(project_path, topology, device)

    @mcp.tool()
    def hcl_restore_config(project_path: str, topology: str, backup_id: str) -> dict:
        """Restore a tested native backup and compare the complete running configuration (gated)."""
        return deployment.restore(project_path, topology, backup_id)

    @mcp.tool()
    def hcl_live_deploy(project_path: str, topology: str, devices: list[str] | None = None) -> dict:
        """Back up all staged devices before deployment; restore modified devices on failure (gated)."""
        return deployment.run(project_path, topology, devices)

    @mcp.tool()
    def hcl_verify_connectivity(
        project_path: str, topology: str, device: str, destination: str, count: int = 5
    ) -> dict:
        """Parse bounded actual ping statistics from a bound staged console; never infer success from exit status."""
        return deployment.connectivity(project_path, topology, device, destination, count)
