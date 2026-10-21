import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_hcl.cli_session import (
    PersistentConsole,
    PipeLease,
    command_echo_present,
    ensure_user_view,
)
from mcp_hcl.console import ConsoleError, ConsoleSession


class EchoAttributionTests(unittest.TestCase):
    """Comware wraps its echo at the terminal width, so the echo of a long
    command arrives as two lines with no character inserted or removed."""

    WRAPPED = (
        "configuration replace file flash:/mcp_63f6e6574bb3465483beb4422504d250_MSR3\r\n"
        "6-20_1_before.cfg\r\n"
        "The running configuration will be lost.\r\n"
        "<H3C>"
    )
    COMMAND = (
        "configuration replace file "
        "flash:/mcp_63f6e6574bb3465483beb4422504d250_MSR36-20_1_before.cfg"
    )

    def test_a_wrapped_echo_is_still_the_same_command(self):
        self.assertTrue(command_echo_present(self.WRAPPED, self.COMMAND))

    def test_a_short_echo_matches_on_one_line(self):
        self.assertTrue(command_echo_present("display version\r\n<H3C>", "display version"))

    def test_the_prompt_prefix_does_not_hide_the_echo(self):
        self.assertTrue(command_echo_present("[H3C-vlan10]vlan 10\r\n[H3C-vlan10]", "vlan 10"))

    def test_output_that_never_repeats_the_command_is_not_an_echo(self):
        self.assertFalse(command_echo_present("% Unrecognized command\r\n<H3C>", "display version"))

    def test_a_later_prompt_alone_is_not_an_echo(self):
        # "vlan 10" must not be attributed to output that merely shows a view
        # whose name happens to start with the command.
        self.assertFalse(command_echo_present("[H3C-vlan10]\r\n[H3C]", "vlan 10"))


class FakeChannel:
    def __init__(self, prompt, views=None):
        self.prompt = prompt
        self.history = []
        self.failed = False
        self.sent = []
        self.views = views or {}

    def exchange(self, command, **kwargs):
        self.sent.append(command)
        if command not in self.views:
            raise ConsoleError("Device rejected the console command", code="command_rejected")
        self.prompt = self.views[command]
        return {"command": command, "complete": True}


class EnsureUserViewTests(unittest.TestCase):
    def test_a_user_view_prompt_needs_no_return(self):
        channel = FakeChannel("<H3C>")
        ensure_user_view(channel)
        self.assertEqual(channel.sent, [])

    def test_a_configuration_view_prompt_is_left_with_one_return(self):
        channel = FakeChannel("[H3C-vlan10]", views={"return": "<H3C>"})
        ensure_user_view(channel)
        self.assertEqual(channel.sent, ["return"])
        self.assertEqual(channel.prompt, "<H3C>")

    def test_a_refused_return_leaves_no_failed_state_behind(self):
        # A fresh telnet connection can already be in user view, where
        # "return" is rejected. The attempt must not poison the session.
        channel = FakeChannel("[H3C-x]")
        with self.assertRaises(ConsoleError):
            ensure_user_view(channel)
        self.assertEqual(channel.history, [])


class ScriptedSession:
    def __init__(self, reply):
        self.reply = reply
        self.chunks = []
        self.raw = []

    def drain(self):
        return b"stale output"

    def send(self, command):
        self.chunks = list(self.reply(command))

    def send_raw(self, payload):
        # Pager continuation arrives here as a bare space.
        self.raw.append(payload)

    def _read_available(self):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        pass


class Console040Tests(unittest.TestCase):
    def test_partial_writes_send_every_byte(self):
        data = []

        def write(fd, payload):
            data.append(payload[:2])
            return min(2, len(payload))

        with patch("os.write", side_effect=write):
            ConsoleSession(1).send("display version")
        self.assertEqual(b"".join(data), b"display version\r")

    def test_persistent_echo_prompt_and_stale_drain(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = ScriptedSession(
                lambda command: [command.encode() + b"\r\noutput\r\n", b"<R1>"]
            )
            with PersistentConsole(
                r"\\.\pipe\lab-device1", Path(temporary), session=session, timeout=0.1
            ) as channel:
                result = channel.exchange("display version")
                self.assertTrue(result["complete"])
                self.assertEqual(result["discarded_stale_bytes"], 12)

    def test_lease_busy_and_missing_echo_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = PipeLease(Path(temporary), r"\\.\pipe\lab-device1")
            try:
                with self.assertRaises(ConsoleError):
                    PipeLease(Path(temporary), r"\\.\pipe\lab-device1")
            finally:
                first.close()
            with PersistentConsole(
                r"\\.\pipe\lab-device1",
                Path(temporary),
                session=ScriptedSession(lambda c: [b"old\n<R1>"]),
                timeout=0.1,
            ) as channel:
                with self.assertRaises(ConsoleError):
                    channel.exchange("display version")

    def test_continuous_output_obeys_deadline_and_overflow_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = ScriptedSession(lambda c: [])
            session._read_available = lambda: b"flood"
            with PersistentConsole(
                r"\\.\pipe\lab-device1",
                Path(temporary),
                session=session,
                timeout=0.02,
                max_bytes=100,
            ) as channel:
                with self.assertRaises(ConsoleError):
                    channel.exchange("display version")
                self.assertFalse(channel.history[-1]["complete"])

    def test_pagination_continues_once_and_confirmation_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = ScriptedSession(lambda c: [c.encode() + b"\n---- More ----", b"\nlast\n<R1>"])
            with PersistentConsole(
                r"\\.\pipe\lab-device1", Path(temporary), session=session, timeout=0.1
            ) as channel:
                self.assertTrue(channel.exchange("display version")["complete"])
            # The pager continuation goes through the session transport, so it
            # works for a pipe descriptor and a telnet socket alike.
            self.assertEqual(session.raw, [b" "])
