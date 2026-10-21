"""Strict Comware readback parsers; unrecognized output is never an empty pass."""

from __future__ import annotations

import re
from ipaddress import ip_interface


class ReadbackError(ValueError):
    pass


INTERFACE = r"[A-Za-z][A-Za-z0-9-]*\d+(?:/\d+)*(?:\.\d+)?"


def interface_states(text: str) -> list[dict]:
    headers = list(
        re.finditer(
            r"^(" + INTERFACE + r")(?:\s+current state\s*:\s*([^\n]+))?\s*$", text, re.M | re.I
        )
    )
    if not headers:
        raise ReadbackError("No supported full interface sections")
    states = sum(
        bool(re.search(r"\bCurrent state\s*:", line, re.I))
        and not bool(re.match(r"\s*Line protocol", line, re.I))
        for line in text.splitlines()
    )
    if states != len(headers):
        raise ReadbackError("Interface section coverage is incomplete")
    result = []
    names = set()
    for index, header in enumerate(headers):
        block = text[
            header.end() : headers[index + 1].start() if index + 1 < len(headers) else len(text)
        ]
        name = header[1]
        physical = header[2] or (
            match[1]
            if (match := re.search(r"^\s*Current state\s*:\s*([^\n]+)", block, re.M | re.I))
            else None
        )
        protocol = re.search(
            r"^\s*Line protocol(?: current)? state\s*:\s*([^\n]+)", block, re.M | re.I
        )
        if not physical or not protocol or name in names:
            raise ReadbackError("Incomplete or duplicate interface state section")

        def state(value):
            if re.search(r"\bUP\b", value, re.I):
                return "up"
            if re.search(r"\bDOWN\b", value, re.I):
                return "down"
            raise ReadbackError("Unknown interface state")

        row = {
            "interface": name,
            "physical_state": state(physical),
            "protocol_state": state(protocol[1]),
            "administratively_down": bool(re.search(r"Administratively", physical, re.I)),
            "physical_state_text": physical.strip(),
            "protocol_state_text": protocol[1].strip(),
            "linked": None,
            "peer": None,
            "live_cable_inventory_verified": False,
            "mac": None,
            "ipv4": None,
            "mtu": None,
            "speed_bps": None,
            "duplex": None,
        }
        mac = re.search(r"hardware address\s*:\s*(\S+)", block, re.I)
        if mac:
            compact = re.sub(r"[.:-]", "", mac[1])
            if not re.fullmatch(r"[0-9a-f]{12}", compact, re.I):
                raise ReadbackError("Invalid reported MAC address")
            row["mac"] = ":".join(compact[i : i + 2].lower() for i in range(0, 12, 2))
        address = re.search(r"Internet address\s*:\s*([0-9.]+/\d+)", block, re.I)
        if address:
            parsed = ip_interface(address[1])
            if parsed.version != 4:
                raise ReadbackError("Unexpected IPv4 address family")
            row["ipv4"] = {
                "address": str(parsed.ip),
                "prefix_length": parsed.network.prefixlen,
                "netmask": str(parsed.netmask),
            }
        mtu = re.search(
            r"(?:MTU\s*:\s*|Maximum\s+(?:Transmit|Transmission)\s+Unit\s*(?:is|:)\s*)(\d+)",
            block,
            re.I,
        )
        if mtu:
            row["mtu"] = int(mtu[1])
        speed = re.search(r"(\d+)\s*(Mbps|Gbps)-speed", block, re.I)
        if speed:
            row["speed_bps"] = int(speed[1]) * (
                1000000 if speed[2].lower() == "mbps" else 1000000000
            )
        duplex = re.search(r"(full|half)-duplex", block, re.I)
        if duplex:
            row["duplex"] = duplex[1].lower()
        row["anomalies"] = (
            ["line_up_protocol_down"]
            if row["physical_state"] == "up" and row["protocol_state"] == "down"
            else []
        )
        names.add(name)
        result.append(row)
    return result


