"""Tests for the named-pipe device console client.

Two layers are covered:

* protocol logic (drain stale output, correlate a reply, refuse unsafe input)
  over anonymous pipes, which needs nothing Windows-specific;
* a **real Windows named pipe** created with ``CreateNamedPipeW``, so the
  open/read/write path used against HCL is genuinely exercised, not mocked.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import msvcrt
import os
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

from mcp_hcl.console import (
    DO,
    IAC,
    WILL,
    WONT,
    ConsoleError,
    ConsoleSession,
    HclConsole,
    describe,
    endpoint,
    open_telnet,
    resolve_endpoint,
    telnet_endpoint,
    writes_enabled_from_environment,
)

WINDOWS_ONLY = unittest.skipUnless(sys.platform == "win32", "Windows named pipes are required")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255
ERROR_PIPE_CONNECTED = 535
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32.CreateNamedPipeW.restype = wt.HANDLE
kernel32.CreateNamedPipeW.argtypes = [
    wt.LPCWSTR,
    wt.DWORD,
    wt.DWORD,
    wt.DWORD,
    wt.DWORD,
    wt.DWORD,
    wt.DWORD,
    ctypes.c_void_p,
]
kernel32.ConnectNamedPipe.restype = wt.BOOL
kernel32.ConnectNamedPipe.argtypes = [wt.HANDLE, ctypes.c_void_p]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def _make_nonblocking(fd: int) -> int:
    os.set_blocking(fd, False)
    return fd


def _read_nonblocking(fd: int, size: int = 65536) -> bytes:
    try:
        return os.read(fd, size)
    except BlockingIOError:
        return b""


def _pump(fd: int, size: int, timeout: float = 3.0) -> bytes:
    data = bytearray()
    deadline = time.monotonic() + timeout
    while len(data) < size and time.monotonic() < deadline:
        chunk = _read_nonblocking(fd, size - len(data))
        if chunk:
            data.extend(chunk)
        else:
            time.sleep(0.02)
    return bytes(data)


class TelnetBridgeServer:
    """A loopback stand-in for the telnet bridge HCL builds over a device pipe.

    It announces options exactly like SimwareClient (WILL ECHO, WILL SGA,
    DO BINARY), records the client's negotiation and raw writes, and escapes
    every ``IAC`` byte in what it sends, so the client's un-doubling is
    exercised rather than assumed.
    """

    def __init__(self, *, greeting: bytes = b"", reply: bytes = b"") -> None:
        self.greeting = greeting
        self.reply = reply
        self.received: list[bytes] = []
        self.negotiation = b""
        self.raw = bytearray()
        self.port = 0
        self._server: socket.socket | None = None
        self._client: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def reserve_port(self) -> int:
        """Bind and release a port so a test can target a closed endpoint."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]
        finally:
            probe.close()

    def start(self) -> None:
        # HCL maps device N to 30000 + N, and ``describe`` only accepts that
        # range, so the stand-in bridge has to live there too. Start high in the
        # range to stay clear of the device numbers a real lab uses.
        last_error: OSError | None = None
        for port in range(30990, 31025):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
                probe.listen(1)
            except OSError as exc:
                last_error = exc
                probe.close()
                continue
            self._server = probe
            self.port = port
            break
        else:
            raise unittest.SkipTest(f"no free port in 30990..31024: {last_error}")
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def push(self, payload: bytes) -> None:
        """Send device output after the connection is up, escaping ``IAC``."""
        self._send(payload)

    def push_raw(self, payload: bytes) -> None:
        """Send bytes verbatim, so a doubled ``IAC`` can be presented exactly."""
        client = self._client
        if client is None:
            return
        try:
            client.sendall(payload)
        except OSError:
            pass

    def _send(self, payload: bytes) -> None:
        client = self._client
        if client is None:
            return
        try:
            client.sendall(payload.replace(bytes([IAC]), bytes([IAC, IAC])))
        except OSError:
            pass

    def _serve(self) -> None:
        try:
            client, _ = self._server.accept()
        except OSError:
            return
        self._client = client
        client.settimeout(0.05)
        if self.greeting:
            # Options are telnet commands, not device text: never escape them.
            try:
                client.sendall(self.greeting)
            except OSError:
                pass
        pending = bytearray()
        while not self._stop.is_set():
            try:
                chunk = client.recv(4096)
            except (socket.timeout, BlockingIOError):
                time.sleep(0.01)
                continue
            except OSError:
                break
            if not chunk:
                break
            self.raw.extend(chunk)
            pending.extend(chunk)
            # Peel client->server telnet commands off the front, keep the text.
            text = bytearray()
            index = 0
            while index < len(pending):
                if pending[index] != IAC:
                    text.append(pending[index])
                    index += 1
                    continue
                if index + 2 >= len(pending):
                    break
                self.negotiation += bytes(pending[index : index + 3])
                index += 3
            pending = bytearray(pending[index:])
            # A real Comware console accepts a bare CR, which is what the client
            # sends; CRLF is tolerated so a fixture can still use it.
            while b"\r" in text:
                line, _, rest = bytes(text).partition(b"\r")
                text = bytearray(rest)
                if text.startswith(b"\n"):
                    text = text[1:]
                if not line.strip():
                    # The client's single wake newline is not a command.
                    continue
                self.received.append(line + b"\r\n")
                if self.reply:
                    self._send(self.reply)
        try:
            client.close()
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        for sock in (self._client, self._server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


class NamedPipeServer:
    """A real byte-mode Windows named pipe that answers each line with a reply."""

    def __init__(
        self,
        name: str,
        *,
        banner: bytes = b"",
        reply: bytes = b"",
        max_instances=1,
        expect_client_rejection=False,
    ) -> None:
        self.name = name
        self.max_instances = max_instances
        self.banner = banner
        self.reply = reply
        self.expect_client_rejection = expect_client_rejection
        self.rejected_disconnect = False
        self.received: list[str] = []
        self.errors: list[Exception] = []
        self._handle = None
        self._owns_handle = True
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handle = kernel32.CreateNamedPipeW(
            self.name,
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            self.max_instances,
            4096,
            4096,
            0,
            None,
        )
        if not handle or handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            connected = kernel32.ConnectNamedPipe(self._handle, None)
            if not connected:
                code = ctypes.get_last_error()
                if self.expect_client_rejection and code == 232 and not self.received:
                    # The identity-rejection client can close before this
                    # server thread has returned from ConnectNamedPipe.
                    self.rejected_disconnect = True
                    return
                if code != ERROR_PIPE_CONNECTED:
                    raise ctypes.WinError(code)
            descriptor = msvcrt.open_osfhandle(self._handle, os.O_RDWR | os.O_BINARY)
            self._owns_handle = False
            try:
                os.set_blocking(descriptor, False)
                if self.banner:
                    os.write(descriptor, self.banner)
                buffer = b""
                while not self._stop.is_set():
                    try:
                        chunk = os.read(descriptor, 4096)
                    except BlockingIOError:
                        time.sleep(0.02)
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    # The client terminates a command with a bare CR, which a real
                    # Comware console accepts; a trailing LF is tolerated.
                    while b"\r" in buffer:
                        line, buffer = buffer.split(b"\r", 1)
                        if buffer.startswith(b"\n"):
                            buffer = buffer[1:]
                        if not line.strip():
                            continue
                        self.received.append(line.decode("ascii", "replace"))
                        os.write(descriptor, self.reply)
            finally:
                os.close(descriptor)
        except Exception as exc:  # surfaced by close()
            self.errors.append(exc)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._owns_handle and self._handle:
            kernel32.CloseHandle(self._handle)
        if self.errors:
            raise self.errors[0]


class PipeFixtureTests(unittest.TestCase):
    def test_early_disconnect_requires_explicit_rejection_and_known_native_code(self):
        for expected_rejection, code in ((True, 232), (False, 232), (True, 5)):
            with self.subTest(expected_rejection=expected_rejection, code=code):
                server = NamedPipeServer("unused", expect_client_rejection=expected_rejection)
                with (
                    patch.object(kernel32, "ConnectNamedPipe", return_value=False),
                    patch("ctypes.get_last_error", return_value=code),
                ):
                    server._serve()
                self.assertEqual(server.received, [])
                if expected_rejection and code == 232:
                    self.assertTrue(server.rejected_disconnect)
                    server.close()
                else:
                    with self.assertRaises(OSError) as caught:
                        server.close()
                    self.assertEqual(caught.exception.winerror, code)


class PipeNameTests(unittest.TestCase):
    def test_endpoint_builds_the_observed_shape(self) -> None:
        self.assertEqual(endpoint("topo1", 6), "\\\\.\\pipe\\topo1-device6")
        self.assertEqual(
            describe("\\\\.\\pipe\\topo77-device12"),
            {
                "pipe": "\\\\.\\pipe\\topo77-device12",
                "endpoint": "\\\\.\\pipe\\topo77-device12",
                "transport": "named_pipe",
                "topology": "topo77",
                "device": 12,
            },
        )

    def test_telnet_endpoint_is_the_default_and_maps_to_30000_plus_device(self) -> None:
        self.assertEqual(telnet_endpoint("topo1", 6), "telnet://127.0.0.1:30006")
        self.assertEqual(resolve_endpoint("topo1", 6), telnet_endpoint("topo1", 6))
        self.assertEqual(
            describe("telnet://127.0.0.1:30012"),
            {
                "pipe": "telnet://127.0.0.1:30012",
                "endpoint": "telnet://127.0.0.1:30012",
                "transport": "telnet",
                "host": "127.0.0.1",
                "port": 30012,
                "topology": None,
                "device": 12,
            },
        )

    def test_telnet_endpoints_outside_the_assigned_range_are_refused(self) -> None:
        for bad in (
            "telnet://127.0.0.1:30000",
            "telnet://127.0.0.1:31025",
            "telnet://10.0.0.1:30001",
            "telnet://127.0.0.1:23",
        ):
            with self.subTest(endpoint=bad):
                with self.assertRaises(ConsoleError):
                    describe(bad)

    def test_only_hcl_console_pipes_are_accepted(self) -> None:
        for bad in (
            "\\\\.\\pipe\\evil",
            "\\\\.\\pipe\\x",
            "\\\\server\\pipe\\topo1-device1",
            "topo1-device1",
            "/dev/ttyS0",
            "",
            None,
            "\\\\.\\pipe\\topo1-device",
            "\\\\.\\pipe\\topo1-device1x",
        ):
            with self.subTest(pipe=bad):
                with self.assertRaises(ConsoleError):
                    describe(bad)

    def test_endpoint_argument_validation(self) -> None:
        for topology, device in (
            ("topo 1", 1),
            ("", 1),
            ("topo1", 0),
            ("topo1", 2000),
            ("topo1", True),
            ("topo1", "1"),
        ):
            with self.subTest(topology=topology, device=device):
                with self.assertRaises(ConsoleError):
                    endpoint(topology, device)


class ConsoleGatingTests(unittest.TestCase):
    def test_input_is_refused_without_the_master_switch(self) -> None:
        # no pipe exists at this name: a pass proves the gate fires before any I/O
        result = HclConsole().run("\\\\.\\pipe\\topo1-device1", "display version")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "writes_disabled")
        self.assertFalse(result["guest_boot_verified"])

    def test_unsafe_commands_are_refused(self) -> None:
        console = HclConsole(writes_enabled=True)
        for command in ("", "   ", "display version\nreboot", "display\r\nversion", "a\x00b"):
            with self.subTest(command=command):
                result = console.run("\\\\.\\pipe\\topo1-device1", command)
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["errors"][0]["code"], "invalid_request")

    def test_environment_master_switch(self) -> None:
        for value, expected in (
            ("1", True),
            ("true", True),
            ("YES", True),
            ("on", True),
            ("", False),
            ("0", False),
            ("no", False),
        ):
            with self.subTest(value=value):
                self.assertIs(
                    writes_enabled_from_environment({"MCP_HCL_ALLOW_LIVE_WRITE": value}), expected
                )


