"""Request-correlated console access to one HCL device.

HCL exposes each device console twice, and only one of the two is usable by a
third party:

* a **Windows named pipe** ``\\\\.\\pipe\\<topology>-device<N>``. The guest side
  only ever *writes* into it and HCL's own bridge keeps the sole read handle, so
  ``CreateFileW`` against it returns ``ERROR_PIPE_BUSY`` for a read request and
  ``ERROR_ACCESS_DENIED`` for a read/write request (measured, not inferred).
* a **telnet bridge** on ``127.0.0.1:(30000 + N)``. ``HCL.log`` records it
  directly: ``create_telnet_server success strPipeName:\\\\.\\pipe\\topo1-device1,
  telnet_port:30001``. Every one of the 240 bridge creations in the local logs
  follows ``device<N> -> 30000 + N``. This is the channel HCL's own console
  window and the shipped PuTTY shortcut actually drive.

So the CLI channel is a telnet byte stream: no window automation and no
keystroke synthesis are involved. The pipe transport is kept because it is the
honest lower bound — it fails with a *reported* reason instead of silently
pretending to work.

Design rules enforced here:

* only endpoints matching an observed HCL shape are accepted (a pipe name from
  the pipe pattern, or a loopback telnet port in ``30001..31024``), so this can
  never be pointed at an arbitrary host service;
* sockets are non-blocking and polled, so there is no reader thread whose
  ``close`` could deadlock;
* output that arrived *before* the command is drained and reported, so stale
  console text can never be mistaken for this request's answer. This matters
  more over telnet: HCL multiplexes every client onto the one console, so a
  second reader sees this session's traffic and vice versa;
* writes are refused unless the process opted in with ``MCP_HCL_ALLOW_LIVE_WRITE=1``;
* a successful read proves the **console channel** works, never that the guest
  booted, that a command was accepted, or that the network is reachable.
"""

from __future__ import annotations

import errno
import hashlib
import math
import os
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

#: ``\\.\pipe\<topology>-device<N>`` exactly as observed in HCL 5.10.3.
PIPE_PATTERN = re.compile(
    r"^\\\\\.\\pipe\\(?P<topology>[A-Za-z0-9_.-]{1,64})-device(?P<device>[0-9]{1,4})$"
)

#: ``telnet://127.0.0.1:<30000 + N>`` as recorded by HCL's own bridge log.
TELNET_PATTERN = re.compile(r"^telnet://(?P<host>127\.0\.0\.1|localhost):(?P<port>[0-9]{4,5})$")
TELNET_BASE_PORT = 30000
TELNET_HOST = "127.0.0.1"

#: Telnet protocol bytes (RFC 854). HCL's bridge opens with WILL ECHO,
#: WILL SUPPRESS-GO-AHEAD and DO BINARY.
IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240

MAX_OUTPUT_BYTES = 256 * 1024
_POLL_SECONDS = 0.02

#: A Comware prompt at the end of the stream: ``<H3C>`` or ``[H3C-lan1]``.
_PROMPT_HINT = re.compile(r"(?:<[^<>\r\n]{1,128}>|\[[^\[\]\r\n]{1,160}\])\s*$")


