from __future__ import annotations

import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

# Telnet IAC constants. Some WTI ports speak telnet (not raw) and refuse to
# forward serial bytes until the client responds to their option negotiation.
# We handle that by refusing every option — DONT to every DO, WONT to every
# WILL. That gets the WTI to stop waiting on us and start forwarding bytes.
IAC = 0xFF
DONT = 0xFE
DO = 0xFD
WONT = 0xFC
WILL = 0xFB
SB = 0xFA
SE = 0xF0


def _consume_iac(buf: bytes) -> tuple[bytes, bytes]:
    """Pull IAC sequences out of `buf`.

    Returns (clean_bytes, reply_bytes) where clean_bytes is the buf with IAC
    sequences removed and reply_bytes is the telnet negotiation reply we
    should send back to keep the peer happy.
    """
    if IAC not in buf:
        return buf, b""
    out = bytearray()
    reply = bytearray()
    i = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            # Truncated IAC at end of chunk — leave it for next read.
            # In practice we just drop it; reassembly across chunks is rare here.
            break
        cmd = buf[i + 1]
        if cmd in (DO, DONT, WILL, WONT) and i + 2 < n:
            opt = buf[i + 2]
            # Refuse everything: DO/DONT -> WONT, WILL/WONT -> DONT.
            if cmd in (DO, DONT):
                reply.extend(bytes((IAC, WONT, opt)))
            else:  # WILL, WONT
                reply.extend(bytes((IAC, DONT, opt)))
            i += 3
        elif cmd == SB:
            j = i + 2
            while j < n - 1 and not (buf[j] == IAC and buf[j + 1] == SE):
                j += 1
            i = j + 2
        elif cmd == IAC:
            # Escaped 0xFF in the data stream.
            out.append(IAC)
            i += 2
        else:
            i += 2
    return bytes(out), bytes(reply)


@dataclass
class TransportStatus:
    connected: bool
    host: str
    port: int
    error: str | None = None
    bytes_in: int = 0
    bytes_out: int = 0
    opened_at: float | None = None


class WTITransport:
    """TCP transport to a WTI console port.

    Background reader thread pushes received bytes into a ring buffer and a
    list of subscribers. Writes are synchronous from the calling thread.
    """

    def __init__(self, host: str, port: int, ring_bytes: int = 64 * 1024):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._ring: deque[bytes] = deque()
        self._ring_max = ring_bytes
        self._ring_len = 0
        self._subscribers: list[deque[bytes]] = []
        self._status = TransportStatus(connected=False, host=host, port=port)

    def open(self, timeout: float = 5.0) -> None:
        if self._sock is not None:
            return
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        s.settimeout(0.5)
        self._sock = s
        self._stop.clear()
        self._status = TransportStatus(
            connected=True, host=self.host, port=self.port, opened_at=time.time()
        )
        self._reader = threading.Thread(
            target=self._read_loop, name=f"wti-reader-{self.host}:{self.port}", daemon=True
        )
        self._reader.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self._status.connected = False

    def reopen(self, timeout: float = 5.0) -> None:
        """Tear down and re-establish the TCP connection.

        Used when the WTI drops us (which it does periodically — e.g. another
        client connects to the same port, or session timeout). Preserves the
        subscriber list so the SSE stream and watcher keep working without
        the browser having to reload.
        """
        self._stop.set()
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        # Re-open.
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        s.settimeout(0.5)
        with self._lock:
            self._sock = s
        self._stop.clear()
        self._status = TransportStatus(
            connected=True,
            host=self.host,
            port=self.port,
            opened_at=time.time(),
        )
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"wti-reader-{self.host}:{self.port}",
            daemon=True,
        )
        self._reader.start()

    def write(self, data: bytes) -> None:
        with self._lock:
            sock = self._sock
        if sock is None:
            raise ConnectionError("transport not open")
        try:
            sock.sendall(data)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            # Peer closed the socket. Mark the transport dead so the UI sees
            # it on the next status poll. Don't let the exception bubble up
            # as a Flask 500.
            self._status.error = f"write failed: {e}"
            self._status.connected = False
            # Best-effort socket cleanup.
            with self._lock:
                self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            raise ConnectionError(str(e)) from e
        self._status.bytes_out += len(data)

    def status(self) -> TransportStatus:
        return self._status

    def subscribe(self, seed_history: bool = True) -> deque[bytes]:
        """Subscribe to incoming chunks.

        seed_history=True: queue starts with the recent ring-buffer history,
        useful for the live SSE view so the browser sees what came before
        it connected.
        seed_history=False: queue starts empty. Use this for command/response
        flows (like the VxWorks dialogue) where you only want the bytes that
        arrive AFTER you sent your command; otherwise prior prompts in the
        history match your wait pattern and short-circuit the read.
        """
        q: deque[bytes] = deque()
        with self._lock:
            self._subscribers.append(q)
            if seed_history:
                for chunk in self._ring:
                    q.append(chunk)
        return q

    def unsubscribe(self, q: deque[bytes]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def snapshot(self) -> bytes:
        with self._lock:
            return b"".join(self._ring)

    def _read_loop(self) -> None:
        first_chunk_logged = False
        while not self._stop.is_set():
            with self._lock:
                sock = self._sock
            if sock is None:
                return
            try:
                raw = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                self._status.error = str(e)
                self._status.connected = False
                print(
                    f"[transport {self.host}:{self.port}] read error: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            if not raw:
                self._status.connected = False
                self._status.error = (
                    self._status.error or "peer closed connection"
                )
                print(
                    f"[transport {self.host}:{self.port}] peer closed connection",
                    file=sys.stderr,
                    flush=True,
                )
                return
            if not first_chunk_logged:
                print(
                    f"[transport {self.host}:{self.port}] first raw chunk "
                    f"({len(raw)}B): {raw[:64]!r}{'...' if len(raw) > 64 else ''}",
                    file=sys.stderr,
                    flush=True,
                )
                first_chunk_logged = True
            chunk, reply = _consume_iac(raw)
            if reply:
                # Respond to the peer's telnet negotiation. Without this some
                # WTI firmware never starts forwarding serial bytes.
                print(
                    f"[transport {self.host}:{self.port}] telnet reply "
                    f"({len(reply)}B): {reply!r}",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    sock.sendall(reply)
                    self._status.bytes_out += len(reply)
                except OSError as e:
                    self._status.error = str(e)
                    self._status.connected = False
                    return
            if not chunk:
                continue
            self._status.bytes_in += len(chunk)
            with self._lock:
                self._ring.append(chunk)
                self._ring_len += len(chunk)
                while self._ring_len > self._ring_max and self._ring:
                    old = self._ring.popleft()
                    self._ring_len -= len(old)
                for q in self._subscribers:
                    q.append(chunk)