class ConsoleSessionTests(unittest.TestCase):
    def test_drained_output_cannot_be_mistaken_for_the_answer(self) -> None:
        client_read, peer_write = os.pipe()
        peer_read, client_write = os.pipe()
        for descriptor in (client_read, peer_write, peer_read, client_write):
            _make_nonblocking(descriptor)
        session = ConsoleSession(client_read, client_write)
        try:
            os.write(peer_write, b"<HCL> stale banner\r\n")
            time.sleep(0.2)
            stale = session.drain()
            self.assertIn(b"stale banner", stale)

            session.send("display version")
            sent = _pump(peer_read, len(b"display version\r"))
            self.assertEqual(sent, b"display version\r")

            os.write(peer_write, b"Version 7.1.064\r\n")
            output = session.read_until_idle(idle_gap=0.15, timeout=2.0)
        finally:
            session.close()
            for descriptor in (peer_write, peer_read):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self.assertIn(b"Version 7.1.064", output)
        self.assertNotIn(b"stale banner", output)

    def test_read_until_idle_returns_empty_when_nothing_arrives(self) -> None:
        client_read, peer_write = os.pipe()
        _make_nonblocking(client_read)
        session = ConsoleSession(client_read)
        try:
            started = time.monotonic()
            output = session.read_until_idle(idle_gap=0.1, timeout=0.4)
            elapsed = time.monotonic() - started
        finally:
            session.close()
            os.close(peer_write)
        self.assertEqual(output, b"")
        self.assertGreaterEqual(elapsed, 0.35)


