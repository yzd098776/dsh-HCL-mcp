"""Bound readback, native PID rejection and formatter conformance; no HCL guests."""

import base64
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from test_console import NamedPipeServer
from test_mcp_contract import call, mcp_session
from test_project_build import configure_fixture, explicit_map, plan_fixture

from mcp_hcl.catalog import HclCatalog
from mcp_hcl.console import ConsoleError, ConsoleSession, open_console_pipe
from mcp_hcl.inspection import QUERIES, Inspection, reply_body
from mcp_hcl.project import ProjectStore
from mcp_hcl.project_build import stage_plan
from mcp_hcl.readback import ReadbackError, interface_states, qos_definitions, vlan_database
from mcp_hcl.runtime_binding import RuntimeBinding

PORTS = """GigabitEthernet0/0
Current state: UP
Line protocol state: UP
IP packet frame type: Ethernet II, hardware address: 0011-2233-4455
Internet address: 10.3.0.1/24 (Primary)
The Maximum Transmit Unit is 1500
1000Mbps-speed mode, full-duplex mode
GigabitEthernet0/1 current state: DOWN (Administratively)
Line protocol current state: DOWN
"""
VLANS = """VLAN ID: 1
VLAN type: Static
Route interface: Not configured
Description: Management
Name: VLAN 0001
Tagged ports: None
Untagged ports:
 GigabitEthernet0/1
VLAN ID: 10
VLAN type: Static
Name: Application
Tagged ports: GigabitEthernet0/0
Untagged ports: None
"""
CLASSIFIERS = """User-defined classifier information:
Classifier: web (ID 17)
 Operator: AND
 Rule(s) :
  If-match acl 3001
"""
BEHAVIORS = """User-defined behavior information:
Behavior: mark (ID 18)
 Marking:
  Remark dscp 46
"""
POLICIES = """User-defined QoS policy information:
Policy: edge (ID 19)
 Classifier: web (ID 17)
  Behavior: mark
   Marking:
    Remark dscp 46
"""


def reply(command, body):
    raw = (command + "\r\n" + body + "\r\n<R1>").encode()
    return {
        "command": command,
        "complete": True,
        "raw_output_b64": base64.b64encode(raw).decode(),
        "output_sha256": hashlib.sha256(raw).hexdigest(),
        "request_sha256": hashlib.sha256(command.encode()).hexdigest(),
    }


