"""Preserve original startup files; select and verify a unique saved result."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from mcp_hcl.cli_session import ensure_user_view
from mcp_hcl.console import ConsoleError


def checksum(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_url(value):
    if (
        not isinstance(value, str)
        or len(value) > 255
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,15}:/[A-Za-z0-9_.-]+\.cfg", value)
    ):
        raise ConsoleError(
            "Startup file is not a safe root configuration URL", code="startup_file_unverified"
        )
    if ".." in value:
        raise ConsoleError(
            "Startup filename contains an ambiguous path", code="startup_file_unverified"
        )
    return value


def parse_startup(output):
    fields = {}
    labels = {"current": "current", "next main": "main", "next backup": "backup"}
    if re.search(r"(?im)^\s*(?:MainBoard|Slot\s+\d+|Chassis\s+\d+)\s*:", output):
        raise ConsoleError(
            "Distributed startup configuration is unverified", code="startup_layout_unverified"
        )
    for label, value in re.findall(
        r"(?im)^\s*(Current|Next main|Next backup) startup saved-configuration file:\s*([^\r\n]+)$",
        output,
    ):
        key = labels[label.casefold()]
        if key in fields:
            raise ConsoleError("Duplicate startup role", code="startup_layout_unverified")
        value = value.strip()
        if key == "current" and value.endswith("(*)"):
            value = value[:-3]
        fields[key] = None if value.casefold() == "null" else file_url(value)
    if set(fields) != {"current", "main", "backup"}:
        raise ConsoleError("Complete startup roles are missing", code="startup_layout_unverified")
    return fields


class StartupConfig:
    def __init__(self, parse_configuration):
        self.parse_configuration = parse_configuration

    def state(self, channel):
        return parse_startup(channel.exchange("display startup")["output"])

    def capture(self, channel, directory, prefix):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", prefix):
            raise ValueError("Invalid startup receipt prefix")
        ensure_user_view(channel)
        state = self.state(channel)
        files = {}
        for index, name in enumerate(
            dict.fromkeys(value for value in state.values() if value is not None)
        ):
            text = self.parse_configuration(channel.exchange("more " + name)["output"])
            local = directory / f"{prefix}-startup-{index}-before.cfg"
            local.write_text(text, encoding="utf-8")
            files[name] = {"sha256": checksum(text), "local_file": str(local)}
        return {"state": state, "files": files}

    def validate(self, record, directory):
        state = record["state"]
        files = record["files"]
        if set(state) != {"current", "main", "backup"}:
            raise ValueError("Startup snapshot roles changed")
        expected = {file_url(value) for value in state.values() if value is not None}
        if set(files) != expected:
            raise ValueError("Startup snapshot file inventory changed")
        root = directory.resolve()
        for item in files.values():
            local = Path(item["local_file"])
            if (
                local.is_symlink()
                or local.resolve().parent != root
                or not local.is_file()
                or local.stat().st_size > 1024 * 1024
            ):
                raise ValueError("Startup snapshot is missing, linked or outside its receipt")
            if (
                not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                or checksum(local.read_text(encoding="utf-8")) != item["sha256"]
            ):
                raise ValueError("Startup snapshot content changed")

    def verify(self, channel, record, *, expected=None):
        if self.state(channel) != (record["state"] if expected is None else expected):
            raise ConsoleError(
                "Startup pointer readback changed", code="startup_pointer_unverified"
            )
        self.verify_files(channel, record)

    def verify_files(self, channel, record):
        for name, item in record["files"].items():
            text = self.parse_configuration(channel.exchange("more " + file_url(name))["output"])
            if checksum(text) != item["sha256"]:
                raise ConsoleError(
                    "Original startup configuration file changed", code="startup_content_changed"
                )

    def persist(self, channel, record, current, guest_file):
        file_url(guest_file)
        ensure_user_view(channel)
        self.verify(channel, record)
        channel.exchange("save " + guest_file, confirmation=True)
        written = self.parse_configuration(channel.exchange("more " + guest_file)["output"])
        if checksum(written) != checksum(current):
            raise ConsoleError(
                "Saved result differs from running configuration",
                code="persistence_content_unverified",
            )
        self.verify(channel, record)
        channel.exchange("startup saved-configuration " + guest_file + " main")
        expected = {**record["state"], "main": guest_file}
        self.verify(channel, record, expected=expected)
        selected = self.parse_configuration(
            channel.exchange("display saved-configuration")["output"]
        )
        if checksum(selected) != checksum(current):
            raise ConsoleError(
                "Selected startup content differs from running configuration",
                code="persistence_content_unverified",
            )
        running = self.parse_configuration(
            channel.exchange("display current-configuration")["output"]
        )
        if checksum(running) != checksum(current):
            raise ConsoleError(
                "Running configuration changed during persistence",
                code="persistence_running_changed",
            )
        return {
            "guest_file": guest_file,
            "sha256": checksum(selected),
            "state": expected,
            "protocol_persistence_verified": True,
        }

    def restore(self, channel, record, directory):
        self.validate(record, directory)
        ensure_user_view(channel)
        self.verify_files(channel, record)
        if self.state(channel) != record["state"]:
            # The vendor command clears pointers without deleting files.
            channel.exchange("undo startup saved-configuration")
            for role in ("main", "backup"):
                name = record["state"][role]
                if name is not None:
                    channel.exchange(f"startup saved-configuration {file_url(name)} {role}")
        self.verify(channel, record)
        return {"state": record["state"], "protocol_startup_rollback_verified": True}
