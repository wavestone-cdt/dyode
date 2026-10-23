"""Fake pyserial: every Serial() opened on the same device name shares one
in-memory byte channel, like the two ends of the optocoupler link."""
import threading
import time

_channels = {}
_lock = threading.Lock()


class _Channel:
    def __init__(self):
        self.buf = bytearray()
        self.cond = threading.Condition()


def channel(name):
    with _lock:
        return _channels.setdefault(name, _Channel())


class Serial:
    def __init__(self, port=None, baudrate=9600, timeout=None, write_timeout=None, **kw):
        self.port, self.timeout = port, timeout
        self.ch = channel(port)
        self.is_open = True

    def write(self, data):
        with self.ch.cond:
            self.ch.buf.extend(data)
            self.ch.cond.notify_all()
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return len(self.ch.buf)

    def read(self, n=1):
        deadline = time.monotonic() + (self.timeout or 0)
        with self.ch.cond:
            while not self.ch.buf and time.monotonic() < deadline:
                self.ch.cond.wait(deadline - time.monotonic())
            out = bytes(self.ch.buf[:n])
            del self.ch.buf[:n]
            return out

    def close(self):
        self.is_open = False