class FormatterTests(unittest.TestCase):
    def test_interface_formats_preserve_reported_values_without_cable_guessing(self):
        parsed = interface_states(PORTS)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["mac"], "00:11:22:33:44:55")
        self.assertEqual(parsed[0]["ipv4"]["netmask"], "255.255.255.0")
        self.assertEqual(
            (parsed[0]["mtu"], parsed[0]["speed_bps"], parsed[0]["duplex"]),
            (1500, 1000000000, "full"),
        )
        self.assertTrue(parsed[1]["administratively_down"])
        self.assertTrue(all(row["linked"] is None for row in parsed))
        self.assertFalse(any(row["live_cable_inventory_verified"] for row in parsed))

    def test_detailed_vlan_rows_and_wrapped_ports_are_strict(self):
        parsed = vlan_database(VLANS)
        self.assertEqual([row["number"] for row in parsed], [1, 10])
        self.assertEqual(parsed[0]["untagged_ports"], ["GigabitEthernet0/1"])
        self.assertIsNone(parsed[0]["is_default"])
        for text in (
            "nothing",
            VLANS.replace("VLAN ID: 10", "VLAN ID: 1"),
            VLANS.replace("VLAN ID: 10", "VLAN ID: 4095"),
        ):
            with self.assertRaises(ReadbackError):
                vlan_database(text)

    def test_qos_id_annotations_and_binding_names(self):
        self.assertEqual(
            qos_definitions(CLASSIFIERS, "classifiers")[0]["rules"], ["If-match acl 3001"]
        )
        self.assertEqual(qos_definitions(BEHAVIORS, "behaviors")[0]["name"], "mark")
        self.assertEqual(
            qos_definitions(POLICIES, "policies")[0]["classes"],
            [{"classifier": "web", "behavior": "mark"}],
        )
        for text, kind in (
            ("User-defined classifier information:", "classifiers"),
            (POLICIES.replace("Behavior: mark", "Something: mark"), "policies"),
            ("unrecognized", "behaviors"),
        ):
            with self.assertRaises(ReadbackError):
                qos_definitions(text, kind)

    def test_a_device_with_no_qos_definitions_answers_with_nothing(self):
        # Live Comware prints no header at all when no user-defined QoS exists,
        # so an empty body is an empty database rather than a failed readback.
        for kind in ("classifiers", "behaviors", "policies"):
            with self.subTest(kind=kind):
                self.assertEqual(qos_definitions("", kind), [])
                self.assertEqual(qos_definitions("  \n\t\n", kind), [])

    def test_partial_states_and_tampered_transcript_are_not_empty_success(self):
        with self.assertRaises(ReadbackError):
            interface_states(PORTS.replace("Line protocol state: UP", ""))
        response = reply("display interface", PORTS)
        self.assertIn("GigabitEthernet0/0", reply_body(response, "display interface"))
        for changed in (
            {"complete": False},
            {"output_sha256": "0" * 64},
            {"request_sha256": "0" * 64},
        ):
            with self.assertRaises(ReadbackError):
                reply_body(response | changed, "display interface")


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        paths = configure_fixture(self.root)
        self.store = ProjectStore([paths["root"]], paths["output"])
        self.project = stage_plan(
            self.store,
            HclCatalog(paths["install"]),
            str(paths["project"]),
            "Inspect",
            plan_fixture(),
            explicit_map(),
        )["project_directory"]
        self.commands = []

    def engine(self, *, binding_error=False, response_override=None):
        commands = self.commands

        class Binder:
            def resolve(self, doc, binding, topology):
                if binding_error:
                    raise ConsoleError("Wrong VM identity", code="runtime_binding_unverified")
                return {"vm_uuid": "synthetic", "device_id": binding["device_id"]}

            def verify_pipe(self, channel, target):
                return {**target, "verified_scope": "synthetic_binding_only"}

        class Channel:
            native_channel = True  # Even this injected claim must not elevate a mock.

            def __init__(self, pipe):
                self.history = []

            def exchange(self, command):
                commands.append(command)
                body = {
                    "display interface": PORTS,
                    "display vlan all": VLANS,
                    QUERIES["qos"][0]: CLASSIFIERS,
                    QUERIES["qos"][1]: BEHAVIORS,
                    QUERIES["qos"][2]: POLICIES,
                }[command]
                response = reply(
                    command, response_override if response_override is not None else body
                )
                self.history.append(response)
                return response

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        return Inspection(
            self.store, writes_enabled=True, runtime_binding=Binder(), session_factory=Channel
        )

    def test_default_gate_and_failed_identity_send_no_commands(self):
        self.assertEqual(
            Inspection(self.store).read("ports", "missing", "lab")["errors"][0]["code"],
            "writes_disabled",
        )
        result = self.engine(binding_error=True).read("ports", self.project, "lab", "r1")
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(self.commands, [])

    def test_vlans_and_qos_report_typed_facts_but_injected_channels_are_not_native(self):
        engine = self.engine()
        result = engine.read("vlans", self.project, "lab", "计划路由器一")
        self.assertEqual(result["status"], "output_received")
        self.assertEqual(len(result["devices"][0]["vlans"]), 2)
        self.assertFalse(result["native_readback_verified"])
        qos = engine.read("qos", self.project, "lab", "r1")
        self.assertEqual(qos["devices"][0]["policies"][0]["classes"][0]["behavior"], "mark")
        self.assertEqual(self.commands, ["display vlan all", *QUERIES["qos"]])
        self.assertTrue(all(command.startswith("display ") for command in self.commands))
        self.assertFalse(qos["full_live_gate_passed"])

    def test_unrecognized_output_retains_failure_transcript_and_link_filter_never_guesses(self):
        engine = self.engine(response_override="unsupported output")
        result = engine.read("ports", self.project, "lab", "r1")
        self.assertEqual(result["status"], "unverified")
        transcript = Path(result["evidence"][0]["source"])
        self.assertIn(
            "unsupported output",
            base64.b64decode(json.loads(transcript.read_text())[0]["raw_output_b64"]).decode(),
        )
        self.commands.clear()
        filtered = engine.read("ports", self.project, "lab", "r1", True)
        self.assertEqual(filtered["errors"][0]["code"], "live_cable_inventory_unverified")
        self.assertEqual(self.commands, [])


class RuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.project = self.home / "staged"
        (self.project / "ConfigDisk").mkdir(parents=True)
        self.net = self.project / "project.net"
        self.net.write_text("synthetic net")
        self.disk = self.project / "ConfigDisk/device3.vdi"
        self.disk.write_bytes(b"synthetic disk")
        self.cfg = self.home / "HCL VMs/lab/device3/lab-device3.vbox"
        self.cfg.parent.mkdir(parents=True)
        self.vm_uuid = str(uuid4())
        self.disk_uuid = str(uuid4())
        self.cfg.write_text(
            f'<VirtualBox><Machine name="lab-device3" uuid="{{{self.vm_uuid}}}"><MediaRegistry><HardDisks><HardDisk uuid="{{{self.disk_uuid}}}" location="{self.disk.as_posix()}"/></HardDisks></MediaRegistry><Hardware><UART><Port hostMode="HostPipe" server="true" path="\\\\.\\pipe\\lab-device3"/></UART></Hardware><StorageControllers><StorageController><AttachedDevice type="HardDisk"><Image uuid="{{{self.disk_uuid}}}"/></AttachedDevice></StorageController></StorageControllers></Machine></VirtualBox>'
        )
        self.values = {
            "name": "lab-device3",
            "UUID": self.vm_uuid,
            "VMState": "running",
            "CfgFile": str(self.cfg),
            "IDE-0-0": str(self.disk),
        }
        self.document = SimpleNamespace(directory=self.project, net_path=self.net)

    def binder(self):
        def run(args, **kwargs):
            data = (
                json.dumps(
                    {"profile": str(self.home), "sid": "S-1-5-21-1", "vbox": "D:/VBox"}
                ).encode()
                if args[0] == "powershell.exe"
                else "\n".join(
                    key + "=" + json.dumps(value) for key, value in self.values.items()
                ).encode()
            )
            return SimpleNamespace(returncode=0, stdout=data)

        return RuntimeBinding(runner=run)

    def test_running_descriptor_registered_storage_and_project_disk_are_all_required(self):
        target = self.binder().resolve(self.document, {"device_id": 3}, "lab")
        self.assertEqual(target["vm_uuid"], self.vm_uuid)
        self.assertFalse(target["runtime_isolation_verified"])
        for field, value in (
            ("UUID", str(uuid4())),
            ("VMState", "poweroff"),
            ("CfgFile", str(self.home / "other.vbox")),
            ("IDE-0-0", str(self.home / "other.vdi")),
        ):
            before = self.values[field]
            self.values[field] = value
            with self.subTest(field=field), self.assertRaises(ConsoleError):
                self.binder().resolve(self.document, {"device_id": 3}, "lab")
            self.values[field] = before

    def test_declaring_but_not_attaching_a_project_disk_fails(self):
        self.cfg.write_text(
            self.cfg.read_text().replace('<Image uuid="{' + self.disk_uuid + '}"/>', "")
        )
        with self.assertRaises(ConsoleError):
            self.binder().resolve(self.document, {"device_id": 3}, "lab")

    def test_native_dmi_board_serial_etx_keeps_raw_identity_and_storage_checks(self):
        raw = self.cfg.read_bytes().replace(
            b"<MediaRegistry>",
            b'<ExtraData><ExtraDataItem name="VBoxInternal/Devices/pcbios/0/Config/DmiBoardSerial" value="\x03\x03\x038888\x03\x03\x03\x03"/></ExtraData><MediaRegistry>',
        )
        self.cfg.write_bytes(raw)
        target = self.binder().resolve(self.document, {"device_id": 3}, "lab")
        self.assertEqual(target["vm_uuid"], self.vm_uuid)
        self.assertEqual(target["descriptor_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.cfg.read_bytes(), raw)
        self.assertFalse(target["runtime_isolation_verified"])
        self.values["UUID"] = str(uuid4())
        with self.assertRaises(ConsoleError):
            self.binder().resolve(self.document, {"device_id": 3}, "lab")

    def test_board_serial_compatibility_does_not_clean_unknown_views_or_controls(self):
        base = self.cfg.read_bytes()
        serial = b'<ExtraDataItem name="VBoxInternal/Devices/pcbios/0/Config/DmiBoardSerial" value="\x038888\x03"/>'
        cases = {
            "unknown_name": b"<ExtraData>"
            + serial.replace(b"DmiBoardSerial", b"OtherName")
            + b"</ExtraData>",
            "wrong_view": b"<Hardware>" + serial + b"</Hardware>",
            "comment": b"<!--" + serial + b"-->",
            "cdata": b"<![CDATA[" + serial + b"]]>",
            "instruction": b"<?probe " + serial + b"?>",
            "other_control": b"<ExtraData>" + serial.replace(b"\x03", b"\x04") + b"</ExtraData>",
        }
        for name, fragment in cases.items():
            with self.subTest(name=name):
                self.cfg.write_bytes(
                    base.replace(b"<MediaRegistry>", fragment + b"<MediaRegistry>")
                )
                with self.assertRaises(ConsoleError):
                    self.binder().resolve(self.document, {"device_id": 3}, "lab")
        self.cfg.write_bytes(base.replace(b"lab-device3", b"lab-\x03device3", 1))
        with self.assertRaises(ConsoleError):
            self.binder().resolve(self.document, {"device_id": 3}, "lab")

    def test_descriptor_dtd_is_rejected_in_utf8_and_utf16(self):
        base = self.cfg.read_text()
        for encoding in ("utf-8", "utf-16"):
            with self.subTest(encoding=encoding):
                data = (
                    f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE VirtualBox [<!ENTITY x "unused">]>'
                    + base
                ).encode(encoding)
                self.cfg.write_bytes(data)
                with self.assertRaisesRegex(ConsoleError, "Unsafe VM descriptor declarations"):
                    self.binder().resolve(self.document, {"device_id": 3}, "lab")

    def test_real_pipe_server_that_is_not_a_virtualbox_process_is_rejected(self):
        pipe = rf"\\.\pipe\identity{uuid4().hex}-device1"
        server = NamedPipeServer(pipe, expect_client_rejection=True)
        server.start()
        fd = open_console_pipe(pipe, timeout=1)
        channel = SimpleNamespace(session=ConsoleSession(fd))
        binder = RuntimeBinding(
            runner=lambda *args, **kwargs: SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "name": "python.exe",
                        "sid": "S-1-5-21-1",
                        "owner_result": 0,
                        "command": "--startvm " + self.vm_uuid,
                    }
                ).encode(),
            )
        )
        try:
            with self.assertRaisesRegex(ConsoleError, "expected current-user VirtualBox process"):
                binder.verify_pipe(channel, {"owner_sid": "S-1-5-21-1", "vm_uuid": self.vm_uuid})
            self.assertEqual(server.received, [])
            self.assertEqual(channel.session.last_write_bytes, 0)
        finally:
            channel.session.close()
            server.close()


