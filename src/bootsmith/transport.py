from __future__ import annotations

import socket
import threading
import time
from collections import deque
from dataclasses import dataclass

# Telnet IAC constants — we don't speak telnet, we just defensively strip
# IAC sequences so a telnet-mode WTI doesn't pollute the serial stream with
# negotiation bytes. Raw WTIs never send 0xFF, so this is a no-op there.
IAC = 0xFF
DONT = 0xFE
DO = 0xFD
WONT = 0xFC
WILL = 0xFB
SB = 0xFA
SE = 0xF0


def _strip_iac(buf: bytes) -> bytes:
    """Remove telnet IAC sequences. Conservative: drop the bytes, don't reply.

    A real telnet client would negotiate. For our use a WTI talking telnet at
    us is usually fine if we just refuse to engage — most WTIs fall back to
    raw passthrough after a couple of unanswered DOs.
    """
    if IAC not in buf:
        return buf
    out = bytearray()
    i = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break
        cmd = buf[i + 1]
        if cmd in (DO, DONT, WILL, WONT):
            i += 3
        elif cmd == SB:
            j = i + 2
            while j < n - 1 and not (buf[j] == IAC and buf[j + 1] == SE):
                j += 1
            i = j + 2
        elif cmd == IAC:
            out.append(IAC)
            i += 2
        else:
            i += 2
    return bytes(out)


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

    def write(self, data: bytes) -> None:
        with self._lock:
            sock = self._sock
        if sock is None:
            raise RuntimeError("transport not open")
        sock.sendall(data)
        self._status.bytes_out += len(data)

    def status(self) -> TransportStatus:
        return self._status

    def subscribe(self) -> deque[bytes]:
        q: deque[bytes] = deque()
        with self._lock:
            self._subscribers.append(q)
            # Seed with current ring contents so a new subscriber sees recent history.
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
        while not self._stop.is_set():
            with self._lock:
                sock = self._sock
            if sock is None:
                return
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                self._status.error = str(e)
                self._status.connected = False
                return
            if not chunk:
                self._status.connected = False
                return
            chunk = _strip_iac(chunk)
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
