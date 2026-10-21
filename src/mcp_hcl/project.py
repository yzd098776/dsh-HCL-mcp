"""Read and stage HCL 5.10.x project changes without touching a live GUI.

The parser preserves unknown fields and edits only the observed ConfigObj-style
device sections. HCL itself remains the final validator when a staged project
is opened. No operation in this module claims to refresh an open canvas.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


class ProjectError(ValueError):
    """A project or requested change cannot be staged safely."""


_HEADER = re.compile(r"^    \[\[(.+)\]\]\s*$")
_FIELD = re.compile(r"^        ([^=]+?)\s*=\s*(.*?)\s*$")
_PORT = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?:/[0-9]+)+")
_CLOUD_HEADER = re.compile(r"^\s*\[\[Cloud\s+(.+?)\]\]\s*$")
_STAGE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,19}")
_DEVICE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
_REQUIRED_FILES = ("project.json", "deviceversion.txt")


@dataclass(frozen=True)
class DeviceSection:
    name: str
    model: str
    start: int
    end: int
    fields: dict[str, str]


@dataclass(frozen=True)
class ProjectDocument:
    directory: Path
    net_path: Path
    metadata: dict[str, Any]
    lines: list[str]
    sections: dict[str, DeviceSection]
    vbox_start: int
    vbox_end: int
    newline: str

    def summary(self) -> dict[str, Any]:
        ids: set[int] = set()
        for section in self.sections.values():
            try:
                device_id = int(section.fields["device_id"])
                coordinates = [float(section.fields.get(axis, "0")) for axis in ("x", "y", "z")]
            except (ValueError, KeyError, OverflowError) as exc:
                raise ProjectError(f"Invalid device ID or coordinate: {section.name}") from exc
            if device_id <= 0 or device_id in ids or not all(math.isfinite(v) for v in coordinates):
                raise ProjectError(
                    f"Duplicate/invalid device ID or nonfinite coordinate: {section.name}"
                )
            ids.add(device_id)
        links: set[tuple[tuple[str, str], tuple[str, str]]] = set()
        for section in self.sections.values():
            for port, value in section.fields.items():
                if not _PORT.fullmatch(port) or " " not in value:
                    continue
                peer_name, peer_port = value.rsplit(" ", 1)
                if peer_name not in self.sections or not _PORT.fullmatch(peer_port):
                    continue
                endpoints = tuple(sorted(((section.name, port), (peer_name, peer_port))))
                links.add(endpoints)
        return {
            "project_name": self.metadata.get("projectInfo", {}).get("name"),
            "project_file": str(self.net_path),
            "devices": [
                {
                    "name": section.name,
                    "model": section.model,
                    "device_id": int(section.fields["device_id"]),
                    "x": float(section.fields.get("x", "0")),
                    "y": float(section.fields.get("y", "0")),
                }
                for section in self.sections.values()
            ],
            "links": [
                {"a": a[0], "a_port": a[1], "b": b[0], "b_port": b[1]} for a, b in sorted(links)
            ],
        }


def _read_document(directory: Path) -> ProjectDocument:
    if not directory.is_dir():
        raise ProjectError(f"Project directory does not exist: {directory}")
    metadata_path = directory / "project.json"
    if metadata_path.is_symlink():
        raise ProjectError("Linked project.json files are not accepted")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectError(f"Cannot read project.json: {exc}") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("deviceInfoList"), list):
        raise ProjectError("project.json needs a deviceInfoList array")
    if not isinstance(metadata.get("projectInfo"), dict):
        raise ProjectError("project.json needs a projectInfo object")
    if any(not isinstance(row, dict) for row in metadata["deviceInfoList"]):
        raise ProjectError("deviceInfoList entries must be objects")
    net_paths = list(directory.glob("*.net"))
    if len(net_paths) != 1:
        raise ProjectError("Project directory must contain exactly one .net file")
    net_path = net_paths[0]
    if net_path.is_symlink():
        raise ProjectError("Linked .net files are not accepted")
    data = net_path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ProjectError(f".net file is not UTF-8: {exc}") from exc
    newline = "\r\n" if b"\r\n" in data else "\n"
    lines = text.splitlines(keepends=True)
    vbox_starts = [i for i, line in enumerate(lines) if line.startswith("[vbox ")]
    if len(vbox_starts) != 1:
        raise ProjectError("Expected exactly one [vbox ...] section")
    vbox_start = vbox_starts[0]
    vbox_end = next(
        (i for i in range(vbox_start + 1, len(lines)) if lines[i].startswith("[")),
        len(lines),
    )
    metadata_names = [
        row.get("resourceName")
        for row in metadata["deviceInfoList"]
        if isinstance(row, dict) and isinstance(row.get("resourceName"), str)
    ]
    headers: list[tuple[int, str]] = []
    for index in range(vbox_start + 1, vbox_end):
        match = _HEADER.match(lines[index].rstrip("\r\n"))
        if match:
            headers.append((index, match.group(1)))
    sections: dict[str, DeviceSection] = {}
    for offset, (start, label) in enumerate(headers):
        end = headers[offset + 1][0] if offset + 1 < len(headers) else vbox_end
        matching_names = [name for name in metadata_names if label.endswith(" " + name)]
        if len(matching_names) != 1:
            raise ProjectError(f"Device section does not match exactly one metadata entry: {label}")
        name = matching_names[0]
        model = label[: -(len(name) + 1)]
        fields: dict[str, str] = {}
        for line in lines[start + 1 : end]:
            match = _FIELD.match(line.rstrip("\r\n"))
            if match:
                key, value = match.group(1).strip(), match.group(2).strip()
                if key in fields:
                    raise ProjectError(f"Duplicate field {key} on {name}")
                fields[key] = value
        if "device_id" not in fields:
            raise ProjectError(f"Missing device_id on {name}")
        if name in sections:
            raise ProjectError(f"Duplicate device {name}")
        sections[name] = DeviceSection(name, model, start, end, fields)
    if not sections:
        raise ProjectError("No device sections found")
    ids = [section.fields["device_id"] for section in sections.values()]
    if len(ids) != len(set(ids)):
        raise ProjectError("Duplicate device IDs")
    for section in sections.values():
        for port, value in section.fields.items():
            if not _PORT.fullmatch(port) or " " not in value:
                continue
            peer_name, peer_port = value.rsplit(" ", 1)
            if peer_name in sections and _PORT.fullmatch(peer_port):
                if sections[peer_name].fields.get(peer_port) != f"{section.name} {port}":
                    raise ProjectError(f"Asymmetric link: {section.name}/{port}")
    return ProjectDocument(
        directory, net_path, metadata, lines, sections, vbox_start, vbox_end, newline
    )


def _cloud_device_names(metadata: dict[str, Any], lines: list[str]) -> set[str]:
    """Metadata entries that map to a [[Cloud ...]] terminal, not a [vbox] device.

    HCL lists cloud terminals such as Host_1 in deviceInfoList even though their
    only block lives under [Non-vBox-DATA] as [[Cloud Host_1]] with no device_id
    and no configPath. Counting every metadata row would reject those projects,
    so the .net file itself decides which rows are real virtual devices.
    """
    declared = set()
    for line in lines:
        match = _CLOUD_HEADER.match(line.rstrip("\r\n"))
        if match:
            declared.add(match.group(1))
    if not declared:
        return set()
    return {
        row["resourceName"]
        for row in metadata["deviceInfoList"]
        if isinstance(row.get("resourceName"), str)
        and row["resourceName"] in declared
        and not row.get("configPath")
    }


def _insert_field(block: list[str], key: str, value: str, newline: str) -> list[str]:
    for line in block:
        match = _FIELD.match(line.rstrip("\r\n"))
        if match and match.group(1).strip() == key:
            raise ProjectError(f"Port or field already in use: {key}")
    position = next(
        (i for i, line in enumerate(block) if re.match(r"^        [xyz] = ", line)),
        len(block),
    )
    result = list(block)
    result.insert(position, f"        {key} = {value}{newline}")
    return result


def _check_port(port: str) -> None:
    if not _PORT.fullmatch(port):
        raise ProjectError(f"Invalid port name: {port}")


def _discard(tree: Path) -> None:
    """Remove an abandoned stage without hiding the error that abandoned it."""
    for path in sorted(tree.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
    shutil.rmtree(tree, ignore_errors=True)


class ProjectStore:
    """Stage copies under a configured output root; never rewrite the source."""

    def __init__(self, project_roots: list[Path], output_root: Path):
        self.output_root = output_root.resolve()
        self.project_roots = [root.resolve() for root in project_roots] + [self.output_root]

    def _open(self, project_path: str) -> ProjectDocument:
        directory = Path(project_path).resolve()
        if not any(directory.is_relative_to(root) for root in self.project_roots):
            raise ProjectError("Project path is outside configured roots")
        document = _read_document(directory)
        document.summary()
        return document

    def inspect(self, project_path: str) -> dict[str, Any]:
        return self._open(project_path).summary()

    def list_projects(self) -> list[dict[str, str]]:
        projects: list[dict[str, str]] = []
        for root in self.project_roots:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if child.is_dir() and not child.is_symlink() and (child / "project.json").is_file():
                    try:
                        doc = self._open(str(child))
                    except (ProjectError, OSError):
                        continue
                    projects.append(
                        {
                            "name": str(
                                doc.metadata.get("projectInfo", {}).get("name", child.name)
                            ),
                            "directory": str(child),
                        }
                    )
        return projects

    def _stage(self, doc: ProjectDocument, stage_name: str) -> tuple[Path, Path]:
        if self.output_root.is_relative_to(doc.directory):
            raise ProjectError("Stage output root must not be inside the source project")
        if not _STAGE_NAME.fullmatch(stage_name):
            raise ProjectError("stage_name must be 1-20 ASCII letters, digits or underscores")
        final_root = self.output_root / stage_name
        if final_root.exists():
            raise ProjectError(f"Stage already exists: {final_root}")
        try:
            self.output_root.mkdir(parents=True, exist_ok=True)
            # A directory taken from the source can be read-only, and read-only files
            # copied out of it stay read-only, so cleanup has to be able to clear the
            # flag. Without this a cleanup failure reports PermissionError instead of
            # the real reason the stage was abandoned.
            temp_root = Path(tempfile.mkdtemp(prefix=".hcl-stage-", dir=self.output_root))
        except PermissionError as exc:
            raise ProjectError(
                f"Cannot create a stage directory under {self.output_root}: {exc}. "
                "MCP_HCL_OUTPUT_ROOT must be writable by the process running this server; "
                "a sandboxed or low-integrity host can write only inside its own workspace."
            ) from exc
        temp_project = temp_root / doc.directory.name
        try:
            temp_project.mkdir()
            for filename in _REQUIRED_FILES:
                source = doc.directory / filename
                if not source.is_file() or source.is_symlink():
                    raise ProjectError(f"Missing or linked project file: {filename}")
                shutil.copy2(source, temp_project / filename)
            shutil.copy2(doc.net_path, temp_project / doc.net_path.name)
            for dirname in ("ConfigDisk", "DeviceConfig"):
                source = doc.directory / dirname
                if (
                    not source.is_dir()
                    or source.is_symlink()
                    or getattr(source, "is_junction", lambda: False)()
                ):
                    raise ProjectError(f"Missing project directory: {dirname}")
                if any(
                    path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                    for path in source.rglob("*")
                ):
                    raise ProjectError(f"Linked files are not accepted in {dirname}")
                shutil.copytree(source, temp_project / dirname)
            for optional_name in ("SerialFile", "MCPArtifacts"):
                optional = doc.directory / optional_name
                if optional.exists():
                    if (
                        not optional.is_dir()
                        or optional.is_symlink()
                        or getattr(optional, "is_junction", lambda: False)()
                    ):
                        raise ProjectError(f"Linked or invalid {optional_name} directory")
                    if any(
                        path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                        for path in optional.rglob("*")
                    ):
                        raise ProjectError(f"Linked files are not accepted in {optional_name}")
                    shutil.copytree(optional, temp_project / optional_name)
            return temp_root, final_root
        except Exception:
            _discard(temp_root)
            raise

    def _commit(
        self,
        doc: ProjectDocument,
        stage_name: str,
        lines: list[str],
        metadata: dict[str, Any],
        new_config_name: str | None = None,
        additional_files: dict[str, bytes] | None = None,
    ) -> dict[str, Any]:
        temp_root, final_root = self._stage(doc, stage_name)
        temp_project = temp_root / doc.directory.name
        try:
            final_project = final_root / doc.directory.name
            text = "".join(lines)
            workingdir = re.compile(r"(?m)^    workingdir = [^\r\n]*")
            if len(workingdir.findall(text)) != 1:
                raise ProjectError("Expected exactly one workingdir field")
            text = workingdir.sub(lambda _: f"    workingdir = {final_project}", text, count=1)
            (temp_project / doc.net_path.name).write_bytes(text.encode("utf-8"))
            metadata["projectInfo"]["name"] = stage_name
            (temp_project / "project.json").write_text(
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
            )
            if new_config_name is not None:
                (temp_project / "DeviceConfig" / f"{new_config_name}.cfg").write_bytes(b"")
            for relative, content in (additional_files or {}).items():
                target = temp_project / relative
                if not target.resolve().is_relative_to(temp_project.resolve()):
                    raise ProjectError("Additional staged file escaped the project")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            checked = _read_document(temp_project)
            cloud_names = _cloud_device_names(metadata, checked.lines)
            expected = [
                row
                for row in metadata["deviceInfoList"]
                if not (
                    isinstance(row.get("resourceName"), str) and row["resourceName"] in cloud_names
                )
            ]
            if len(checked.sections) != len(expected):
                raise ProjectError("Staged .net and project.json device counts disagree")
            summary = checked.summary()
            operation_id = uuid4().hex
            marker = temp_project / "MCPArtifacts" / "stage-receipt.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation_id": operation_id,
                        "source_directory": str(doc.directory),
                        "project_file": str(final_project / doc.net_path.name),
                        "stage_name": stage_name,
                        "runtime_verified": False,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.rename(temp_root, final_root)
            return {
                "status": "staged",
                "operation_id": operation_id,
                "project_file": str(final_project / doc.net_path.name),
                "project_directory": str(final_project),
                "activation": "Open this .net file in HCL; the current canvas is unchanged",
                "device_count": len(checked.sections),
                "link_count": len(summary["links"]),
            }
        except Exception:
            if temp_root.exists():
                _discard(temp_root)
            raise

    def stage_copy(self, project_path: str, stage_name: str) -> dict[str, Any]:
        """Create an isolated, reloadable project without adding topology changes."""
        doc = self._open(project_path)
        return self._commit(doc, stage_name, list(doc.lines), copy.deepcopy(doc.metadata))

    def stage_add_device(
        self,
        project_path: str,
        stage_name: str,
        template_device: str,
        new_name: str,
        x: float,
        y: float,
        connect_to: str | None = None,
        connect_to_port: str | None = None,
        new_port: str | None = None,
    ) -> dict[str, Any]:
        doc = self._open(project_path)
        if template_device not in doc.sections:
            raise ProjectError(f"Unknown template device: {template_device}")
        if not _DEVICE_NAME.fullmatch(new_name) or any(
            existing.lower() == new_name.lower() for existing in doc.sections
        ):
            raise ProjectError("New device name is invalid or already used")
        if not math.isfinite(x) or not math.isfinite(y) or abs(x) > 10000 or abs(y) > 10000:
            raise ProjectError("Coordinates exceed supported planning range")
        if any(value is not None for value in (connect_to, connect_to_port, new_port)):
            if not all(value is not None for value in (connect_to, connect_to_port, new_port)):
                raise ProjectError(
                    "connect_to, connect_to_port and new_port must be supplied together"
                )
            if connect_to not in doc.sections:
                raise ProjectError(f"Unknown peer device: {connect_to}")
            _check_port(connect_to_port)
            _check_port(new_port)
            if connect_to_port in doc.sections[connect_to].fields:
                raise ProjectError(f"Peer port is already used: {connect_to}/{connect_to_port}")
        template = doc.sections[template_device]
        new_id = max(int(section.fields["device_id"]) for section in doc.sections.values()) + 1
        existing_macs = {
            section.fields.get("bridge_mac", "").lower() for section in doc.sections.values()
        }
        while True:
            mac = "64-52-" + "-".join(f"{byte:02x}" for byte in secrets.token_bytes(4))
            if mac not in existing_macs:
                break
        newline = doc.newline
        new_block = [
            f"    [[{template.model} {new_name}]]{newline}",
            f"        device_id = {new_id}{newline}",
            f"        bridge_mac = {mac}{newline}",
        ]
        for key, value in template.fields.items():
            if key not in {"device_id", "bridge_mac", "x", "y", "z"} and not _PORT.fullmatch(key):
                new_block.append(f"        {key} = {value}{newline}")
        if connect_to is not None:
            new_block.append(f"        {new_port} = {connect_to} {connect_to_port}{newline}")
        new_block.extend(
            (f"        x = {x}{newline}", f"        y = {y}{newline}", f"        z = 5.0{newline}")
        )
        lines = list(doc.lines)
        if connect_to is not None:
            peer = doc.sections[connect_to]
            lines[peer.start : peer.end] = _insert_field(
                lines[peer.start : peer.end], connect_to_port, f"{new_name} {new_port}", newline
            )
        vbox_end = next(
            (i for i, line in enumerate(lines) if line.startswith("[Non-vBox-DATA]")), len(lines)
        )
        lines[vbox_end:vbox_end] = new_block
        metadata = copy.deepcopy(doc.metadata)
        template_meta = next(
            row for row in metadata["deviceInfoList"] if row.get("resourceName") == template_device
        )
        new_meta = copy.deepcopy(template_meta)
        new_meta["resourceName"] = new_name
        new_meta["configPath"] = f"DeviceConfig\\{new_name}.cfg"
        metadata["deviceInfoList"].append(new_meta)
        return self._commit(doc, stage_name, lines, metadata, new_config_name=new_name)

    def stage_link(
        self,
        project_path: str,
        stage_name: str,
        a: str,
        a_port: str,
        b: str,
        b_port: str,
    ) -> dict[str, Any]:
        doc = self._open(project_path)
        if a == b or a not in doc.sections or b not in doc.sections:
            raise ProjectError("Link requires two different known devices")
        _check_port(a_port)
        _check_port(b_port)
        if a_port in doc.sections[a].fields or b_port in doc.sections[b].fields:
            raise ProjectError("One or both link ports are already used")
        lines = list(doc.lines)
        changes = (
            (doc.sections[a], a_port, f"{b} {b_port}"),
            (doc.sections[b], b_port, f"{a} {a_port}"),
        )
        for section, port, value in sorted(changes, key=lambda item: item[0].start, reverse=True):
            lines[section.start : section.end] = _insert_field(
                lines[section.start : section.end], port, value, doc.newline
            )
        return self._commit(doc, stage_name, lines, copy.deepcopy(doc.metadata))
