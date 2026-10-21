"""Project pydantic JSON Schema into the keyword subset DeepSeek Harness accepts.

DeepSeek Harness validates every tool schema it registers against a closed
subset (``type`` / ``oneOf`` / ``properties`` / ``required`` /
``additionalProperties`` / ``items`` / ``enum`` / ``const`` plus the
``description`` / ``title`` / ``default`` / ``examples`` annotations).  Pydantic
v2 emits ``$defs`` / ``$ref`` / ``anyOf`` / ``discriminator`` / numeric and
string bounds, none of which that subset allows.  A schema outside the subset
still registers, but the harness degrades its generated SDK declaration to
``unknown`` (TypeScript) or ``Any`` (Python) for code-mode calls.

This module rewrites a generated schema into the accepted subset.  It is a
*transport* projection only: it never participates in argument validation.
``StrictFastMCP.call_tool`` validates raw arguments against the pydantic model,
so a projected schema cannot loosen server-side enforcement.

Two projections are provided:

``project``
    Resolve ``$ref`` and inline the whole subtree, dropping unsupported
    keywords.  Used for the small, hand-shaped parameter models.

``outline``
    Keep the structure down to ``depth`` levels and collapse deeper nodes to
    their type only.  Used for the very large ``plan`` / ``request`` payloads,
    whose full schema is served separately as an MCP resource.
"""

from __future__ import annotations

import json
import os
from typing import Any

SCHEMA_MODES = ("dsh", "raw")

#: Keywords the harness accepts besides annotations.
CONSTRAINT_KEYWORDS = frozenset(
    {"type", "oneOf", "properties", "required", "additionalProperties", "items", "enum", "const"}
)
ANNOTATION_KEYWORDS = frozenset({"description", "title", "default", "examples"})
SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean", "null"})
SCHEMA_TYPES = frozenset({"object", "array"}) | SCALAR_TYPES
#: Keywords that may not sit beside ``oneOf``.
ONE_OF_SIBLING_KEYWORDS = frozenset(
    {"properties", "required", "additionalProperties", "items", "enum", "const"}
)

#: Deepest structural level kept before a node collapses to its type.
DEFAULT_DEPTH = 6


def schema_mode() -> str:
    """Return the active schema projection mode.

    ``raw`` is the default so this server keeps advertising exactly the
    pydantic schemas it validates against, which is what a client-agnostic MCP
    server should do.  Set ``MCP_HCL_SCHEMA_MODE=dsh`` to opt into the
    DeepSeek Harness projection.
    """
    mode = os.environ.get("MCP_HCL_SCHEMA_MODE", "raw").strip().lower()
    return mode if mode in SCHEMA_MODES else "raw"


def _json_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _resolve_ref(ref: str, defs: dict[str, Any]) -> dict[str, Any] | None:
    prefix = "#/$defs/"
    if not isinstance(ref, str) or not ref.startswith(prefix):
        return None
    target = defs.get(ref[len(prefix) :])
    return target if isinstance(target, dict) else None


def _scalar_type_of(value: Any) -> str | None:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return None


def _pick_type(node: dict[str, Any]) -> str | None:
    declared = node.get("type")
    if isinstance(declared, str):
        return declared if declared in SCHEMA_TYPES else None
    if isinstance(declared, list):
        for entry in declared:
            if isinstance(entry, str) and entry in SCHEMA_TYPES and entry != "null":
                return entry
        for entry in declared:
            if isinstance(entry, str) and entry in SCHEMA_TYPES:
                return entry
    return None


def _permissive(node: dict[str, Any], schema_type: str | None) -> dict[str, Any]:
    """Collapse a node to the least-constrained schema of its own type."""
    out: dict[str, Any] = {}
    if isinstance(node.get("description"), str):
        out["description"] = node["description"]
    if schema_type is not None:
        out["type"] = schema_type
        if schema_type == "object":
            out["additionalProperties"] = True
    return out


