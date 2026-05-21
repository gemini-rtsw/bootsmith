"""PPCBug (a.k.a. RTEMS / PPC) boot ROM driver.

PPCBug uses two configuration commands:

* `ENV` — environment vars (auto-boot enable, debugger flags, etc.).
* `NIOT` — network I/O teach: client IP, server IP, gateway, subnet mask,
  controller LUN/device, boot file name, argument file name.

For our use NIOT is what controls where the IOC boots from. The interactive
NIOT dialogue prints each field on its own line, e.g.:

    Controller LUN                  =00?
    Device LUN                      =00?
    Node Control Memory Address     =FFE10000?
    Client IP Address               =10.2.2.105?
    Server IP Address               =10.2.71.30?
    Subnet IP Address Mask          =255.255.255.0?
    Broadcast IP Address            =255.255.255.255?
    Gateway IP Address              =10.2.2.1?
    Boot File Name ("NULL" for None)=/gem_prod/epics/ioc/mcs_mk/mcs.boot?
    Argument File Name ("NULL" for None)=/gem_prod/epics/ioc/mcs_mk/stmcs?

User types: Enter to keep, new value + Enter to set, "." + Enter to clear.

PPCBug returns to the `PPC1-Bug>` prompt at the end of the dialogue.

The driver matches the same shape Bootsmith uses for VxWorks: read_params
sends `NIOT` with all-Enter to walk-and-display, write_params sends `NIOT`
walking the prompts with our values.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from typing import Optional

from .transport import WTITransport


# Ordered list of (label, key). Labels are the literal text PPCBug prints in
# the NIOT dialogue (minus the `=value?` part). Keys are how Bootsmith stores
# them in profile.boot_params.
FIELDS: tuple[tuple[str, str], ...] = (
    ("Controller LUN",                              "controller_lun"),
    ("Device LUN",                                  "device_lun"),
    ("Node Control Memory Address",                 "node_control_memory_addr"),
    ("Client IP Address",                           "client_ip"),
    ("Server IP Address",                           "server_ip"),
    ("Subnet IP Address Mask",                      "subnet_mask"),
    ("Broadcast IP Address",                        "broadcast_ip"),
    ("Gateway IP Address",                          "gateway_ip"),
    ('Boot File Name ("NULL" for None)',            "boot_file_name"),
    ('Argument File Name ("NULL" for None)',        "argument_file_name"),
    ("Boot File Load Address",                      "boot_load_address"),
    ("Boot File Execution Address",                 "boot_exec_address"),
    ("Boot File Execution Delay",                   "boot_exec_delay"),
    ("Boot File Length",                            "boot_file_length"),
    ("Boot File Byte Offset",                       "boot_file_offset"),
    ("BOOTP/RARP Request Retry",                    "bootp_retry"),
    ("TFTP/ARP Request Retry",                      "tftp_retry"),
    ("Trace Character Buffer Address",              "trace_buffer_address"),
    ("BOOTP/RARP Request Control",                  "bootp_control"),
)


PROMPT_RE = re.compile(rb"PPC[0-9](?:-Bug)?>\s*\Z")
# A pending NIOT prompt: "<label> =<current_value>?" with no trailing newline.
# We build a per-field anchor on the fly because labels contain special chars.


@dataclass
class ReadResult:
    params: dict[str, str]
    raw: bytes


@dataclass
class WriteResult:
    raw: bytes
    fields_written: list[str]


def _log(msg: str) -> None:
    print(f"[ppcbug] {msg}", file=sys.stderr, flush=True)


def read_params(transport: WTITransport, timeout: float = 8.0) -> ReadResult:
    """Walk the NIOT dialogue pressing only Enter at each prompt.

    The dialogue prints each `Label =CURRENT?` line as we Enter through.
    We capture the bytes and parse them. At the end PPCBug returns to
    `PPC1-Bug>`.
    """
    try:
        transport.write(b"\r")
    except Exception:
        pass
    time.sleep(0.2)
    return _walk(transport, values={}, timeout=timeout, want_writes=False)


def write_params(transport: WTITransport, values: dict[str, str], timeout: float = 8.0) -> WriteResult:
    """Walk the NIOT dialogue with the given values."""
    return _walk(transport, values=values, timeout=timeout, want_writes=True)


def boot(transport: WTITransport) -> None:
    """Send `G <address>` would normally boot. For network boot the user
    typically just types `nbo` (Network Boot) — but the exact verb is
    board-dependent. We don't auto-boot from the UI for PPCBug yet; the
    Boot button is wired up but the implementation is intentionally a
    no-op until we can test against a real RTEMS/PPC target.
    """
    _log("boot() called but no-op until tested on real PPCBug target")


def _walk(
    transport: WTITransport,
    values: dict[str, str],
    timeout: float,
    want_writes: bool,
) -> ReadResult | WriteResult:
    raw_buf = bytearray()
    fields_written: list[str] = []
    captured: dict[str, str] = {}

    label_to_key = {label: key for label, key in FIELDS}
    labels_sorted = sorted((l for l, _ in FIELDS), key=len, reverse=True)
    labels_alt = b"|".join(re.escape(l.encode()) for l in labels_sorted)
    pending_prompt_re = re.compile(
        rb"(" + labels_alt + rb")\s*=([^?\r\n]*)\?\s*\Z"
    )

    q = transport.subscribe(seed_history=False)
    last_label: Optional[bytes] = None
    try:
        transport.write(b"NIOT\r")
        for _ in range(64):
            deadline = time.time() + timeout
            label_match: Optional[bytes] = None
            current_value: Optional[bytes] = None
            dialogue_closed = False
            while time.time() < deadline:
                while q:
                    raw_buf.extend(q.popleft())
                tail = bytes(raw_buf[-512:])
                if PROMPT_RE.search(tail):
                    dialogue_closed = True
                    break
                m = pending_prompt_re.search(tail)
                if m is not None:
                    candidate = m.group(1)
                    if candidate != last_label:
                        label_match = candidate
                        current_value = m.group(2)
                        break
                time.sleep(0.02)

            if dialogue_closed:
                break
            if label_match is None:
                _log("timed out waiting for next NIOT prompt; bailing")
                break

            label = label_match.decode()
            key = label_to_key.get(label)
            if current_value is not None and key is not None:
                captured[key] = current_value.decode(errors="replace").strip()

            raw_value = values.get(key, "") if key else ""
            if raw_value == "":
                transport.write(b"\r")
            elif raw_value == ".":
                transport.write(b".\r")
                if want_writes and key:
                    fields_written.append(key)
            else:
                transport.write(raw_value.encode() + b"\r")
                if want_writes and key:
                    fields_written.append(key)
            last_label = label_match

    finally:
        transport.unsubscribe(q)

    if want_writes:
        return WriteResult(raw=bytes(raw_buf), fields_written=fields_written)
    return ReadResult(raw=bytes(raw_buf), params=captured)
