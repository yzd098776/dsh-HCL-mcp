"""Necessary VLAN model/readback and multi-device transaction boundaries."""

import copy
import tempfile
import unittest
from pathlib import Path

import test_policy as fixtures
from contract_inventory import TOOLS
from pydantic import ValidationError
from test_mcp_contract import call, mcp_session
from test_project_build import configure_fixture, explicit_map, plan_fixture, snapshot

from mcp_hcl.catalog import HclCatalog
from mcp_hcl.console import ConsoleError
from mcp_hcl.generation import render_vlan
from mcp_hcl.policy import Policy
from mcp_hcl.project import ProjectStore
from mcp_hcl.project_build import stage_plan
from mcp_hcl.services import Vlan, VlanPort
from mcp_hcl.vlan_network import VlanNetwork, VlanNetworkRequest
from mcp_hcl.vlan_semantics import exact_vlan_commands, permit_commands, verify_vlan

REQUEST = {
    "switch": "r1",
    "router": "r2",
    "vlans": [{"vlan_id": 10, "name": "User LAN"}, {"vlan_id": 20, "name": "Servers"}],
    "trunks": [
        {"switch": "r1", "port": "GigabitEthernet0/0", "allowed_vlans": [10, 20], "native_vlan": 10}
    ],
    "subinterfaces": [
        {
            "router": "r2",
            "parent_port": "GigabitEthernet0/1",
            "vlan_id": 10,
            "ip_cidr": "192.168.10.1/24",
        },
        {
            "router": "r2",
            "parent_port": "GigabitEthernet0/1",
            "vlan_id": 20,
            "ip_cidr": "192.168.20.1/24",
        },
    ],
}


class VlanModelReadbackTests(unittest.TestCase):
    def test_name_native_vlan_and_ranges_compare_semantics(self):
        model = Vlan.model_validate(
            {
                "vlans": [10, 11, 12],
                "names": [{"vlan": 10, "name": "User LAN"}],
                "ports": [
                    {
                        "interface": "GigabitEthernet0/0",
                        "mode": "trunk",
                        "allowed_vlans": [10, 11, 12],
                        "native_vlan": 10,
                    }
                ],
            }
        )
        config = "#\nvlan 10\n name User LAN\n#\nvlan 11\n#\nvlan 12\n#\ninterface GigabitEthernet0/0\n port link-type trunk\n port trunk permit vlan 10 to 12\n port trunk pvid vlan 10\n#\nreturn\n"
        self.assertTrue(verify_vlan(config, model))
        self.assertFalse(verify_vlan(config.replace("User LAN", "user LAN"), model))
        self.assertFalse(verify_vlan(config.replace("pvid vlan 10", "pvid vlan 1"), model))
        self.assertFalse(verify_vlan(config.replace("10 to 12", "10 to 11"), model))
        self.assertIn("name User LAN", render_vlan(model))
        for value in (
            {"interface": "GigabitEthernet0/0", "mode": "access", "vlan": 10, "native_vlan": 1},
            {"interface": "GigabitEthernet0/0", "mode": "trunk", "allowed_vlans": [10, 10]},
        ):
            with self.assertRaises(ValidationError):
                VlanPort.model_validate(value)

    def test_all_and_default_pvid_and_command_batches_have_one_clear(self):
        model = Vlan.model_validate(
            {
                "vlans": [10],
                "ports": [{"interface": "GigabitEthernet0/0", "mode": "trunk", "native_vlan": 1}],
            }
        )
        self.assertTrue(
            verify_vlan(
                "#\nvlan 10\n#\ninterface GigabitEthernet0/0\n port link-type trunk\n port trunk permit vlan 1 to 4094\n#\nreturn\n",
                model,
            )
        )
        ids = list(range(1, 42, 2))
        port = Vlan.model_validate(
            {
                "vlans": ids,
                "ports": [
                    {"interface": "GigabitEthernet0/0", "mode": "trunk", "allowed_vlans": ids}
                ],
            }
        )
        commands = exact_vlan_commands(render_vlan(port))
        self.assertEqual(commands.count("undo port trunk permit vlan all"), 1)
        self.assertEqual(
            len([command for command in commands if command.startswith("port trunk permit vlan ")]),
            3,
        )
        self.assertEqual(permit_commands([10, 11, 12]), ["port trunk permit vlan 10 to 12"])