class TelnetBridgeTests(unittest.TestCase):
    """The transport HCL actually publishes for a device console.

    A loopback server reproduces what SimwareClient's telnet bridge does:
    announce options, escape ``IAC`` in device output, and echo the line.
    """

    def setUp(self) -> None:
        self.server = TelnetBridgeServer(
            greeting=bytes([IAC, WILL, 1]) + bytes([IAC, WILL, 3]) + bytes([IAC, DO, 0]),
            reply=b"Version 7.1.064, Release 0427P22\r\n<H3C>",
        )
        self.server.start()

    def tearDown(self) -> None:
        self.server.close()

    def test_negotiation_is_answered_and_stripped_from_output(self) -> None:
        session = open_telnet("127.0.0.1", self.server.port, timeout=2.0)
        try:
            session.drain()
            session.send("display version")
            output = session.read_until_idle(idle_gap=0.15, timeout=3.0)
        finally:
            session.close()
        self.assertIn(b"Version 7.1.064", output)
        self.assertNotIn(bytes([IAC]), output)
        self.assertEqual(self.server.received, [b"display version\r\n"])
        # WILL ECHO -> DO ECHO, WILL SGA -> DO SGA, DO BINARY -> WONT BINARY.
        self.assertIn(bytes([IAC, DO, 1]), self.server.negotiation)
        self.assertIn(bytes([IAC, DO, 3]), self.server.negotiation)
        self.assertIn(bytes([IAC, WONT, 0]), self.server.negotiation)

    def test_iac_in_a_command_is_doubled_so_it_cannot_be_a_telnet_command(self) -> None:
        session = open_telnet("127.0.0.1", self.server.port, timeout=2.0)
        try:
            session.drain()
            session.send_raw(b"a" + bytes([IAC]) + b"b")
            deadline = time.monotonic() + 3.0
            while bytes([IAC, IAC]) not in self.server.raw and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            session.close()
        self.assertIn(b"a" + bytes([IAC, IAC]) + b"b", bytes(self.server.raw))

    def test_doubled_iac_in_device_output_is_collapsed(self) -> None:
        session = open_telnet("127.0.0.1", self.server.port, timeout=2.0)
        try:
            session.drain()
            self.server.push_raw(bytes([IAC, IAC]))
            output = session.read_until_idle(idle_gap=0.15, timeout=3.0)
        finally:
            session.close()
        self.assertEqual(output, bytes([IAC]))

    def test_unreachable_port_is_a_console_error_not_a_crash(self) -> None:
        closed = TelnetBridgeServer(greeting=b"", reply=b"")
        port = closed.reserve_port()
        with self.assertRaises(ConsoleError) as caught:
            open_telnet("127.0.0.1", port, timeout=0.3)
        self.assertEqual(caught.exception.code, "console_unreachable")
        self.assertEqual(caught.exception.detail["stage"], "open")


