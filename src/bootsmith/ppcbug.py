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
    # Boot behaviour. Gemini sets PReP + SYSFAIL to Y (defaults N);
    # leaves all autoboots at N because IOCs are booted manually.
    "env_prep_boot",
    "env_negate_sysfail",
    "env_auto_boot_enable",
    "env_auto_boot_powerup_only",
    "env_net_auto_boot_enable",
    "env_net_auto_boot_powerup_only",
    # Memory geometry. Ending Address must match physical DIMM size
    # (04000000=64MB, 08000000=128MB, 10000000=256MB). DRAM speed
    # varies by DIMM type (50 vs 60 ns).
    "env_mem_size_end",
    "env_dram_speed",
    # PCI Slave Image map decoders (Universe-II inbound windows).
    # Healthy MVME2700 has slave 0/1/2 all zero (disabled) and slave
    # 3 set to the VME CSR window (C0400000/2FFF0000/30000000/D0000000).
    # Boards that lose battery come back with stale C0xxxxxx values in
    # slaves 1 and 2 that crash RTEMS bus enumeration.
    "env_pci_slave_1_ctrl",
    "env_pci_slave_1_base",
    "env_pci_slave_1_bound",
    "env_pci_slave_1_xlate",
    "env_pci_slave_2_ctrl",
    "env_pci_slave_2_base",
    "env_pci_slave_2_bound",
    "env_pci_slave_2_xlate",
    "env_pci_slave_3_ctrl",
    "env_pci_slave_3_base",
    "env_pci_slave_3_bound",
    "env_pci_slave_3_xlate",
    # VMEbus Slave Image 0 (A32 inbound window from VME to local RAM).
    # Bound must match physical memory size; Gemini uses
    # ctrl=E0F20000, base=00000000, bound=<mem size>, xlate=80000000.
    "env_vme_slave_0_ctrl",
    "env_vme_slave_0_base",
    "env_vme_slave_0_bound",
    "env_vme_slave_0_xlate",
    # VME3PCI / Universe-II top-level config. Working board:
    # master=Y, misc=10000000, master_ctrl=80C00000, misc_ctrl=52060000.
    "env_vme3pci_master",
    "env_pci_misc",
    "env_master_ctrl",
    "env_misc_ctrl",
)

# UI-visible per-dialogue schemas. The connected-mode UI shows NIOT
# and ENV as separate dialogs (one button each in the action bar);
# the home-screen profile editor still uses the combined FIELDS tuple
# so all saved boot_params land in one place.
NIOT_USER_FIELDS: tuple[tuple[str, str], ...] = NIOT_FIELDS
ENV_USER_FIELDS: tuple[tuple[str, str], ...] = tuple(
    (label, key) for label, key in ENV_FIELDS if key in ENV_USER_EDITABLE_KEYS
)
FIELDS: tuple[tuple[str, str], ...] = NIOT_USER_FIELDS + ENV_USER_FIELDS


# Diag-mode commands (entered after `SD` drops us into PPC1-Diag>). Curated
# subset of what `he` lists, focused on basic CPU-board sanity. Each entry
# is (label, key, command-to-send). The Diag button walks this list in order
# and only sends commands whose key is enabled in profile.diag_commands.
DIAG_COMMANDS: tuple[tuple[str, str, str], ...] = (
    # Self-test umbrellas first.
    ("Quick Self Test",                       "diag_qst",       "QST"),
    ("Full Self Test",                        "diag_st",        "ST"),
    # CPU / memory.
    ("RAM tests",                             "diag_ram",       "RAM"),
    ("L2 Cache",                              "diag_l2cache",   "L2CACHE"),
    # Host bridges / VME.
    ("Raven host bridge",                     "diag_raven",     "RAVEN"),
    ("Falcon memory controller",              "diag_falcon",    "FALCON"),
    ("VME2Chip2",                             "diag_vme2",      "VME2"),
    ("VME3 / Universe-II",                    "diag_vme3",      "VME3"),
    ("PCI / PMC generic",                     "diag_pcibus",    "PCIBUS"),
    ("ISA bridge",                            "diag_isabridge", "ISABRDGE"),
    # Storage / network.
    ("EIDE",                                  "diag_eide",      "EIDE"),
    ("NCR 53C8XX SCSI",                       "diag_ncr",       "NCR"),
    ("Ethernet controller (DEC21x4x)",        "diag_dec",       "DEC"),
    # I/O.
    ("Serial I/O (UART)",                     "diag_uart",      "UART"),
    ("Serial controller (Z85C230 SCC)",       "diag_scc",       "SCC"),
    ("Parallel interface (CL1283)",           "diag_cl1283",    "CL1283"),
    ("Parallel interface (PC8730x)",          "diag_par8730x",  "PAR8730X"),
    ("Keyboard/Mouse (8730x)",                "diag_kbd",       "KBD8730X"),
    ("Z8536 counter/timer I/O",               "diag_z8536",     "Z8536"),
    # Misc.
    ("Real-time clock (MK48Txx)",             "diag_rtc",       "RTC"),
    ("VGA controller (GD54XX)",               "diag_vga",       "VGA54XX"),
    # Display-only command; safe to leave checked, just prints errors
    # collected during the preceding tests.
    ("Display errors after run",              "diag_de",        "DE"),
)


