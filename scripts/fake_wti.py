#!/usr/bin/env python3
"""Fake WTI/board for local Bootsmith testing.

Run it, then add a Bootsmith profile pointing at 127.0.0.1:<port>.
The fake server pretends a board is running an EPICS IOC. Press Enter in
this terminal to simulate a reboot — it will emit the boot banner, a short
countdown, and finally drop to the loader prompt. If Bootsmith spams the
abort key during the countdown, the fake server "halts" early and goes
straight to the prompt. If it doesn't, the fake server "auto-boots" back
into the IOC so you can test the missed-abort flow.

Usage:
    python3 scripts/fake_wti.py --port 9991 --loader vxworks
    python3 scripts/fake_wti.py --port 9992 --loader ppcbug
"""

from __future__ import annotations

import argparse
import select
import socket
import sys
import threading
import time

BANNERS = {
    "vxworks": (
        b"\r\n"
        b"VxWorks System Boot\r\n"
        b"\r\n"
        b"Copyright 1984-1998  Wind River Systems, Inc.\r\n"
        b"\r\n"
        b"CPU:    MVME2700 - PowerPC 750\r\n"
        b"Version:  5.4\r\n"
        b"BSP version: 1.2/1\r\n"
        b"Creation date: Jan 14 1999, 10:23:45\r\n"
        b"\r\n"
        b"Press any key to stop auto-boot...\r\n"
    ),
    "ppcbug": (
        b"\r\n"
        b"PPC1-Bug Debugger/Diagnostics Release Version 4.2 - 06/15/01 RM01\r\n"
        b"COLD Start\r\n"
        b"Local Memory Found =04000000 (&67108864)\r\n"
        b"MPU Clock Speed =367Mhz\r\n"
        b"Reset Status Register =80000000\r\n"
        b"\r\n"
        b"Copyright Motorola Inc. 1988 - 2001, All Rights Reserved\r\n"
        b"\r\n"
        b"Press <ESC> to bypass, <SPC> to continue\r\n"
    ),
}

PROMPTS = {
    "vxworks": b"\r\n[VxWorks Boot]: ",
    "ppcbug": b"\r\nPPC1-Bug>",
}

# What the loader expects as the abort character. Used only for the
# "did the client try to halt me?" check.
ABORT_CHARS = {
    "vxworks": None,   # any character halts
    "ppcbug": b"\x1b", # Esc
}


def serve_one(conn: socket.socket, addr, loader: str, countdown: int) -> None:
    print(f"[fake-wti] client {addr} connected", flush=True)
    conn.sendall(b"epics-ioc> hello from fake target\r\n")

    # Wait for the operator (you) to press Enter on this script to trigger a "reboot".
    print(f"[fake-wti] press Enter in THIS terminal to reboot the fake board "
          f"(loader={loader})", flush=True)

    # Heartbeat the IOC line while we wait so Bootsmith's live view shows life.
    stop_heartbeat = threading.Event()

    def heartbeat() -> None:
        i = 0
        while not stop_heartbeat.is_set():
            try:
                conn.sendall(f"epics-ioc> heartbeat {i}\r\n".encode())
            except OSError:
                return
            i += 1
            stop_heartbeat.wait(2.0)

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()

    try:
        sys.stdin.readline()
    except KeyboardInterrupt:
        stop_heartbeat.set()
        return
    stop_heartbeat.set()
    hb.join(timeout=0.5)

    # Reboot sequence ----------------------------------------------------
    conn.sendall(b"\r\n*** reboot ***\r\n")
    time.sleep(0.3)
    conn.sendall(BANNERS[loader])

    # Countdown phase — listen for an abort key. Any byte halts for vxworks;
    # specifically Esc for ppcbug.
    expected = ABORT_CHARS[loader]
    halted = False
    conn.setblocking(False)
    deadline = time.time() + countdown
    last_tick = 0
    while time.time() < deadline:
        remaining = int(deadline - time.time())
        if remaining != last_tick:
            try:
                conn.sendall(f"  {remaining}\r\n".encode())
            except OSError:
                return
            last_tick = remaining
        r, _, _ = select.select([conn], [], [], 0.1)
        if r:
            try:
                data = conn.recv(64)
            except OSError:
                return
            if not data:
                print("[fake-wti] client closed during countdown", flush=True)
                return
            if expected is None or expected in data:
                halted = True
                print(f"[fake-wti] halted by client (got {data!r})", flush=True)
                break

    if not halted:
        # Auto-boot back into the IOC — simulates missed abort.
        print("[fake-wti] auto-boot expired; back to IOC", flush=True)
        try:
            conn.sendall(b"\r\nauto-booting...\r\n")
            time.sleep(0.5)
            conn.sendall(b"epics-ioc> hello again from fake target\r\n")
            # Keep the connection alive so Bootsmith can see the missed state in the UI.
            while True:
                try:
                    conn.sendall(b"epics-ioc> heartbeat\r\n")
                except OSError:
                    return
                time.sleep(2)
        except OSError:
            return

    # Loader prompt ------------------------------------------------------
    try:
        conn.sendall(PROMPTS[loader])
    except OSError:
        return

    # Echo whatever the client types so you can poke at the "prompt" manually.
    conn.setblocking(True)
    conn.settimeout(0.5)
    try:
        while True:
            try:
                data = conn.recv(256)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            try:
                conn.sendall(data)
            except OSError:
                return
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--loader", choices=("vxworks", "ppcbug"), default="vxworks")
    ap.add_argument("--countdown", type=int, default=7,
                    help="seconds the loader will wait for an abort key")
    args = ap.parse_args()

    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((args.host, args.port))
    s.listen(1)
    print(f"[fake-wti] listening on {args.host}:{args.port} (loader={args.loader})",
          flush=True)

    try:
        while True:
            conn, addr = s.accept()
            try:
                serve_one(conn, addr, args.loader, args.countdown)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
            print("[fake-wti] ready for next connection", flush=True)
    except KeyboardInterrupt:
        print("\n[fake-wti] bye", flush=True)


if __name__ == "__main__":
    main()