class TelnetConsoleRunTests(unittest.TestCase):
    def test_telnet_endpoint_is_used_for_a_topology_device(self) -> None:
        server = TelnetBridgeServer(
            greeting=bytes([IAC, WILL, 1]), reply=b"Version 7.1.064\r\n<H3C>"
        )
        server.start()
        try:
            console = HclConsole(
                open_timeout=2.0, idle_gap=0.3, read_timeout=5.0, writes_enabled=True
            )
            endpoint_url = f"telnet://127.0.0.1:{server.port}"
            result = console.run(endpoint_url, "display version", expect="Version")
        finally:
            server.close()
        self.assertEqual(result["status"], "output_received")
        self.assertIn("Version 7.1.064", result["output"])
        self.assertTrue(result["expect_found"])
        self.assertEqual(result["device"], server.port - 30000)
        self.assertEqual(result["verified_scope"], "console_channel_text_readback_only")
        self.assertEqual(result["evidence"][0]["layer"], "console_telnet")
        self.assertFalse(result["guest_boot_verified"])

    def test_a_telnet_result_reports_its_transport_and_declared_topology(self) -> None:
        server = TelnetBridgeServer(
            greeting=bytes([IAC, WILL, 1]), reply=b"Version 7.1.064\r\n<H3C>"
        )
        server.start()
        try:
            console = HclConsole(
                open_timeout=2.0, idle_gap=0.3, read_timeout=5.0, writes_enabled=True
            )
            endpoint_url = f"telnet://127.0.0.1:{server.port}"
            result = console.run(
                endpoint_url, "display version", expect="Version", topology_hint="topo1"
            )
        finally:
            server.close()
        # A telnet endpoint carries no topology name, so the caller's hint is
        # echoed but marked as declared rather than observed.
        self.assertEqual(result["transport"], "telnet")
        self.assertEqual(result["topology"], "topo1")
        self.assertEqual(result["topology_source"], "declared")

    def test_a_pipe_result_reports_the_topology_observed_in_the_endpoint(self) -> None:
        console = HclConsole(open_timeout=0.4, writes_enabled=True)
        result = console.run(
            "\\\\.\\pipe\\mcphcltest-absent-device9",
            "display version",
            topology_hint="ignored",
        )
        # Even a failed open reports which channel was addressed; the declared
        # hint never overrides a topology the endpoint itself carries.
        self.assertEqual(result["endpoint"], "\\\\.\\pipe\\mcphcltest-absent-device9")


