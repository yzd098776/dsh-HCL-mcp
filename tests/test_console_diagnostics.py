"""Native error attribution and incomplete console transactions; no HCL guest."""

import ctypes
import errno
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from test_040_console import ScriptedSession
from test_console import INVALID_HANDLE_VALUE, NamedPipeServer, kernel32

from mcp_hcl.cli_session import PersistentConsole
from mcp_hcl.console import ConsoleError, ConsoleSession, HclConsole, open_console_pipe


def native_error(number):
    error = OSError(errno.EACCES, "Synthetic native failure")
    error.winerror = number
    return error


class DiagnosticTests(unittest.TestCase):
    pipe = r"\\.\pipe\diagnostics-device1"

    def test_access_denied_and_sharing_violation_are_not_retried(self):
        for number, reason in ((5, "pipe_access_denied"), (32, "pipe_sharing_violation")):
            with (
                self.subTest(number=number),
                patch(
                    "mcp_hcl.console._open_pipe_descriptor", side_effect=native_error(number)
                ) as opened,
            ):
                with self.assertRaises(ConsoleError) as caught:
                    open_console_pipe(self.pipe, timeout=2)
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(caught.exception.code, reason)
                self.assertEqual(caught.exception.detail["winerror"], number)
                self.assertEqual(caught.exception.detail["stage"], "open")

    def test_busy_and_absent_wait_is_bounded_and_keeps_last_native_error(self):
        for number, reason in ((231, "pipe_busy"), (2, "pipe_not_found")):
            with (
                self.subTest(number=number),
                patch("mcp_hcl.console._open_pipe_descriptor", side_effect=native_error(number)),
            ):
                started = time.monotonic()
                with self.assertRaises(ConsoleError) as caught:
                    open_console_pipe(self.pipe, timeout=0.02)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(caught.exception.code, reason)
                self.assertEqual(caught.exception.detail["winerror"], number)
                self.assertTrue(caught.exception.detail["wait_expired"])

    def test_invalid_timeouts_never_open_the_endpoint(self):
        with patch("mcp_hcl.console._open_pipe_descriptor") as opened:
            for value in (float("nan"), float("inf"), -1, True):
                with self.assertRaises(ConsoleError):
                    open_console_pipe(self.pipe, timeout=value)
            opened.assert_not_called()

    def test_tool_error_is_compatible_and_adds_native_details(self):
        with patch("mcp_hcl.console._open_pipe_descriptor", side_effect=native_error(5)):
            result = HclConsole(writes_enabled=True).run(self.pipe, "display version")
        self.assertEqual(result["errors"][0]["code"], "console_unavailable")
        self.assertEqual(result["errors"][0]["detail"]["reason"], "pipe_access_denied")
        self.assertEqual(result["errors"][0]["detail"]["winerror"], 5)
        self.assertEqual(result["command_outcome"], "not_sent")
        self.assertFalse(result["complete"])
        self.assertFalse(result["full_live_gate_passed"])

    def test_read_overflow_retains_a_prefix_and_never_returns_success(self):
        session = ConsoleSession(123, max_bytes=4)
        with patch.object(session, "_read_available", return_value=b"abcdefgh"):
            with self.assertRaises(ConsoleError) as caught:
                session.read_until_idle(idle_gap=0.01, timeout=0.02)
        self.assertEqual(caught.exception.code, "output_limit")
        self.assertEqual(session.partial_output, b"abcd")
        self.assertEqual(caught.exception.detail["observed_bytes"], 8)

    def test_stale_stream_limit_prevents_command_input(self):
        session = ConsoleSession(123, max_bytes=4)
        with patch.object(session, "_read_available", return_value=b"abcd"):
            with self.assertRaises(ConsoleError) as caught:
                session.drain()
        self.assertEqual(caught.exception.code, "stale_output_limit")

    def test_continuous_low_level_output_cannot_bypass_deadline(self):
        session = ConsoleSession(123, max_bytes=1024 * 1024)
        with patch.object(session, "_read_available", return_value=b"x"):
            started = time.monotonic()
            session.read_until_idle(idle_gap=0.01, timeout=0.02)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(session.read_termination, "timeout")

    def test_partial_write_has_unknown_outcome_and_is_not_replayed(self):
        session = ConsoleSession(123, write_timeout=0.01)
        calls = []

        def write(fd, data):
            calls.append(data)
            if len(calls) == 1:
                return 2
            raise BlockingIOError()

        with patch("mcp_hcl.console.os.write", side_effect=write):
            with self.assertRaises(ConsoleError) as caught:
                session.send("display version")
        self.assertEqual(caught.exception.code, "write_timeout")
        self.assertEqual(caught.exception.detail["bytes_written"], 2)
        self.assertEqual(caught.exception.detail["command_outcome"], "unknown")
        self.assertEqual(calls[0], b"display version\r")
        self.assertTrue(all(data == b"splay version\r" for data in calls[1:]))

    def test_missing_echo_and_rejection_are_failure_receipts(self):
        for reply, reason in (
            (b"old output\n<R1>", "command_echo_missing"),
            (b"display version\nError: refused\n<R1>", "command_rejected"),
        ):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                session = ScriptedSession(lambda c: [reply])
                with PersistentConsole(
                    self.pipe, Path(temporary), session=session, timeout=0.1
                ) as channel:
                    with self.assertRaises(ConsoleError) as caught:
                        channel.exchange("display version")
                    self.assertEqual(caught.exception.code, reason)
                    self.assertFalse(channel.history[-1]["complete"])
                    self.assertEqual(channel.history[-1]["command_outcome"], "unknown")
                    self.assertEqual(channel.history[-1]["errors"][0]["code"], reason)
                    self.assertTrue(channel.history[-1]["raw_output_b64"])
                    with patch.object(session, "send") as sent:
                        with self.assertRaises(ConsoleError):
                            channel.exchange("display version")
                        sent.assert_not_called()

    def test_send_failure_is_recorded_before_receive(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = ScriptedSession(lambda c: [])
            error = ConsoleError(
                "Partial input",
                code="write_timeout",
                detail={"bytes_written": 3, "command_outcome": "unknown"},
            )
            with PersistentConsole(
                self.pipe, Path(temporary), session=session, timeout=0.1
            ) as channel:
                with patch.object(session, "send", side_effect=error):
                    with self.assertRaises(ConsoleError):
                        channel.exchange("display version")
                self.assertEqual(len(channel.history), 1)
                self.assertFalse(channel.history[0]["complete"])
                self.assertEqual(channel.history[0]["errors"][0]["detail"]["bytes_written"], 3)

    def test_native_lease_is_shared_across_output_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = ScriptedSession(lambda c: [])
            with (
                patch("mcp_hcl.cli_session.Path.home", return_value=root),
                patch(
                    "subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout=b"false"),
                ),
                patch("mcp_hcl.cli_session.open_console_pipe", return_value=-1),
                patch("mcp_hcl.cli_session.ConsoleSession", return_value=session),
            ):
                first = PersistentConsole(self.pipe, root / "output-a")
                try:
                    with self.assertRaises(ConsoleError) as caught:
                        PersistentConsole(self.pipe, root / "output-b")
                    self.assertEqual(caught.exception.code, "console_lease_busy")
                    self.assertFalse((root / "output-b").exists())
                finally:
                    first.close()

    def test_native_lease_root_override_moves_the_lock(self):
        """A host whose profile is unwritable passes an explicit lease root."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "profile"
            profile.mkdir()
            moved = root / "workspace" / "console-leases"
            session = ScriptedSession(lambda c: [])
            with (
                patch("mcp_hcl.cli_session.Path.home", return_value=profile),
                patch(
                    "subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout=b"false"),
                ),
                patch("mcp_hcl.cli_session.open_console_pipe", return_value=-1),
                patch("mcp_hcl.cli_session.ConsoleSession", return_value=session),
            ):
                channel = PersistentConsole(self.pipe, root / "output", native_lease_root=moved)
                try:
                    self.assertTrue(moved.is_dir())
                    self.assertFalse((profile / ".mcp_hcl").exists())
                finally:
                    channel.close()


class PublicDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_keeps_native_error_as_a_business_result(self):
        from mcp_hcl.contracts import StrictFastMCP

        server = StrictFastMCP("diagnostics")

        @server.tool()
        def probe() -> dict:
            raise ConsoleError(
                "Native pipe busy", code="pipe_busy", detail={"winerror": 231, "stage": "open"}
            )

        result = await server.call_tool("probe", {})
        import json

        body = json.loads(result.content[0].text)
        self.assertFalse(result.isError)
        self.assertEqual(body["status"], "unverified")
        self.assertEqual(body["errors"][0]["detail"]["winerror"], 231)
        self.assertFalse(body["full_live_gate_passed"])


@unittest.skipUnless(os.name == "nt", "Win32 synthetic pipes required")
class NativeDiagnosticTests(unittest.TestCase):
    def test_real_busy_pipe_has_error_231_not_an_acl_claim(self):
        pipe = rf"\\.\pipe\diag{uuid4().hex}-device1"
        server = NamedPipeServer(pipe)
        server.start()
        descriptor = None
        try:
            descriptor = open_console_pipe(pipe, timeout=1)
            with self.assertRaises(ConsoleError) as caught:
                open_console_pipe(pipe, timeout=0.05)
            self.assertEqual(caught.exception.code, "pipe_busy")
            self.assertEqual(caught.exception.detail["winerror"], 231)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            server.close()

    def test_real_incompatible_access_has_error_5_without_retry(self):
        pipe = rf"\\.\pipe\diag{uuid4().hex}-device2"
        # A write-only server cannot accept a read/write client. This tests
        # native ACCESS_DENIED without altering any ACL or security setting.
        handle = kernel32.CreateNamedPipeW(pipe, 2, 0, 1, 4096, 4096, 0, None)
        if not handle or handle == INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            with self.assertRaises(ConsoleError) as caught:
                open_console_pipe(pipe, timeout=1)
            self.assertEqual(caught.exception.code, "pipe_access_denied")
            self.assertEqual(caught.exception.detail["winerror"], 5)
            self.assertEqual(caught.exception.detail["attempts"], 1)
        finally:
            kernel32.CloseHandle(handle)