def vlan_database(text: str) -> list[dict]:
    headers = list(re.finditer(r"^\s*VLAN ID\s*:\s*(\d+)\s*$", text, re.M | re.I))
    if not headers:
        raise ReadbackError("No supported detailed VLAN sections")
    if len(re.findall(r"^\s*VLAN ID\s*:", text, re.M | re.I)) != len(headers):
        raise ReadbackError("Unrecognized VLAN section header")
    count = re.search(r"Total number of existing VLANs\s*:\s*(\d+)", text, re.I)
    if count and int(count[1]) != len(headers):
        raise ReadbackError("VLAN count does not match detailed readback")
    result = []
    seen = set()
    for index, header in enumerate(headers):
        block = text[
            header.end() : headers[index + 1].start() if index + 1 < len(headers) else len(text)
        ]
        number = int(header[1])
        if not 1 <= number <= 4094 or number in seen:
            raise ReadbackError("Duplicate or invalid VLAN ID")
        fields = {
            match[1].lower(): match[2].strip()
            for match in re.finditer(
                r"^\s*(VLAN type|Name|Description|Route interface|Tagged ports|Untagged ports)\s*:\s*([^\n]*)",
                block,
                re.M | re.I,
            )
        }
        if not {"vlan type", "name", "tagged ports", "untagged ports"} <= fields.keys():
            raise ReadbackError("Incomplete VLAN section")

        def ports(label):
            match = re.search(
                r"^\s*"
                + label
                + r"\s*:\s*([^\n]*)\n?([\s\S]*?)(?=^\s*(?:Tagged ports|Untagged ports|[A-Za-z ]+:)|\Z)",
                block,
                re.M | re.I,
            )
            value = (match[1] + " " + match[2]).strip() if match else fields[label.lower()]
            if value.lower() == "none":
                return []
            tokens = value.split()
            if not tokens or any(not re.fullmatch(INTERFACE, token) for token in tokens):
                raise ReadbackError("Unsupported VLAN port list")
            return tokens

        result.append(
            {
                "number": number,
                "name": fields["name"],
                "type": fields["vlan type"],
                "description": fields.get("description"),
                "route_interface": fields.get("route interface"),
                "tagged_ports": ports("Tagged ports"),
                "untagged_ports": ports("Untagged ports"),
                "is_default": None,
                "factory_default_verified": False,
            }
        )
        seen.add(number)
    return result


def qos_definitions(text: str, kind: str) -> list[dict]:
    label = {"classifiers": "Classifier", "behaviors": "Behavior", "policies": "Policy"}[kind]
    if not text.strip():
        # A device with no user-defined QoS answers the query with nothing at all:
        # no header, no "none is configured" line. An empty body is an empty
        # database, and the transcript integrity checks have already run.
        return []
    if not re.search(
        r"User[- ]defined\s+(?:classifier|behavior|QoS policy|policy)\s+information", text, re.I
    ):
        raise ReadbackError("Missing user-defined QoS result header")
    headers = list(re.finditer(r"^\s*" + label + r"\s*:\s*([^\n]+)", text, re.M | re.I))
    result = []
    seen = set()
    for index, header in enumerate(headers):
        name = re.sub(r"\s+\(ID\s+\d+\)\s*$", "", header[1], flags=re.I).strip()
        block = text[
            header.end() : headers[index + 1].start() if index + 1 < len(headers) else len(text)
        ].strip()
        if not name or name in seen:
            raise ReadbackError("Duplicate or empty QoS definition")
        row = {"name": name, "definition": block, "is_default": False}
        if kind == "classifiers":
            operator = re.search(r"^\s*Operator\s*:\s*(AND|OR)\s*$", block, re.M | re.I)
            if not operator or not re.search(r"Rule\(s\)\s*:", block, re.I):
                raise ReadbackError("Incomplete classifier definition")
            row["match_type"] = operator[1].lower()
            row["rules"] = [
                match[1].strip()
                for match in re.finditer(r"^\s*(If-match\s+[^\n]+)", block, re.M | re.I)
            ]
        elif kind == "policies":
            classes = []
            class_headers = list(re.finditer(r"^\s*Classifier\s*:\s*([^\n]+)", block, re.M | re.I))
            for ordinal, item in enumerate(class_headers):
                classifier = re.sub(r"\s+\(ID\s+\d+\)\s*$", "", item[1], flags=re.I).strip()
                contents = block[
                    item.end() : class_headers[ordinal + 1].start()
                    if ordinal + 1 < len(class_headers)
                    else len(block)
                ]
                behavior = re.search(r"^\s*Behavior\s*:\s*(\S+)\s*$", contents, re.M | re.I)
                if not classifier or not behavior:
                    raise ReadbackError("Incomplete QoS policy binding")
                classes.append({"classifier": classifier, "behavior": behavior[1]})
            if not class_headers and not re.search(r"\b(?:none|no classifier)\b", block, re.I):
                raise ReadbackError("QoS policy has no binding or explicit empty marker")
            row["classes"] = classes
        elif not block:
            raise ReadbackError("Incomplete behavior definition")
        seen.add(name)
        result.append(row)
    if not result and not re.search(
        r"(?:no\s+\S+\s+(?:is|are)\s+configured|none|total\s*:\s*0)", text, re.I
    ):
        raise ReadbackError("QoS header without a definition or explicit empty marker")
    return result