def _sanitize(
    node: Any,
    defs: dict[str, Any],
    depth: int,
    ref_stack: tuple[str, ...],
) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}

    # Resolve local $ref chains first; a self-referential chain collapses.
    ref = node.get("$ref")
    if isinstance(ref, str):
        target = _resolve_ref(ref, defs)
        if target is None or ref in ref_stack:
            return _permissive(node, _pick_type(node))
        merged = {key: value for key, value in node.items() if key != "$ref"}
        merged = {**target, **merged}
        return _sanitize(merged, defs, depth, ref_stack + (ref,))

    out: dict[str, Any] = {}
    description = node.get("description")
    if isinstance(description, str) and description:
        out["description"] = description

    # pydantic Optional[T] / Union[...] arrive as anyOf.
    any_of = node.get("anyOf")
    if isinstance(any_of, list) and any_of:
        branches = [entry for entry in any_of if not _is_null_branch(entry)]
        if not branches:
            return out
        if len(branches) == 1:
            merged = {key: value for key, value in node.items() if key != "anyOf"}
            merged.pop("description", None)
            merged = {**branches[0], "description": description} if description else branches[0]
            out.update(_sanitize(merged, defs, depth, ref_stack))
            if description:
                out["description"] = description
            return out
        types = {_pick_type(branch) for branch in branches}
        if len(types) == 1:
            collapsed = _permissive(node, types.pop())
            return collapsed or out
        out["description"] = description or "Any JSON value."
        return out

    one_of = node.get("oneOf")
    if isinstance(one_of, list) and len(one_of) >= 2:
        tagged = _tagged_union(node, one_of, defs, depth, ref_stack)
        if tagged is not None:
            if description:
                tagged["description"] = description
            return tagged
        branches = [_sanitize(branch, defs, depth - 1, ref_stack) for branch in one_of]
        collapsed = _collapse_union(branches)
        if collapsed is not None:
            if description:
                collapsed["description"] = description
            return collapsed
        out["oneOf"] = branches
        return out

    schema_type = _pick_type(node)
    if schema_type is None:
        # Annotation-only node: the subset forbids every constraint keyword here.
        return out

    if depth <= 0:
        return _permissive(node, schema_type)

    out["type"] = schema_type

    if schema_type == "object":
        properties = node.get("properties")
        if isinstance(properties, dict) and properties:
            projected = {
                name: _sanitize(child, defs, depth - 1, ref_stack)
                for name, child in properties.items()
                if isinstance(name, str)
            }
            out["properties"] = projected
            required = node.get("required")
            if isinstance(required, list):
                kept = [name for name in required if isinstance(name, str) and name in projected]
                if kept:
                    out["required"] = kept
        extra = node.get("additionalProperties")
        out["additionalProperties"] = extra if isinstance(extra, bool) else True
        return out

    if schema_type == "array":
        items = node.get("items")
        if isinstance(items, dict):
            out["items"] = _sanitize(items, defs, depth - 1, ref_stack)
        else:
            out["items"] = {}
        return out

    # Scalars: enum/const must match the declared scalar type.
    enum = node.get("enum")
    if isinstance(enum, list) and enum:
        values = [value for value in enum if _scalar_type_of(value) == schema_type]
        if values:
            out["enum"] = values
    const = node.get("const")
    if "const" in node and _scalar_type_of(const) == schema_type:
        out["const"] = const
    default = node.get("default")
    if "default" in node and _json_scalar(default) and _scalar_type_of(default) == schema_type:
        out["default"] = default
    return out


def _is_null_branch(branch: Any) -> bool:
    return isinstance(branch, dict) and branch.get("type") == "null"


