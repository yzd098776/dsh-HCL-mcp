"""Persistent, leased, bounded Comware console transactions."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import time
from pathlib import Path

from mcp_hcl.console import (
    ConsoleError,
    ConsoleSession,
    _decode,
    describe,
    open_console_pipe,
    open_telnet,
)

PROMPT = re.compile(r"(?:^|\n)(<[^<>\r\n]{1,128}>|\[[^\[\]\r\n]{1,160}\])\s*$")
PAGER = re.compile(r"(?:----\s*More\s*----|--More--)\s*$", re.I)
PAGER_NOISE = re.compile(r"[ \t]*(?:----\s*More\s*----|--More--)[ \t]*", re.I)
CLI_ERROR = re.compile(
    r"(?:^|\n)\s*(?:%\s*(?:Unrecognized|Incomplete|Wrong|Error|Too many)|Error:|Invalid command|Permission denied)",
    re.I,
)


def _normalize(raw: bytes) -> str:
    text = _decode(raw)[0].replace("\r", "")
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    while "\b" in text:
        text = re.sub(r".?\x08", "", text)
    return text


def pager_pending(raw: bytes) -> bool:
    """Whether the device is waiting at a pager prompt.

    Checked against the raw text, before :func:`clean_text` removes the pager
    markers: the marker is transport noise in a readback body but it is the only
    signal that a page must be continued.
    """
    return bool(PAGER.search(_normalize(raw)))


def clean_text(raw: bytes) -> str:
    text = _normalize(raw)
    # Paging prompts are transport noise, not device output: the pager emits them
    # after a page is complete, and they can land inside a readback body (for
    # example between two VLAN sections) or be glued to the trailing prompt.
    text = PAGER_NOISE.sub("", text)
    return text


def command_echo_present(text: str, command: str) -> bool:
    """Whether the device echoed ``command``, tolerating terminal-width wrapping.

    Comware echoes at the terminal width (80 columns by default), so a long
    command returns split across physical lines with nothing inserted between
    the fragments - ``configuration replace file flash:/mcp_<32 hex>_device1``
    arrives as ``...MSR3`` then ``6-20_1_before.cfg``. Matching a single line
    for the whole command therefore fails for exactly the commands that carry a
    flash path, so the fragments are rejoined here before comparing.

    A leading ``>`` or ``]`` is the prompt the echo was typed after; only a
    suffix that itself looks like the start of the command is stripped, so an
    output line that merely ends with a bracket cannot become a false echo.
    """
    lines = text.split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        tail = stripped
        for marker in (">", "]"):
            position = tail.rfind(marker)
            if position == -1:
                continue
            after = tail[position + 1 :]
            if after and (command.startswith(after) or after.startswith(command)):
                tail = after
                break
        if not tail:
            continue
        accumulated = tail
        if accumulated.startswith(command):
            return True
        if not command.startswith(accumulated):
            continue
        for continuation in lines[index + 1 :]:
            accumulated += continuation.strip()
            if accumulated.startswith(command):
                return True
            if not command.startswith(accumulated):
                break
    return False


def ensure_user_view(channel):
    """Leave any configuration view, tolerating Comware's rejection in user view.

    ``return`` exists in every view except user view, where the device answers
    ``% Unrecognized command``. A fresh connection therefore cannot blindly send
    it: doing so would fail the whole transaction on an already-clean channel.
    The prompt captured by the session decides, and when it is unknown the
    rejection itself is the proof that the channel is already in user view.
    """
    prompt = getattr(channel, "prompt", None)
    if isinstance(prompt, str) and prompt.startswith("<"):
        return
    history = getattr(channel, "history", None)
    recorded = len(history) if isinstance(history, list) else None
    try:
        channel.exchange("return")
    except ConsoleError:
        observed = getattr(channel, "prompt", None)
        if not (isinstance(observed, str) and observed.startswith("<")):
            raise
        # The device read the line and rejected it, so nothing was left
        # half-entered. The captured user-view prompt is positive evidence, and
        # the rejected line is dropped so it cannot be mistaken for a step.
        if recorded is not None and len(history) > recorded:
            del history[recorded:]
        channel.failed = False


class PipeLease:
    def __init__(self, root: Path, pipe: str):
        describe(pipe)
        if any(
            path.is_symlink() or getattr(path, "is_junction", lambda: False)()
            for path in (root, *root.parents)
        ):
            raise ConsoleError("Pipe lease root cannot contain links", code="invalid_lease_root")
        root.mkdir(parents=True, exist_ok=True)
        path = root / (hashlib.sha256(pipe.casefold().encode()).hexdigest() + ".lock")
        if path.is_symlink():
            raise ConsoleError("Pipe lease cannot be linked")
        self.fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.fstat(self.fd).st_size == 0:
                os.write(self.fd, b"0")
            os.lseek(self.fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            raise ConsoleError(
                "Console channel is busy; release the other client",
                code="console_lease_busy",
                detail={
                    "stage": "lease",
                    "errno": exc.errno,
                    "winerror": getattr(exc, "winerror", None),
                },
            ) from exc

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class PersistentConsole:
    def __init__(
        self,
        pipe: str,
        lease_root: Path,
        *,
        session=None,
        timeout=12.0,
        max_bytes=1024 * 1024,
        open_timeout=2.0,
        native_lease_root: Path | None = None,
    ):
        self.pipe = pipe
        self.native_channel = session is None
        # Native consumers using different output directories still compete
        # for the same per-user endpoint lock, so they bypass the caller's
        # output-root-scoped ``lease_root``. The per-user location is only the
        # default: a host running this server under a restricted token can
        # write only in its own workspace, so it passes an explicit
        # ``native_lease_root``. Synthetic sessions stay local to ``lease_root``.
        if self.native_channel and native_lease_root is not None:
            actual_lease_root = native_lease_root
        elif self.native_channel:
            actual_lease_root = Path.home() / ".mcp_hcl" / "console-leases"
        else:
            actual_lease_root = lease_root
        self.lease = PipeLease(actual_lease_root, pipe)
        self.timeout = timeout
        self.max_bytes = max_bytes
        location = describe(pipe)
        self.transport = location["transport"]
        try:
            # Native sessions prove the connected instance is exclusive before
            # any drain or input. Process existence is not ownership: an idle
            # MultiCC (or an unrelated PuTTY) must not veto this endpoint.
            #
            # The device pipe itself cannot be opened by a second process: HCL's
            # SimwareClient holds the only read handle and republishes the
            # stream as a telnet server on 30000 + device, so that bridge is the
            # transport a live console actually uses.
            if session is not None:
                self.session = session
            elif location["transport"] == "telnet":
                self.session = open_telnet(location["host"], location["port"], timeout=open_timeout)
            else:
                self.session = ConsoleSession(
                    open_console_pipe(pipe, timeout=open_timeout), max_bytes=max_bytes
                )
        except Exception:
            self.lease.close()
            raise
        self.prompt = None
        self.history = []
        self.partial_output = b""
        self.failed = False

    def _receive(self, *, confirmation=False, initial=b""):
        answer = "Y" if confirmation is True else confirmation
        if confirmation is not False and (not isinstance(answer, str) or answer not in {"Y", "N"}):
            raise ConsoleError(
                "Only explicit Y/N confirmations are supported", code="invalid_confirmation"
            )
        output = bytearray(initial)
        deadline = time.monotonic() + self.timeout
        pager_count = 0
        last_pager_size = -1
        confirmed = False
        while time.monotonic() < deadline:
            if len(output) > self.max_bytes:
                raise ConsoleError(
                    "Console output exceeds the evidence limit; truncation is not accepted",
                    code="output_limit",
                    detail={"stage": "read", "observed_bytes": len(output)},
                )
            text = clean_text(bytes(output))
            if confirmation and not confirmed and re.search(r"\[(?:Y/N|y/n)\]:?\s*$", text):
                self.session.send(answer)
                confirmed = True
            if pager_pending(bytes(output)) and len(output) != last_pager_size:
                # Space is the documented pager continuation, not a command line.
                # ``send_raw`` keeps this working for both the pipe descriptor
                # and the telnet socket transport.
                self.session.send_raw(b" ")
                last_pager_size = len(output)
                pager_count += 1
                if pager_count > 256:
                    raise ConsoleError(
                        "Console pagination exceeded the page limit",
                        code="pager_limit",
                        detail={"stage": "read"},
                    )
            match = PROMPT.search(text)
            if match:
                self.prompt = match[1]
                return bytes(output)
            chunk = self.session._read_available()
            if chunk:
                output.extend(chunk)
                self.partial_output = bytes(output)
            else:
                time.sleep(0.02)
        raise ConsoleError(
            "No complete console prompt before deadline",
            code="prompt_timeout",
            detail={"stage": "read"},
        )

    def exchange(self, command: str, *, confirmation=False) -> dict:
        self.partial_output = b""
        stale = b""
        sent = False
        try:
            if self.failed:
                raise ConsoleError(
                    "Failed console transaction requires reconciliation and a new connection",
                    code="console_session_failed",
                    detail={"stage": "precondition"},
                )
            if (
                confirmation is not False
                and confirmation is not True
                and (not isinstance(confirmation, str) or confirmation not in {"Y", "N"})
            ):
                raise ConsoleError(
                    "Only explicit Y/N confirmations are supported",
                    code="invalid_confirmation",
                    detail={"stage": "precondition"},
                )
            stale = self.session.drain()
            if len(stale) >= self.max_bytes:
                raise ConsoleError(
                    "Stale stream cannot be drained safely",
                    code="stale_output_limit",
                    detail={"stage": "drain"},
                )
            self.session.send(command)
            sent = True
            raw = self._receive(confirmation=confirmation)
            text = clean_text(raw)
            if not command_echo_present(text, command):
                raise ConsoleError(
                    "Console command echo is missing; response attribution failed",
                    code="command_echo_missing",
                    detail={"stage": "attribution"},
                )
            if CLI_ERROR.search(text):
                raise ConsoleError(
                    "Device rejected the console command",
                    code="command_rejected",
                    detail={"stage": "response"},
                )
        except ConsoleError as exc:
            self.failed = True
            outcome = (
                "unknown" if sent or exc.detail.get("command_outcome") == "unknown" else "not_sent"
            )
            exc.detail["command_outcome"] = outcome
            self.history.append(
                {
                    "command": command,
                    "output": clean_text(self.partial_output),
                    "raw_output_b64": base64.b64encode(self.partial_output).decode("ascii"),
                    "output_sha256": hashlib.sha256(self.partial_output).hexdigest(),
                    "discarded_stale_bytes": len(stale),
                    "command_outcome": outcome,
                    "complete": False,
                    "error": str(exc),
                    "errors": [exc.as_error()],
                }
            )
            raise
        result = {
            "command": command,
            "output": text,
            "prompt": self.prompt,
            "raw_output_b64": base64.b64encode(raw).decode("ascii"),
            "output_sha256": hashlib.sha256(raw).hexdigest(),
            "request_sha256": hashlib.sha256(command.encode("ascii")).hexdigest(),
            "discarded_stale_bytes": len(stale),
            "complete": True,
        }
        self.history.append(result)
        return result

    def close(self):
        try:
            self.session.close()
        finally:
            self.lease.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
