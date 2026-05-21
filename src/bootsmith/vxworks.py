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
PRINT_LINE_RE = re.compile(rb"^[ \t]*([A-Za-z][^:\r\n]*?)\s*:[ \t]*([^\r\n]*?)[ \t\r]*$", re.MULTILINE)


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


def read_params(transport: WTITransport, timeout: float = 6.0) -> ReadResult:
    """Send `p` and parse the response.

    Sends an extra CR first to make sure we're at a fresh prompt before
    issuing `p`. Without this, if the prior command left echoed bytes in
    the pipeline, `_command`'s prompt-match can short-circuit and miss
    the actual `p` output.
    """
    import time as _t

    try:
        transport.write(b"\r")
    except Exception:
        pass
    _t.sleep(0.3)
    raw = _command(transport, b"p\r", timeout=timeout)
    parsed = _parse_print(raw)
    _log(
        f"read_params: {len(raw)}B captured, parsed {len(parsed)} fields. "
        f"raw[-300:]={raw[-300:]!r}"
    )
    return ReadResult(params=parsed, raw=raw)


def _discover_field_order(transport: WTITransport, timeout: float = 4.0) -> list[tuple[str, str]]:
    """Run `p` and return the (label, key) tuples in the order this firmware
    prints them. Some firmware skips fields (e.g. unit_number on dc devices).
    Returning the actual visible order lets write_params walk the dialogue
    in lock-step without having to identify each prompt by label.
    """
    raw = _command(transport, b"p\r", timeout=timeout)
    label_set = {label: key for label, key in FIELDS_WITH_UNIT}
    out: list[tuple[str, str]] = []
    for line in re.split(rb"[\r\n]+", raw):
        m = re.match(rb"^[ \t]*([A-Za-z][^:\r\n]*?)\s*:[ \t]*(.*?)[ \t\r]*$", line)
        if m is None:
            continue
        label = m.group(1).decode(errors="replace").strip()
        if label in label_set:
            out.append((label, label_set[label]))
    return out


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

    # Discover the field order this firmware actually shows by running `p`
    # first. The schema FIELDS_WITH_UNIT is the union of fields across
    # firmware variants; any specific firmware may show only a subset.
    # Walking the dialogue in the discovered order means we don't have to
    # identify each prompt by label — we KNOW what field is being prompted
    # because we counted them.
    try:
        order = _discover_field_order(transport, timeout=timeout_per_field)
    except Exception as e:
        _log(f"discover_field_order failed: {e}; falling back to full schema")
        order = list(FIELDS_WITH_UNIT)
    _log(f"write_params: discovered field order ({len(order)}): {[k for _, k in order]}")

    end_prompt_re = re.compile(rb"\[VxWorks Boot\]:\s*\Z")
    # A prompt is "label : ..." at end of buffer. We only use it as a
    # "yes a prompt arrived" gate; the label we trust is the one from `order`.
    any_prompt_re = re.compile(rb":\s*[^\r\n]*\Z")

    q = transport.subscribe(seed_history=False)
    last_responded_at_len = 0
    iters = 0
    _log(f"write_params: sending c\\r (values keys: {list(values.keys())})")
    try:
        transport.write(b"c\r")
        for label, key in order:
            iters += 1
            # Wait for the next prompt to arrive: tail ends with ":..."
            # and buffer has grown past last response.
            deadline = time.time() + timeout_per_field
            arrived = False
            dialogue_closed = False
            while time.time() < deadline:
                while q:
                    raw_buf.extend(q.popleft())
                if len(raw_buf) <= last_responded_at_len:
                    time.sleep(0.02)
                    continue
                tail = bytes(raw_buf[-512:])
                if end_prompt_re.search(tail):
                    dialogue_closed = True
                    break
                if any_prompt_re.search(tail):
                    arrived = True
                    break
                time.sleep(0.02)

            if dialogue_closed:
                _log(f"write_params: dialogue closed early after {iters} of {len(order)}")
                break
            if not arrived:
                _log(
                    f"write_params: timed out (iter {iters}/{len(order)}) "
                    f"waiting for prompt for {label!r}; tail={bytes(raw_buf[-200:])!r}"
                )
                try:
                    transport.write(b"\x04")
                except Exception:
                    pass
                break

            raw_value = values.get(key, "")
            _log(f"iter {iters}: prompt for {label!r} (key={key}) -> sending {raw_value!r}")
            if raw_value == "":
                transport.write(b"\r")
            elif raw_value == ".":
                transport.write(b".\r")
                fields_written.append(key)
            else:
                # Slow-type to avoid input-buffer overrun on this firmware.
                for ch in raw_value.encode():
                    transport.write(bytes([ch]))
                    time.sleep(0.015)
                transport.write(b"\r")
                # Wait for the echo of our typed value before considering
                # this field done, so we don't blur into the next prompt.
                want_echo = raw_value.encode()
                edl = time.time() + 4.0
                while time.time() < edl:
                    while q:
                        raw_buf.extend(q.popleft())
                    if want_echo in bytes(raw_buf[-(len(want_echo) + 100):]):
                        break
                    time.sleep(0.02)
                fields_written.append(key)
            last_responded_at_len = len(raw_buf)

        # Drain to the closing [VxWorks Boot]: prompt.
        if not PROMPT_RE.search(bytes(raw_buf[-512:])):
            _read_until(q, PROMPT_RE, timeout=3.0, accumulator=raw_buf)
    finally:
        transport.unsubscribe(q)

    return WriteResult(raw=bytes(raw_buf), fields_written=fields_written)


def boot(transport: WTITransport) -> None:
    """Send `@` to boot using the current params."""
    transport.write(b"@\r")


def _command(transport: WTITransport, cmd: bytes, timeout: float) -> bytes:
    """Send a command and read bytes until the next prompt is seen.

    Requires the command to be echoed back before declaring the next prompt
    match valid — otherwise a stray prompt already in flight on the line
    can short-circuit the read before the command's output arrives.
    """
    q = transport.subscribe(seed_history=False)
    buf = bytearray()
    cmd_echo = cmd.replace(b"\r", b"").replace(b"\n", b"")
    try:
        transport.write(cmd)
        deadline = time.time() + timeout
        saw_echo = False
        while time.time() < deadline:
            while q:
                buf.extend(q.popleft())
            if not saw_echo and cmd_echo and cmd_echo in buf:
                saw_echo = True
            if saw_echo:
                # Look for the closing prompt at the very tail, AFTER the
                # command's echo position. Avoids matching a prompt that
                # was already on the line before we sent the command.
                tail = bytes(buf[-512:])
                if PROMPT_RE.search(tail):
                    return bytes(buf)
            time.sleep(0.02)
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

    We split on EITHER \\r or \\n (not just \\n) because the board's command
    echo (e.g. 'p\\r') can land on the same chunk as the first data line,
    producing 'p\\rboot device : geisc' with no \\n between them. Splitting
    on both characters separately gives us a clean line per item.
    """
    label_to_key = {label.lower(): key for label, key in FIELDS_WITH_UNIT}
    out: dict[str, str] = {}
    for line in re.split(rb"[\r\n]+", raw):
        m = re.match(rb"^[ \t]*([A-Za-z][^:\r\n]*?)\s*:[ \t]*(.*?)[ \t]*$", line)
        if m is None:
            continue
        label = m.group(1).decode(errors="replace").strip().lower()
        value = m.group(2).decode(errors="replace").strip()
        if label in label_to_key:
            out[label_to_key[label]] = value
    return out
