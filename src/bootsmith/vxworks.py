"""VxWorks boot ROM driver. Schema also re-exported as VXWORKS_FIELDS.

Talks to the interactive boot ROM at the `[VxWorks Boot]:` prompt to:

* Read current boot params with the `p` command.
* Write new params with the `c` command, which walks an interactive
  dialogue: for each field it prints the prompt, the user types the new
  value (or `.` to clear, or Enter to keep). We send the new value for
  every field (user-requested "write all every time" semantics).
* Verify by reading back with `p` and comparing.

Sample output from a real board:

    boot device          : geisc
    unit number          : 0
    processor number     : 0
    host name            : mkogmosdev-lv1
    file name            : /gemdev/vxworks/mv6100_314Test3/vxWorks.5
    inet on ethernet (e) : 10.2.126.101:ffffff00
    host inet (h)        : 10.2.126.21
    user (u)             : gemdev
    flags (f)            : 0x8
    target name (tn)     : gmosdc
    startup script (s)   : /gemdev/rt/gmosdc/bin/vxWorks-ppc604_long/startup

Not all fields are present on every board. We treat the union as the
schema and just don't show missing ones in the form.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from typing import Optional

from .transport import WTITransport


# Ordered list of (label, key) pairs. The `key` is what we use in JSON / forms;
# the `label` is what the boot ROM prints when reading and when prompting in
# the `c` dialogue. The order matches the order the dialogue walks them in.
FIELDS: tuple[tuple[str, str], ...] = (
    ("boot device",       "boot_device"),
    ("processor number",  "processor_number"),
    ("host name",         "host_name"),
    ("file name",         "file_name"),
    ("inet on ethernet (e)", "inet_on_ethernet"),
    ("inet on backplane (b)", "inet_on_backplane"),
    ("host inet (h)",     "host_inet"),
    ("gateway inet (g)",  "gateway_inet"),
    ("user (u)",          "user"),
    ("ftp password (pw) (blank = use rsh)", "ftp_password"),
    ("flags (f)",         "flags"),
    ("target name (tn)",  "target_name"),
    ("startup script (s)", "startup_script"),
    ("other (o)",         "other"),
)

# unit_number is intercalated between boot_device and processor_number on some
# boards. We treat it separately because the `c` dialogue order is fixed.
FIELDS_WITH_UNIT: tuple[tuple[str, str], ...] = (
    ("boot device",       "boot_device"),
    ("unit number",       "unit_number"),
    ("processor number",  "processor_number"),
    ("host name",         "host_name"),
    ("file name",         "file_name"),
    ("inet on ethernet (e)", "inet_on_ethernet"),
    ("inet on backplane (b)", "inet_on_backplane"),
    ("host inet (h)",     "host_inet"),
    ("gateway inet (g)",  "gateway_inet"),
    ("user (u)",          "user"),
    ("ftp password (pw) (blank = use rsh)", "ftp_password"),
    ("flags (f)",         "flags"),
    ("target name (tn)",  "target_name"),
    ("startup script (s)", "startup_script"),
    ("other (o)",         "other"),
)


PROMPT_RE = re.compile(rb"\[VxWorks Boot\]:\s*$")
# Print-output line: "label : value". Label may contain parens, the value
# extends to end of line. We're permissive about leading whitespace.
PRINT_LINE_RE = re.compile(rb"^\s*([A-Za-z][^:]*?)\s*:\s*(.*?)\s*$", re.MULTILINE)


def _log(msg: str) -> None:
    print(f"[vxworks] {msg}", file=sys.stderr, flush=True)


@dataclass
class ReadResult:
    """Outcome of a `p` round-trip."""
    params: dict[str, str]
    raw: bytes  # full bytes received between sending `p` and the next prompt


@dataclass
class WriteResult:
    """Outcome of a `c` round-trip."""
    raw: bytes
    fields_written: list[str]


def read_params(transport: WTITransport, timeout: float = 4.0) -> ReadResult:
    """Send `p` and parse the response."""
    raw = _command(transport, b"p\r", timeout=timeout)
    return ReadResult(params=_parse_print(raw), raw=raw)


def write_params(
    transport: WTITransport,
    values: dict[str, str],
    timeout_per_field: float = 4.0,
) -> WriteResult:
    """Send `c` and walk the interactive dialogue, writing every known field.

    `values` is keyed by the schema key (`host_name`, `inet_on_ethernet`, ...).

    Semantics per field:
        - Missing key OR empty string -> send Enter (keep current).
        - Literal "." (a single dot)  -> send `.\r` (clear the field on board).
        - Anything else                -> send value + Enter (set to that).

    The dialogue prints each prompt with a trailing ":" then a space, e.g.:

        boot device          : geisc

    On a blank line it just prints "label : <current>" and waits. So we wait
    for the exact label of the next field plus ":" before sending its value,
    and we hard-anchor on the next-prompt boundary so we never overrun the
    dialogue and have our verify "p" leak into a field. After the last field
    the board returns to "[VxWorks Boot]: " — we wait for that explicitly
    before declaring write_params complete.
    """
    raw_buf = bytearray()
    fields_written: list[str] = []

    q = transport.subscribe()
    try:
        transport.write(b"c\r")
        for label, key in FIELDS_WITH_UNIT:
            # Wait for the EXACT label followed by ":" near end-of-buffer.
            # Some firmware versions display the current value after the
            # colon and then wait; others just show the colon. Either way,
            # ": " followed by no further newline is the anchor.
            pat = re.compile(
                rb"(^|[\r\n])" + re.escape(label.encode())
                + rb"[^\r\n:]*:\s*([^\r\n]*)$"
            )
            if _read_until(q, pat, timeout=timeout_per_field, accumulator=raw_buf) is None:
                _log(
                    f"timed out waiting for prompt for {label!r}; aborting write"
                )
                # Try to bail out cleanly by sending ^D (quit).
                try:
                    transport.write(b"\x04")
                except Exception:
                    pass
                break

            raw_value = values.get(key, "")
            if raw_value == "":
                # Keep current.
                transport.write(b"\r")
            elif raw_value == ".":
                transport.write(b".\r")
                fields_written.append(key)
            else:
                transport.write(raw_value.encode() + b"\r")
                fields_written.append(key)

        # After the last field, the boot ROM returns to the [VxWorks Boot]:
        # prompt. Wait for it explicitly so any follow-up command (like the
        # verify `p`) goes to the prompt, not into the dialogue.
        if _read_until(q, PROMPT_RE, timeout=4.0, accumulator=raw_buf) is None:
            _log("did not see [VxWorks Boot]: after dialogue; sending CR to nudge")
            transport.write(b"\r")
            _read_until(q, PROMPT_RE, timeout=2.0, accumulator=raw_buf)
    finally:
        transport.unsubscribe(q)

    return WriteResult(raw=bytes(raw_buf), fields_written=fields_written)


def boot(transport: WTITransport) -> None:
    """Send `@` to boot using the current params."""
    transport.write(b"@\r")


def _command(transport: WTITransport, cmd: bytes, timeout: float) -> bytes:
    """Send a command and read bytes until the next prompt is seen.

    Returns the raw bytes received (including command echo).
    """
    q = transport.subscribe()
    buf = bytearray()
    try:
        transport.write(cmd)
        chunk = _read_until(q, pattern=PROMPT_RE, timeout=timeout, accumulator=buf)
        if chunk is None:
            _log(f"command {cmd!r} timed out after {timeout}s waiting for prompt")
    finally:
        transport.unsubscribe(q)
    return bytes(buf)


def _read_until(q, pattern: re.Pattern[bytes], timeout: float, accumulator: bytearray) -> Optional[bytes]:
    """Drain `q` into `accumulator` until `pattern` matches the tail.

    Returns the matched tail bytes, or None on timeout. Looks at the last 512
    bytes for the pattern so we don't have to re-scan the whole buffer.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        progressed = False
        while q:
            chunk = q.popleft()
            accumulator.extend(chunk)
            progressed = True
        tail = bytes(accumulator[-512:]) if len(accumulator) > 512 else bytes(accumulator)
        if pattern.search(tail):
            return tail
        if not progressed:
            time.sleep(0.02)
    return None


def _parse_print(raw: bytes) -> dict[str, str]:
    """Parse the output of `p` into a {key: value} dict.

    The boot ROM prints lines like `boot device          : geisc`. We map each
    known label to its schema key. Unknown lines are ignored.
    """
    label_to_key = {label.lower(): key for label, key in FIELDS_WITH_UNIT}
    out: dict[str, str] = {}
    for match in PRINT_LINE_RE.finditer(raw):
        label = match.group(1).decode(errors="replace").strip().lower()
        value = match.group(2).decode(errors="replace").strip()
        if label in label_to_key:
            out[label_to_key[label]] = value
    return out