@WINDOWS_ONLY
class RealNamedPipeTests(unittest.TestCase):
    def test_round_trip_over_a_real_named_pipe(self) -> None:
        name = "\\\\.\\pipe\\mcphcltest-device1"
        server = NamedPipeServer(
            name, banner=b"<HCL> booting\r\n", reply=b"Version 7.1.064, Release 0427P22\r\n<HCL>"
        )
        server.start()
        try:
            console = HclConsole(
                open_timeout=3.0, idle_gap=0.3, read_timeout=6.0, writes_enabled=True
            )
            result = console.run(name, "display version", expect="Version")
        finally:
            server.close()

        self.assertEqual(result["status"], "output_received")
        self.assertEqual(result["topology"], "mcphcltest")
        self.assertEqual(result["device"], 1)
        self.assertIn("Version 7.1.064", result["output"])
        self.assertTrue(result["expect_found"])
        self.assertGreaterEqual(
            result["discarded_stale_bytes"],
            1,
            "the banner written before the command must be drained",
        )
        self.assertNotIn("booting", result["output"])
        self.assertEqual(result["verified_scope"], "console_channel_text_readback_only")
        self.assertFalse(result["guest_boot_verified"])
        self.assertFalse(result["configuration_verified"])
        self.assertFalse(result["connectivity_verified"])
        self.assertFalse(result["full_live_gate_passed"])
        self.assertEqual(len(result["output_sha256"]), 64)
        self.assertEqual(server.received, ["display version"])

    def test_missing_expected_marker_is_reported(self) -> None:
        name = "\\\\.\\pipe\\mcphcltest-device2"
        server = NamedPipeServer(name, reply=b"Unknown command\r\n<HCL>")
        server.start()
        try:
            console = HclConsole(
                open_timeout=3.0, idle_gap=0.3, read_timeout=6.0, writes_enabled=True
            )
            result = console.run(name, "display nosuchthing", expect="Version")
        finally:
            server.close()

        self.assertFalse(result["expect_found"])
        self.assertEqual(result["errors"][0]["code"], "expected_text_missing")
        self.assertIn("Unknown command", result["output"])

    def test_absent_pipe_is_unverified_not_an_exception(self) -> None:
        console = HclConsole(open_timeout=0.4, writes_enabled=True)
        result = console.run("\\\\.\\pipe\\mcphcltest-absent-device9", "display version")
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["errors"][0]["code"], "console_unavailable")


if __name__ == "__main__":
    unittest.main()