class ConsoleError(RuntimeError):
    """The console channel cannot be used safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "console_unavailable",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = {"reason": code, **(detail or {})}

    def as_error(self, *, code: str | None = None) -> dict[str, Any]:
        return {"code": code or self.code, "message": str(self), "detail": dict(self.detail)}


def _os_error(exc: OSError, *, stage: str, api: str) -> ConsoleError:
    """Keep the native error, rather than interpreting a CRT exception name."""
    native = getattr(exc, "winerror", None)
    reasons = {
        2: "pipe_not_found",
        3: "pipe_not_found",
        5: "pipe_access_denied",
        32: "pipe_sharing_violation",
        109: "pipe_broken",
        231: "pipe_busy",
        232: "pipe_closing",
        233: "pipe_not_connected",
        121: "pipe_timeout",
    }
    reason = reasons.get(native, "console_io_failed")
    if native is None:
        if exc.errno == errno.ENOENT:
            reason = "pipe_not_found"
        elif exc.errno in {errno.EACCES, errno.EPERM}:
            reason = "pipe_access_denied"
        elif exc.errno == errno.EPIPE:
            reason = "pipe_broken"
    return ConsoleError(
        f"Console {stage} failed: {exc.__class__.__name__}",
        code=reason,
        detail={
            "stage": stage,
            "api": api,
            "winerror": native,
            "errno": exc.errno,
            "access_mode": "read_write",
        },
    )


def _require_single_pipe_instance(kernel, handle) -> None:
    """Prove endpoint exclusivity on the connected client handle, before I/O.

    CreateFile uses share mode zero. A maximum of one byte-mode instance
    additionally rules out another client connected to a different instance
    of the same name. This does not establish the VM or server principal;
    staged transactions still require RuntimeBinding.verify_pipe.
    """
    import ctypes
    import ctypes.wintypes as wt

    flags, maximum = wt.DWORD(), wt.DWORD()
    try:
        query = kernel.GetNamedPipeInfo
        query.argtypes = [
            wt.HANDLE,
            ctypes.POINTER(wt.DWORD),
            ctypes.POINTER(wt.DWORD),
            ctypes.POINTER(wt.DWORD),
            ctypes.POINTER(wt.DWORD),
        ]
        query.restype = wt.BOOL
        if not query(handle, ctypes.byref(flags), None, None, ctypes.byref(maximum)):
            raise ConsoleError(
                "Connected pipe exclusivity could not be observed",
                code="console_exclusivity_unverified",
                detail={
                    "stage": "ownership",
                    "api": "GetNamedPipeInfo",
                    "winerror": ctypes.get_last_error(),
                    "command_outcome": "not_sent",
                },
            )
    except (AttributeError, OSError, TypeError) as exc:
        raise ConsoleError(
            "Connected pipe exclusivity observation is unavailable",
            code="console_exclusivity_unverified",
            detail={"stage": "ownership", "api": "GetNamedPipeInfo", "command_outcome": "not_sent"},
        ) from exc
    if flags.value != 0 or maximum.value != 1:
        raise ConsoleError(
            "A single byte-mode client instance is required before console input",
            code="console_exclusivity_unverified",
            detail={
                "stage": "ownership",
                "api": "GetNamedPipeInfo",
                "pipe_flags": flags.value,
                "maximum_instances": maximum.value,
                "command_outcome": "not_sent",
            },
        )


def _open_pipe_descriptor(pipe_name: str) -> int:
    if os.name != "nt":
        return os.open(pipe_name, os.O_RDWR | os.O_BINARY)
    # CRT os.open can collapse PIPE_BUSY into PermissionError. Capture the
    # Win32 result before transferring ownership to a Python descriptor.
    import ctypes
    import ctypes.wintypes as wt
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wt.LPCWSTR,
        wt.DWORD,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        wt.DWORD,
        wt.HANDLE,
    ]
    kernel.CreateFileW.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.CloseHandle.restype = wt.BOOL
    handle = kernel.CreateFileW(pipe_name, 0xC0000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        native_error = ctypes.get_last_error()
        raise ctypes.WinError(native_error)
    try:
        _require_single_pipe_instance(kernel, handle)
        return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except ConsoleError:
        kernel.CloseHandle(handle)
        raise
    except OSError as exc:
        kernel.CloseHandle(handle)
        raise _os_error(exc, stage="descriptor", api="open_osfhandle") from exc


def _validate_device_parts(topology: str, device: int) -> None:
    if not isinstance(topology, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", topology):
        raise ConsoleError("topology must match [A-Za-z0-9_.-]{1,64}")
    if isinstance(device, bool) or not isinstance(device, int) or not 1 <= device <= 1024:
        raise ConsoleError("device must be an integer between 1 and 1024")


def endpoint(topology: str, device: int) -> str:
    """Build the observed pipe name for one device, validating both parts."""
    _validate_device_parts(topology, device)
    return f"\\\\.\\pipe\\{topology}-device{device}"


def telnet_endpoint(topology: str, device: int) -> str:
    """Build the telnet endpoint HCL bridges that pipe onto (``30000 + N``)."""
    _validate_device_parts(topology, device)
    return f"telnet://{TELNET_HOST}:{TELNET_BASE_PORT + device}"


def resolve_endpoint(topology: str, device: int) -> str:
    """Default device endpoint: the telnet bridge, the channel that works."""
    return telnet_endpoint(topology, device)


def describe(pipe_name: str) -> dict[str, Any]:
    """Split a console endpoint into its topology and device, or refuse it."""
    text = pipe_name or ""
    match = PIPE_PATTERN.fullmatch(text)
    if match is not None:
        return {
            "pipe": text,
            "endpoint": text,
            "transport": "named_pipe",
            "topology": match.group("topology"),
            "device": int(match.group("device")),
        }
    telnet = TELNET_PATTERN.fullmatch(text)
    if telnet is not None:
        port = int(telnet.group("port"))
        device = port - TELNET_BASE_PORT
        if not 1 <= device <= 1024:
            raise ConsoleError(
                "Telnet console ports must lie in 30001..31024 (HCL assigns 30000 + device number)"
            )
        return {
            "pipe": text,
            "endpoint": text,
            "transport": "telnet",
            "host": telnet.group("host"),
            "port": port,
            "topology": None,
            "device": device,
        }
    raise ConsoleError(
        "Only HCL console endpoints are accepted: \\\\.\\pipe\\<topology>-device<N> "
        "or telnet://127.0.0.1:<30001..31024>"
    )


def _validate_command(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ConsoleError("command must be a non-empty string")
    if any(character in command for character in "\r\n\x00"):
        raise ConsoleError("command must not contain CR, LF or NUL")
    if len(command) > 512:
        raise ConsoleError("command is longer than 512 characters")
    if not command.isascii():
        raise ConsoleError("Console commands require verified ASCII encoding")
    return command.strip()


class _PolledSession:
    """Non-blocking, polled console byte stream shared by both transports.

    Subclasses supply :meth:`_read_available` and :meth:`send_raw`. Polling is
    used instead of a reader thread so ``close`` can never deadlock.
    """

    def __init__(
        self,
        *,
        max_bytes: int = MAX_OUTPUT_BYTES,
        write_timeout: float = 4.0,
    ) -> None:
        self._max_bytes = max_bytes
        self._closed = False
        self.write_timeout = write_timeout
        self.partial_output = b""
        self.read_termination: str | None = None
        self.last_write_bytes = 0
        self.wakeup_output = b""

    # ------------------------------------------------------------------ io
    def _read_available(self) -> bytes:
        raise NotImplementedError

    def send_raw(self, payload: bytes) -> None:
        raise NotImplementedError

    def drain(self) -> bytes:
        """Discard and return everything already buffered (the 'stale output')."""
        collected = bytearray()
        while len(collected) < self._max_bytes:
            chunk = self._read_available()
            if not chunk:
                break
            collected.extend(chunk)
        if len(collected) >= self._max_bytes:
            raise ConsoleError(
                "Stale stream reached the evidence limit; no command was sent",
                code="stale_output_limit",
                detail={"stage": "drain", "bytes": len(collected)},
            )
        return bytes(collected)

    def read_until_idle(self, *, idle_gap: float, timeout: float) -> bytes:
        """Collect output until it stops arriving for ``idle_gap`` seconds."""
        collected = bytearray()
        self.partial_output = b""
        self.read_termination = None
        deadline = time.monotonic() + timeout
        last_data = time.monotonic()
        while True:
            if time.monotonic() >= deadline:
                self.read_termination = "timeout"
                break
            chunk = self._read_available()
            if chunk:
                collected.extend(chunk)
                if len(collected) > self._max_bytes:
                    self.partial_output = bytes(collected[: self._max_bytes])
                    raise ConsoleError(
                        "Console output exceeds the evidence limit",
                        code="output_limit",
                        detail={
                            "stage": "read",
                            "truncated": True,
                            "observed_bytes": len(collected),
                        },
                    )
                self.partial_output = bytes(collected)
                last_data = time.monotonic()
                continue
            now = time.monotonic()
            if now >= deadline:
                self.read_termination = "timeout"
                break
            if collected and (now - last_data) >= idle_gap:
                self.read_termination = "idle"
                break
            time.sleep(min(_POLL_SECONDS, max(deadline - now, 0.001)))
        return bytes(collected)

    def send(self, command: str) -> None:
        # A bare CR is the line ending HCL's console bridge expects: CRLF makes
        # Comware answer with an extra empty prompt, which then lands inside the
        # next readback body (for example after the last line of a VLAN block).
        payload = (_validate_command(command) + "\r").encode("ascii", errors="strict")
        self.send_raw(payload)

    def close(self) -> None:
        raise NotImplementedError


class ConsoleSession(_PolledSession):
    """Line-oriented session over pipe file descriptors.

    Descriptors are injected so the protocol logic can be tested with anonymous
    pipes; :func:`open_console_pipe` is the only OS-specific part.
    """

    def __init__(
        self,
        read_fd: int,
        write_fd: int | None = None,
        *,
        max_bytes: int = MAX_OUTPUT_BYTES,
        write_timeout: float = 4.0,
    ) -> None:
        super().__init__(max_bytes=max_bytes, write_timeout=write_timeout)
        self._read_fd = read_fd
        self._write_fd = read_fd if write_fd is None else write_fd

    def _read_available(self) -> bytes:
        try:
            return os.read(self._read_fd, 65536)
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise _os_error(exc, stage="read", api="os.read") from exc

    def send_raw(self, payload: bytes) -> None:
        deadline = time.monotonic() + self.write_timeout
        offset = 0
        self.last_write_bytes = 0
        while offset < len(payload):
            if time.monotonic() >= deadline:
                raise ConsoleError(
                    "Console partial write timed out",
                    code="write_timeout",
                    detail={
                        "stage": "write",
                        "bytes_written": offset,
                        "command_outcome": "unknown" if offset else "not_sent",
                    },
                )
            try:
                sent = os.write(self._write_fd, payload[offset:])
                if sent == 0:
                    raise ConsoleError(
                        "Console channel closed during write",
                        code="pipe_broken",
                        detail={
                            "stage": "write",
                            "bytes_written": offset,
                            "command_outcome": "unknown" if offset else "not_sent",
                        },
                    )
                offset += sent
                self.last_write_bytes = offset
            except BlockingIOError:
                time.sleep(_POLL_SECONDS)
            except OSError as exc:
                failure = _os_error(exc, stage="write", api="os.write")
                failure.detail.update(
                    bytes_written=offset, command_outcome="unknown" if offset else "not_sent"
                )
                raise failure from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in {self._read_fd, self._write_fd}:
            try:
                os.close(descriptor)
            except OSError:
                pass


class TelnetSession(_PolledSession):
    """Console session over HCL's telnet bridge (``30000 + device``).

    Telnet negotiation is answered inline: HCL's bridge announces WILL ECHO,
    WILL SUPPRESS-GO-AHEAD and DO BINARY, and a client that ignores the
    negotiation still receives data but cannot rely on the byte stream being
    unescaped. Every ``IAC`` in device output is doubled by the bridge, so it is
    un-doubled here before the text is handed on.
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        max_bytes: int = MAX_OUTPUT_BYTES,
        write_timeout: float = 4.0,
    ) -> None:
        super().__init__(max_bytes=max_bytes, write_timeout=write_timeout)
        self._sock = sock
        self._pending = bytearray()

    # ------------------------------------------------------------ transport
    def _read_raw(self) -> bytes:
        try:
            return self._sock.recv(65536)
        except (BlockingIOError, socket.timeout):
            return b""
        except OSError as exc:
            raise ConsoleError(
                f"Console read failed: {exc.__class__.__name__}",
                code="console_io_failed",
                detail={
                    "stage": "read",
                    "api": "socket.recv",
                    "winerror": getattr(exc, "winerror", None),
                    "errno": getattr(exc, "errno", None),
                },
            ) from exc

    def _negotiate(self, data: bytes) -> bytes:
        """Strip telnet commands, answering options, and un-double IAC."""
        payload = bytearray()
        replies = bytearray()
        index = 0
        size = len(data)
        while index < size:
            byte = data[index]
            if byte != IAC:
                payload.append(byte)
                index += 1
                continue
            if index + 1 >= size:
                break
            command = data[index + 1]
            if command == IAC:
                payload.append(IAC)
                index += 2
                continue
            if command in (DO, DONT, WILL, WONT):
                if index + 2 >= size:
                    break
                option = data[index + 2]
                if command == DO:
                    replies += bytes([IAC, WONT, option])
                elif command == WILL:
                    replies += bytes([IAC, DO, option])
                else:
                    replies += bytes([IAC, WONT, option])
                index += 3
                continue
            if command == SB:
                end = data.find(bytes([IAC, SE]), index)
                index = size if end < 0 else end + 2
                continue
            index += 2
        if replies:
            try:
                # The socket is non-blocking, so a negotiation reply needs the
                # same polled write path as a command rather than sendall.
                self._send_polled(bytes(replies))
            except ConsoleError:
                # A failed negotiation reply never invalidates the read.
                pass
        return bytes(payload)

    def _send_polled(self, payload: bytes, *, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        offset = 0
        while offset < len(payload):
            if time.monotonic() >= deadline:
                raise ConsoleError(
                    "Telnet negotiation write timed out",
                    code="write_timeout",
                    detail={"stage": "negotiate", "bytes_written": offset},
                )
            try:
                sent = self._sock.send(payload[offset:])
            except (BlockingIOError, socket.timeout):
                time.sleep(_POLL_SECONDS)
                continue
            if sent == 0:
                raise ConsoleError(
                    "Console channel closed during negotiation",
                    code="pipe_broken",
                    detail={"stage": "negotiate", "bytes_written": offset},
                )
            offset += sent

    def _read_available(self) -> bytes:
        return self._negotiate(self._read_raw())

    def send_raw(self, payload: bytes) -> None:
        # Escape IAC so a command byte can never be read as a telnet command.
        escaped = payload.replace(bytes([IAC]), bytes([IAC, IAC]))
        deadline = time.monotonic() + self.write_timeout
        offset = 0
        self.last_write_bytes = 0
        while offset < len(escaped):
            if time.monotonic() >= deadline:
                raise ConsoleError(
                    "Console partial write timed out",
                    code="write_timeout",
                    detail={
                        "stage": "write",
                        "bytes_written": offset,
                        "command_outcome": "unknown" if offset else "not_sent",
                    },
                )
            try:
                sent = self._sock.send(escaped[offset:])
                if sent == 0:
                    raise ConsoleError(
                        "Console channel closed during write",
                        code="pipe_broken",
                        detail={
                            "stage": "write",
                            "bytes_written": offset,
                            "command_outcome": "unknown" if offset else "not_sent",
                        },
                    )
                offset += sent
                self.last_write_bytes = offset
            except (BlockingIOError, socket.timeout):
                time.sleep(_POLL_SECONDS)
            except OSError as exc:
                failure = ConsoleError(
                    f"Console write failed: {exc.__class__.__name__}",
                    code="console_io_failed",
                    detail={
                        "stage": "write",
                        "api": "socket.send",
                        "winerror": getattr(exc, "winerror", None),
                        "errno": getattr(exc, "errno", None),
                        "bytes_written": offset,
                        "command_outcome": "unknown" if offset else "not_sent",
                    },
                )
                raise failure from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass


def open_telnet(host: str, port: int, *, timeout: float = 2.0) -> TelnetSession:
    """Connect to one HCL telnet bridge and wake the guest console.

    The guest parks at ``Press ENTER to get started.`` until it receives a line
    ending, so a bare newline is sent once. That newline is the only byte this
    function writes; it cannot change device configuration.
    """
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 0:
        raise ConsoleError("Open timeout must be finite and nonnegative", code="invalid_timeout")
    try:
        sock = socket.create_connection((host, port), timeout=max(timeout, 0.5))
    except OSError as exc:
        raise ConsoleError(
            f"Console connect failed: {exc.__class__.__name__}",
            code="console_unreachable",
            detail={
                "stage": "open",
                "api": "socket.create_connection",
                "host": host,
                "port": port,
                "winerror": getattr(exc, "winerror", None),
                "errno": getattr(exc, "errno", None),
            },
        ) from exc
    sock.setblocking(False)
    session = TelnetSession(sock)
    try:
        # Answer the greeting so the bridge's option state is settled.
        deadline = time.monotonic() + min(timeout, 2.0)
        while time.monotonic() < deadline:
            if session._read_available():
                break
            time.sleep(_POLL_SECONDS)
        # A freshly booted guest parks at "Press ENTER to get started." and only
        # prints its prompt after a line ending arrives. Wait for that prompt
        # here, so the caller's first command is not attributed to the wake-up
        # banner. The newline is the only byte written and cannot configure
        # anything; each retry is bounded and the loop stops at ``timeout``.
        wakeup = bytearray()
        settle_deadline = time.monotonic() + max(timeout, 0.5)
        for _ in range(3):
            session.send_raw(b"\r\n")
            wakeup.extend(
                session.read_until_idle(
                    idle_gap=0.3, timeout=max(settle_deadline - time.monotonic(), 0.1)
                )
            )
            if _PROMPT_HINT.search(_decode(bytes(wakeup))[0].replace("\r", "")):
                break
            if time.monotonic() >= settle_deadline:
                break
        session.wakeup_output = bytes(wakeup)
    except ConsoleError:
        session.close()
        raise
    return session


def open_console_pipe(pipe_name: str, *, timeout: float = 2.0) -> int:
    """Open one HCL pipe; wait only for absent/busy endpoints within a deadline."""
    describe(pipe_name)  # refuse anything that is not an HCL console pipe
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 0:
        raise ConsoleError("Open timeout must be finite and nonnegative", code="invalid_timeout")
    deadline = time.monotonic() + timeout
    attempts = 0
    while True:
        attempts += 1
        try:
            descriptor = _open_pipe_descriptor(pipe_name)
        except OSError as exc:
            failure = _os_error(
                exc, stage="open", api="CreateFileW" if os.name == "nt" else "os.open"
            )
            failure.detail.update(attempts=attempts)
            remaining = deadline - time.monotonic()
            if failure.code not in {"pipe_not_found", "pipe_busy"} or remaining <= 0:
                failure.detail["wait_expired"] = remaining <= 0
                raise failure from exc
            time.sleep(min(0.05, remaining))
            continue
        try:
            os.set_blocking(descriptor, False)
        except (OSError, ValueError) as exc:
            os.close(descriptor)
            raise ConsoleError(
                f"Cannot make the console pipe non-blocking: {exc.__class__.__name__}",
                code="nonblocking_failed",
                detail={
                    "stage": "configure",
                    "winerror": getattr(exc, "winerror", None),
                    "errno": getattr(exc, "errno", None),
                },
            ) from exc
        return descriptor


def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8/replace"


@dataclass(frozen=True)
class HclConsole:
    """Bounded, request-correlated console client."""

    open_timeout: float = 2.0
    idle_gap: float = 0.4
    read_timeout: float = 8.0
    settle_seconds: float = 0.25
    writes_enabled: bool = False
    lease_root: Path | None = None
    native_lease_root: Path | None = None

    def run(
        self,
        pipe_name: str,
        command: str,
        *,
        expect: str | None = None,
        topology_hint: str | None = None,
    ) -> dict[str, Any]:
        """Send one command and return only the output that followed it.

        A telnet endpoint carries no topology name, so a caller that resolved it
        from a project passes ``topology_hint``; the result marks it as declared
        rather than observed.
        """
        operation_id = uuid4().hex
        result: dict[str, Any] = {
            "status": "rejected",
            "operation": "console_exec",
            "operation_id": operation_id,
            "endpoint": pipe_name,
            "command": command,
            "target_project": None,
            "evidence": [],
            # A console read proves the channel, never the guest's state.
            "runtime_verified": False,
            "guest_boot_verified": False,
            "configuration_verified": False,
            "connectivity_verified": False,
            "full_live_gate_passed": False,
        }
        if not self.writes_enabled:
            result["errors"] = [
                {
                    "code": "writes_disabled",
                    "message": "Sending console input changes device state. Start the server with "
                    "MCP_HCL_ALLOW_LIVE_WRITE=1 after an explicit decision.",
                }
            ]
            return result
        try:
            location = describe(pipe_name)
            command = _validate_command(command)
        except ConsoleError as exc:
            result["errors"] = [{"code": "invalid_request", "message": str(exc)}]
            return result

        started = time.monotonic()
        session: ConsoleSession | None = None
        channel = None
        sent = False
        try:
            if self.lease_root is not None:
                import base64

                from mcp_hcl.cli_session import PersistentConsole

                with PersistentConsole(
                    pipe_name,
                    self.lease_root,
                    timeout=self.read_timeout,
                    max_bytes=MAX_OUTPUT_BYTES,
                    open_timeout=self.open_timeout,
                    native_lease_root=self.native_lease_root,
                ) as channel:
                    reply = channel.exchange(command)
                    sent = True
                    raw = base64.b64decode(reply["raw_output_b64"])
                    stale = b"0" * reply["discarded_stale_bytes"]
            elif location["transport"] == "telnet":
                # HCL bridges each device pipe onto telnet; the pipe itself is
                # held open read-only by SimwareClient, so telnet is the only
                # channel a second process can actually write to.
                session = open_telnet(location["host"], location["port"], timeout=self.open_timeout)
                time.sleep(min(self.settle_seconds, self.idle_gap))
                stale = session.drain()
                session.send(command)
                sent = True
                raw = session.read_until_idle(idle_gap=self.idle_gap, timeout=self.read_timeout)
            else:
                descriptor = open_console_pipe(pipe_name, timeout=self.open_timeout)
                session = ConsoleSession(descriptor)
                time.sleep(min(self.settle_seconds, self.idle_gap))
                stale = session.drain()
                session.send(command)
                sent = True
                raw = session.read_until_idle(idle_gap=self.idle_gap, timeout=self.read_timeout)
        except ConsoleError as exc:
            result["status"] = "unverified"
            result["errors"] = [exc.as_error(code="console_unavailable")]
            result["complete"] = False
            result["command_outcome"] = (
                "unknown" if sent or exc.detail.get("command_outcome") == "unknown" else "not_sent"
            )
            failed_raw = (
                session.partial_output
                if session is not None
                else getattr(channel, "partial_output", b"")
            )
            result.update(
                output=_decode(failed_raw)[0],
                output_bytes=len(failed_raw),
                output_sha256=hashlib.sha256(failed_raw).hexdigest(),
            )
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            return result
        finally:
            if session is not None:
                session.close()

        text, encoding = _decode(raw)
        result.update(
            {
                "status": "output_received" if raw else "no_output",
                "topology": location["topology"] or topology_hint,
                "topology_source": "endpoint"
                if location["topology"]
                else ("declared" if topology_hint else None),
                "transport": location["transport"],
                "device": location["device"],
                "discarded_stale_bytes": len(stale),
                "output_bytes": len(raw),
                "output_sha256": hashlib.sha256(raw).hexdigest(),
                "output_encoding": encoding,
                "output": text[-MAX_OUTPUT_BYTES:],
                "complete": bool(channel is not None and reply["complete"]),
                "read_termination": "prompt" if channel is not None else session.read_termination,
                "command_outcome": "response_received" if raw else "unknown",
                "expect": expect,
                "expect_found": None if expect is None else (expect in text),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "verified_scope": "console_channel_text_readback_only",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "evidence": [
                    {
                        "source": pipe_name,
                        "layer": f"console_{location['transport']}",
                        "request_sha256": hashlib.sha256(
                            command.encode("ascii", "replace")
                        ).hexdigest(),
                        "output_sha256": hashlib.sha256(raw).hexdigest(),
                    }
                ],
            }
        )
        if session is not None and raw and session.read_termination == "timeout":
            result["status"] = "unverified"
            result["errors"] = [
                {
                    "code": "console_read_timeout",
                    "message": "Output did not become idle before the deadline",
                }
            ]
        if expect is not None and result["expect_found"] is False:
            result["errors"] = [
                {
                    "code": "expected_text_missing",
                    "message": f"Console output did not contain the expected marker: {expect}",
                }
            ]
        return result


def writes_enabled_from_environment(environment: dict[str, str] | None = None) -> bool:
    source = os.environ if environment is None else environment
    return source.get("MCP_HCL_ALLOW_LIVE_WRITE", "").strip().lower() in {"1", "true", "yes", "on"}
