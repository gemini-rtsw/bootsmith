"""PPCBug (Motorola/SMC, a.k.a. "RTEMS / PPC") boot ROM driver.

PPCBug configuration is split across two interactive commands:

* `NIOT` — Network I/O Teach. Sets the network boot params (controller
  LUN, client/server IP, boot file name, etc.). About 20 fields.
* `ENV`  — Environment. Sets the broader boot environment (auto-boot
  flags, VMEbus master config, etc.). ~85 fields. We only edit a few
  boot-related ones, but we still walk the whole dialogue hitting Enter
  on every field we don't care about, because there's no way to skip.

Both dialogues use the same prompt shape:

    Controller LUN =00?
    Boot File Name ("NULL" for None)     =/foo/bar?

i.e. `LABEL =CURRENT?` with the prompt sitting at the end of the line
waiting for input. User responses:

    Enter         keep current value
    <value> + CR  set new value
    . + CR        abort the dialogue (returns to PPC1-Bug>)

There is no per-write "save?" confirmation: PPCBug commits to NVRAM
when the dialogue closes normally. (Verified against MVME2700.)

The driver mirrors the shape of vxworks.py:

    read_params(transport)   -> walk both dialogues, all Enter, parse current
    write_params(transport, values) -> walk both, type values, return raw
    boot(transport)          -> issue Network Boot (NBO) command

`values` is a flat dict keyed by schema key. NIOT keys and ENV keys
live in the same dict; the driver dispatches each to its dialogue.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from typing import Optional

from .transport import WTITransport


# ---------------------------------------------------------------------------
# Field schemas. Each entry: (printed_label, schema_key).
# The printed_label MUST exactly match what PPCBug prints before the `=`,
# including embedded parens. Trailing whitespace before `=` is normalized
# away by the regex.
# ---------------------------------------------------------------------------

# Full NIOT dialogue as observed on MVME2700 (~20 fields).
NIOT_FIELDS: tuple[tuple[str, str], ...] = (
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
    ("BOOTP/RARP Request Control: Always/When-Needed (A/W)", "bootp_request_control"),
    ("BOOTP/RARP Reply Update Control: Yes/No (Y/N)",        "bootp_reply_update"),
)

# ENV dialogue as observed on MVME2700. We walk the entire thing so we
# don't skip-then-corrupt anything, but only these keys are user-editable;
# everything else stays at whatever value the board currently has (Enter).
ENV_FIELDS: tuple[tuple[str, str], ...] = (
    ("Bug or System environment [B/S]",                              "env_bug_or_system"),
    ("Field Service Menu Enable [Y/N]",                              "env_field_service_menu"),
    ("Remote Start Method Switch [G/M/B/N]",                         "env_remote_start_method"),
    ("Probe System for Supported I/O Controllers [Y/N]",             "env_probe_io"),
    ("Auto-Initialize of NVRAM Header Enable [Y/N]",                 "env_auto_init_nvram"),
    ("Network PReP-Boot Mode Enable [Y/N]",                          "env_prep_boot"),
    ("Negate VMEbus SYSFAIL* Always [Y/N]",                          "env_negate_sysfail"),
    ("SCSI Bus Reset on Debugger Startup [Y/N]",                     "env_scsi_bus_reset"),
    ("Primary SCSI Bus Negotiations Type [A/S/N]",                   "env_scsi_neg_type"),
    ("Primary SCSI Data Bus Width [W/N]",                            "env_scsi_bus_width"),
    ("Secondary SCSI Identifier",                                    "env_scsi_id_secondary"),
    ("NVRAM Bootlist (GEV.fw-boot-path) Boot Enable [Y/N]",          "env_nvram_bootlist_enable"),
    ("NVRAM Bootlist (GEV.fw-boot-path) Boot at power-up only [Y/N]","env_nvram_bootlist_powerup"),
    ("NVRAM Bootlist (GEV.fw-boot-path) Boot Abort Delay",           "env_nvram_bootlist_abort_delay"),
    ("Auto Boot Enable [Y/N]",                                       "env_auto_boot_enable"),
    ("Auto Boot at power-up only [Y/N]",                             "env_auto_boot_powerup_only"),
    ("Auto Boot Scan Enable [Y/N]",                                  "env_auto_boot_scan_enable"),
    ("Auto Boot Scan Device Type List",                              "env_auto_boot_scan_list"),
    ("Auto Boot Controller LUN",                                     "env_auto_boot_ctrl_lun"),
    ("Auto Boot Device LUN",                                         "env_auto_boot_dev_lun"),
    ("Auto Boot Partition Number",                                   "env_auto_boot_partition"),
    ("Auto Boot Abort Delay",                                        "env_auto_boot_abort_delay"),
    ("Auto Boot Default String [NULL for an empty string]",          "env_auto_boot_default_string"),
    ("ROM Boot Enable [Y/N]",                                        "env_rom_boot_enable"),
    ("ROM Boot at power-up only [Y/N]",                              "env_rom_boot_powerup"),
    ("ROM Boot Enable search of VMEbus [Y/N]",                       "env_rom_boot_vmebus"),
    ("ROM Boot Abort Delay",                                         "env_rom_boot_abort_delay"),
    ("ROM Boot Direct Starting Address",                             "env_rom_boot_start_addr"),
    ("ROM Boot Direct Ending Address",                               "env_rom_boot_end_addr"),
    ("Network Auto Boot Enable [Y/N]",                               "env_net_auto_boot_enable"),
    ("Network Auto Boot at power-up only [Y/N]",                     "env_net_auto_boot_powerup_only"),
    ("Network Auto Boot Controller LUN",                             "env_net_auto_boot_ctrl_lun"),
    ("Network Auto Boot Device LUN",                                 "env_net_auto_boot_dev_lun"),
    ("Network Auto Boot Abort Delay",                                "env_net_auto_boot_abort_delay"),
    ("Network Auto Boot Configuration Parameters Offset (NVRAM)",    "env_net_auto_boot_cfg_offset"),
    ("Memory Size Enable [Y/N]",                                     "env_mem_size_enable"),
    ("Memory Size Starting Address",                                 "env_mem_size_start"),
    ("Memory Size Ending Address",                                   "env_mem_size_end"),
    ("DRAM Speed in NANO Seconds",                                   "env_dram_speed"),
    ("ROM First Access Length (0 - 31)",                             "env_rom_first_access"),
    ("ROM Next Access Length  (0 - 15)",                             "env_rom_next_access"),
    ("DRAM Parity Enable [On-Detection/Always/Never - O/A/N]",       "env_dram_parity"),
    ("L2Cache Parity Enable [On-Detection/Always/Never - O/A/N]",    "env_l2cache_parity"),
    ("PCI Interrupts Route Control Registers (PIRQ0/1/2/3)",         "env_pci_pirq"),
    ("Serial Startup Code Master Enable [Y/N]",                      "env_serial_startup_master"),
    ("Serial Startup Code LF Enable [Y/N]",                          "env_serial_startup_lf"),
    ("VME3PCI Master Master Enable [Y/N]",                           "env_vme3pci_master"),
    ("PCI Slave Image 0 Control",                                    "env_pci_slave_0_ctrl"),
    ("PCI Slave Image 0 Base Address Register",                      "env_pci_slave_0_base"),
    ("PCI Slave Image 0 Bound Address Register",                     "env_pci_slave_0_bound"),
    ("PCI Slave Image 0 Translation Offset",                         "env_pci_slave_0_xlate"),
    ("PCI Slave Image 1 Control",                                    "env_pci_slave_1_ctrl"),
    ("PCI Slave Image 1 Base Address Register",                      "env_pci_slave_1_base"),
    ("PCI Slave Image 1 Bound Address Register",                     "env_pci_slave_1_bound"),
    ("PCI Slave Image 1 Translation Offset",                         "env_pci_slave_1_xlate"),
    ("PCI Slave Image 2 Control",                                    "env_pci_slave_2_ctrl"),
    ("PCI Slave Image 2 Base Address Register",                      "env_pci_slave_2_base"),
    ("PCI Slave Image 2 Bound Address Register",                     "env_pci_slave_2_bound"),
    ("PCI Slave Image 2 Translation Offset",                         "env_pci_slave_2_xlate"),
    ("PCI Slave Image 3 Control",                                    "env_pci_slave_3_ctrl"),
    ("PCI Slave Image 3 Base Address Register",                      "env_pci_slave_3_base"),
    ("PCI Slave Image 3 Bound Address Register",                     "env_pci_slave_3_bound"),
    ("PCI Slave Image 3 Translation Offset",                         "env_pci_slave_3_xlate"),
    ("VMEbus Slave Image 0 Control",                                 "env_vme_slave_0_ctrl"),
    ("VMEbus Slave Image 0 Base Address Register",                   "env_vme_slave_0_base"),
    ("VMEbus Slave Image 0 Bound Address Register",                  "env_vme_slave_0_bound"),
    ("VMEbus Slave Image 0 Translation Offset",                      "env_vme_slave_0_xlate"),
    ("VMEbus Slave Image 1 Control",                                 "env_vme_slave_1_ctrl"),
    ("VMEbus Slave Image 1 Base Address Register",                   "env_vme_slave_1_base"),
    ("VMEbus Slave Image 1 Bound Address Register",                  "env_vme_slave_1_bound"),
    ("VMEbus Slave Image 1 Translation Offset",                      "env_vme_slave_1_xlate"),
    ("VMEbus Slave Image 2 Control",                                 "env_vme_slave_2_ctrl"),
    ("VMEbus Slave Image 2 Base Address Register",                   "env_vme_slave_2_base"),
    ("VMEbus Slave Image 2 Bound Address Register",                  "env_vme_slave_2_bound"),
    ("VMEbus Slave Image 2 Translation Offset",                      "env_vme_slave_2_xlate"),
    ("VMEbus Slave Image 3 Control",                                 "env_vme_slave_3_ctrl"),
    ("VMEbus Slave Image 3 Base Address Register",                   "env_vme_slave_3_base"),
    ("VMEbus Slave Image 3 Bound Address Register",                  "env_vme_slave_3_bound"),
    ("VMEbus Slave Image 3 Translation Offset",                      "env_vme_slave_3_xlate"),
    ("PCI Miscellaneous Register",                                   "env_pci_misc"),
    ("Special PCI Slave Image Register",                             "env_pci_special_slave"),
    ("Master Control Register",                                      "env_master_ctrl"),
    ("Miscellaneous Control Register",                               "env_misc_ctrl"),
    ("User AM Codes",                                                "env_user_am_codes"),
)

# What the UI exposes to the user. NIOT first, then the *user-relevant*
# ENV fields (the ones Gemini sets after a battery removal). The driver
# still walks all of ENV under the hood; this is just the schema for the
# edit form.
ENV_USER_EDITABLE_KEYS: tuple[str, ...] = (
    "env_auto_boot_enable",
    "env_auto_boot_powerup_only",
    "env_net_auto_boot_enable",
    "env_net_auto_boot_powerup_only",
)

# UI-visible schema = NIOT fields + the subset of ENV the user edits.
FIELDS: tuple[tuple[str, str], ...] = NIOT_FIELDS + tuple(
    (label, key) for label, key in ENV_FIELDS if key in ENV_USER_EDITABLE_KEYS
)


PROMPT_RE = re.compile(rb"PPC[0-9](?:-Bug)?>\s*\Z")


def _log(msg: str) -> None:
    print(f"[ppcbug] {msg}", file=sys.stderr, flush=True)


@dataclass
class ReadResult:
    params: dict[str, str]
    raw: bytes


@dataclass
class WriteResult:
    raw: bytes
    fields_written: list[str]


def read_params(transport: WTITransport, timeout: float = 8.0) -> ReadResult:
    """Walk NIOT and ENV pressing only Enter; capture current values.

    NIOT is walked fully (only ~20 fields). ENV is walked only as far
    as needed to capture the user-editable subset, then aborted with
    `.` — walking all ~85 ENV fields would take ~30s of round-trips
    and we don't display the rest.

    Sends one bare CR first to make sure we're at a fresh prompt
    before issuing the dialogue command — same defensive measure as
    vxworks.py.
    """
    try:
        transport.write(b"\r")
    except Exception:
        pass
    time.sleep(0.2)
    niot = _walk(transport, "NIOT", NIOT_FIELDS, values={}, timeout=timeout,
                 stop_after_last_of=None)
    try:
        transport.write(b"\r")
    except Exception:
        pass
    time.sleep(0.2)
    env = _walk(transport, "ENV", ENV_FIELDS, values={}, timeout=timeout,
                stop_after_last_of=set(ENV_USER_EDITABLE_KEYS))
    merged_params: dict[str, str] = {}
    merged_params.update(niot.params)
    merged_params.update(env.params)
    return ReadResult(params=merged_params, raw=niot.raw + env.raw)


def write_params(
    transport: WTITransport,
    values: dict[str, str],
    timeout: float = 8.0,
) -> WriteResult:
    """Walk NIOT (fully) and ENV (only up to the last user-editable field).

    For ENV, after the last user-editable field is processed we send
    `.` to abort the dialogue. PPCBug preserves changes made up to that
    point on abort, so this is safe and skips ~55 unnecessary prompts.
    """
    niot = _walk(transport, "NIOT", NIOT_FIELDS, values=values, timeout=timeout,
                 stop_after_last_of=None)
    try:
        transport.write(b"\r")
    except Exception:
        pass
    time.sleep(0.2)
    env = _walk(transport, "ENV", ENV_FIELDS, values=values, timeout=timeout,
                stop_after_last_of=set(ENV_USER_EDITABLE_KEYS))
    return WriteResult(
        raw=niot.raw + env.raw,
        fields_written=niot.fields_written + env.fields_written,
    )


def boot(transport: WTITransport) -> None:
    """Issue Network Boot (NBO) — the normal way to boot a Gemini IOC.

    NBO uses the params we just configured via NIOT. The board will
    print the boot sequence on its own; we don't wait for any
    particular response here. The watcher and the WS terminal will
    surface the output to the user.
    """
    transport.write(b"NBO\r")


@dataclass
class _DialogueResult:
    raw: bytes
    params: dict[str, str]
    fields_written: list[str]


def _last_line_start(buf: bytes) -> int:
    """Index of the start of the last line in buf, treating either
    \\r or \\n as a line separator. PPCBug observed to emit \\r\\n."""
    i = max(buf.rfind(b"\n"), buf.rfind(b"\r"))
    return i + 1 if i >= 0 else 0


def _walk(
    transport: WTITransport,
    command: str,
    fields: tuple[tuple[str, str], ...],
    values: dict[str, str],
    timeout: float,
    stop_after_last_of: Optional[set[str]] = None,
) -> _DialogueResult:
    """Drive one PPCBug interactive dialogue (NIOT or ENV).

    Uses the same prompt-line-anchor pattern as vxworks.write_params:
    track the byte offset where the just-responded prompt's line began.
    The next prompt is valid when the last line in raw_buf starts
    strictly after that offset.

    If `stop_after_last_of` is given, we abort the dialogue (send `.`)
    after processing the last field whose key is in that set. PPCBug
    preserves prior changes on abort. If None, we walk to the end.
    """
    # Index of the last targeted field in `fields`. After we've handled
    # that index, abort the dialogue. -1 means "no abort, walk to end".
    last_target_idx = -1
    if stop_after_last_of:
        for i, (_l, k) in enumerate(fields):
            if k in stop_after_last_of:
                last_target_idx = i
    raw_buf = bytearray()
    params: dict[str, str] = {}
    fields_written: list[str] = []

    # Build a single regex that recognizes any field's prompt at the end
    # of the buffer. We can't gate the loop on a SPECIFIC label because
    # we want to detect dialogue closure (returning to PPC1-Bug>) too.
    pending_prompt_re = re.compile(rb"=([^?\r\n]*)\?\s*\Z")
    # PPCBug emits "Update Non-Volatile RAM (Y/N)?" at the end of NIOT
    # (and sometimes ENV) when values were changed. We always answer Y
    # to commit. We could also see "Reset Local System (Y/N)?" after
    # ENV — we always say N (we don't want a reset; the user will boot
    # explicitly afterward).
    save_prompt_re = re.compile(rb"Update Non-Volatile RAM\s*\(Y/N\)\?\s*\Z")
    reset_prompt_re = re.compile(rb"Reset Local System\s*\(Y/N\)\?\s*\Z")
    # For label extraction we grab the largest known label that ends
    # right before the `=` token.
    label_to_key = {label: key for label, key in fields}
    # Longest-first so "BOOTP/RARP Request Control: Always/When-Needed (A/W)"
    # wins over a shorter prefix.
    labels_sorted = sorted(label_to_key, key=len, reverse=True)
    index_of_key = {k: i for i, (_l, k) in enumerate(fields)}

    q = transport.subscribe(seed_history=False)
    prev_prompt_start = -1
    iters = 0
    _log(f"_walk({command}): {len(fields)} fields; values keys present: "
         f"{[k for _, k in fields if k in values and values[k] != '']}")

    try:
        transport.write(f"{command}\r".encode())

        while iters < len(fields) + 4:  # +4 slack for stray prompts
            iters += 1
            # Wait for the next prompt or dialogue close.
            deadline = time.time() + timeout
            arrived = False
            dialogue_closed = False
            cur_prompt_start = -1
            current_value: bytes = b""
            while time.time() < deadline:
                while q:
                    raw_buf.extend(q.popleft())
                tail = bytes(raw_buf[-512:])
                if PROMPT_RE.search(tail):
                    dialogue_closed = True
                    break
                # Handle PPCBug's "Update NVRAM?" / "Reset?" prompts
                # that can appear before or instead of the next field.
                if save_prompt_re.search(tail):
                    _log(f"_walk({command}): answering Y to Update NVRAM prompt")
                    try:
                        transport.write(b"Y\r")
                    except Exception:
                        pass
                    # Don't break the loop; keep waiting for either the
                    # next field prompt or PPC1-Bug>. Advance prev_prompt
                    # _start past this line so we don't re-match it.
                    prev_prompt_start = _last_line_start(bytes(raw_buf))
                    # Small grace period to let the response be echoed
                    # before the next prompt arrives.
                    time.sleep(0.1)
                    continue
                if reset_prompt_re.search(tail):
                    _log(f"_walk({command}): answering N to Reset prompt")
                    try:
                        transport.write(b"N\r")
                    except Exception:
                        pass
                    prev_prompt_start = _last_line_start(bytes(raw_buf))
                    time.sleep(0.1)
                    continue
                m = pending_prompt_re.search(tail)
                if m is not None:
                    line_start = _last_line_start(bytes(raw_buf))
                    if line_start > prev_prompt_start:
                        cur_prompt_start = line_start
                        current_value = m.group(1)
                        arrived = True
                        break
                time.sleep(0.02)

            if dialogue_closed:
                _log(f"_walk({command}): dialogue closed after {iters - 1} prompts")
                break
            if not arrived:
                _tail = bytes(raw_buf[-200:])
                _log(
                    f"_walk({command}): timed out (iter {iters}/{len(fields)}) "
                    f"prev_prompt_start={prev_prompt_start} "
                    f"last_line_start={_last_line_start(bytes(raw_buf))} "
                    f"tail={_tail!r}"
                )
                # Abort the dialogue so we don't leave the board mid-edit.
                try:
                    transport.write(b".\r")
                except Exception:
                    pass
                break

            # Identify the label at this prompt. The prompt sits on a
            # single line; the label is everything from the line start
            # up to the `=` (with trailing whitespace trimmed).
            #
            # PPCBug emits prompts very fast, so by the time we get
            # here raw_buf may already contain the NEXT prompt (or
            # several) past cur_prompt_start. Cut the slice at the
            # first line-break to isolate just this prompt's line.
            prompt_slice = bytes(raw_buf[cur_prompt_start:])
            nl_pos = -1
            for sep in (b"\r", b"\n"):
                p = prompt_slice.find(sep)
                if p >= 0 and (nl_pos < 0 or p < nl_pos):
                    nl_pos = p
            if nl_pos >= 0:
                prompt_slice = prompt_slice[:nl_pos]
            eq_pos = prompt_slice.find(b"=")
            actual_label = (
                prompt_slice[:eq_pos].decode(errors="replace").rstrip()
                if eq_pos >= 0
                else prompt_slice.decode(errors="replace")
            )
            # Also re-extract current_value from this line specifically
            # (the regex on the buffer tail may have matched a later
            # prompt's value, not this one's).
            if eq_pos >= 0:
                q_pos = prompt_slice.find(b"?", eq_pos)
                if q_pos >= 0:
                    current_value = prompt_slice[eq_pos + 1 : q_pos]

            key = label_to_key.get(actual_label)
            if key is None:
                # Try a loose match: a known label is a prefix of the
                # printed label (PPCBug pads with spaces before `=`).
                for l in labels_sorted:
                    if actual_label.startswith(l):
                        key = label_to_key[l]
                        break
            if key is None:
                _log(
                    f"_walk({command}) iter {iters}: unknown label "
                    f"{actual_label!r}; treating as keep-current"
                )
            else:
                params[key] = current_value.decode(errors="replace").strip()

            raw_value = values.get(key, "") if key else ""
            if raw_value == "" or raw_value is None:
                transport.write(b"\r")
                _log(f"_walk({command}) iter {iters}: {actual_label!r} (key={key}) keep")
            elif raw_value == ".":
                # `.` inside the NIOT/ENV dialogue means abort, not clear.
                # We don't expose dot-clear semantics for PPCBug.
                _log(
                    f"_walk({command}) iter {iters}: {actual_label!r} (key={key}) "
                    f"'.' requested but treating as keep (PPCBug `.` aborts dialogue)"
                )
                transport.write(b"\r")
            else:
                _log(
                    f"_walk({command}) iter {iters}: {actual_label!r} (key={key}) "
                    f"-> sending {raw_value!r}"
                )
                # Slow-type to be gentle on the firmware's input buffer
                # (matches the VxWorks driver's approach; not strictly
                # known to be needed for PPCBug, but cheap insurance).
                for ch in raw_value.encode():
                    transport.write(bytes([ch]))
                    time.sleep(0.010)
                transport.write(b"\r")
                if key:
                    fields_written.append(key)
            prev_prompt_start = cur_prompt_start

            # If we're past the last targeted field, abort the rest of
            # the dialogue with `.` rather than walking every remaining
            # prompt. PPCBug saves the changes made so far on abort.
            if last_target_idx >= 0 and key is not None:
                cur_idx = index_of_key.get(key, -1)
                if cur_idx >= last_target_idx:
                    _log(
                        f"_walk({command}): past last target field "
                        f"(idx {cur_idx} >= {last_target_idx}); aborting with `.`"
                    )
                    # Wait for the NEXT prompt to arrive, then send `.`.
                    # Sending it at the current prompt-just-responded
                    # moment would race against the board printing the
                    # next prompt; cleaner to wait for that prompt.
                    abort_deadline = time.time() + timeout
                    while time.time() < abort_deadline:
                        while q:
                            raw_buf.extend(q.popleft())
                        tail = bytes(raw_buf[-512:])
                        if PROMPT_RE.search(tail):
                            # Dialogue closed on its own (last field
                            # really was the last in the schema).
                            break
                        m = pending_prompt_re.search(tail)
                        if m is not None:
                            line_start = _last_line_start(bytes(raw_buf))
                            if line_start > prev_prompt_start:
                                try:
                                    transport.write(b".\r")
                                except Exception:
                                    pass
                                break
                        time.sleep(0.02)
                    break

        # Drain to the closing PPC1-Bug> prompt.
        if not PROMPT_RE.search(bytes(raw_buf[-200:])):
            deadline = time.time() + 3.0
            while time.time() < deadline:
                while q:
                    raw_buf.extend(q.popleft())
                if PROMPT_RE.search(bytes(raw_buf[-200:])):
                    break
                time.sleep(0.02)

    finally:
        transport.unsubscribe(q)

    return _DialogueResult(
        raw=bytes(raw_buf),
        params=params,
        fields_written=fields_written,
    )
