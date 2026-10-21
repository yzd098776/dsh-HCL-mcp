"""Portable private model evidence. Integrity never implies runtime acceptance."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import socket
import tempfile
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import Field

from mcp_hcl.artifact_store import ArtifactRetries, linked, tree_hash
from mcp_hcl.models import StrictModel
from mcp_hcl.project import ProjectStore

MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
ROLES = Literal[
    "template",
    "metadata",
    "device_version",
    "port_inventory",
    "configuration",
    "version_readback",
    "interface_readback",
    "phone_before",
    "phone_after",
    "phone_page",
    "mapping_observation",
    "native_transcript",
    "acceptance_receipt",
]


class EvidenceFile(StrictModel):
    path: str
    role: ROLES
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_source: str


class EvidenceOrigin(StrictModel):
    host: str = Field(min_length=1, max_length=256)
    hcl_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    capture_method: Literal["manual_saved_files", "native_console", "test_fixture"]


class ModelCapture(StrictModel):
    model: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    device_name: str = Field(min_length=1, max_length=64)
    device_id: int = Field(ge=1)
    control_kind: Literal["comware", "native_gui"]
    configuration: str | None = None
    version_readback: str | None = None
    interface_readback: str | None = None
    phone_before: str | None = None
    phone_after: str | None = None
    phone_page: str | None = None
    native_transcript: str | None = None


class EvidenceManifest(StrictModel):
    schema_version: Literal[2] = 2
    origin: EvidenceOrigin
    files: list[EvidenceFile] = Field(min_length=3, max_length=128)
    models: list[ModelCapture] = Field(min_length=1, max_length=32)


class CaptureDevice(StrictModel):
    name: str
    control_kind: Literal["comware", "native_gui"]
    configuration: str | None = None
    version_readback: str | None = None
    interface_readback: str | None = None
    phone_before: str | None = None
    phone_after: str | None = None
    phone_page: str | None = None


class CaptureSpec(StrictModel):
    project_directory: str
    installation: str
    hcl_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    devices: list[CaptureDevice] = Field(min_length=1, max_length=32)
    mapping_observations: list[str] = Field(default_factory=list, max_length=32)
    acceptance_receipts: list[str] = Field(default_factory=list, max_length=16)
    native_readbacks: bool = False
    topology: str | None = None
    gui_receipt: str | None = None


def read_file(path: Path) -> bytes:
    # Check each original component before resolve, including Windows junctions.
    if any(linked(p) for p in [path, *path.parents]):
        raise ValueError("Evidence paths cannot contain links")
    if not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError("Evidence file missing or oversized")
    return path.read_bytes()


def relative_file(root: Path, name: str) -> Path:
    p = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or ":" in name
        or p.is_absolute()
        or any(v in ("..", ".") for v in p.parts)
        or p.as_posix() != name
    ):
        raise ValueError("Evidence file must use a canonical relative path")
    destination = root / name
    if not destination.resolve().is_relative_to(root.resolve()):
        raise ValueError("Evidence path escaped bundle")
    return destination


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def audit_evidence(directory: Path) -> dict:
    raw = read_file(directory / "manifest.json")
    if len(raw) > 65536:
        raise ValueError("Evidence manifest oversized")
    manifest = EvidenceManifest.model_validate(json.loads(raw.decode("utf-8-sig")))
    files = {}
    total = 0
    for row in manifest.files:
        if row.path.casefold() in {key.casefold() for key in files}:
            raise ValueError("Duplicate evidence file")
        path = relative_file(directory, row.path)
        data = read_file(path)
        total += len(data)
        if total > MAX_TOTAL or digest(data) != row.sha256:
            raise ValueError("Evidence contents changed or oversized")
        if path.suffix.lower() in {".vdi", ".vmdk", ".chm", ".exe", ".dll", ".pdf"}:
            raise ValueError("Vendor binaries/manuals are not evidence payloads")
        files[row.path] = (row, path, data)
    actual = tree_hash(directory)
    if set(actual) != {"manifest.json", *files}:
        raise ValueError("Unlisted files in evidence bundle")

    def one(role):
        found = [entry for entry in files.values() if entry[0].role == role]
        if len(found) != 1:
            raise ValueError(f"Exactly one {role} source is required")
        return found[0]

    template = one("template")
    metadata = one("metadata")
    version = one("device_version")
    if (
        template[1].parent != metadata[1].parent
        or template[1].parent != version[1].parent
        or metadata[1].name != "project.json"
        or version[1].name != "deviceversion.txt"
    ):
        raise ValueError("Template companions must be in the same project directory")
    doc = ProjectStore([template[1].parent], directory.parent / "unused-output")._open(
        str(template[1].parent)
    )
    # ProjectStore canonicalizes Windows 8.3 aliases. Compare the same resolved
    # file rather than rejecting a valid bundle solely for its spelling.
    # read_file/relative_file above still reject links, traversal and bad hashes.
    if doc.net_path != template[1].resolve(strict=True):
        raise ValueError("Ambiguous template file")
    doc.summary()
    recorded_version = re.search(r"(?m)^version\s*=\s*(\S+)", template[2].decode("utf-8-sig"))
    if not recorded_version or recorded_version[1] != manifest.origin.hcl_version:
        raise ValueError("HCL version differs from project evidence")
    profiles = []
    missing = []
    seen = set()

    def referenced(name, role):
        if not name or name not in files or files[name][0].role != role:
            raise ValueError(f"Missing {role} source")
        return files[name][2].decode("utf-8-sig")

    for record in manifest.models:
        if record.model in seen:
            raise ValueError("Duplicate/conflicting model capture")
        seen.add(record.model)
        section = doc.sections.get(record.device_name)
        if (
            not section
            or section.model != record.model
            or int(section.fields["device_id"]) != record.device_id
        ):
            raise ValueError("Model/device identity differs from real template")
        profile = {
            "model": record.model,
            "control_kind": record.control_kind,
            "device_name": record.device_name,
            "device_id": record.device_id,
            "default_interfaces": [],
            "native_interfaces": [],
            "default_firmware": None,
            "runtime_verified": False,
            "mapping_verified": False,
            "factory_defaults_verified": False,
            "native_settings_supported": False,
            "origin": manifest.origin.model_dump(),
            "evidence": {"source": str(directory / "manifest.json"), "sha256": digest(raw)},
            "interface_evidence_scope": "portable_captured_configuration_only",
        }
        if record.control_kind == "comware":
            text = referenced(record.configuration, "configuration")
            if not re.search(r"(?m)^#\s*$", text) or not re.search(r"(?m)^return\s*$", text):
                raise ValueError("Configuration is not a complete native file")
            interfaces = re.findall(r"^interface\s+([A-Za-z][\w/.-]*)\s*$", text, re.M)
            firmware = re.search(r"^\s*version\s+(.+)$", text, re.M)
            if not interfaces or not firmware:
                raise ValueError("Captured interfaces or firmware missing")
            profile.update(default_interfaces=interfaces, default_firmware=firmware[1].strip())
            for attr, role in [
                ("version_readback", "version_readback"),
                ("interface_readback", "interface_readback"),
            ]:
                if getattr(record, attr):
                    observed = referenced(getattr(record, attr), role)
                    if attr == "interface_readback" and any(
                        i not in observed for i in interfaces if i != "NULL0"
                    ):
                        raise ValueError("Interface readback differs from configuration")
                    if attr == "version_readback":
                        observed_version = re.search(
                            r"(?im)Comware[^\r\n]*?\bVersion\s+([^\r\n]+)", observed
                        )

                        def normalize(value):
                            return re.sub(r"[\s,]+", " ", value).strip().casefold()

                        if not observed_version or normalize(observed_version[1]) != normalize(
                            firmware[1]
                        ):
                            raise ValueError("Firmware version readback differs from configuration")
                else:
                    missing.append(f"{record.model}: {role}")
            if manifest.origin.capture_method == "native_console":
                from mcp_hcl.cli_session import CLI_ERROR, PROMPT, clean_text
                from mcp_hcl.deployment import config_text

                transcript = json.loads(referenced(record.native_transcript, "native_transcript"))
                commands = ["display current-configuration", "display version", "display interface"]
                if not isinstance(transcript, list) or len(transcript) != 3:
                    raise ValueError("Incomplete native command history")
                for reply, command, role in zip(
                    transcript,
                    commands,
                    ["configuration", "version_readback", "interface_readback"],
                ):
                    try:
                        raw_reply = base64.b64decode(reply["raw_output_b64"], validate=True)
                    except (ValueError, KeyError) as exc:
                        raise ValueError("Invalid native raw output") from exc
                    observed = clean_text(raw_reply)
                    if (
                        reply.get("command") != command
                        or reply.get("complete") is not True
                        or reply.get("output_sha256") != digest(raw_reply)
                        or reply.get("request_sha256") != digest(command.encode("ascii"))
                        or reply.get("output") != observed
                        or not PROMPT.search(observed)
                        or CLI_ERROR.search(observed)
                        or not any(
                            line.strip() == command
                            or line.strip().endswith(">" + command)
                            or line.strip().endswith("]" + command)
                            for line in observed.splitlines()
                        )
                    ):
                        raise ValueError(
                            "Native output is incomplete or cannot be attributed to the command"
                        )
                    captured = referenced(getattr(record, role), role)
                    if (config_text(observed) if role == "configuration" else observed) != captured:
                        raise ValueError("Captured output differs from raw native transcript")
        else:
            if record.model != "Phone":
                raise ValueError("No native GUI terminal profile is verified for this model")
            inventory = one("port_inventory")[2].decode("utf-8-sig")
            family = re.search(r"(?ms)^\[Phone\]\s*\n(.*?)(?=^\[[^\[]|\Z)", inventory)
            radios = re.findall(
                r"\[\[(\d+)\]\]\s*\n\s*WLAN-Radio\s*=\s*PHY_RADIO", family[1] if family else ""
            )
            if not radios:
                raise ValueError("Phone radio inventory missing")
            profile["native_interfaces"] = [f"WLAN-Radio_0/{n}" for n in radios]
            profile["capabilities"] = {
                "ipv4_modes": ["dhcp"],
                "ipv6_modes": ["none"],
                "wireless_security": ["open"],
                "security_evidence_scope": "5.10.1_manual_only_5.10.3_security_unverified",
            }
            if record.phone_before and record.phone_after:
                before = referenced(record.phone_before, "phone_before")
                after = referenced(record.phone_after, "phone_after")
                from mcp_hcl.project import _FIELD

                def fields(text):
                    lines = text.splitlines()
                    start = next(
                        (
                            i
                            for i, line in enumerate(lines)
                            if line.strip() == f"[[Phone {record.device_name}]]"
                        ),
                        None,
                    )
                    if start is None:
                        raise ValueError("Phone identity missing from saved project")
                    end = next(
                        (
                            i
                            for i in range(start + 1, len(lines))
                            if lines[i].startswith("    [[") or lines[i].startswith("[Non-vBox")
                        ),
                        len(lines),
                    )
                    return {
                        m[1].strip(): m[2]
                        for line in lines[start + 1 : end]
                        if (m := _FIELD.match(line))
                    }

                old, new = fields(before), fields(after)
                if old.get("device_id") != str(record.device_id) or new.get("device_id") != str(
                    record.device_id
                ):
                    raise ValueError("Phone save identity changed")
                profile["observed_changed_fields"] = sorted(
                    k for k in old.keys() | new.keys() if old.get(k) != new.get(k)
                )
                # A field difference does not prove its semantic meaning or PSK support.
            else:
                missing.append("Phone: before/after saved project pair")
            if record.phone_page:
                if (
                    record.phone_page not in files
                    or files[record.phone_page][0].role != "phone_page"
                ):
                    raise ValueError("Phone page source missing")
            else:
                missing.append("Phone: native configuration page")
            missing.extend(
                [
                    "Phone: validated native setting serialization",
                    "Phone: WPA2-PSK association, DHCP and ping acceptance",
                ]
            )
        missing.append(f"{record.model}: independently accepted scene/CLI mapping")
        profiles.append(profile)
    if manifest.origin.capture_method == "test_fixture":
        missing.append("Synthetic fixtures cannot become catalog profiles")
    return {
        "status": "verified",
        "verified_scope": "portable_source_integrity_and_structure_only",
        "profiles": profiles,
        "missing": sorted(set(missing)),
        "origin": manifest.origin.model_dump(),
        "locally_executed": False,
        "runtime_verified": False,
        "wireless_gate_complete": False,
        "full_live_gate_passed": False,
        "file_hashes": actual,
    }


def collect_evidence(spec_data: dict, output: Path) -> dict:
    spec = CaptureSpec.model_validate(spec_data)
    if output.exists():
        raise ValueError("Evidence output exists; use a new directory")
    project = Path(spec.project_directory)
    if output.resolve().is_relative_to(project.resolve()):
        raise ValueError("Evidence output must be outside source project")
    doc = ProjectStore([project], output.parent / "unused-output")._open(str(project))
    native = spec.native_readbacks
    if native:
        if not spec.topology or not spec.gui_receipt:
            raise ValueError(
                "Native collection requires observed topology and exact-project GUI receipt"
            )
        gui = json.loads(read_file(Path(spec.gui_receipt)).decode("utf-8-sig"))
        if (
            gui.get("status") != "verified"
            or gui.get("project_identity_verified") is not True
            or Path(gui.get("target_project", "")).resolve() != doc.net_path
        ):
            raise ValueError("Exact-project GUI identity receipt required")
        if any(
            d.control_kind == "comware"
            and any((d.configuration, d.version_readback, d.interface_readback))
            for d in spec.devices
        ):
            raise ValueError("Native reads cannot be replaced by supplied Comware output files")
    elif spec.topology or spec.gui_receipt:
        raise ValueError("Topology/GUI receipt belongs to native_readbacks mode")
    devices = []
    rows = []
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".evidence-", dir=output.parent))

    def add(source, name, role):
        source = Path(source)
        data = read_file(source)
        destination = relative_file(temp, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        rows.append(
            {
                "path": name,
                "role": role,
                "sha256": digest(data),
                "original_source": str(source.resolve()),
            }
        )
        return name

    def add_bytes(data, name, role, original):
        path = relative_file(temp, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        rows.append(
            {"path": name, "role": role, "sha256": digest(data), "original_source": original}
        )
        return name

    try:
        add(doc.net_path, "project/" + doc.net_path.name, "template")
        add(project / "project.json", "project/project.json", "metadata")
        add(project / "deviceversion.txt", "project/deviceversion.txt", "device_version")
        add(Path(spec.installation) / "deviceInfo.ini", "sources/deviceInfo.ini", "port_inventory")
        for index, device in enumerate(spec.devices):
            section = doc.sections.get(device.name)
            if not section:
                raise ValueError("Requested device not in source project")
            row = {
                "model": section.model,
                "device_name": section.name,
                "device_id": int(section.fields["device_id"]),
                "control_kind": device.control_kind,
            }
            if native and device.control_kind == "comware":
                from mcp_hcl.cli_session import PersistentConsole
                from mcp_hcl.console import resolve_endpoint
                from mcp_hcl.deployment import config_text

                # The device pipe is read-exclusively by HCL's SimwareClient, which
                # republishes it as the telnet console HCL's own GUI uses.
                pipe = resolve_endpoint(spec.topology, row["device_id"])
                from mcp_hcl.console import ConsoleError

                channel = None
                try:
                    with PersistentConsole(pipe, output.parent / ".console-leases") as channel:
                        for role, command in [
                            ("configuration", "display current-configuration"),
                            ("version_readback", "display version"),
                            ("interface_readback", "display interface"),
                        ]:
                            reply = channel.exchange(command)
                            text = (
                                config_text(reply["output"])
                                if role == "configuration"
                                else reply["output"]
                            )
                            row[role] = add_bytes(
                                text.encode("utf-8"), f"sources/{index}-{role}.txt", role, pipe
                            )
                        row["native_transcript"] = add_bytes(
                            json.dumps(channel.history, indent=2).encode(),
                            f"sources/{index}-transcript.json",
                            "native_transcript",
                            pipe,
                        )
                except (ConsoleError, OSError) as exc:
                    history = channel.history if channel else []
                    add_bytes(
                        json.dumps(history, indent=2).encode(),
                        f"sources/{index}-failed-transcript.json",
                        "native_transcript",
                        pipe,
                    )
                    failure = {
                        "status": "partial",
                        "error": str(exc),
                        "device": section.name,
                        "origin_host": socket.gethostname(),
                        "receipt_directory": str(output),
                        "runtime_verified": False,
                        "full_live_gate_passed": False,
                    }
                    failure["errors"] = (
                        [exc.as_error()]
                        if isinstance(exc, ConsoleError)
                        else [
                            {
                                "code": "native_capture_failed",
                                "message": str(exc),
                                "detail": {
                                    "winerror": getattr(exc, "winerror", None),
                                    "errno": getattr(exc, "errno", None),
                                },
                            }
                        ]
                    )
                    (temp / "capture-failed.json").write_text(
                        json.dumps(failure, indent=2), encoding="utf-8"
                    )
                    temp.rename(output)
                    return failure
            for role in (
                "configuration",
                "version_readback",
                "interface_readback",
                "phone_before",
                "phone_after",
                "phone_page",
            ):
                source = getattr(device, role)
                if source:
                    row[role] = add(source, f"sources/{index}-{role}{Path(source).suffix}", role)
            devices.append(row)
        for role, values in [
            ("mapping_observation", spec.mapping_observations),
            ("acceptance_receipt", spec.acceptance_receipts),
        ]:
            for index, source in enumerate(values):
                add(source, f"sources/{role}-{index}.json", role)
        manifest = {
            "schema_version": 2,
            "origin": {
                "host": socket.gethostname(),
                "hcl_version": spec.hcl_version,
                "capture_method": "native_console" if native else "manual_saved_files",
            },
            "files": rows,
            "models": devices,
        }
        (temp / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        result = audit_evidence(temp)
        temp.rename(output)
        return {k: v for k, v in result.items() if k != "profiles"} | {
            "artifact_directory": str(output),
            "operation_id": uuid4().hex,
        }
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def import_evidence(source: Path, output_root: Path, name: str, key: str | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("Invalid evidence artifact name")
    from mcp_hcl.project_build import safe_filename

    safe_filename(name)
    if any(linked(p) for p in [output_root, *output_root.parents]):
        raise ValueError("Import root cannot contain links")
    audit = audit_evidence(source)
    if audit["origin"]["capture_method"] == "test_fixture":
        raise ValueError("Synthetic evidence cannot be imported as model profiles")
    if output_root.resolve().is_relative_to(source.resolve()):
        raise ValueError("Import root cannot be inside the evidence source")
    destination = output_root / name

    def action():
        if destination.exists():
            raise ValueError("Evidence artifact already exists")
        output_root.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix=".import-", dir=output_root))
        try:
            shutil.copytree(source, temp / "evidence")
            verified = audit_evidence(temp / "evidence")
            (temp / "import-report.json").write_text(
                json.dumps({k: v for k, v in verified.items() if k != "profiles"}, indent=2),
                encoding="utf-8",
            )
            temp.rename(destination)
            return {
                "status": "verified",
                "operation_id": uuid4().hex,
                "artifact_directory": str(destination),
                "verified_scope": verified["verified_scope"],
                "origin": verified["origin"],
                "runtime_verified": False,
                "full_live_gate_passed": False,
                "missing": verified["missing"],
            }
        finally:
            if temp.exists():
                shutil.rmtree(temp)

    return ArtifactRetries(output_root, [source]).run(
        "import_model_evidence",
        {"artifact_name": name, "idempotency_key": key, "source_hashes": audit["file_hashes"]},
        action,
        source=str(source),
    )