class VdiDiskUuidTests(unittest.TestCase):
    """The staged copy and the running VM's disk are the same disk by UUID."""

    def vdi(self, path, disk_uuid):
        from mcp_hcl.runtime_binding import _vdi_uuid_bytes

        header = bytearray(512)
        header[0:4] = b"<<< "
        header[0x40:0x44] = (0xBEDA107F).to_bytes(4, "little")
        header[0x44:0x48] = (0x00010001).to_bytes(4, "little")
        header[0x188:0x198] = _vdi_uuid_bytes(disk_uuid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(header))

    def test_a_vdi_reports_the_disk_uuid_from_its_linkage_field(self):
        from mcp_hcl.runtime_binding import vdi_disk_uuid

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "device1.vdi"
            disk_uuid = str(uuid4())
            self.vdi(path, disk_uuid)
            self.assertEqual(vdi_disk_uuid(path), disk_uuid)

    def test_something_that_is_not_a_vdi_has_no_disk_uuid(self):
        from mcp_hcl.runtime_binding import vdi_disk_uuid

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "device1.vdi"
            path.write_bytes(b"synthetic disk")
            self.assertIsNone(vdi_disk_uuid(path))
            self.assertIsNone(vdi_disk_uuid(Path(temporary) / "absent.vdi"))


