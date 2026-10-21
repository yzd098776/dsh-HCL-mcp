"""Read-only binding of a staged device to a running HCL VM and pipe server."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import UUID

from mcp_hcl.console import ConsoleError, endpoint


def fail(message):
    raise ConsoleError(
        message, code="runtime_binding_unverified", detail={"stage": "runtime_identity"}
    )


def unlinked(path: Path):
    if any(
        item.is_symlink() or getattr(item, "is_junction", lambda: False)()
        for item in (path, *path.parents)
    ):
        fail("Runtime identity paths cannot contain links")


TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25
_INTEGRITY_NAMES = {0x0000: "untrusted", 0x1000: "low", 0x2000: "medium", 0x3000: "high"}

# VirtualBox writes a fixed comment line and then the versioned header at 0x40.
VDI_COMMENT_BYTES = 0x40
VDI_MAGIC = 0xBEDA107F
VDI_VERSION_1_1 = 0x00010001
VDI_HEADER_BYTES = 0x200
VDI_LINKAGE_OFFSET = 0x188

AF_INET = 2
TCP_TABLE_OWNER_PID_LISTENER = 3
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CONSOLE_BRIDGE_IMAGE = "simwareclient.exe"
CONSOLE_BRIDGE_BASE_PORT = 30000
_ATTACHED_IMAGE_FIELD = re.compile(r"^(?:IDE|SATA|SCSI|SAS|NVME)-ImageUUID-\d+-\d+$")
_BRIDGE_LOG = re.compile(
    r"create_telnet_server success strPipeName:(?P<pipe>[^,\r\n]+),\s*"
    r"telnet_port:(?P<port>\d+)"
)


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


class _TokenMandatoryLabel(ctypes.Structure):
    _fields_ = [("Label", _SidAndAttributes)]


def token_integrity() -> dict:
    """Read this process token's integrity level.

    ``VBoxManage`` needs to create a VirtualBox COM client, and a Low-integrity
    token is denied that with ``E_ACCESSDENIED`` before any VM fact is read. The
    usual cause on this host is an interpreter installed under a directory that
    carries a Low mandatory label, since the label is inherited by the files and
    then by the processes started from them.

    Returns ``{"level": str, "rid": int}`` or ``{"error": int}``; it never raises,
    because it only annotates an identity failure.
    """

    if os.name != "nt":
        return {"error": "not_windows"}
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32.OpenProcessToken.argtypes = [
            wt.HANDLE,
            wt.DWORD,
            ctypes.POINTER(wt.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wt.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wt.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wt.DWORD,
            ctypes.POINTER(wt.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wt.BOOL
        advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
        advapi32.GetLengthSid.restype = wt.DWORD
        advapi32.CopySid.argtypes = [wt.DWORD, ctypes.c_void_p, ctypes.c_void_p]
        advapi32.CopySid.restype = wt.BOOL
        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
        ):
            return {"error": ctypes.get_last_error()}
        try:
            size = wt.DWORD(0)
            advapi32.GetTokenInformation(token, TokenIntegrityLevel, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(max(size.value, 64))
            if not advapi32.GetTokenInformation(
                token, TokenIntegrityLevel, buffer, size.value, ctypes.byref(size)
            ):
                return {"error": ctypes.get_last_error()}
            label = ctypes.cast(buffer, ctypes.POINTER(_TokenMandatoryLabel)).contents
            length = advapi32.GetLengthSid(label.Label.Sid)
            copy = ctypes.create_string_buffer(length)
            if not advapi32.CopySid(length, copy, label.Label.Sid):
                return {"error": ctypes.get_last_error()}
            rid = int.from_bytes(bytes(copy)[8:12], "little")
            return {"level": _INTEGRITY_NAMES.get(rid & 0xF000, hex(rid)), "rid": rid}
        finally:
            kernel32.CloseHandle(token)
    except (OSError, AttributeError):
        return {"error": "unavailable"}


def _vdi_uuid_bytes(value: str) -> bytes:
    """Encode a UUID the way VirtualBox stores it in a VDI header.

    VirtualBox keeps the first three groups little-endian and the trailing eight
    bytes big-endian, which is neither ``uuid.UUID.bytes`` nor its little-endian
    form.
    """

    parsed = UUID(value)
    return (
        parsed.bytes[0:4][::-1]
        + parsed.bytes[4:6][::-1]
        + parsed.bytes[6:8][::-1]
        + parsed.bytes[8:]
    )


def vdi_disk_uuid(path: Path):
    """Return the VirtualBox disk UUID recorded in a VDI header, or ``None``.

    This is a plain file read, so it works from a token that cannot create the
    VirtualBox COM client. A staged copy and the medium a running VM mounts keep
    the same disk UUID even though their paths and their whole-file hashes differ,
    which is what makes it usable as a "same medium, other path" identity.
    """

    try:
        with path.open("rb") as handle:
            header = handle.read(VDI_HEADER_BYTES)
    except OSError:
        return None
    if len(header) < VDI_HEADER_BYTES or header[:4] != b"<<< ":
        return None
    fields = VDI_COMMENT_BYTES
    magic, version = (
        int.from_bytes(header[fields : fields + 4], "little"),
        int.from_bytes(header[fields + 4 : fields + 8], "little"),
    )
    if magic != VDI_MAGIC or version != VDI_VERSION_1_1:
        return None
    start = fields + VDI_LINKAGE_OFFSET - 0x40
    raw = header[start : start + 16]
    if len(raw) != 16 or raw == b"\x00" * 16:
        return None
    return str(UUID(bytes=raw[0:4][::-1] + raw[4:6][::-1] + raw[6:8][::-1] + raw[8:]))


class _TcpListenerRow(ctypes.Structure):
    _fields_ = [
        ("state", wt.DWORD),
        ("local_address", wt.DWORD),
        ("local_port", wt.DWORD),
        ("remote_address", wt.DWORD),
        ("remote_port", wt.DWORD),
        ("owning_pid", wt.DWORD),
    ]


def listener_pids(port: int):
    """Return the PIDs listening on a TCP port, without PowerShell or COM."""

    if os.name != "nt":
        return []
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        size = wt.DWORD(0)
        iphlpapi.GetExtendedTcpTable(
            None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0
        )
        buffer = ctypes.create_string_buffer(max(size.value, 4))
        if (
            iphlpapi.GetExtendedTcpTable(
                buffer,
                ctypes.byref(size),
                False,
                AF_INET,
                TCP_TABLE_OWNER_PID_LISTENER,
                0,
            )
            != 0
        ):
            return []
        count = wt.DWORD.from_buffer(buffer).value
        offset = ctypes.sizeof(wt.DWORD)
        rows = (_TcpListenerRow * count).from_buffer(buffer, offset)
    except (OSError, AttributeError, ValueError):
        return []
    found = []
    for row in rows:
        port_value = ((row.local_port & 0xFF) << 8) | ((row.local_port >> 8) & 0xFF)
        if port_value == port and row.owning_pid not in found:
            found.append(int(row.owning_pid))
    return found


def process_image(pid: int):
    """Return the executable path of a process, or ``None`` when unreadable.

    ``PROCESS_QUERY_LIMITED_INFORMATION`` is granted across integrity levels, so
    a Medium-integrity MCP server can name the High-integrity HCL bridge process
    even though it cannot read that process's security descriptor.
    """

    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel32.OpenProcess.restype = wt.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wt.HANDLE,
            wt.DWORD,
            wt.LPWSTR,
            ctypes.POINTER(wt.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            length = wt.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(length.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return None
            return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError):
        return None


def process_session(pid: int):
    """Return the Windows session a process belongs to, or ``None``."""

    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        session = wt.DWORD(0)
        if not kernel32.ProcessIdToSessionId(pid, ctypes.byref(session)):
            return None
        return int(session.value)
    except (OSError, AttributeError):
        return None


def current_session():
    return process_session(os.getpid())


def bridge_pipes(log_root: Path, port: int):
    """Return the pipe names HCL's log records as bridged onto a telnet port.

    HCL writes ``create_telnet_server success strPipeName:\\\\.\\pipe\\<name>,
    telnet_port:<port>`` once per device console. The mapping is what binds a
    telnet port back to a topology device, so it is read from HCL's own record
    rather than inferred from the port arithmetic alone.
    """

    found = []
    try:
        logs = sorted(log_root.glob("*.log*"))
    except OSError:
        return found
    for path in logs:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _BRIDGE_LOG.finditer(text):
            if int(match["port"]) == port:
                pipe = match["pipe"].strip()
                if pipe not in found:
                    found.append(pipe)
    return found


def machine_values(text):
    """Parse ``VBoxManage --machinereadable`` output.

    ``VBoxManage`` is not consistent about escaping. Fields it emits from
    ``Bstr`` values (``CfgFile``, ``SnapFldr``, ``LogFldr``, ``capturefilename``)
    are JSON-escaped, so ``"C:\\\\Users\\\\..."`` decodes to ``C:\\Users\\...``.
    Storage and UART fields (``"IDE-1-0"``, ``uartmode2``) keep raw single
    backslashes, which JSON rejects as an invalid escape. A JSON decode is
    therefore attempted first and plain quote-stripping is the fallback, so both
    conventions yield the on-disk string the caller compares.

    A duplicate key is refused because two observations of one field could not be
    attributed unambiguously.
    """

    result = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in result:
            fail("Duplicate machine observation field")
        result[_unwrap(key)] = _decode(value)
    return result


def _unwrap(field: str) -> str:
    """Strip one matched surrounding quote pair, leaving any other shape intact."""

    if len(field) >= 2 and field.startswith('"') and field.endswith('"'):
        return field[1:-1]
    return field


def _decode(field: str) -> str:
    """Decode one value, honouring JSON escaping when VBoxManage used it."""

    if not (len(field) >= 2 and field.startswith('"') and field.endswith('"')):
        return field
    try:
        decoded = json.loads(field)
    except ValueError:
        return field[1:-1]
    return decoded if isinstance(decoded, str) else field[1:-1]


def _observation_failure(result) -> str:
    """Explain a failed ``VBoxManage`` run without leaking its full transcript.

    ``E_ACCESSDENIED`` on ``VirtualBoxClientWrap`` means VBoxManage could not
    create the COM client at all, which happens when the calling process runs at
    Low integrity -- for example when the interpreter lives under a directory
    carrying a Low mandatory label. That is a permission boundary, not a wrong VM,
    so it must not be reported as an identity mismatch.
    """

    text = b""
    for stream in ("stdout", "stderr"):
        chunk = getattr(result, stream, b"") or b""
        text += chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8", "replace")
    lowered = text.lower()
    if b"e_accessdenied" in lowered or b"failed to create the virtualbox object" in lowered:
        integrity = token_integrity()
        level = integrity.get("level") or f"unknown ({integrity.get('error')})"
        return (
            "VBoxManage cannot reach VirtualBox from this process "
            f"(token integrity: {level}); a Low-integrity token cannot create the "
            "COM client, which happens when the interpreter is installed under a "
            "directory carrying a Low mandatory label"
        )
    return "Registered VM identity observation failed"


class _DescriptorTreeBuilder(ET.TreeBuilder):
    def doctype(self, *args):
        # Parser-level rejection also covers UTF-16 declarations, for which
        # an ASCII byte scan alone cannot enforce the no-DTD contract.
        fail("Unsafe VM descriptor declarations")


def _descriptor_tree(data):
    return ET.fromstring(data, parser=ET.XMLParser(target=_DescriptorTreeBuilder()))


def identity_descriptor(data):
    """Parse identity XML, tolerating only observed ETX in ignored HCL DMI metadata.

    VirtualBox accepts HCL's literal ETX board serial, which XML 1.0 rejects.
    The on-disk bytes and their identity hash are never changed. UUID/storage/
    UART fields and all other invalid controls retain strict parsing.
    """
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        fail("Unsafe VM descriptor declarations")
    try:
        return _descriptor_tree(data)
    except ET.ParseError:
        if b"\x03" not in data:
            raise
    # Never treat tag-looking text inside comments, CDATA or PIs as metadata.
    for block in re.finditer(rb"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>", data, re.S):
        if b"\x03" in block[0]:
            fail("Unsupported control bytes outside HCL board-serial metadata")
    key = b"VBoxInternal/Devices/pcbios/0/Config/DmiBoardSerial"
    pattern = rb'<ExtraDataItem\s+name="' + re.escape(key) + rb'"\s+value="[^"<>&]*"\s*/>'
    parsed_data = re.sub(pattern, lambda match: match[0].replace(b"\x03", b" "), data)
    if b"\x03" in parsed_data:
        fail("Unsupported control bytes outside HCL board-serial metadata")
    root = _descriptor_tree(parsed_data)
    parents = {child: parent for parent in root.iter() for child in parent}
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "ExtraDataItem" or item.get("name") != key.decode():
            continue
        parent = parents.get(item)
        grandparent = parents.get(parent)
        if (
            parent is None
            or grandparent is None
            or parent.tag.rsplit("}", 1)[-1] != "ExtraData"
            or grandparent.tag.rsplit("}", 1)[-1] != "Machine"
            or set(item.attrib) != {"name", "value"}
        ):
            fail("HCL board-serial metadata is outside its expected view")
    return root


class RuntimeBinding:
    def __init__(self, *, runner=subprocess.run, log_root: Path | None = None):
        self.runner = runner
        self.log_root = log_root

    def facts(self, query):
        query = (
            "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false);$ErrorActionPreference='Stop';"
            + query
        )
        try:
            result = self.runner(
                ["powershell.exe", "-NoProfile", "-Command", query], capture_output=True, timeout=12
            )
            if result.returncode:
                fail("Windows runtime observation failed")
            return json.loads(result.stdout.decode("utf-8-sig"))
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ConsoleError(
                "Windows runtime identity is unknown",
                code="runtime_binding_unverified",
                detail={"stage": "runtime_identity"},
            ) from exc

    def resolve(self, document, binding, topology):
        if os.name != "nt":
            fail("Native runtime binding requires Windows")
        device_id = binding["device_id"]
        pipe = endpoint(topology, device_id)
        facts = self.facts(
            "$v=Get-ItemProperty 'HKLM:\\SOFTWARE\\Oracle\\VirtualBox';@{profile=[Environment]::GetFolderPath('UserProfile');sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;vbox=$v.InstallDir}|ConvertTo-Json -Compress"
        )
        if (
            not isinstance(facts, dict)
            or not re.fullmatch(r"S-\d+(?:-\d+)+", str(facts.get("sid", "")))
            or not isinstance(facts.get("profile"), str)
            or not isinstance(facts.get("vbox"), str)
        ):
            fail("Current Windows principal was not observed")
        profile = Path(facts["profile"])
        if not profile.is_absolute():
            fail("Windows profile is not absolute")
        name = f"{topology}-device{device_id}"
        config = profile / "HCL VMs" / topology / f"device{device_id}" / (name + ".vbox")
        unlinked(config)
        if not config.is_file() or config.stat().st_size > 1024 * 1024:
            fail("The expected current-user VM descriptor is missing or oversized")
        data = config.read_bytes()
        try:
            root = identity_descriptor(data)
            machines = [item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "Machine"]
            if len(machines) != 1:
                fail("VM descriptor has no unique machine")
            machine = machines[0]
            uuid = str(UUID(machine.attrib["uuid"].strip("{}")))
        except (ET.ParseError, KeyError, ValueError) as exc:
            raise ConsoleError(
                "VM descriptor identity is unreadable", code="runtime_binding_unverified"
            ) from exc
        if machine.get("name") != name:
            fail("VM descriptor name does not match staged device")
        ports = [
            item
            for item in machine.iter()
            if item.tag.rsplit("}", 1)[-1] == "Port" and item.get("hostMode") == "HostPipe"
        ]
        if (
            len(ports) != 1
            or ports[0].get("path", "").casefold() != pipe.casefold()
            or ports[0].get("server") != "true"
        ):
            fail("VM UART server does not match the requested HCL pipe")
        expected_disk = document.directory / "ConfigDisk" / f"device{device_id}.vdi"
        unlinked(expected_disk)
        if not expected_disk.is_file():
            fail("The staged project has no configuration disk for this device")
        disks = [
            disk
            for disk in machine.iter()
            if disk.tag.rsplit("}", 1)[-1] == "HardDisk" and disk.get("location")
        ]
        config_disk_uuids = set()
        for disk in disks:
            location = Path(disk.get("location"))
            if (location if location.is_absolute() else config.parent / location).resolve() == (
                expected_disk.resolve()
            ):
                config_disk_uuids.add(disk.get("uuid", "").strip("{}").lower())
        binding_mode = "path"
        staged_uuid = None
        if not config_disk_uuids:
            # A staged copy cannot share the running VM's path, but VirtualBox
            # records the medium UUID in the VDI header and keeps it on a copy, so
            # an equal UUID proves the copy and the mounted medium are one disk.
            staged_uuid = vdi_disk_uuid(expected_disk)
            if staged_uuid is None:
                fail("The staged configuration disk is not a readable VDI")
            config_disk_uuids = {
                disk.get("uuid", "").strip("{}").lower()
                for disk in disks
                if disk.get("uuid", "").strip("{}").lower() == staged_uuid
            }
            binding_mode = "disk_uuid"
        controllers = [
            item for item in machine if item.tag.rsplit("}", 1)[-1] == "StorageControllers"
        ]
        attached = {
            item.get("uuid", "").strip("{}").lower()
            for controller in controllers
            for item in controller.iter()
            if item.tag.rsplit("}", 1)[-1] == "Image"
        }
        if len(config_disk_uuids) != 1 or not config_disk_uuids.issubset(attached):
            fail("Running VM has no configuration disk bound to this staged project")
        configuration_disk_uuid = next(iter(config_disk_uuids))
        executable = Path(facts["vbox"]) / "VBoxManage.exe"
        try:
            observed = self.runner(
                [str(executable), "showvminfo", name, "--machinereadable"],
                capture_output=True,
                timeout=12,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConsoleError(
                "Registered VM observation is unknown", code="runtime_binding_unverified"
            ) from exc
        if observed.returncode:
            fail(_observation_failure(observed))
        values = machine_values(observed.stdout.decode("utf-8-sig"))
        if (
            values.get("name") != name
            or values.get("UUID") != uuid
            or values.get("VMState") != "running"
            or Path(values.get("CfgFile", "")).resolve() != config.resolve()
        ):
            fail("Registered running VM does not match its current-user descriptor")
        if not any(
            isinstance(value, str)
            and Path(value).is_absolute()
            and Path(value).resolve() == expected_disk.resolve()
            for value in values.values()
        ) and not any(
            _ATTACHED_IMAGE_FIELD.match(field)
            and str(value).strip("{}").lower() == configuration_disk_uuid
            for field, value in values.items()
        ):
            fail("Registered VM storage does not mount the staged configuration disk")
        return {
            "pipe": pipe,
            "topology": topology,
            "device_id": device_id,
            "vm_name": name,
            "vm_uuid": uuid,
            "owner_sid": facts["sid"],
            "vm_descriptor": str(config),
            "descriptor_sha256": hashlib.sha256(data).hexdigest(),
            "project_file": str(document.net_path),
            "project_sha256": hashlib.sha256(document.net_path.read_bytes()).hexdigest(),
            "configuration_disk": str(expected_disk),
            "configuration_disk_uuid": configuration_disk_uuid,
            "configuration_disk_binding": binding_mode,
            "verified_scope": "running_vm_descriptor_and_staged_config_disk_only",
            "runtime_isolation_verified": False,
        }

    def verify_pipe(self, channel, target):
        """Verify the connected console really serves the resolved HCL VM.

        The two transports prove this differently. A named pipe belongs to a
        VirtualBox process, so the pipe's server PID, its principal and its
        ``--startvm`` argument identify the VM. HCL's telnet bridge is served by
        ``SimwareClient.exe`` instead, which is not the VM process at all, so the
        telnet branch proves the port belongs to this device's pipe and that the
        listener runs in this session.
        """

        if getattr(channel, "transport", None) == "telnet":
            return self._verify_telnet_bridge(channel, target)
        return self._verify_pipe_server(channel, target)

    def _verify_pipe_server(self, channel, target):
        """Verify the actual pipe handle's server PID, principal and VM start identity."""
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetNamedPipeServerProcessId.argtypes = [wt.HANDLE, ctypes.POINTER(wt.ULONG)]
        kernel.GetNamedPipeServerProcessId.restype = wt.BOOL
        pid = wt.ULONG()
        handle = msvcrt.get_osfhandle(channel.session._read_fd)
        if not kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
            fail("Console server PID is unknown")
        observed = self.facts(
            "$p=Get-CimInstance Win32_Process -Filter 'ProcessId="
            + str(pid.value)
            + "';if(-not $p){throw 'Console process missing'};$s=Invoke-CimMethod -InputObject $p -MethodName GetOwnerSid;@{name=$p.Name;command=$p.CommandLine;sid=$s.Sid;owner_result=$s.ReturnValue}|ConvertTo-Json -Compress"
        )
        if (
            not isinstance(observed, dict)
            or observed.get("name")
            not in {"VBoxHeadless.exe", "VirtualBoxVM.exe", "VirtualBox.exe"}
            or type(observed.get("owner_result")) is not int
            or observed.get("owner_result") != 0
            or observed.get("sid") != target["owner_sid"]
        ):
            fail("Console server is not the expected current-user VirtualBox process")
        command = observed.get("command")
        if not isinstance(command, str) or not re.search(
            r'--startvm\s+"?' + re.escape(target["vm_uuid"]) + r'"?(?:\s|$)', command, re.I
        ):
            fail("Console server command line does not bind the expected VM UUID")
        return {
            **target,
            "console_server_pid": pid.value,
            "console_transport": "named_pipe",
            "verified_scope": "running_vm_staged_config_disk_and_native_pipe_server_identity_only",
        }

    def _verify_telnet_bridge(self, channel, target):
        """Verify HCL's telnet bridge for a device console without COM or CIM.

        HCL republishes each device pipe as ``telnet 30000 + device`` from
        ``SimwareClient.exe``. That process runs at a higher integrity level than
        this server, so its security descriptor is unreadable and its command
        line is empty; the checks below use only calls that cross integrity
        levels (TCP table, ``PROCESS_QUERY_LIMITED_INFORMATION``, session id).
        """

        port = CONSOLE_BRIDGE_BASE_PORT + target["device_id"]
        pids = listener_pids(port)
        if len(pids) != 1:
            fail("Telnet console port does not have exactly one listener")
        pid = pids[0]
        image = process_image(pid)
        if image is None or Path(image).name.casefold() != CONSOLE_BRIDGE_IMAGE:
            fail("Telnet console is not served by HCL's console bridge")
        if process_session(pid) != current_session():
            fail("Telnet console bridge does not run in the current Windows session")
        expected_pipe = target["pipe"].casefold()
        bridged = [item.casefold() for item in bridge_pipes(self.bridge_log_root(), port)]
        if expected_pipe not in bridged:
            fail("HCL has no recorded telnet bridge from this device pipe to that port")
        return {
            **target,
            "console_server_pid": pid,
            "console_transport": "telnet",
            "telnet_port": port,
            "verified_scope": ("running_vm_staged_config_disk_and_hcl_telnet_bridge_identity_only"),
        }

    def bridge_log_root(self) -> Path:
        """Return the HCL log directory the bridge record is read from."""

        if self.log_root is not None:
            return self.log_root
        return Path(os.environ.get("MCP_HCL_INSTALL_ROOT", "D:/HCL")) / "Log" / "HCLLog"
