"""Contract tests for the MCP_HCL stdio surface (dsh branch).

These tests treat the server as a black box: they launch it over stdio with the
official MCP client SDK and assert the *documented* contract plus the behaviour
observed while extending the live acceptance probe.  Fixtures are synthetic
(temp install root, temp projects), so nothing here reads or writes ``D:\\HCL``,
the user's real projects, or ``work\\staged``.

Each test owns its whole session inside one coroutine: anyio cancel scopes must
be entered and exited in the same task, which rules out holding the stdio
session across ``asyncSetUp``/``asyncTearDown``.

Coverage groups:
  * discovery surface and pinned 62/5 inventory
  * plan validation: implemented checks, documented non-checks, error shapes
  * project path and configured-root safety
  * staging name / coordinate grammar
  * argument and JSON-RPC edge behaviour
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from contract_inventory import RESOURCES as EXPECTED_RESOURCES
from contract_inventory import TOOLS as EXPECTED_TOOLS
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from mcp_hcl import tool_annotations

# Any tool whose name suggests live canvas, power, console or capture control.
LIVE_TOOL_NAME = re.compile(
    r"deploy|start|stop|power|cli|console|ping|capture|canvas|packet", re.IGNORECASE
)

MSR_CFG = """ version 7.1.064, Release 0427P22
interface GigabitEthernet0/0
interface GigabitEthernet0/1
interface GigabitEthernet0/2
interface Serial1/0
interface NULL0
"""
SWITCH_CFG = """ version CMW7.1.075-A7571
interface GigabitEthernet0/0
interface GigabitEthernet0/1
interface M-GigabitEthernet0/0
interface NULL0
"""
PORT_INI = """[PHY_PORT_VALUE]
 PHY_ETH_GE = 3
 PHY_SERIAL = 5
[MSR36]
 [[0]]
  GE = PHY_ETH_GE
[MSR36-SERIAL]
 [[0]]
  S = PHY_SERIAL
"""

NET_TEMPLATE = """version = 5.10.3
[vbox 127.0.0.1:16600]
    workingdir = {workingdir}
    udp = 10000
    [[MSR36-20 R1]]
        device_id = 1
        bridge_mac = 64-52-00-00-01-00
        slot0 = MSR36 384
        GE_0/0 = R2 GE_0/0
        x = 0.0
        y = 0.0
        z = 5.0
    [[MSR36-20 R2]]
        device_id = 2
        bridge_mac = 64-52-00-00-02-00
        slot0 = MSR36 384
        GE_0/0 = R1 GE_0/0
        x = 100.0
        y = 0.0
        z = 5.0
[Non-vBox-DATA]
    m11 = 1.0
"""


def write_project(directory: Path, *, project_name: str = "Lab One") -> Path:
    """Create a minimal but complete HCL project and return its directory."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ConfigDisk").mkdir(exist_ok=True)
    (directory / "DeviceConfig").mkdir(exist_ok=True)
    (directory / "ConfigDisk" / "device1.vdi").write_bytes(b"synthetic disk marker")
    (directory / "DeviceConfig" / "R1.cfg").write_text("sysname R1\n", encoding="utf-8")
    (directory / "deviceversion.txt").write_text("MSR36:b75\n", encoding="utf-8")
    metadata = {
        "projectInfo": {"name": project_name, "path": directory.name},
        "deviceInfoList": [
            {
                "resourceName": name,
                "resourceCategory": "router",
                "resourceModel": "MSR36-20",
                "resourceVersion": "test",
                "configPath": f"DeviceConfig\\{name}.cfg",
            }
            for name in ("R1", "R2")
        ],
    }
    (directory / "project.json").write_text(
        json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
    )
    (directory / f"{directory.name}.net").write_bytes(
        NET_TEMPLATE.format(workingdir=directory).replace("\n", "\r\n").encode("utf-8")
    )
    return directory