# CNFG (board information block) fields, in the order PPCBug prompts
# them under `CNFG;M`. Labels match exactly what the board prints
# before the `=`. Used for one-shot VPD repair after the backup
# battery dies and the VPD EEPROM goes blank. These values are NOT
# saved in the profile -- the user types them once from the sticker
# on the board.
CNFG_FIELDS: tuple[tuple[str, str], ...] = (
    ("Board (PWA) Serial Number", "cnfg_board_sn"),
    ("Board Identifier",          "cnfg_board_id"),
    ("Artwork (PWA) Identifier",  "cnfg_artwork_id"),
    ("MPU Clock Speed",           "cnfg_mpu_clock"),
    ("BUS Clock Speed",           "cnfg_bus_clock"),
    ("Ethernet Address",          "cnfg_mac"),
    ("Primary SCSI Identifier",   "cnfg_scsi_id"),
    ("System Serial Number",      "cnfg_system_sn"),
    ("System Identifier",         "cnfg_system_id"),
    ("License Identifier",        "cnfg_license_id"),
)


PROMPT_RE = re.compile(rb"PPC[0-9](?:-Bug)?>\s*\Z")
DIAG_PROMPT_RE = re.compile(rb"PPC[0-9]-Diag>\s*\Z")


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


def read_niot(transport: WTITransport, timeout: float = 8.0) -> ReadResult:
    """Walk NIOT pressing only Enter; capture current values.

    Sends one bare CR first to make sure we're at a fresh prompt
    before issuing the dialogue command -- same defensive measure as
    vxworks.py. NIOT is ~20 fields so the full walk is quick.
    """
    try:
        transport.write(b"\r")
    except Exception:
        pass
    time.sleep(0.1)
    r = _walk(transport, "NIOT", NIOT_FIELDS, values={}, timeout=timeout,
              stop_after_last_of=None)
    return ReadResult(params=r.params, raw=r.raw)


def write_niot(
    transport: WTITransport,
    values: dict[str, str],
    timeout: float = 8.0,
) -> WriteResult:
    """Walk NIOT to the end, typing values for any matching keys."""
    r = _walk(transport, "NIOT", NIOT_FIELDS, values=values, timeout=timeout,
              stop_after_last_of=None)
    return WriteResult(raw=r.raw, fields_written=r.fields_written)


def read_env(transport: WTITransport, timeout: float = 8.0) -> ReadResult:
    """Walk ENV pressing only Enter; capture the user-editable subset.

    ENV has ~85 fields; we abort with `.` after the last user-editable
    field, because reading the full thing wastes ~30s of round-trips
    on stuff we don't display.
    """
    try:
        transport.write(b"\r")
    except Exception:
        pass
    time.sleep(0.1)
    r = _walk(transport, "ENV", ENV_FIELDS, values={}, timeout=timeout,
              stop_after_last_of=set(ENV_USER_EDITABLE_KEYS))
    return ReadResult(params=r.params, raw=r.raw)


def write_env(
    transport: WTITransport,
    values: dict[str, str],
    timeout: float = 8.0,
) -> WriteResult:
    """Walk ENV to the natural end, typing values for matching keys.

    Aborting with `.` after the last user-editable field rolls back
    changes made mid-dialogue (PPCBug only commits on natural close),
    so we always walk all ~85 fields. Slower (~30s) but values save.
    """
    r = _walk(transport, "ENV", ENV_FIELDS, values=values, timeout=timeout,
              stop_after_last_of=None)
    return WriteResult(raw=r.raw, fields_written=r.fields_written)