class BridgeEvidenceTests(unittest.TestCase):
    """The telnet bridge is identified without any privileged process access."""

    def test_the_bridge_pipe_mapping_comes_from_hcls_own_log(self):
        from mcp_hcl.runtime_binding import bridge_pipes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "HCL.log").write_text(
                "2026-10-21 13:02:25,362 - HCL topo1 - INFO: create_telnet_server success "
                "strPipeName:\\\\.\\pipe\\topo1-device1, telnet_port:30001)\n"
                "2026-10-21 13:02:25,400 - HCL topo1 - INFO: create_telnet_server success "
                "strPipeName:\\\\.\\pipe\\topo1-device2, telnet_port:30002)\n",
                encoding="utf-8",
            )
            self.assertEqual(bridge_pipes(root, 30001), ["\\\\.\\pipe\\topo1-device1"])
            self.assertEqual(bridge_pipes(root, 30009), [])

    def test_the_telnet_branch_accepts_the_hcl_bridge_process(self):
        from unittest import mock

        from mcp_hcl.runtime_binding import RuntimeBinding

        target = {
            "device_id": 1,
            "pipe": "\\\\.\\pipe\\topo1-device1",
            "vm_uuid": str(uuid4()),
            "owner_sid": "S-1-5-21-1",
        }
        channel = SimpleNamespace(transport="telnet")
        with (
            mock.patch("mcp_hcl.runtime_binding.listener_pids", return_value=[4321]),
            mock.patch(
                "mcp_hcl.runtime_binding.process_image", return_value="E:/HCL/SimwareClient.exe"
            ),
            mock.patch("mcp_hcl.runtime_binding.process_session", return_value=1),
            mock.patch("mcp_hcl.runtime_binding.current_session", return_value=1),
            mock.patch(
                "mcp_hcl.runtime_binding.bridge_pipes",
                return_value=["\\\\.\\pipe\\topo1-device1"],
            ),
        ):
            identity = RuntimeBinding().verify_pipe(channel, target)
        self.assertEqual(identity["console_transport"], "telnet")
        self.assertEqual(identity["console_server_pid"], 4321)
        self.assertEqual(identity["telnet_port"], 30001)
        self.assertEqual(
            identity["verified_scope"],
            "running_vm_staged_config_disk_and_hcl_telnet_bridge_identity_only",
        )

    def test_a_bridge_that_is_not_hcl_or_not_in_this_session_is_rejected(self):
        from unittest import mock

        from mcp_hcl.runtime_binding import RuntimeBinding

        target = {
            "device_id": 1,
            "pipe": "\\\\.\\pipe\\topo1-device1",
            "vm_uuid": str(uuid4()),
            "owner_sid": "S-1-5-21-1",
        }
        channel = SimpleNamespace(transport="telnet")
        cases = {
            "not_hcl": {"listener_pids": [4321], "process_image": "C:/Windows/System32/telnet.exe"},
            "no_listener": {"listener_pids": []},
            "two_listeners": {"listener_pids": [4321, 4322]},
            "other_session": {"process_session": 2},
            "unrecorded_bridge": {"bridge_pipes": ["\\\\.\\pipe\\other-device1"]},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                patches = {
                    "listener_pids": [4321],
                    "process_image": "E:/HCL/SimwareClient.exe",
                    "process_session": 1,
                    "current_session": 1,
                    "bridge_pipes": ["\\\\.\\pipe\\topo1-device1"],
                }
                patches.update(overrides)
                with (
                    mock.patch(
                        "mcp_hcl.runtime_binding.listener_pids",
                        return_value=patches["listener_pids"],
                    ),
                    mock.patch(
                        "mcp_hcl.runtime_binding.process_image",
                        return_value=patches["process_image"],
                    ),
                    mock.patch(
                        "mcp_hcl.runtime_binding.process_session",
                        return_value=patches["process_session"],
                    ),
                    mock.patch(
                        "mcp_hcl.runtime_binding.current_session",
                        return_value=patches["current_session"],
                    ),
                    mock.patch(
                        "mcp_hcl.runtime_binding.bridge_pipes",
                        return_value=patches["bridge_pipes"],
                    ),
                    self.assertRaises(ConsoleError),
                ):
                    RuntimeBinding().verify_pipe(channel, target)


class StagedDiskBindingTests(unittest.TestCase):
    """A staged copy binds to the running VM through the shared disk UUID."""

    def setUp(self):
        from mcp_hcl.runtime_binding import _vdi_uuid_bytes

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.project = self.home / "staged"
        (self.project / "ConfigDisk").mkdir(parents=True)
        self.net = self.project / "project.net"
        self.net.write_text("synthetic net")
        self.disk = self.project / "ConfigDisk/device3.vdi"
        self.vm_uuid = str(uuid4())
        self.disk_uuid = str(uuid4())
        header = bytearray(512)
        header[0:4] = b"<<< "
        header[0x40:0x44] = (0xBEDA107F).to_bytes(4, "little")
        header[0x44:0x48] = (0x00010001).to_bytes(4, "little")
        header[0x188:0x198] = _vdi_uuid_bytes(self.disk_uuid)
        self.disk.write_bytes(bytes(header))

        # The running VM mounts the *source* project's disk, not the staged copy;
        # both files carry the same VirtualBox disk UUID.
        live = self.home / "source/ConfigDisk/device3.vdi"
        live.parent.mkdir(parents=True)
        live.write_bytes(bytes(header))
        self.cfg = self.home / "HCL VMs/lab/device3/lab-device3.vbox"
        self.cfg.parent.mkdir(parents=True)
        self.cfg.write_text(
            f'<VirtualBox><Machine name="lab-device3" uuid="{{{self.vm_uuid}}}">'
            f'<MediaRegistry><HardDisks><HardDisk uuid="{{{self.disk_uuid}}}" '
            f'location="{live.as_posix()}"/></HardDisks></MediaRegistry>'
            f'<Hardware><UART><Port hostMode="HostPipe" server="true" '
            f'path="\\\\.\\pipe\\lab-device3"/></UART></Hardware>'
            f"<StorageControllers><StorageController>"
            f'<AttachedDevice type="HardDisk"><Image uuid="{{{self.disk_uuid}}}"/>'
            f"</AttachedDevice></StorageController></StorageControllers></Machine></VirtualBox>"
        )
        self.values = {
            "name": "lab-device3",
            "UUID": self.vm_uuid,
            "VMState": "running",
            "CfgFile": str(self.cfg),
            "IDE-ImageUUID-0-0": self.disk_uuid,
        }
        self.document = SimpleNamespace(directory=self.project, net_path=self.net)

    def binder(self):
        def run(args, **kwargs):
            data = (
                json.dumps(
                    {"profile": str(self.home), "sid": "S-1-5-21-1", "vbox": "D:/VBox"}
                ).encode()
                if args[0] == "powershell.exe"
                else "\n".join(
                    key + "=" + json.dumps(value) for key, value in self.values.items()
                ).encode()
            )
            return SimpleNamespace(returncode=0, stdout=data)

        return RuntimeBinding(runner=run)

    def test_a_shared_disk_uuid_binds_the_staged_copy_to_the_running_vm(self):
        target = self.binder().resolve(self.document, {"device_id": 3}, "lab")
        self.assertEqual(target["configuration_disk_binding"], "disk_uuid")
        self.assertEqual(target["configuration_disk_uuid"], self.disk_uuid)
        self.assertEqual(target["vm_uuid"], self.vm_uuid)

    def test_a_different_disk_uuid_is_still_refused(self):
        self.values["IDE-ImageUUID-0-0"] = str(uuid4())
        with self.assertRaisesRegex(ConsoleError, "does not mount the staged configuration disk"):
            self.binder().resolve(self.document, {"device_id": 3}, "lab")

    def test_a_staged_disk_that_is_not_a_vdi_cannot_be_bound(self):
        self.disk.write_bytes(b"synthetic disk")
        with self.assertRaisesRegex(ConsoleError, "not a readable VDI"):
            self.binder().resolve(self.document, {"device_id": 3}, "lab")

    def test_a_path_binding_is_still_reported_as_such(self):
        self.cfg.write_text(
            self.cfg.read_text().replace(
                (self.home / "source/ConfigDisk/device3.vdi").as_posix(),
                self.disk.as_posix(),
            )
        )
        self.values.pop("IDE-ImageUUID-0-0")
        self.values["IDE-0-0"] = str(self.disk)
        target = self.binder().resolve(self.document, {"device_id": 3}, "lab")
        self.assertEqual(target["configuration_disk_binding"], "path")


class MachineValuesTests(unittest.TestCase):
    """VBoxManage mixes JSON-escaped and raw-backslash values in one listing."""

    def test_both_escaping_conventions_decode_to_the_on_disk_string(self):
        from mcp_hcl.runtime_binding import machine_values

        # Captured from ``VBoxManage showvminfo topo1-device1 --machinereadable``:
        # CfgFile is JSON-escaped (double backslashes), IDE-1-0 and uartmode2 are
        # raw single backslashes, and VideoMode carries quotes that are not a
        # wrapper. ``json.loads`` alone rejects every backslash field.
        listing = (
            'name="topo1-device1"\n'
            'CfgFile="C:\\\\Users\\\\yzdwxl\\\\HCL VMs\\\\topo1\\\\device1\\\\x.vbox"\n'
            '"IDE-1-0"="c:\\\\users\\\\yzdwxl\\\\hcl\\\\ConfigDisk\\\\device1.vdi"\n'
            'uartmode2="server,\\\\\\\\.\\\\pipe\\\\topo1-device1"\n'
            'VideoMode="720,400,0"@0,0 1\n'
        )
        values = machine_values(listing)
        self.assertEqual(values["name"], "topo1-device1")
        self.assertEqual(values["CfgFile"], "C:\\Users\\yzdwxl\\HCL VMs\\topo1\\device1\\x.vbox")
        self.assertEqual(values["IDE-1-0"], "c:\\users\\yzdwxl\\hcl\\ConfigDisk\\device1.vdi")
        self.assertEqual(values["uartmode2"], "server,\\\\.\\pipe\\topo1-device1")
        self.assertEqual(values["VideoMode"], '"720,400,0"@0,0 1')

    def test_quoted_storage_key_is_unwrapped_and_a_duplicate_is_refused(self):
        from mcp_hcl.runtime_binding import machine_values

        self.assertEqual(machine_values('"IDE-1-0"="/x/y.vdi"')["IDE-1-0"], "/x/y.vdi")
        with self.assertRaises(ConsoleError):
            machine_values('name="a"\nname="b"\n')


class IntegrityDiagnosticTests(unittest.TestCase):
    """A denied COM client must not be reported as a VM identity mismatch."""

    def test_a_denied_vboxmanage_explains_the_token_instead_of_the_vm(self):
        from mcp_hcl.runtime_binding import _observation_failure

        denied = SimpleNamespace(
            stdout=b"",
            stderr=(
                b"VBoxManage.exe: error: Failed to create the VirtualBox object!\n"
                b"VBoxManage.exe: error: Details: code E_ACCESSDENIED (0x80070005), "
                b"component VirtualBoxClientWrap, interface IVirtualBoxClient\n"
            ),
        )
        message = _observation_failure(denied)
        self.assertIn("cannot reach VirtualBox", message)
        self.assertIn("token integrity", message)
        self.assertNotIn("does not match", message)

    def test_an_unrelated_vboxmanage_failure_stays_an_identity_failure(self):
        from mcp_hcl.runtime_binding import _observation_failure

        other = SimpleNamespace(stdout=b"", stderr=b"VBoxManage.exe: error: no such VM\n")
        self.assertEqual(_observation_failure(other), "Registered VM identity observation failed")

    def test_the_reported_integrity_level_is_a_known_name(self):
        from mcp_hcl.runtime_binding import token_integrity

        observed = token_integrity()
        if "error" in observed:
            self.skipTest(f"integrity unavailable: {observed['error']}")
        self.assertIn(observed["level"], {"untrusted", "low", "medium", "high"})
        self.assertIsInstance(observed["rid"], int)


class InspectionStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspection_surface_is_visible_and_all_queries_are_off_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = configure_fixture(Path(temporary))
            async with mcp_session(paths) as session:
                names = {tool.name for tool in (await session.list_tools()).tools}
                self.assertTrue({"hcl_inspect_ports", "hcl_read_vlans", "hcl_read_qos"} <= names)
                for tool in ("hcl_inspect_ports", "hcl_read_vlans", "hcl_read_qos"):
                    response, body = await call(
                        session,
                        tool,
                        {"project_path": "missing", "topology": "lab", "device": "r1"},
                    )
                    self.assertFalse(response.isError)
                    self.assertEqual(body["errors"][0]["code"], "writes_disabled")
                    self.assertFalse(body["full_live_gate_passed"])