def build_fixture(root: Path) -> dict[str, Any]:
    install = root / "install"
    (install / "devicecfgs").mkdir(parents=True)
    (install / "devicecfgs" / "MSR36-20.cfg").write_text(MSR_CFG, encoding="utf-8")
    (install / "devicecfgs" / "S5820V2-54QS-GE.cfg").write_text(SWITCH_CFG, encoding="utf-8")
    (install / "deviceInfo.ini").write_text(PORT_INI, encoding="utf-8")

    root_a = root / "projects"
    root_b = root / "other-projects"
    root_b.mkdir()
    return {
        "install": install,
        "root": root_a,
        "other_root": root_b,
        # direct child of root A
        "project": write_project(root_a / "lab1"),
        # direct child of root B -> proves the ';'-separated multi-root form
        "second_project": write_project(root_b / "lab2", project_name="Lab Two"),
        # nested two levels down -> must NOT be listed (list_projects is not recursive)
        "nested_project": write_project(root_a / "group" / "lab3", project_name="Lab Three"),
        "output": root / "staged",
        "outside": root / "outside",
    }


@contextlib.asynccontextmanager
async def mcp_session(paths: dict[str, Any]) -> AsyncIterator[ClientSession]:
    environment = dict(os.environ)
    environment.update(
        {
            "MCP_HCL_PROJECT_ROOTS": f"{paths['root']};{paths['other_root']}",
            "MCP_HCL_OUTPUT_ROOT": str(paths["output"]),
            "MCP_HCL_INSTALL_ROOT": str(paths["install"]),
            # a port nothing listens on: these tests never assert backend state
            "MCP_HCL_BACKEND_PORT": "16666",
            "MCP_HCL_ALLOW_LIVE_WRITE": "0",
        }
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_hcl.server"],
        env=environment,
        cwd=str(paths["root"]),
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            yield session


async def call(session: ClientSession, tool: str, arguments: dict[str, Any]):
    result = await session.call_tool(tool, arguments)
    text = result.content[0].text if result.content else ""
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = None
    return result, body


async def plan_result(session: ClientSession, plan: Any) -> dict[str, Any]:
    _, body = await call(session, "hcl_validate_plan", {"plan": plan})
    return body


async def inspect(session: ClientSession, path: Any) -> dict[str, Any]:
    _, body = await call(session, "hcl_inspect_project", {"project_path": str(path)})
    return body


def base_plan() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "契约计划",
        "devices": [
            {"id": "r1", "name": "R1", "model": "MSR36-20", "x": 0, "y": 0},
            {"id": "r2", "name": "R2", "model": "MSR36-20", "x": 200, "y": 0},
        ],
        "links": [
            {
                "a": {"device": "r1", "interface": "GigabitEthernet0/0"},
                "b": {"device": "r2", "interface": "GigabitEthernet0/0"},
                "medium": "ethernet",
            }
        ],
        "addresses": [],
        "routes": [],
        "services": [],
        "annotations": [],
    }


def semantic_cases() -> list[tuple[str, Callable[[dict], None], str]]:
    """(label, mutation, expected error code) for the checks that ARE implemented."""

    def set_link(plan, index, **changes):
        plan["links"][index].update(changes)

    def add_link(plan, **link):
        plan["links"].append(link)

    def add_address(plan, device, interface, cidr):
        plan["addresses"].append({"device": device, "interface": interface, "cidr": cidr})

    return [
        (
            "duplicate_device_id",
            lambda p: p["devices"][1].update({"id": "r1"}),
            "duplicate_device_id",
        ),
        (
            "duplicate_device_name",
            lambda p: p["devices"][1].update({"name": "R1"}),
            "duplicate_device_name",
        ),
        ("unknown_model", lambda p: p["devices"][0].update({"model": "NOPE-1"}), "unknown_model"),
        (
            "unknown_device_in_link",
            lambda p: set_link(p, 0, b={"device": "r9", "interface": "GigabitEthernet0/0"}),
            "unknown_device",
        ),
        (
            "unknown_physical_interface",
            lambda p: set_link(p, 0, b={"device": "r2", "interface": "GigabitEthernet9/9"}),
            "unknown_physical_interface",
        ),
        (
            "null0_is_not_connectable",
            lambda p: set_link(p, 0, b={"device": "r2", "interface": "NULL0"}),
            "unknown_physical_interface",
        ),
        (
            "duplicate_port",
            lambda p: add_link(
                p,
                a={"device": "r1", "interface": "GigabitEthernet0/0"},
                b={"device": "r2", "interface": "GigabitEthernet0/1"},
                medium="ethernet",
            ),
            "duplicate_port",
        ),
        (
            "self_link",
            lambda p: set_link(p, 0, b={"device": "r1", "interface": "GigabitEthernet0/0"}),
            "self_link",
        ),
        (
            "medium_mismatch_serial_on_ethernet",
            lambda p: set_link(
                p, 0, a={"device": "r1", "interface": "Serial1/0"}, medium="ethernet"
            ),
            "medium_mismatch",
        ),
        (
            "duplicate_ip",
            lambda p: (
                add_address(p, "r1", "GigabitEthernet0/0", "10.0.0.1/30"),
                add_address(p, "r2", "GigabitEthernet0/0", "10.0.0.1/30"),
            ),
            "duplicate_ip",
        ),
        (
            "second_address_same_family",
            lambda p: (
                add_address(p, "r1", "GigabitEthernet0/0", "10.0.0.1/30"),
                add_address(p, "r1", "GigabitEthernet0/0", "10.0.0.5/30"),
            ),
            "duplicate_interface_address_family",
        ),
        (
            "reserved_network_address",
            lambda p: add_address(p, "r1", "GigabitEthernet0/0", "10.0.0.0/30"),
            "reserved_host_address",
        ),
        (
            "reserved_broadcast_address",
            lambda p: add_address(p, "r1", "GigabitEthernet0/0", "10.0.0.3/30"),
            "reserved_host_address",
        ),
        (
            "route_address_family_mismatch",
            lambda p: p["routes"].append(
                {"device": "r1", "network": "10.9.0.0/24", "next_hop": "2001:db8::1"}
            ),
            "route_address_family_mismatch",
        ),
    ]


