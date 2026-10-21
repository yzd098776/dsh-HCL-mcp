"""MCP safety hints for every advertised tool.

Clients use ``ToolAnnotations`` to decide whether a call needs confirmation and
how to warn a user.  The MCP defaults are not safe for a lab server: an omitted
``destructiveHint`` defaults to ``True`` and an omitted ``readOnlyHint`` to
``False``, which would paint the read-only inspection tools as destructive while
saying nothing precise about the ones that really do change device state.

The classification below is deliberately explicit so a newly registered tool is
*unclassified* until someone decides what it does.  ``validate_coverage`` is the
gate used by the test suite; ``for_tool`` falls back to the conservative default
(not read-only, destructive, open world) so an unclassified tool can never be
advertised as harmless.

Rules
-----
``READ_ONLY_TOOLS``
    Observe or compute only: HCL catalog reads, plan validation, generated
    configuration text, live device readback, audit and diff.  Nothing under the
    HCL install, the staged output root or a device changes.
``DEVICE_MUTATING_TOOLS``
    Can change a device's running or saved configuration, its power state, or
    deliver arbitrary CLI: the ``hcl_apply_*`` / ``hcl_remove_*`` services,
    console delivery, deployment, restore and power control.
``OTHER_TOOLS``
    Everything between the two: staging a copy, exporting artifacts, saving a
    staged project, opening/closing the backend session, backing up a
    configuration, or sending probe traffic.  These write to the isolated output
    root or to HCL's own files, but they never rewrite a device configuration.
"""

from __future__ import annotations

from collections.abc import Iterable

from mcp.types import ToolAnnotations

READ_ONLY_TOOLS = frozenset(
    {
        "hcl_audit_security",
        "hcl_backend_capabilities",
        "hcl_bridge_status",
        "hcl_capabilities",
        "hcl_diff",
        "hcl_estimate_plan",
        "hcl_explain_plan",
        "hcl_fix_plan",
        "hcl_generate_configs",
        "hcl_generate_operations",
        "hcl_get_device_details",
        "hcl_health_check",
        "hcl_inspect_ports",
        "hcl_inspect_project",
        "hcl_list_devices",
        "hcl_list_modules",
        "hcl_list_projects",
        "hcl_list_templates",
        "hcl_load_plan",
        "hcl_parity_report",
        "hcl_plan_topology",
        "hcl_read_qos",
        "hcl_read_vlans",
        "hcl_validate_plan",
    }
)

DEVICE_MUTATING_TOOLS = frozenset(
    {
        "hcl_apply_acl",
        "hcl_apply_interface_tuning",
        "hcl_apply_nat",
        "hcl_apply_netstream",
        "hcl_apply_port_security",
        "hcl_apply_stp",
        "hcl_apply_vlan",
        "hcl_apply_vlan_network",
        "hcl_console_exec",
        "hcl_device_power",
        "hcl_live_deploy",
        "hcl_remove_acl",
        "hcl_remove_nat",
        "hcl_restore_config",
    }
)

OTHER_TOOLS = frozenset(
    {
        "hcl_backend_topology",
        "hcl_backup_config",
        "hcl_export_artifacts",
        "hcl_full_build",
        "hcl_open_project",
        "hcl_save_project",
        "hcl_stage_add_device",
        "hcl_stage_add_note",
        "hcl_stage_clear_annotations",
        "hcl_stage_copy",
        "hcl_stage_delete_device",
        "hcl_stage_link",
        "hcl_stage_move_device",
        "hcl_stage_plan",
        "hcl_stage_rename_device",
        "hcl_stage_unlink",
        "hcl_verify_connectivity",
    }
)

#: Tools whose effect leaves the local HCL install (probe traffic to a device).
OPEN_WORLD_TOOLS = frozenset({"hcl_verify_connectivity"})

READ_ONLY_HINTS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

DEVICE_MUTATING_HINTS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)

OTHER_HINTS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

#: Hints for a name nobody classified: never advertise it as harmless.
UNCLASSIFIED_HINTS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)


def classifications() -> dict[str, str]:
    """Map every classified tool name to ``read_only`` / ``device_mutating`` / ``other``."""
    rows: dict[str, str] = {}
    for name in READ_ONLY_TOOLS:
        rows[name] = "read_only"
    for name in DEVICE_MUTATING_TOOLS:
        rows[name] = "device_mutating"
    for name in OTHER_TOOLS:
        rows[name] = "other"
    return rows


def for_tool(name: str) -> ToolAnnotations:
    """Safety hints for ``name``; unknown names get the conservative default."""
    if name in READ_ONLY_TOOLS:
        return READ_ONLY_HINTS.model_copy()
    if name in DEVICE_MUTATING_TOOLS:
        return DEVICE_MUTATING_HINTS.model_copy()
    if name in OTHER_TOOLS:
        hints = OTHER_HINTS.model_copy()
        if name in OPEN_WORLD_TOOLS:
            hints.openWorldHint = True
        return hints
    return UNCLASSIFIED_HINTS.model_copy()


def validate_coverage(names: Iterable[str]) -> None:
    """Raise ``ValueError`` unless ``names`` equals the union of the three sets.

    Run at test time so adding a tool to the server without classifying it fails
    the build instead of silently shipping a destructive-looking or, worse, a
    harmless-looking entry point.
    """
    advertised = set(names)
    classified = READ_ONLY_TOOLS | DEVICE_MUTATING_TOOLS | OTHER_TOOLS
    unclassified = sorted(advertised - classified)
    stale = sorted(classified - advertised)
    overlapping = sorted(
        (READ_ONLY_TOOLS & DEVICE_MUTATING_TOOLS)
        | (READ_ONLY_TOOLS & OTHER_TOOLS)
        | (DEVICE_MUTATING_TOOLS & OTHER_TOOLS)
    )
    problems: list[str] = []
    if unclassified:
        problems.append(f"unclassified tools: {', '.join(unclassified)}")
    if stale:
        problems.append(f"classified but not advertised: {', '.join(stale)}")
    if overlapping:
        problems.append(f"tools in more than one class: {', '.join(overlapping)}")
    if problems:
        raise ValueError("tool annotation coverage is incomplete: " + "; ".join(problems))