def _tagged_union(
    node: dict[str, Any],
    one_of: list[Any],
    defs: dict[str, Any],
    depth: int,
    ref_stack: tuple[str, ...],
) -> dict[str, Any] | None:
    """Recover a pydantic tagged union as an object keyed by its tag values.

    The subset has no ``discriminator`` and cannot express a ten-way ``oneOf``
    cheaply, but the tag itself (``feature`` -> model name) carries the choice.
    Emit ``properties.feature.enum`` plus a per-tag property, which keeps the
    payload shape discoverable instead of collapsing it to "any object".
    """
    discriminator = node.get("discriminator")
    if not isinstance(discriminator, dict):
        return None
    tag = discriminator.get("propertyName")
    mapping = discriminator.get("mapping")
    if not isinstance(tag, str) or not isinstance(mapping, dict):
        return None
    by_ref: dict[str, Any] = {}
    for ref in mapping.values():
        if isinstance(ref, str) and ref not in by_ref:
            by_ref[ref] = _sanitize({"$ref": ref}, defs, depth - 1, ref_stack)
    if not by_ref:
        return None
    per_tag: dict[str, Any] = {}
    for value, ref in mapping.items():
        if not isinstance(value, str) or not isinstance(ref, str):
            continue
        branch = by_ref.get(ref)
        if isinstance(branch, dict):
            per_tag[value] = branch
    if not per_tag:
        return None
    properties: dict[str, Any] = {
        tag: {"type": "string", "enum": sorted(per_tag)},
    }
    properties.update(per_tag)
    return {
        "type": "object",
        "properties": properties,
        "required": [tag],
        "additionalProperties": True,
    }


def _collapse_union(branches: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Collapse a discriminated union that carries no discriminating detail.

    Pydantic renders a tagged union of several structurally distinct models; once
    deep nodes are collapsed those branches often become identical permissive
    objects, and a ten-branch ``oneOf`` of the same shape tells a reader nothing.
    """
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for branch in branches:
        key = json.dumps(branch, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            unique.append(branch)
    if len(unique) == 1:
        return unique[0]
    if all(
        branch.get("type") == "object"
        and not branch.get("properties")
        and branch.get("additionalProperties") is True
        for branch in unique
    ):
        return {"type": "object", "additionalProperties": True}
    types = {branch.get("type") for branch in unique}
    if len(types) == 1:
        only = types.pop()
        if only in SCALAR_TYPES and all(
            set(branch) <= {"type", "description"} for branch in unique
        ):
            return {"type": only}
    return None


def project(
    schema: dict[str, Any], *, depth: int = DEFAULT_DEPTH, defs: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return ``schema`` projected into the DeepSeek Harness keyword subset.

    ``defs`` supplies ``$defs`` that live outside ``schema`` (pydantic hoists
    every nested model's definitions to the root of the arguments schema).
    """
    if not isinstance(schema, dict):
        return {}
    if defs is None:
        local = schema.get("$defs")
        defs = local if isinstance(local, dict) else {}
    root = {key: value for key, value in schema.items() if key != "$defs"}
    return _sanitize(root, defs, depth, ())


def outline(
    schema: dict[str, Any], *, depth: int = 2, defs: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return a shallow structural sketch: field names plus permissive leaves."""
    return project(schema, depth=depth, defs=defs)


def plan_depth() -> int:
    """Structural depth kept for the large ``plan`` / ``request`` payloads.

    Depth 3 keeps every top-level plan section plus the field names of each
    element inside it, which is what an author needs to write a payload, while
    staying near 60 KB for all 55 tools instead of 254 KB.  Raise it with
    ``MCP_HCL_PLAN_SCHEMA_DEPTH`` when full nesting is wanted in the listing.
    """
    raw = os.environ.get("MCP_HCL_PLAN_SCHEMA_DEPTH", "3").strip()
    try:
        depth = int(raw)
    except ValueError:
        return 3
    return max(1, min(depth, 12))


def project_tool_parameters(
    parameters: dict[str, Any], *, depth: int = DEFAULT_DEPTH
) -> dict[str, Any]:
    """Project a whole ``tools/list`` parameters object (root is an object)."""
    projected = project(parameters, depth=depth)
    if projected.get("type") != "object":
        projected = {
            "type": "object",
            "properties": projected.get("properties", {}),
            "additionalProperties": False,
        }
        if isinstance(parameters.get("required"), list):
            required = [
                name
                for name in parameters["required"]
                if isinstance(name, str) and name in projected["properties"]
            ]
            if required:
                projected["required"] = required
    return projected


def schema_bytes(schema: Any) -> int:
    """Byte length of the compact JSON encoding, for budget reporting."""
    return len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
