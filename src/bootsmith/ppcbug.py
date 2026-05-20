"""PPCBug (a.k.a. RTEMS / PPC) boot ROM driver — schema only for now.

The PPCBug debugger uses two configuration commands:

* `ENV` — environment vars (auto-boot enable, debugger flags, etc.).
* `NIOT` — network I/O teach: client IP, server IP, gateway, subnet mask,
  controller LUN/device, boot file name, argument file name.

For our use the NIOT fields are what we actually need to set, because they
control where the IOC boots from. ENV is mostly left at defaults.

This module currently only exposes the field schema — enough for the UI to
render a save/edit form. The interactive command driver (sending `NIOT`,
walking the prompts) lands in a follow-up commit.
"""

from __future__ import annotations


# Ordered list of (label, key). Labels mirror what PPCBug prints in NIOT;
# keys are how Bootsmith stores them in profile.boot_params.
FIELDS: tuple[tuple[str, str], ...] = (
    ("Controller LUN",            "controller_lun"),
    ("Device LUN",                "device_lun"),
    ("Node Control Memory Addr",  "node_control_memory_addr"),
    ("Client IP Address",         "client_ip"),
    ("Server IP Address",         "server_ip"),
    ("Subnet IP Address Mask",    "subnet_mask"),
    ("Broadcast IP Address",      "broadcast_ip"),
    ("Gateway IP Address",        "gateway_ip"),
    ("Boot File Name",            "boot_file_name"),
    ("Argument File Name",        "argument_file_name"),
    ("Boot File Load Address",    "boot_load_address"),
    ("Boot File Execution Address", "boot_exec_address"),
    ("Boot File Execution Delay", "boot_exec_delay"),
    ("Boot File Length",          "boot_file_length"),
    ("Boot File Byte Offset",     "boot_file_offset"),
    ("BOOTP/RARP Request Retry",  "bootp_retry"),
    ("TFTP/ARP Request Retry",    "tftp_retry"),
    ("Trace Character Buffer Address", "trace_buffer_address"),
    ("BOOTP/RARP Request Control", "bootp_control"),
    ("BOOTP/RARP Request Control", "bootp_control_2"),
)