def read_cnfg(transport: WTITransport, timeout: float = 12.0) -> ReadResult:
    """Run `CNFG` (read-only) and parse the 10 VPD fields.

    `CNFG` (no `;M`) just dumps the Board Information Block without
    prompting. Each line is `LABEL = VALUE` where VALUE is either a
    quoted ASCII string (`"5195622     "`) or a bare hex string
    (`08003E2E8946`). On a board with a bad checksum the values
    contain garbage characters (`?`, `U`, `E`) but the field labels
    are still recognizable.

    Returns ReadResult.params keyed by CNFG_FIELDS keys; the values
    are stripped of surrounding quotes and trailing spaces.
    """
    q = transport.subscribe(seed_history=False)
    raw_buf = bytearray()
    params: dict[str, str] = {}
    label_to_key = {label: key for label, key in CNFG_FIELDS}

    def drain():
        while q:
            raw_buf.extend(q.popleft())

    try:
        # Send a bare CR so the board's current prompt-echo is flushed
        # into OUR subscriber queue (we subscribed before, so we see
        # everything from now on). Then send CNFG.
        try:
            transport.write(b"\r")
        except Exception:
            pass
        time.sleep(0.2)
        # Drain whatever the bare-CR triggered before we even send
        # CNFG, so the PROMPT_RE tail-match below can't fire on the
        # *pre-CNFG* prompt.
        drain()
        prefix_len = len(raw_buf)
        try:
            transport.write(b"CNFG\r")
        except Exception as e:
            _log(f"read_cnfg: write CNFG failed: {e}")
            return ReadResult(params={}, raw=bytes(raw_buf))

        deadline = time.time() + timeout
        # Wait for CNFG output + closing PPC1-Bug>. CNFG is non-
        # interactive so we just wait for the closing prompt to appear
        # AFTER prefix_len (so we don't trip on the pre-CNFG prompt
        # that may have been re-echoed by the bare CR).
        last_change = time.time()
        last_len = len(raw_buf)
        saw_data = False
        while time.time() < deadline:
            drain()
            if len(raw_buf) != last_len:
                last_len = len(raw_buf)
                last_change = time.time()
                saw_data = True
            # Only check the prompt against bytes added AFTER prefix_len.
            tail = bytes(raw_buf[max(prefix_len, len(raw_buf) - 400):])
            if saw_data and PROMPT_RE.search(tail):
                if time.time() - last_change > 0.3:
                    break
            time.sleep(0.05)
        else:
            _log(
                f"read_cnfg: timed out after {timeout}s; "
                f"buf_len={len(raw_buf)} saw_data={saw_data}"
            )

        # Parse only the data appended after the pre-CNFG prefix.
        text = bytes(raw_buf[prefix_len:]).decode(errors="replace")
        for line in text.splitlines():
            if "=" not in line:
                continue
            label_part, _, value_part = line.partition("=")
            label = label_part.strip()
            key = label_to_key.get(label)
            if key is None:
                continue
            value = value_part.strip()
            # Strip surrounding double quotes if present.
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            # Trim trailing space-padding the board uses to fill the
            # fixed-width field; user-supplied values will be re-padded
            # by the board on write.
            value = value.rstrip()
            params[key] = value

        if len(params) < len(CNFG_FIELDS):
            # Dump a small head/tail sample so a partial read is debuggable.
            sample = text[:400].replace("\r", "\\r").replace("\n", "\\n")
            _log(
                f"read_cnfg: parsed {len(params)}/{len(CNFG_FIELDS)} fields; "
                f"buf_len={len(raw_buf)} prefix_len={prefix_len}; "
                f"sample={sample!r}"
            )
        else:
            _log(f"read_cnfg: parsed {len(params)}/{len(CNFG_FIELDS)} fields")
    finally:
        transport.unsubscribe(q)

    return ReadResult(params=params, raw=bytes(raw_buf))


def write_cnfg(
    transport: WTITransport,
    values: dict[str, str],
    timeout: float = 8.0,
) -> WriteResult:
    """Walk `CNFG;M` once, typing the supplied values into the VPD.

    Same dialogue shape as NIOT/ENV — `LABEL =VALUE?` per field,
    Enter = keep current, type new value + CR = set. After the last
    field PPCBug prints "Update Non-Volatile RAM (Y/N)?" which the
    shared `_walk` answers Y to automatically.

    One-shot operation: values are NOT stored anywhere; the caller is
    expected to assemble them from a per-board form and discard after
    the write.
    """
    return _cnfg_walk(transport, values, timeout)


