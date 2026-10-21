"""Strict raw MCP arguments and a request-correlated result envelope."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import ConfigDict, ValidationError, create_model

from mcp_hcl import dsh_compat, tool_annotations
from mcp_hcl.console import ConsoleError


def validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"code": row["type"], "message": row["msg"], "path": list(row["loc"])}
        for row in exc.errors(include_input=False, include_context=False, include_url=False)
    ]


def envelope(
    body: dict[str, Any],
    *,
    request_id: Any = None,
    operation: str | None = None,
    target: Any = None,
) -> dict[str, Any]:
    result = dict(body)
    if "request_id" in result:
        result["backend_request_id"] = result.pop("request_id")
    result.setdefault("status", "verified")
    result.setdefault("operation_id", uuid4().hex)
    result.update(request_id=request_id, contract_version=2)
    result.setdefault("operation", operation)
    result.setdefault(
        "target", result.get("target_project") or result.get("project_file") or target
    )
    result.setdefault(
        "verified_scope",
        "offline_result_only" if result["status"] in {"ok", "verified", "staged"} else "none",
    )
    result.setdefault("evidence", [])
    if result["status"] == "staged" and result.get("project_file"):
        if "verified_scope" not in body:
            result["verified_scope"] = "isolated_saved_project_only"
        if not any(item.get("source") == result["project_file"] for item in result["evidence"]):
            path = Path(result["project_file"])
            result["evidence"].append(
                {
                    "source": str(path),
                    "layer": "saved_project",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    result.setdefault(
        "errors",
        [{"code": "operation_failed", "message": result["error"]}] if result.get("error") else [],
    )
    result.setdefault("warnings", [])
    result.setdefault("runtime_verified", False)
    result.setdefault("full_live_gate_passed", False)
    return result


class StrictFastMCP(FastMCP):
    """Validate BEFORE FastMCP's JSON pre-parsing or numeric coercion.

    The adapter is intentionally isolated from the SDK's implementation details.
    The same generated model supplies tools/list and calls/tool validation.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._strict_models: dict[str, Any] = {}
        self._raw_parameters: dict[str, Any] = {}
        self.artifact_retries: Any = None

    def raw_parameters(self, name: str) -> dict[str, Any] | None:
        """Full pydantic schema for ``name``, before DeepSeek Harness projection."""
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            return None
        self._model(tool)
        return self._raw_parameters.get(name)

    def raw_parameters_all(self) -> dict[str, Any]:
        """Full pydantic schema for every advertised tool, keyed by tool name."""
        for tool in self._tool_manager.list_tools():
            self._model(tool)
        return dict(self._raw_parameters)

    def _model(self, tool: Any) -> Any:
        if tool.name not in self._strict_models:
            model = create_model(
                tool.name + "Arguments",
                __base__=tool.fn_metadata.arg_model,
                __config__=ConfigDict(strict=True, extra="forbid", allow_inf_nan=False),
            )
            tool.parameters = model.model_json_schema()
            if "plan" in model.model_fields:
                from mcp_hcl.topology import TopologyPlan

                schema = TopologyPlan.model_json_schema()
                tool.parameters.setdefault("$defs", {}).update(schema.pop("$defs", {}))
                tool.parameters["properties"]["plan"] = schema
            if "request" in model.model_fields:
                from mcp_hcl.planning import TopologyRequest

                schema = TopologyRequest.model_json_schema()
                tool.parameters.setdefault("$defs", {}).update(schema.pop("$defs", {}))
                tool.parameters["properties"]["request"] = schema
            if "parameters" in model.model_fields and tool.name in {
                "hcl_apply_acl",
                "hcl_apply_vlan",
                "hcl_apply_vlan_network",
                "hcl_remove_acl",
                "hcl_apply_nat",
                "hcl_remove_nat",
                "hcl_apply_interface_tuning",
                "hcl_apply_stp",
                "hcl_apply_port_security",
                "hcl_apply_netstream",
            }:
                from mcp_hcl.acl_removal import AclRemoval
                from mcp_hcl.interface_tuning import InterfaceTuning
                from mcp_hcl.nat_models import NatApplication, NatRemoval
                from mcp_hcl.netstream_policy import NetstreamRequest
                from mcp_hcl.services import Acl, Vlan
                from mcp_hcl.switch_security import PortSecurityRequest, StpRequest
                from mcp_hcl.vlan_network import VlanNetworkRequest

                schema = (
                    {
                        "hcl_apply_acl": Acl,
                        "hcl_apply_vlan": Vlan,
                        "hcl_apply_vlan_network": VlanNetworkRequest,
                        "hcl_remove_acl": AclRemoval,
                        "hcl_apply_nat": NatApplication,
                        "hcl_remove_nat": NatRemoval,
                        "hcl_apply_interface_tuning": InterfaceTuning,
                        "hcl_apply_stp": StpRequest,
                        "hcl_apply_port_security": PortSecurityRequest,
                        "hcl_apply_netstream": NetstreamRequest,
                    }[tool.name]
                ).model_json_schema()
                tool.parameters.setdefault("$defs", {}).update(schema.pop("$defs", {}))
                tool.parameters["properties"]["parameters"] = schema
            if dsh_compat.schema_mode() == "dsh":
                # DeepSeek Harness only accepts a closed keyword subset and
                # degrades anything else to `unknown`/`Any`.  Project the
                # advertised schema; validation still uses this model.
                self._raw_parameters[tool.name] = json.loads(
                    json.dumps(tool.parameters, ensure_ascii=False)
                )
                tool.parameters = self._project(tool.parameters)
            self._strict_models[tool.name] = model
        return self._strict_models[tool.name]

    @staticmethod
    def _project(parameters: dict[str, Any]) -> dict[str, Any]:
        """Rewrite advertised parameters into the DeepSeek Harness subset."""
        defs = parameters.get("$defs")
        local_defs = defs if isinstance(defs, dict) else {}
        properties = parameters.get("properties")
        if isinstance(properties, dict):
            depth = dsh_compat.plan_depth()
            for field in ("plan", "request"):
                schema = properties.get(field)
                if isinstance(schema, dict):
                    properties[field] = dsh_compat.outline(schema, depth=depth, defs=local_defs)
            payload = properties.get("parameters")
            if isinstance(payload, dict):
                properties["parameters"] = dsh_compat.project(payload, defs=local_defs)
        return dsh_compat.project_tool_parameters(parameters)

    async def list_tools(self) -> Any:
        for tool in self._tool_manager.list_tools():
            self._model(tool)
            if tool.annotations is None:
                # Safety hints are part of the discovery surface, not the call
                # path: a client decides whether to ask for confirmation from
                # tools/list alone.
                tool.annotations = tool_annotations.for_tool(tool.name)
        return await super().list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        context = self.get_context()
        try:
            request_id = context.request_context.request_id
        except (ValueError, LookupError):
            request_id = None
        tool = self._tool_manager.get_tool(name)
        is_error = False
        target = arguments.get("project_path", arguments.get("artifact_directory"))
        if tool is None:
            body = {"status": "rejected", "errors": [{"code": "unknown_tool", "message": name}]}
            is_error = True
        else:
            try:
                validated = self._model(tool).model_validate(arguments).model_dump_one_level()
            except ValidationError as exc:
                body = {"status": "rejected", "errors": validation_errors(exc)}
                is_error = True
            else:
                try:
                    if (
                        name == "hcl_backend_topology"
                        and arguments.get("action") in {"close", "status"}
                        and set(arguments) != {"action"}
                    ):
                        body = {
                            "status": "rejected",
                            "errors": [
                                {
                                    "code": "invalid_arguments",
                                    "message": "close/status accept only action",
                                }
                            ],
                        }
                    elif self.artifact_retries and (
                        name.startswith("hcl_stage_")
                        or (name == "hcl_full_build" and not validated["activate"])
                    ):
                        body = self.artifact_retries.run(
                            name,
                            validated,
                            lambda: tool.fn(**validated),
                            source=validated.get("project_path"),
                        )
                    else:
                        body = await tool.fn_metadata.call_fn_with_arg_validation(
                            tool.fn,
                            tool.is_async,
                            validated,
                            {tool.context_kwarg: context} if tool.context_kwarg else None,
                        )
                except ConsoleError as exc:
                    body = {"status": "unverified", "errors": [exc.as_error()]}
                except (ValueError, OSError) as exc:
                    body = {
                        "status": "rejected",
                        "error": str(exc),
                        "errors": [{"code": "invalid_operation", "message": str(exc)}],
                    }
                except Exception:
                    body = {
                        "status": "unverified",
                        "errors": [
                            {
                                "code": "internal_error",
                                "message": "The operation failed without a verified result",
                            }
                        ],
                    }
                    is_error = True
        body = envelope(body, request_id=request_id, operation=name, target=target)
        return CallToolResult(
            isError=is_error,
            structuredContent=body,
            content=[TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
        )
