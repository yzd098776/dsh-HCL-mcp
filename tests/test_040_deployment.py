import tempfile
import unittest
from pathlib import Path

from test_project_build import configure_fixture, explicit_map, plan_fixture

from mcp_hcl.catalog import HclCatalog
from mcp_hcl.console import ConsoleError
from mcp_hcl.deployment import Deployment, verification_commands
from mcp_hcl.project import ProjectStore
from mcp_hcl.project_build import stage_plan

BASE = "#\nsysname factory\n#\nreturn\n"


class FakeComware:
    def __init__(self, device, fail=False, rollback_fail=False, backup_bad=False):
        self.device = device
        self.fail = fail
        self.rollback_fail = rollback_fail
        self.backup_bad = backup_bad
        self.current = BASE
        self.files = {}
        self.history = []
        self.applied = False
        self.startup = {"current": None, "main": None, "backup": None}
        self.expected = f"#\nsysname {device}\ninterface GigabitEthernet0/{0 if device == 'r1' else 1}\n ip address 10.0.0.{1 if device == 'r1' else 2} 255.255.255.252\n#\nreturn\n"

    def exchange(self, command, **kwargs):
        self.history.append({"command": command})
        if command == "display startup":
            output = (
                "\n".join(
                    f"{label} startup saved-configuration file: {self.startup[key] or 'NULL'}"
                    for label, key in (
                        ("Current", "current"),
                        ("Next main", "main"),
                        ("Next backup", "backup"),
                    )
                )
                + "\n"
            )
            return {"output": command + "\n" + output + "<R1>", "complete": True}
        if command == "display saved-configuration":
            return {
                "output": self.files[self.startup["main"] or self.startup["backup"]],
                "complete": True,
            }
        if command == "undo startup saved-configuration":
            self.startup["main"] = None
            self.startup["backup"] = None
        if command.startswith("startup saved-configuration "):
            self.startup[command.split()[-1]] = command.split()[2]
        if command.startswith("save "):
            self.files[command[5:]] = self.current
        if command.startswith("more "):
            output = "not a configuration" if self.backup_bad else self.files[command[5:]]
        elif command == "display current-configuration":
            output = self.current
        elif command.startswith("configuration replace file "):
            if self.rollback_fail and self.applied:
                raise ConsoleError("native rollback failed")
            self.current = self.files[command.split()[-1]]
            output = "Configuration replaced"
        elif command.startswith("sysname "):
            self.applied = True
            self.current = "#\nsysname " + self.device + "\n#\nreturn\n"
            output = ""
        elif command.startswith("ip address "):
            if self.fail:
                raise ConsoleError("command rejected")
            self.current = self.expected
            output = ""
        elif command.startswith("ping "):
            output = "5 packet(s) transmitted, 5 packet(s) received, 0.0% packet loss"
        else:
            output = ""
        return {"output": command + "\n" + output + "<R1>", "complete": True}

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Deployment040Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        paths = configure_fixture(Path(self.temporary.name))
        self.store = ProjectStore([paths["root"]], paths["output"])
        self.catalog = HclCatalog(paths["install"])
        self.project = stage_plan(
            self.store,
            self.catalog,
            str(paths["project"]),
            "Deploy",
            plan_fixture(),
            explicit_map(),
        )["project_directory"]

    def engine(self, channels):
        return Deployment(
            self.store,
            catalog=self.catalog,
            writes_enabled=True,
            # The live console is the telnet bridge (30000 + device number), so
            # the fixture maps devices by the port it is asked to open.
            session_factory=lambda pipe: channels["r1" if pipe.endswith(":30003") else "r2"],
        )

    def test_all_backups_before_configuration_and_actual_readback(self):
        channels = {"r1": FakeComware("r1"), "r2": FakeComware("r2")}
        result = self.engine(channels).run(self.project, "lab")
        self.assertEqual(result["status"], "output_received", result.get("errors"))
        self.assertTrue(result["protocol_configuration_verified"])
        self.assertFalse(result["configuration_verified"])
        self.assertFalse(result["full_live_gate_passed"])
        self.assertEqual(
            [step["stage"] for step in result["steps"][:2]], ["backup_and_restore_preflight"] * 2
        )
        self.assertTrue(all(b["protocol_restore_test_verified"] for b in result["backups"]))
        self.assertFalse(any(b["restore_test_verified"] for b in result["backups"]))
        self.assertTrue(Path(result["receipt_directory"], "r1-transcript.json").is_file())

    def test_second_device_failure_restores_every_modified_device(self):
        channels = {"r1": FakeComware("r1"), "r2": FakeComware("r2", fail=True)}
        result = self.engine(channels).run(self.project, "lab")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["protocol_rollback_verified"])
        self.assertFalse(result["rollback_verified"])
        self.assertEqual(channels["r1"].current, BASE)
        self.assertEqual(channels["r2"].current, BASE)
        self.assertEqual([r["device"] for r in result["rollback"]], ["r2", "r1"])

    def test_rollback_failure_is_not_reported_as_recovered(self):
        channels = {"r1": FakeComware("r1"), "r2": FakeComware("r2", fail=True, rollback_fail=True)}
        result = self.engine(channels).run(self.project, "lab")
        self.assertFalse(result["rollback_verified"])
        self.assertIn("manual_restore_command", result["rollback"][0])

    def test_unusable_backup_stops_before_any_configuration(self):
        channels = {"r1": FakeComware("r1", backup_bad=True), "r2": FakeComware("r2")}
        result = self.engine(channels).run(self.project, "lab")
        self.assertEqual(result["status"], "partial")
        self.assertFalse(any(c.applied for c in channels.values()))
        self.assertFalse(result["configuration_verified"])

    def test_ping_statistics_and_gate_not_exit_status(self):
        channels = {"r1": FakeComware("r1"), "r2": FakeComware("r2")}
        result = self.engine(channels).connectivity(self.project, "lab", "r1", "10.0.0.2")
        self.assertTrue(result["protocol_ping_verified"])
        self.assertFalse(result["connectivity_verified"])
        self.assertEqual(result["statistics"]["received"], 5)
        self.assertEqual(
            Deployment(self.store).run("missing", "lab")["errors"][0]["code"], "writes_disabled"
        )

    def test_injected_channel_native_flag_cannot_claim_hardware_configuration(self):
        channels = {"r1": FakeComware("r1"), "r2": FakeComware("r2")}
        for channel in channels.values():
            channel.native_channel = True
            channel.target_identity = {"vm_uuid": "unverified"}
        result = self.engine(channels).run(self.project, "lab")
        self.assertTrue(result["protocol_configuration_verified"])
        self.assertFalse(result["configuration_verified"])
        self.assertFalse(result["native_channels"])

    def test_synthetic_backup_restore_and_ping_cannot_gain_native_verification(self):
        channels = {"r1": FakeComware("r1"), "r2": FakeComware("r2")}
        for channel in channels.values():
            channel.native_channel = True
            channel.target_identity = {"vm_uuid": "unverified"}
        engine = self.engine(channels)
        backup = engine.backup(self.project, "lab", "r1")
        self.assertTrue(backup["protocol_backup_verified"])
        self.assertFalse(backup["backup_verified"])
        self.assertFalse(backup["backup"]["restore_test_verified"])
        restored = engine.restore(self.project, "lab", backup["backup_id"])
        self.assertTrue(restored["protocol_rollback_verified"])
        self.assertFalse(restored["rollback_verified"])
        ping = engine.connectivity(self.project, "lab", "r1", "10.0.0.2")
        self.assertTrue(ping["protocol_ping_verified"])
        self.assertFalse(ping["connectivity_verified"])

    def test_readback_cannot_match_a_command_in_the_wrong_interface(self):
        self.assertFalse(
            verification_commands(
                [
                    "system-view",
                    "interface GigabitEthernet0/0",
                    "ip address 10.0.0.1 255.255.255.0",
                    "quit",
                    "return",
                ],
                "#\ninterface GigabitEthernet0/1\n ip address 10.0.0.1 255.255.255.0\nreturn\n",
            )
        )