def snapshot(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


class McpContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = build_fixture(Path(self.temp.name))

    @property
    def source(self) -> str:
        return str(self.paths["project"])

    # ------------------------------------------------------------ discovery
    async def test_every_advertised_tool_carries_safety_annotations(self) -> None:
        """Clients decide whether to ask for confirmation from tools/list alone."""
        async with mcp_session(self.paths) as session:
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            tool_annotations.validate_coverage(names)
            for tool in listed.tools:
                hints = tool.annotations
                self.assertIsNotNone(hints, f"{tool.name} advertises no annotations")
                expected = tool_annotations.for_tool(tool.name)
                self.assertEqual(
                    (hints.readOnlyHint, hints.destructiveHint, hints.openWorldHint),
                    (expected.readOnlyHint, expected.destructiveHint, expected.openWorldHint),
                    tool.name,
                )
                if tool.name in tool_annotations.READ_ONLY_TOOLS:
                    self.assertTrue(hints.readOnlyHint, tool.name)
                    self.assertFalse(hints.destructiveHint, tool.name)
                if tool.name in tool_annotations.DEVICE_MUTATING_TOOLS:
                    self.assertFalse(hints.readOnlyHint, tool.name)
                    self.assertTrue(hints.destructiveHint, tool.name)

    async def test_discovery_surface_matches_pinned_inventory(self) -> None:
        async with mcp_session(self.paths) as session:
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            self.assertEqual(names, EXPECTED_TOOLS)
            self.assertIsNone(listed.nextCursor, "tools/list must not need pagination")

            resources = await session.list_resources()
            self.assertEqual({str(item.uri) for item in resources.resources}, EXPECTED_RESOURCES)
            templates = await session.list_resource_templates()
            self.assertEqual(
                [item.uriTemplate for item in templates.resourceTemplates],
                [],
                "aliases/cables/templates catalog resources are still unimplemented",
            )

            _, report = await call(session, "hcl_parity_report", {})
            self.assertEqual(len(report["tools"]), 62)
            self.assertEqual(len(report["resources"]), 5)
            self.assertEqual(
                report["tool_state_counts"], {"implemented": 12, "planned": 47, "unverified": 3}
            )
            self.assertEqual(report["completion"], "incomplete")
            self.assertFalse(report["full_live_gate_passed"])
            self.assertEqual(set(report["registered_tools"]), names)
            self.assertEqual(set(report["registered_resources"]), EXPECTED_RESOURCES)

    async def test_control_plane_tools_are_gated_and_claim_nothing_live(self) -> None:
        """Live-looking tools may exist, but they must be inert until opted in."""
        async with mcp_session(self.paths) as session:
            listed = await session.list_tools()
            suspects = sorted(
                tool.name for tool in listed.tools if LIVE_TOOL_NAME.search(tool.name)
            )
            self.assertEqual(
                suspects,
                ["hcl_console_exec", "hcl_device_power", "hcl_live_deploy"],
                "the only live-looking tools are the two gated control-plane ones",
            )

            _, capabilities = await call(session, "hcl_capabilities", {})
            self.assertEqual(capabilities["live_canvas_api"], "not verified and not exposed")
            self.assertFalse(capabilities["live_gate"]["full_live_gate_passed"])
            self.assertIn("MCP_HCL_ALLOW_LIVE_WRITE", capabilities["control_plane"])

            _, registry = await call(session, "hcl_backend_capabilities", {})
            self.assertFalse(registry["write_verbs_enabled"], "writes default to off")
            self.assertFalse(registry["console_exec_enabled"], "console input defaults to off")
            self.assertEqual(registry["status"], "verified")
            self.assertEqual(registry["verified_scope"], "observed_verb_registry_only")
            not_invocable = [row["verb"] for row in registry["verbs"] if not row["invocable"]]
            self.assertIn("create", not_invocable)
            self.assertIn("create_udp", not_invocable)

            for tool, arguments in (
                ("hcl_device_power", {"device": 1, "on": True}),
                (
                    "hcl_console_exec",
                    {"command": "display version", "topology": "topo1", "device": 1},
                ),
                ("hcl_backend_topology", {"action": "close"}),
            ):
                with self.subTest(tool=tool):
                    _, body = await call(session, tool, arguments)
                    self.assertEqual(body["status"], "rejected")
                    self.assertEqual(body["errors"][0]["code"], "writes_disabled")
                    self.assertFalse(body["full_live_gate_passed"])

    # ------------------------------------------------------------------ plan
    async def test_plan_structural_errors_use_normalized_codes(self) -> None:
        unknown_field = base_plan()
        unknown_field["unexpected"] = 1
        wrong_version = base_plan()
        wrong_version["schema_version"] = 2
        missing_name = {key: value for key, value in base_plan().items() if key != "name"}

        async with mcp_session(self.paths) as session:
            for label, plan, expected_type in (
                ("unknown_field", unknown_field, "extra_forbidden"),
                ("schema_version_2", wrong_version, "literal_error"),
                ("missing_name", missing_name, "missing"),
            ):
                with self.subTest(label=label):
                    body = await plan_result(session, plan)
                    self.assertEqual(body["status"], "rejected")
                    self.assertFalse(body["valid"])
                    first = body["errors"][0]
                    # structural errors carry the raw pydantic shape: no "code" key
                    self.assertEqual(first["code"], expected_type)
                    self.assertIn("message", first)
                    self.assertIn("path", first)

            result, _ = await call(session, "hcl_validate_plan", {"plan": "not-an-object"})
            self.assertTrue(result.isError, "a non-object plan is an argument error, not a verdict")

    async def test_plan_semantic_checks_that_are_implemented(self) -> None:
        async with mcp_session(self.paths) as session:
            for label, mutate, expected_code in semantic_cases():
                with self.subTest(label=label):
                    plan = copy.deepcopy(base_plan())
                    mutate(plan)
                    body = await plan_result(session, plan)
                    self.assertEqual(body["status"], "rejected")
                    self.assertFalse(body["valid"])
                    self.assertIn(expected_code, [row.get("code") for row in body["errors"]])
                    self.assertTrue(all("message" in row for row in body["errors"]))
                    self.assertFalse(body["runtime_verified"])
                    self.assertFalse(body["full_live_gate_passed"])

    async def test_plan_checks_documented_as_unimplemented_accept_input(self) -> None:
        reachability = copy.deepcopy(base_plan())
        reachability["addresses"] = [
            {"device": "r1", "interface": "GigabitEthernet0/0", "cidr": "10.0.0.1/30"},
            {"device": "r2", "interface": "GigabitEthernet0/0", "cidr": "192.168.77.1/24"},
        ]
        no_route = copy.deepcopy(base_plan())
        no_route["routes"] = [
            {"device": "r1", "network": "172.16.0.0/24", "next_hop": "203.0.113.1"}
        ]
        dual_stack = copy.deepcopy(base_plan())
        dual_stack["addresses"] = [
            {"device": "r1", "interface": "GigabitEthernet0/0", "cidr": "10.0.0.1/30"},
            {"device": "r1", "interface": "GigabitEthernet0/0", "cidr": "2001:db8::1/64"},
        ]

        async with mcp_session(self.paths) as session:
            for label, plan in (
                ("subnets_never_checked", reachability),
                ("next_hop_reachability_never_checked", no_route),
                ("dual_stack_same_interface_allowed", dual_stack),
            ):
                with self.subTest(label=label):
                    body = await plan_result(session, plan)
                    self.assertEqual(body["status"], "verified")
                    self.assertTrue(body["valid"])
                    self.assertEqual(
                        body["verified_scope"],
                        "offline_plan_wired_graph_and_configuration_semantics_only",
                    )

            services = copy.deepcopy(base_plan())
            services["services"] = [
                {"device": "r1", "feature": "ospf", "parameters": {"area": "0.0.0.0"}}
            ]
            body = await plan_result(session, services)
            self.assertFalse(body["valid"])
            self.assertEqual(body["status"], "rejected")
            self.assertTrue(body["errors"])

    # ---------------------------------------------------------- path safety
    async def test_project_path_and_root_safety_rejections(self) -> None:
        outside = self.paths["outside"] / "noproj"
        outside.mkdir(parents=True)
        not_a_project = self.paths["root"] / "notaproject"
        not_a_project.mkdir()
        two_net = self.paths["root"] / "twonet"
        two_net.mkdir()
        for name in ("a.net", "b.net"):
            (two_net / name).write_bytes((self.paths["project"] / "lab1.net").read_bytes())
        (two_net / "project.json").write_bytes(
            (self.paths["project"] / "project.json").read_bytes()
        )

        async with mcp_session(self.paths) as session:
            for label, path, token in (
                ("outside_configured_roots", outside, "outside configured roots"),
                ("missing_directory", self.paths["root"] / "ghost", "does not exist"),
                ("directory_without_project_json", not_a_project, "project.json"),
                ("directory_with_two_net_files", two_net, "exactly one .net file"),
                (
                    "net_file_instead_of_directory",
                    self.paths["project"] / "lab1.net",
                    "does not exist",
                ),
            ):
                with self.subTest(label=label):
                    body = await inspect(session, path)
                    self.assertEqual(body["status"], "rejected")
                    self.assertIn(token, body["error"])

            result, body = await call(
                session,
                "hcl_stage_copy",
                {"project_path": str(outside), "stage_name": "OutsideCopy"},
            )
            self.assertFalse(result.isError)
            self.assertEqual(body["status"], "rejected")
            self.assertIn("outside configured roots", body["error"])

    async def test_symlinked_project_json_is_rejected(self) -> None:
        linked = self.paths["root"] / "linked"
        linked.mkdir()
        (linked / "lab4.net").write_bytes((self.paths["project"] / "lab1.net").read_bytes())
        target = linked / "real.json"
        target.write_bytes((self.paths["project"] / "project.json").read_bytes())
        try:
            os.symlink(target, linked / "project.json")
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not permitted for this account")

        async with mcp_session(self.paths) as session:
            body = await inspect(session, linked)
            self.assertEqual(body["status"], "rejected")
            self.assertIn("Linked project.json", body["error"])

    async def test_multi_root_listing_is_one_level_only(self) -> None:
        async with mcp_session(self.paths) as session:
            _, listing = await call(session, "hcl_list_projects", {})
            listed = {Path(row["directory"]).resolve() for row in listing["projects"]}
            self.assertIn(self.paths["project"].resolve(), listed)
            self.assertIn(self.paths["second_project"].resolve(), listed)
            self.assertNotIn(
                self.paths["nested_project"].resolve(),
                listed,
                "list_projects must not recurse below the configured root",
            )

    # -------------------------------------------------------- staging rules
    async def test_stage_name_and_device_name_grammar(self) -> None:
        async with mcp_session(self.paths) as session:
            for label, stage_name in (
                ("leading_digit", "1abc"),
                ("hyphen", "Text-Copy"),
                ("twenty_one_chars", "a" * 21),
                ("non_ascii", "文本"),
                ("empty", ""),
            ):
                with self.subTest(stage_name=label):
                    _, body = await call(
                        session,
                        "hcl_stage_copy",
                        {"project_path": self.source, "stage_name": stage_name},
                    )
                    self.assertEqual(body["status"], "rejected")
                    self.assertIn("stage_name", body["error"])

            _, body = await call(
                session, "hcl_stage_copy", {"project_path": self.source, "stage_name": "a" * 20}
            )
            self.assertEqual(body["status"], "staged", "20 characters is the documented maximum")

            for label, new_name in (
                ("leading_digit", "1R9"),
                ("thirty_three_chars", "R" + "a" * 32),
                ("non_ascii", "文本R"),
                ("duplicate_case_insensitive", "r2"),
            ):
                with self.subTest(device_name=label):
                    _, body = await call(
                        session,
                        "hcl_stage_add_device",
                        {
                            "project_path": self.source,
                            "stage_name": f"Dev{label[:6]}",
                            "template_device": "R1",
                            "new_name": new_name,
                            "x": 10,
                            "y": 10,
                        },
                    )
                    self.assertEqual(body["status"], "rejected")
                    self.assertIn("New device name", body["error"])

            _, body = await call(
                session,
                "hcl_stage_add_device",
                {
                    "project_path": self.source,
                    "stage_name": "DevHyphen",
                    "template_device": "R1",
                    "new_name": "Text-R9",
                    "x": 300,
                    "y": 300,
                },
            )
            self.assertEqual(
                body["status"], "staged", "hyphens are allowed after the first character"
            )

            for label, x in (("positive", 10001), ("negative", -10001)):
                with self.subTest(coordinate=label):
                    _, body = await call(
                        session,
                        "hcl_stage_add_device",
                        {
                            "project_path": self.source,
                            "stage_name": f"Coord{label[:3]}",
                            "template_device": "R1",
                            "new_name": f"Coord{label[:3]}R",
                            "x": x,
                            "y": 0,
                        },
                    )
                    self.assertEqual(body["status"], "rejected")
                    self.assertIn("Coordinates exceed", body["error"])

    async def test_link_arguments_must_be_complete_and_unused(self) -> None:
        async with mcp_session(self.paths) as session:
            _, body = await call(
                session,
                "hcl_stage_add_device",
                {
                    "project_path": self.source,
                    "stage_name": "LinkPartial",
                    "template_device": "R1",
                    "new_name": "PartialR",
                    "x": 1,
                    "y": 1,
                    "connect_to": "R2",
                },
            )
            self.assertEqual(body["status"], "rejected")
            self.assertIn("must be supplied together", body["error"])

            for label, arguments in (
                ("unknown_peer", {"a": "R1", "a_port": "GE_0/1", "b": "Ghost", "b_port": "GE_0/1"}),
                ("same_device", {"a": "R1", "a_port": "GE_0/1", "b": "R1", "b_port": "GE_0/2"}),
                ("used_port", {"a": "R1", "a_port": "GE_0/0", "b": "R2", "b_port": "GE_0/1"}),
                ("malformed_port", {"a": "R1", "a_port": "0/0", "b": "R2", "b_port": "GE_0/1"}),
            ):
                with self.subTest(case=label):
                    _, body = await call(
                        session,
                        "hcl_stage_link",
                        {
                            "project_path": self.source,
                            "stage_name": f"Link{label[:8]}",
                            **arguments,
                        },
                    )
                    self.assertEqual(body["status"], "rejected")

            _, body = await call(
                session, "hcl_stage_copy", {"project_path": self.source, "stage_name": "LinkOk"}
            )
            staged = body["project_directory"]
            _, body = await call(
                session,
                "hcl_stage_link",
                {
                    "project_path": staged,
                    "stage_name": "LinkOk2",
                    "a": "R1",
                    "a_port": "GE_0/1",
                    "b": "R2",
                    "b_port": "GE_0/1",
                },
            )
            self.assertEqual(body["status"], "staged")
            self.assertEqual((body["device_count"], body["link_count"]), (2, 2))

    async def test_staging_never_rewrites_the_source_project(self) -> None:
        before = snapshot(self.paths["project"])
        async with mcp_session(self.paths) as session:
            _, copy_body = await call(
                session,
                "hcl_stage_copy",
                {"project_path": self.source, "stage_name": "IntegrityCopy"},
            )
            self.assertEqual(copy_body["status"], "staged")
            self.assertEqual((copy_body["device_count"], copy_body["link_count"]), (2, 1))
            _, add_body = await call(
                session,
                "hcl_stage_add_device",
                {
                    "project_path": copy_body["project_directory"],
                    "stage_name": "IntegrityAdd",
                    "template_device": "R1",
                    "new_name": "NewR",
                    "x": 5,
                    "y": 5,
                },
            )
            self.assertEqual(add_body["status"], "staged")
        self.assertEqual(snapshot(self.paths["project"]), before)
        self.assertFalse((self.paths["project"] / "DeviceConfig" / "NewR.cfg").exists())

    # ------------------------------------------------------- copy fidelity
    def write_variant(
        self, label: str, mutate: Callable[[str], str], *, raw: bytes | None = None
    ) -> Path:
        directory = self.paths["root"] / label
        directory.mkdir()
        (directory / "project.json").write_bytes(
            (self.paths["project"] / "project.json").read_bytes()
        )
        text = (self.paths["project"] / "lab1.net").read_bytes().decode("utf-8")
        payload = raw if raw is not None else mutate(text).encode("utf-8")
        (directory / "lab1.net").write_bytes(payload)
        return directory

    async def test_stage_copy_preserves_every_other_field_byte_for_byte(self) -> None:
        net_path = self.paths["project"] / "lab1.net"
        text = net_path.read_bytes().decode("utf-8")
        text = text.replace(
            "        slot0 = MSR36 384",
            "        slot0 = MSR36 384\r\n        custom_attr = keep-me",
            1,
        )
        text += '    [[NOTE 1]]\r\n        text = "保留中文注释"\r\n'
        net_path.write_bytes(text.encode("utf-8"))
        source_text = net_path.read_text(encoding="utf-8")

        async with mcp_session(self.paths) as session:
            _, body = await call(
                session,
                "hcl_stage_copy",
                {"project_path": self.source, "stage_name": "FidelityCopy"},
            )
            self.assertEqual(body["status"], "staged")
            staged_net = Path(body["project_file"])
            staged_text = staged_net.read_text(encoding="utf-8")

        # the only rewrite is the workingdir line, so normalise it and compare
        normalised = staged_text.replace(
            f"workingdir = {body['project_directory']}", f"workingdir = {self.paths['project']}"
        )
        self.assertEqual(normalised, source_text)
        self.assertIn("custom_attr = keep-me", staged_text)
        self.assertIn('text = "保留中文注释"', staged_text)
        self.assertIn("m11 = 1.0", staged_text)

    async def test_utf8_bom_source_is_readable_and_not_written_back(self) -> None:
        # Characterisation: the reader accepts a BOM (utf-8-sig) but the writer
        # emits plain UTF-8.  The observed HCL 5.10.3 projects have no BOM, so
        # this is currently inert - but a BOM-bearing project would lose it.
        net_path = self.paths["project"] / "lab1.net"
        net_path.write_bytes(b"\xef\xbb\xbf" + net_path.read_bytes())

        async with mcp_session(self.paths) as session:
            source = await inspect(session, self.paths["project"])
            self.assertEqual(source["status"], "ok")
            _, body = await call(
                session, "hcl_stage_copy", {"project_path": self.source, "stage_name": "BomCopy"}
            )
            self.assertEqual(body["status"], "staged")
            staged_bytes = Path(body["project_file"]).read_bytes()

        self.assertFalse(
            staged_bytes.startswith(b"\xef\xbb\xbf"), "the staged .net is written without a BOM"
        )
        self.assertIn("MSR36-20 R1", staged_bytes.decode("utf-8"))

    async def test_corrupt_projects_never_report_ok(self) -> None:
        """Malformed projects must never come back as status=ok.

        The handoff warns that "some corrupt projects may raise a tool error
        instead of a business rejected".  This test pins down which shapes are
        already handled as business rejections and which two still escape as
        tool errors, so hardening them is a visible change.
        """
        structural = {
            "no_vbox_section": lambda t: t.replace("[vbox 127.0.0.1:16600]\r\n", ""),
            "two_vbox_sections": lambda t: t.replace(
                "[vbox 127.0.0.1:16600]\r\n", "[vbox 127.0.0.1:16600]\r\n[vbox 127.0.0.1:16600]\r\n"
            ),
            "missing_device_id": lambda t: t.replace("        device_id = 1\r\n", ""),
            "duplicate_field": lambda t: t.replace(
                "        slot0 = MSR36 384", "        slot0 = MSR36 384\r\n        x = 7.0", 1
            ),
            "asymmetric_link": lambda t: t.replace("GE_0/0 = R1 GE_0/0", "GE_0/0 = R1 GE_0/1"),
        }
        escaping = {
            "non_numeric_device_id": lambda t: t.replace("device_id = 1", "device_id = abc"),
            "non_numeric_coordinate": lambda t: t.replace("x = 0.0", "x = not-a-number"),
        }

        async with mcp_session(self.paths) as session:
            for label, mutate in structural.items():
                with self.subTest(case=label):
                    path = self.write_variant(label, mutate)
                    result, body = await call(
                        session, "hcl_inspect_project", {"project_path": str(path)}
                    )
                    self.assertFalse(result.isError, f"{label} should be a business rejection")
                    self.assertEqual(body["status"], "rejected")

            with self.subTest(case="invalid_utf8"):
                path = self.write_variant(
                    "invalid_utf8", lambda t: t, raw=b"\xff\xfe\x00 not utf-8"
                )
                result, body = await call(
                    session, "hcl_inspect_project", {"project_path": str(path)}
                )
                self.assertFalse(result.isError)
                self.assertEqual(body["status"], "rejected")
                self.assertIn("not UTF-8", body["error"])

            for label, mutate in escaping.items():
                with self.subTest(case=label):
                    path = self.write_variant(label, mutate)
                    result, body = await call(
                        session, "hcl_inspect_project", {"project_path": str(path)}
                    )
                    # documented caveat: this shape escapes as a tool error
                    self.assertFalse(result.isError)
                    self.assertEqual(body["status"], "rejected")

    # -------------------------------------------------- argument / protocol
    async def test_argument_and_jsonrpc_edge_behaviour(self) -> None:
        async with mcp_session(self.paths) as session:
            result, _ = await call(session, "hcl_inspect_project", {})
            self.assertTrue(result.isError, "a missing required argument is an argument error")
            self.assertEqual(json.loads(result.content[0].text)["errors"][0]["code"], "missing")

            result, _ = await call(session, "hcl_inspect_project", {"project_path": 5})
            self.assertTrue(result.isError, "a wrong JSON type is not coerced")

            result, body = await call(session, "hcl_inspect_project", {"project_path": self.source})
            self.assertFalse(result.isError)
            self.assertEqual(body["status"], "ok")

            result, _ = await call(session, "hcl_unknown_future_tool", {})
            self.assertTrue(result.isError)
            self.assertEqual(
                json.loads(result.content[0].text)["errors"][0]["code"], "unknown_tool"
            )

            with self.assertRaises(McpError):
                await session.read_resource(AnyUrl("hcl://does-not-exist"))
            with self.assertRaises(McpError):
                await session.subscribe_resource(AnyUrl("hcl://catalog/devices"))

    async def test_raw_argument_types_and_unknown_fields_are_rejected(self) -> None:
        async with mcp_session(self.paths) as session:
            for tool, arguments in (
                ("hcl_capabilities", {"bogus": 1}),
                (
                    "hcl_stage_add_device",
                    {
                        "project_path": self.source,
                        "stage_name": "CoerceStr",
                        "template_device": "R1",
                        "new_name": "CoerceR",
                        "x": "300",
                        "y": 300,
                    },
                ),
                (
                    "hcl_stage_copy",
                    {
                        "project_path": self.source,
                        "stage_name": "IgnoreExtra",
                        "not_a_real_argument": True,
                    },
                ),
                ("hcl_device_power", {"device": True}),
                ("hcl_device_power", {"device": "1"}),
                ("hcl_validate_plan", {"plan": json.dumps(base_plan())}),
            ):
                with self.subTest(tool=tool, arguments=arguments):
                    result, body = await call(session, tool, arguments)
                    self.assertTrue(result.isError)
                    self.assertEqual(body["status"], "rejected")
                    self.assertTrue(body["errors"])
            self.assertFalse((self.paths["output"] / "CoerceStr").exists())


if __name__ == "__main__":
    unittest.main()