class NetworkFixture(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PolicyTests("test_master_gate_and_dry_run_do_not_open_a_console")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.guests = {"r1": fixtures.Guest(), "r2": fixtures.Guest()}
        self.events = []
        self.opens = {}
        self.recovery_changed = False

    def engine(self, writes=True):
        test = self

        class Binder:
            def resolve(self, document, binding, topology):
                identifier = binding["plan_id"]
                return {
                    "vm_uuid": identifier
                    + (
                        "-changed"
                        if test.recovery_changed and test.opens.get(identifier, 0)
                        else ""
                    )
                }

            def verify_pipe(self, channel, target):
                return target

        class Channel:
            native_channel = True

            def __init__(self, pipe):
                # The live console is the telnet bridge (30000 + device number),
                # so the fixture maps devices by the port it is asked to open.
                self.identifier = "r1" if pipe.endswith(":30003") else "r2"
                self.history = []
                test.opens[self.identifier] = test.opens.get(self.identifier, 0) + 1

            def exchange(self, command, **kwargs):
                test.events.append((self.identifier, command))
                self.history.append({"command": command})
                return {
                    "output": command
                    + "\n"
                    + test.guests[self.identifier].exchange(command)
                    + "\n<R1>",
                    "complete": True,
                }

            def close(self):
                pass

        policy = Policy(
            self.fixture.store,
            self.fixture.catalog,
            writes_enabled=writes,
            runtime_binding=Binder(),
            session_factory=Channel,
        )
        return VlanNetwork(policy)


class NetworkTransactionTests(NetworkFixture):
    def test_switch_and_router_complete_after_all_backups_preserving_other_fields(self):
        result = self.engine().apply(self.fixture.project, "lab", REQUEST)
        self.assertTrue(result["protocol_effect_verified"], result)
        self.assertTrue(result["protocol_persistence_verified"])
        self.assertFalse(result["configuration_verified"])
        self.assertFalse(result["persistence_verified"])
        self.assertIn("name User LAN", self.guests["r1"].current)
        self.assertIn("port trunk pvid vlan 10", self.guests["r1"].current)
        self.assertIn("interface GigabitEthernet0/1.20", self.guests["r2"].current)
        self.assertIn("ip address 192.168.20.1 255.255.255.0", self.guests["r2"].current)
        first_change = next(
            index for index, (_, command) in enumerate(self.events) if command == "vlan 10"
        )
        self.assertTrue(
            all(
                any(
                    owner == identifier and command.startswith("configuration replace file ")
                    for owner, command in self.events[:first_change]
                )
                for identifier in self.guests
            )
        )
        self.assertEqual(snapshot(Path(self.fixture.project)), self.fixture.before)

    def test_default_gate_dry_run_and_alias_collision_precede_native_io(self):
        engine = self.engine(False)
        self.assertEqual(
            engine.apply("missing", "lab", REQUEST)["errors"][0]["code"], "writes_disabled"
        )
        dry = engine.apply(self.fixture.project, "lab", REQUEST, True)
        self.assertEqual(dry["status"], "verified")
        self.assertFalse(self.opens)
        duplicate = copy.deepcopy(REQUEST)
        duplicate["access_ports"] = [
            {"switch": "计划路由器一", "port": "GigabitEthernet0/0", "vlan_id": 10}
        ]
        with self.assertRaises(ValueError):
            engine.apply(self.fixture.project, "lab", duplicate, True)
        with self.assertRaises(ValidationError):
            VlanNetworkRequest.model_validate(
                REQUEST
                | {"subinterfaces": [REQUEST["subinterfaces"][0] | {"ip_cidr": "192.168.10.0/24"}]}
            )

    def test_second_device_command_failure_restores_both_without_replay(self):
        before = {identifier: guest.current for identifier, guest in self.guests.items()}
        startup = {identifier: dict(guest.startup) for identifier, guest in self.guests.items()}
        self.guests["r2"].fail = "vlan-type "
        result = self.engine().apply(self.fixture.project, "lab", REQUEST)
        self.assertTrue(result["protocol_rollback_verified"], result)
        self.assertFalse(result["rollback_verified"])
        self.assertEqual(
            {identifier: guest.current for identifier, guest in self.guests.items()}, before
        )
        self.assertEqual(
            {identifier: guest.startup for identifier, guest in self.guests.items()}, startup
        )
        self.assertEqual(self.events.count(("r1", "vlan 10")), 1)

    def test_unusable_second_backup_stops_before_any_vlan_or_subinterface_change(self):
        self.guests["r2"].backup_bad = True
        result = self.engine().apply(self.fixture.project, "lab", REQUEST)
        self.assertFalse(result["protocol_effect_verified"])
        self.assertFalse(
            any(command.startswith(("vlan ", "port ", "vlan-type ")) for _, command in self.events)
        )

    def test_second_persistence_failure_reverts_first_committed_startup(self):
        from test_acl_persistence import PersistenceGuest

        self.guests["r2"] = PersistenceGuest("bad_selected")
        before = {identifier: guest.current for identifier, guest in self.guests.items()}
        startup = {identifier: dict(guest.startup) for identifier, guest in self.guests.items()}
        result = self.engine().apply(self.fixture.project, "lab", REQUEST)
        self.assertEqual(result["errors"][0]["code"], "persistence_content_unverified")
        self.assertTrue(result["protocol_rollback_verified"], result)
        self.assertEqual(
            {identifier: guest.current for identifier, guest in self.guests.items()}, before
        )
        self.assertEqual(
            {identifier: guest.startup for identifier, guest in self.guests.items()}, startup
        )
        first_save = next(
            index
            for index, (_, command) in enumerate(self.events)
            if command.startswith("save ") and "_after.cfg" in command
        )
        for identifier in self.guests:
            self.assertTrue(
                any(
                    owner == identifier and command == "display current-configuration"
                    for owner, command in self.events[:first_save]
                )
            )

    def test_unrequested_changes_and_recovery_uuid_change_remain_unverified(self):
        self.guests["r1"].unrequested = True
        result = self.engine().apply(self.fixture.project, "lab", REQUEST)
        self.assertEqual(result["errors"][0]["code"], "vlan_scope_changed")
        self.assertTrue(result["protocol_rollback_verified"], result)
        self.guests = {"r1": fixtures.Guest(), "r2": fixtures.Guest()}
        self.opens = {}
        self.events = []
        self.guests["r2"].fail = "vlan-type "
        self.recovery_changed = True
        result = self.engine().apply(self.fixture.project, "lab", REQUEST)
        self.assertFalse(result["protocol_rollback_verified"])
        self.assertTrue(
            any(row["error"]["code"] == "recovery_target_changed" for row in result["rollback"])
        )


class NetworkStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_batch_schema_preview_and_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = configure_fixture(Path(temporary))
            store = ProjectStore([paths["root"]], paths["output"])
            catalog = HclCatalog(paths["install"])
            project = stage_plan(
                store, catalog, str(paths["project"]), "VlanNetwork", plan_fixture(), explicit_map()
            )["project_directory"]
            async with mcp_session(paths) as session:
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                self.assertEqual(set(tools), TOOLS)
                properties = tools["hcl_apply_vlan_network"].inputSchema["properties"][
                    "parameters"
                ]["properties"]
                self.assertIn("subinterfaces", properties)
                self.assertIn("trunks", properties)
                arguments = {"project_path": project, "topology": "lab", "parameters": REQUEST}
                response, body = await call(session, "hcl_apply_vlan_network", arguments)
                self.assertFalse(response.isError)
                self.assertEqual(body["errors"][0]["code"], "writes_disabled")
                response, body = await call(
                    session, "hcl_apply_vlan_network", arguments | {"dry_run": True}
                )
                self.assertEqual(body["status"], "verified", body)
                self.assertEqual(set(body["commands"]), {"r1", "r2"})