def _cnfg_walk(
    transport: WTITransport,
    values: dict[str, str],
    timeout: float,
) -> WriteResult:
    """Same as the generic _walk, but issues `CNFG;M` and walks the
    CNFG_FIELDS list to the natural end."""
    result = _walk(
        transport,
        "CNFG;M",
        CNFG_FIELDS,
        values=values,
        timeout=timeout,
        stop_after_last_of=None,
    )
    return WriteResult(raw=result.raw, fields_written=result.fields_written)


def boot(transport: WTITransport) -> None:
    """Issue Network Boot (NBO) — the normal way to boot a Gemini IOC.

    NBO uses the params we just configured via NIOT. The board will
    print the boot sequence on its own; we don't wait for any
    particular response here. The watcher and the WS terminal will
    surface the output to the user.
    """
    transport.write(b"NBO\r")


def diag(transport: WTITransport, enabled_keys: set[str], timeout: float = 300.0) -> None:
    """Run a sequence of PPC1-Diag commands then RESET back to PPC1-Bug>.

    Sends `SD` to switch into diag mode, then for each entry in
    DIAG_COMMANDS whose key is in `enabled_keys`, sends the command
    and waits for the next `PPC1-Diag>` prompt before moving on.
    Finally sends `RESET` to cold-reset the board back to PPC1-Bug>.

    The transport will likely drop briefly during RESET — the watcher
    and session manager handle the reopen; the caller doesn't have to
    do anything special.

    timeout is per-command. Some diag commands (RAM, full ST) can run
    for tens of seconds; the 60s default is generous but not infinite.
    """
    q = transport.subscribe(seed_history=False)
    raw_buf = bytearray()

    def wait_for(pattern: re.Pattern[bytes], deadline: float) -> bool:
        while time.time() < deadline:
            while q:
                raw_buf.extend(q.popleft())
            if pattern.search(bytes(raw_buf[-200:])):
                return True
            time.sleep(0.05)
        return False

    try:
        _log(f"diag: sending SD; enabled keys: {sorted(enabled_keys)}")
        transport.write(b"SD\r")
        if not wait_for(DIAG_PROMPT_RE, time.time() + 5.0):
            _log("diag: timed out waiting for PPC1-Diag> after SD")
            return
        # Reset raw_buf so per-command waits don't see stale prompts.
        raw_buf.clear()

        for label, key, cmd in DIAG_COMMANDS:
            if key not in enabled_keys:
                continue
            _log(f"diag: {label} (sending {cmd!r})")
            transport.write(cmd.encode() + b"\r")
            if not wait_for(DIAG_PROMPT_RE, time.time() + timeout):
                _log(
                    f"diag: {cmd!r} did not return to PPC1-Diag> within "
                    f"{timeout:.0f}s -- continuing to next command anyway"
                )
            raw_buf.clear()

        # `SD` toggles between PPC1-Diag> and PPC1-Bug>. Cleaner than
        # RESET (which itself walks two interactive Y/N prompts).
        _log("diag: sending SD to return to PPC1-Bug>")
        try:
            transport.write(b"SD\r")
        except Exception as e:
            _log(f"diag: SD write failed: {e}")
    finally:
        transport.unsubscribe(q)


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
    # A field prompt is `LABEL =VALUE?` at the END of its line (no trailing
    # newline — the board is waiting for input). We scan one line at a time
    # rather than matching against the tail of raw_buf, because prompts
    # can arrive in bursts and tail-matching skips intermediate prompts.
    field_prompt_re = re.compile(rb"^([^=\r\n]+?)\s*=([^?\r\n]*)\?\s*$")
    save_prompt_substr = b"Update Non-Volatile RAM"
    reset_prompt_substr = b"Reset Local System"
    # For label extraction we grab the largest known label that ends
    # right before the `=` token.
    label_to_key = {label: key for label, key in fields}
    # Longest-first so "BOOTP/RARP Request Control: Always/When-Needed (A/W)"
    # wins over a shorter prefix.
    labels_sorted = sorted(label_to_key, key=len, reverse=True)
    index_of_key = {k: i for i, (_l, k) in enumerate(fields)}

    q = transport.subscribe(seed_history=False)
    # scan_pos: byte offset in raw_buf past which we haven't yet processed
    # input. Every prompt the board emits has its terminating `?` at the
    # end of its line. We find the next `?` past scan_pos (and confirm it
    # ends a line — no more chars after it on that line), classify the
    # prompt, respond, then advance scan_pos past the line.
    scan_pos = 0
    fields_processed = 0
    _log(f"_walk({command}): {len(fields)} fields; values keys present: "
         f"{[k for _, k in fields if k in values and values[k] != '']}")

    def drain():
        while q:
            raw_buf.extend(q.popleft())

    try:
        transport.write(f"{command}\r".encode())

        # Each outer iteration: wait for the next "actionable" event past
        # scan_pos -- either a complete prompt line ending in `?`, or the
        # PPC1-Bug> exit prompt at the tail.
        while True:
            if fields_processed > len(fields) + 8:  # safety bound
                _log(f"_walk({command}): hit safety bound; bailing")
                break

            deadline = time.time() + timeout
            event = None  # ('field', label, current_value, line_end) or ('save',) etc.

            while time.time() < deadline:
                drain()

                # Look for next `?` at or past scan_pos, ON its own line
                # (no chars after it on that line apart from whitespace).
                qpos = raw_buf.find(b"?", scan_pos)
                if qpos >= 0:
                    # Confirm the rest of this line (until line break or
                    # end-of-buffer) is whitespace only. If the line break
                    # hasn't arrived yet that's fine -- the `?` is the
                    # last char so far, meaning the board has stopped
                    # writing and is waiting for input.
                    j = qpos + 1
                    line_break_seen = False
                    valid_prompt = True
                    while j < len(raw_buf):
                        c = raw_buf[j]
                        if c in (0x0A, 0x0D):
                            line_break_seen = True
                            break
                        if c not in (0x20, 0x09):
                            valid_prompt = False
                            break
                        j += 1
                    if valid_prompt:
                        # Extract the line: from the previous line break
                        # (or scan_pos) up to qpos+1.
                        line_start = scan_pos
                        # Find start of this line within raw_buf >= scan_pos.
                        k = qpos
                        while k > scan_pos:
                            if raw_buf[k - 1] in (0x0A, 0x0D):
                                break
                            k -= 1
                        line_start = k
                        line = bytes(raw_buf[line_start : qpos + 1])
                        # Determine where to advance scan_pos past this
                        # prompt. If the line break has arrived, advance
                        # past it; otherwise just past the `?` (the next
                        # bytes will be the board's response to OUR input
                        # plus the next prompt).
                        if line_break_seen:
                            # Skip the \r and any immediately following \n.
                            advance_to = j + 1
                            if raw_buf[j] == 0x0D and j + 1 < len(raw_buf) and raw_buf[j + 1] == 0x0A:
                                advance_to = j + 2
                        else:
                            advance_to = qpos + 1
                        event = ("prompt", line, advance_to)
                        break

                # No prompt yet. Check if the board has returned to
                # PPC1-Bug> (dialogue ended). Look at the tail.
                # BUT: only treat PPC1-Bug> as "closed" if we have
                # already processed at least one field. A PPC1-Bug>
                # in the buffer before the first field-prompt is a
                # leftover from before this _walk began (e.g. the
                # echo of a bare \r after the previous dialogue
                # closed). If we exit on it, we never even see the
                # first ENV prompt.
                if fields_processed > 0 and PROMPT_RE.search(bytes(raw_buf[-200:])):
                    event = ("closed",)
                    break

                time.sleep(0.02)

            if event is None:
                _log(
                    f"_walk({command}): timed out waiting for next prompt "
                    f"after {fields_processed} fields; tail={bytes(raw_buf[-200:])!r}"
                )
                # Try to escape the dialogue cleanly.
                try:
                    transport.write(b".\r")
                except Exception:
                    pass
                break

            if event[0] == "closed":
                _log(f"_walk({command}): dialogue closed (PPC1-Bug>) after {fields_processed} fields")
                break

            # event = ("prompt", line, advance_to)
            line = event[1]
            advance_to = event[2]
            scan_pos = advance_to

            # Classify the prompt.
            if save_prompt_substr in line:
                _log(f"_walk({command}): answering Y to Update NVRAM prompt")
                try:
                    transport.write(b"Y\r")
                except Exception:
                    pass
                continue
            if reset_prompt_substr in line:
                _log(f"_walk({command}): answering N to Reset prompt")
                try:
                    transport.write(b"N\r")
                except Exception:
                    pass
                continue

            # Field prompt. Parse "LABEL =VALUE?".
            m = field_prompt_re.match(line)
            if m is None:
                _log(
                    f"_walk({command}): could not parse field prompt "
                    f"{line!r}; sending Enter to skip"
                )
                try:
                    transport.write(b"\r")
                except Exception:
                    pass
                fields_processed += 1
                continue

            actual_label = m.group(1).decode(errors="replace").strip()
            current_value = m.group(2)

            key = label_to_key.get(actual_label)
            if key is None:
                for l in labels_sorted:
                    if actual_label.startswith(l):
                        key = label_to_key[l]
                        break

            if key is None:
                _log(
                    f"_walk({command}) field {fields_processed + 1}: "
                    f"unknown label {actual_label!r}; keep-current"
                )
            else:
                params[key] = current_value.decode(errors="replace").strip()

            raw_value = values.get(key, "") if key else ""
            if not raw_value:
                transport.write(b"\r")
                _log(
                    f"_walk({command}) field {fields_processed + 1}: "
                    f"{actual_label!r} (key={key}) keep"
                )
            elif raw_value == ".":
                # `.` typed at a PPCBug NIOT/ENV prompt aborts the
                # dialogue. The user is borrowing VxWorks's "clear
                # field" convention. PPCBug accepts the literal string
                # NULL as "no value" ONLY for the boot/argument file-
                # name fields. For everything else (hex addresses,
                # IPs, numeric retry counts) NULL is rejected as an
                # illegal argument. So:
                #   * On a known file-name field -> send NULL.
                #   * Anywhere else -> treat `.` as keep (send Enter).
                FILE_NAME_KEYS = {"boot_file_name", "argument_file_name"}
                if key in FILE_NAME_KEYS:
                    _log(
                        f"_walk({command}) field {fields_processed + 1}: "
                        f"{actual_label!r} value '.' -> sending 'NULL'"
                    )
                    for ch in b"NULL":
                        transport.write(bytes([ch]))
                        time.sleep(0.002)
                    transport.write(b"\r")
                    if key:
                        fields_written.append(key)
                else:
                    _log(
                        f"_walk({command}) field {fields_processed + 1}: "
                        f"{actual_label!r} value '.' on non-file-name "
                        f"field -> keep (PPCBug rejects NULL here)"
                    )
                    transport.write(b"\r")
            else:
                # PPCBug Y/N fields only accept lowercase y/n -- typing
                # uppercase Y leaves the field unchanged. Force lowercase
                # for any single-character Y/N value on a [Y/N] prompt.
                send_value = raw_value
                if (
                    "[Y/N]" in actual_label
                    and len(send_value) == 1
                    and send_value.upper() in ("Y", "N")
                ):
                    send_value = send_value.lower()
                _log(
                    f"_walk({command}) field {fields_processed + 1}: "
                    f"{actual_label!r} (key={key}) -> sending {send_value!r}"
                )
                for ch in send_value.encode():
                    transport.write(bytes([ch]))
                    time.sleep(0.010)
                transport.write(b"\r")
                if key:
                    fields_written.append(key)

            fields_processed += 1

            # Abort after the last user-editable field, if configured.
            if last_target_idx >= 0 and key is not None:
                cur_idx = index_of_key.get(key, -1)
                if cur_idx >= last_target_idx:
                    _log(
                        f"_walk({command}): past last target field "
                        f"(idx {cur_idx} >= {last_target_idx}); will abort "
                        f"with `.` on next prompt"
                    )
                    # Wait for the NEXT prompt to arrive, send `.`.
                    abort_deadline = time.time() + timeout
                    while time.time() < abort_deadline:
                        drain()
                        next_q = raw_buf.find(b"?", scan_pos)
                        if next_q >= 0:
                            try:
                                transport.write(b".\r")
                            except Exception:
                                pass
                            break
                        if PROMPT_RE.search(bytes(raw_buf[-200:])):
                            break
                        time.sleep(0.02)
                    break

        # If we aborted, drain through to PPC1-Bug>. Otherwise we already
        # exited on closed/timeout, but the prompt may still be in flight.
        if not PROMPT_RE.search(bytes(raw_buf[-200:])):
            close_deadline = time.time() + 5.0
            while time.time() < close_deadline:
                drain()
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
