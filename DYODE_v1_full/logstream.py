# -*- coding: utf-8 -*-
"""Log streaming through the diode (module type 'logs').

Input side (OT):  collect log lines from any mix of
                    * syslog over UDP           (listen_udp)
                    * syslog over TCP           (listen_tcp; LF or RFC 6587
                                                 octet-counted framing)
                    * local files, like tail -F (tail)
                  and send them through the diode in compressed, numbered
                  batches.
Output side (IT): write the lines to files and/or forward them to a SIEM
                  or syslog server over UDP or TCP.

A diode has no return path, so nothing can be re-requested.  Instead:

  * every batch carries a sequence number, so a lost batch is *reported*
    (log line + 'logs_gap' event) rather than silently missing;
  * every batch is sent again after a short delay ('copies', default 2), so
    a burst of loss has to hit both copies to lose anything;
  * batches are zlib-compressed: log text shrinks 5-10x, which means fewer
    datagrams per batch and fewer ways to lose one;
  * a heartbeat keeps the sequence moving when nothing is logged, so the
    output side can tell "quiet" from "link down" ('stale_after');
  * a bandwidth cap ('max_kbps') stops a log storm on the OT side from
    starving folder transfers sharing the same link.

Wire format (inside the usual DYO1 UDP framing from dyode_common):
    b"DYLG" | version (1 byte) | zlib(JSON)
    JSON = {"b": boot id, "s": seq, "t": send time, "d": dropped so far,
            "l": [[ts, src, chan, msg], ...]}
"""

import collections
import datetime
import glob
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
import zlib

import dyode_common as common

log = logging.getLogger("dyode.logs")

MAGIC = b"DYLG"
VERSION = 1
HEADER = MAGIC + bytes([VERSION])

# Per-datagram overhead on the wire: DYO1 header + UDP + IPv4 + Ethernet.
WIRE_OVERHEAD = common.UDP_HEADER.size + 8 + 20 + 14

STATS_EVERY = 60.0
SYSLOG_PRI_RE = re.compile(rb"^<\d{1,3}>")
OCTET_RE = re.compile(rb"^(\d{1,7}) <")

DEFAULTS = {
    "listen_udp": None,
    "listen_tcp": None,
    "tail": [],
    "tail_from": "end",
    "tail_state": None,
    "allow_from": [],
    "max_line": 16384,
    "buffer_lines": 100000,
    "batch_interval": 0.5,
    "batch_bytes": 48000,
    "max_kbps": 2000,
    "copies": 2,
    "heartbeat": 10.0,
    "max_tcp_clients": 64,
    # output side
    "out": None,
    "out_file": "received.log",
    "split_by_source": False,
    "format": "prefixed",
    "forward": [],
    "forward_wrap": True,
    "reorder_window": 3.0,
    "stale_after": None,       # default 3 x heartbeat
}
FORMATS = ("prefixed", "raw", "json")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def parse_hostport(text, default_host="0.0.0.0"):
    """'514' / ':514' / '10.0.0.1:514' / 'host:514' -> (host, port)."""
    text = str(text).strip()
    host, sep, port = text.rpartition(":")
    if not sep:
        host, port = default_host, text
    host = host.strip("[]") or default_host
    port = int(port)
    if not 0 <= port <= 65535:
        raise ValueError("port out of range: %d" % port)
    return host, port


def parse_forward(text):
    """'udp://host:514' or 'tcp://host:601' -> (proto, host, port)."""
    text = str(text).strip()
    proto, sep, rest = text.partition("://")
    proto = proto.lower()
    if not sep or proto not in ("udp", "tcp"):
        raise ValueError("expected udp://host:port or tcp://host:port, got %r"
                         % text)
    host, port = parse_hostport(rest, default_host="")
    if not host or port == 0:
        raise ValueError("forward target needs a host and a port: %r" % text)
    return proto, host, port


def iso_utc(ts):
    when = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return "%s.%03dZ" % (when.strftime("%Y-%m-%dT%H:%M:%S"),
                         when.microsecond // 1000)


def to_text(raw, max_line):
    """Bytes off the wire -> str, truncated, invalid UTF-8 kept losslessly
    (surrogateescape) so the output side can write the original bytes."""
    extra = len(raw) - max_line
    if extra > 0:
        raw = raw[:max_line]
    text = raw.decode("utf-8", "surrogateescape")
    if extra > 0:
        text += " [dyode: truncated %d bytes]" % extra
    return text


def to_bytes(text):
    try:
        return text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        # Lone surrogates that did not come from surrogateescape.
        return text.encode("utf-8", "backslashreplace")


_CTRL = {c: "\\x%02x" % c for c in range(32) if c != 9}
_CTRL[127] = "\\x7f"


def escape_controls(text):
    """Escape control characters (except tab) for line-oriented outputs, so
    one record is always one line and nothing reaches a terminal or parser
    as an escape sequence.  Same idea as rsyslog's control-character
    escaping."""
    return text.translate(_CTRL)


def safe_name(text):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", text)[:64].strip(".")
    return name or "unknown"


def repeat_delay(props):
    """Seconds between the copies of one batch."""
    return max(0.2, props["batch_interval"])


def min_reorder_window(props):
    """Time from a batch's first copy until its last one has certainly been
    sent: one delay per extra copy, plus a send-loop tick of slack each."""
    extra = props["copies"] - 1
    return extra * (repeat_delay(props) + props["batch_interval"]) + 0.5


def wire_bytes(payload_len):
    chunks = max(1, -(-payload_len // common.UDP_CHUNK))
    return payload_len + chunks * WIRE_OVERHEAD


def encode_batch(boot, seq, records, dropped, now=None):
    body = common.encode_json({"b": boot, "s": seq,
                               "t": round(now if now is not None else time.time(), 3),
                               "d": dropped, "l": records})
    return HEADER + zlib.compress(body, 6)


def decode_batch(payload, max_size=64 * 1024 * 1024):
    """Return the batch dict, or None if the payload is not a valid batch."""
    if not payload.startswith(HEADER):
        return None
    try:
        dec = zlib.decompressobj()
        body = dec.decompress(payload[len(HEADER):], max_size)
        if dec.unconsumed_tail:
            return None                       # decompression bomb
        batch = json.loads(body.decode("ascii"))
    except (zlib.error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(batch, dict):
        return None
    try:
        batch["b"] = int(batch["b"])
        batch["s"] = int(batch["s"])
        batch["d"] = int(batch.get("d", 0))
        records = batch.get("l") or []
        clean = []
        for rec in records:
            ts, src, chan, msg = rec
            clean.append((float(ts), str(src), str(chan), str(msg)))
        batch["l"] = clean
    except (KeyError, TypeError, ValueError):
        return None
    return batch


# --------------------------------------------------------------------------
# Config validation (called from dyode_common._normalize_module)
# --------------------------------------------------------------------------

def normalize_props(name, props, error):
    """Validate and fill in a 'logs' module. `error` is the ConfigError
    class, passed in so this file does not import-cycle with
    dyode_common."""
    def num(key, cast=float, minimum=0.0, allow_zero=False):
        value = props.get(key, DEFAULTS[key])
        try:
            value = cast(value)
        except (TypeError, ValueError):
            raise error("module %r: %r must be a number, got %r" % (name, key, value))
        if value < minimum or (value == 0 and not allow_zero):
            raise error("module %r: %r is out of range: %r" % (name, key, value))
        props[key] = value

    for key in ("listen_udp", "listen_tcp"):
        if props.get(key) not in (None, ""):
            try:
                props[key] = parse_hostport(props[key])
            except ValueError as err:
                raise error("module %r: %r: %s" % (name, key, err))
        else:
            props[key] = None
    tail = props.get("tail") or []
    if isinstance(tail, str):
        tail = [tail]
    for pattern in tail:
        if not os.path.isabs(str(pattern)):
            raise error("module %r: 'tail' entries must be absolute paths, "
                        "got %r" % (name, pattern))
    props["tail"] = [str(p) for p in tail]
    if not (props["listen_udp"] or props["listen_tcp"] or props["tail"]):
        raise error("module %r: a logs module needs at least one source: "
                    "listen_udp, listen_tcp or tail" % name)

    props["tail_from"] = str(props.get("tail_from", "end")).lower()
    if props["tail_from"] not in ("end", "start"):
        raise error("module %r: 'tail_from' must be 'end' or 'start'" % name)
    state = props.get("tail_state")
    if state is not None and not os.path.isabs(str(state)):
        raise error("module %r: 'tail_state' must be an absolute path" % name)

    allow = props.get("allow_from") or []
    if isinstance(allow, str):
        allow = [allow]
    try:
        props["allow_from"] = [ipaddress.ip_network(str(a), strict=False)
                               for a in allow]
    except ValueError as err:
        raise error("module %r: 'allow_from': %s" % (name, err))

    num("max_line", int, 64)
    num("buffer_lines", int, 1)
    num("batch_interval", float, 0.05)
    num("batch_bytes", int, 1024)
    num("max_kbps", float, 8)
    num("copies", int, 1)
    num("heartbeat", float, 1)
    num("max_tcp_clients", int, 1)
    num("reorder_window", float, 0, allow_zero=True)
    if props["copies"] > 4:
        raise error("module %r: 'copies' above 4 only wastes bandwidth" % name)
    if props["max_line"] > props["batch_bytes"]:
        raise error("module %r: 'max_line' must not exceed 'batch_bytes'" % name)
    # The output side must wait at least as long as the last copy of a batch
    # takes to arrive, or 'copies' is wasted.  Raised here rather than
    # rejected: it is a minimum, and the same file configures both boxes.
    props["reorder_window"] = max(props["reorder_window"],
                                  min_reorder_window(props))
    if props.get("stale_after") is None:
        props["stale_after"] = 3 * props["heartbeat"]
    else:
        num("stale_after", float, 1)

    if props.get("out") not in (None, ""):
        if not os.path.isabs(str(props["out"])):
            raise error("module %r: 'out' must be an absolute directory" % name)
        props["out"] = str(props["out"])
    else:
        props["out"] = None
    out_file = str(props.get("out_file", DEFAULTS["out_file"]))
    if "/" in out_file or out_file in ("", ".", ".."):
        raise error("module %r: 'out_file' must be a bare file name" % name)
    props["out_file"] = out_file
    props["split_by_source"] = bool(props.get("split_by_source", False))
    props["format"] = str(props.get("format", "prefixed")).lower()
    if props["format"] not in FORMATS:
        raise error("module %r: 'format' must be one of %s"
                    % (name, ", ".join(FORMATS)))
    forward = props.get("forward") or []
    if isinstance(forward, str):
        forward = [forward]
    try:
        props["forward"] = [parse_forward(f) for f in forward]
    except ValueError as err:
        raise error("module %r: 'forward': %s" % (name, err))
    props["forward_wrap"] = bool(props.get("forward_wrap", True))
    if not props["out"] and not props["forward"]:
        raise error("module %r: a logs module needs somewhere to put lines on "
                    "the output side: 'out' and/or 'forward'" % name)
    return props


# --------------------------------------------------------------------------
# Input side: the batch sender
# --------------------------------------------------------------------------

class TokenBucket:
    def __init__(self, rate_bytes, burst, clock):
        self.rate = float(rate_bytes)
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.clock = clock
        self.last = clock()

    def _refill(self):
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def try_take(self, amount):
        # A payload bigger than the burst could never be sent: let the
        # bucket grow to fit it rather than wait forever.
        if amount > self.capacity:
            self.capacity = float(amount)
        self._refill()
        # The epsilon stops float rounding leaving the bucket a hair short
        # forever (seen with large clock values: the wait rounds to zero).
        if self.tokens + 1e-6 >= amount:
            self.tokens = max(0.0, self.tokens - amount)
            return True
        return False

    def wait_time(self, amount):
        self._refill()
        return max(0.001, (amount - self.tokens) / self.rate)


class LogSender:
    """Bounded record buffer + batching + repeats + rate cap.

    Sources call add() from their own threads.  One thread calls tick().
    `send` takes a payload and puts it on the wire.
    """

    def __init__(self, props, send, clock=time.monotonic, sleep=time.sleep,
                 module=None):
        self.props = props
        self.send = send
        self.clock = clock
        self.sleep = sleep
        self.module = module
        self.boot = secrets.randbits(32)
        self.seq = 0
        self.lock = threading.Lock()
        self.queue = collections.deque()
        self.dropped = 0           # lines lost because the buffer was full
        self.repeats = collections.deque()   # (due, payload, remaining)
        rate = props["max_kbps"] * 1000 / 8
        burst = max(rate, wire_bytes(props["batch_bytes"]) * 2)
        self.bucket = TokenBucket(rate, burst, clock)
        self.last_send = None
        self.stats = collections.Counter()

    # -- called by sources -------------------------------------------------

    def add(self, src, chan, msg, ts=None):
        with self.lock:
            if len(self.queue) >= self.props["buffer_lines"]:
                self.dropped += 1
                self.stats["dropped"] += 1
                return False
            self.queue.append((round(ts if ts is not None else time.time(), 3),
                               src, chan, msg))
            self.stats["lines_in"] += 1
            return True

    # -- called by the send loop ---------------------------------------------

    def _take_batch(self):
        limit = self.props["batch_bytes"]
        records, size = [], 0
        with self.lock:
            while self.queue:
                rec = self.queue[0]
                cost = len(rec[3]) + len(rec[1]) + len(rec[2]) + 24
                if records and size + cost > limit:
                    break
                records.append(self.queue.popleft())
                size += cost
            dropped = self.dropped
        return records, dropped

    def _emit(self, records, dropped):
        payload = encode_batch(self.boot, self.seq, records, dropped)
        self.seq += 1
        cost = wire_bytes(len(payload))
        # New data waits for bandwidth; it is never skipped.  While we wait
        # the buffer fills, and overflow is counted and reported.
        while not self.bucket.try_take(cost):
            self.sleep(min(1.0, self.bucket.wait_time(cost)))
        self.send(payload)
        now = self.clock()
        self.last_send = now
        self.stats["batches"] += 1
        self.stats["lines_sent"] += len(records)
        self.stats["wire_bytes"] += cost
        delay = repeat_delay(self.props)
        if self.props["copies"] > 1:
            self.repeats.append((now + delay, payload, self.props["copies"] - 1))

    def _send_repeats(self):
        now = self.clock()
        delay = repeat_delay(self.props)
        keep = collections.deque()
        while self.repeats:
            due, payload, remaining = self.repeats.popleft()
            if due > now:
                keep.append((due, payload, remaining))
                continue
            cost = wire_bytes(len(payload))
            # Repeats are insurance: under bandwidth pressure they are
            # dropped in favour of new data.
            if self.bucket.try_take(cost):
                self.send(payload)
                self.stats["repeats"] += 1
                self.stats["wire_bytes"] += cost
            else:
                self.stats["repeats_skipped"] += 1
            if remaining > 1:
                keep.append((now + delay, payload, remaining - 1))
        self.repeats = keep

    def tick(self):
        """Send everything queued, due repeats, and a heartbeat if idle."""
        sent_any = False
        while True:
            records, dropped = self._take_batch()
            if not records:
                break
            self._emit(records, dropped)
            sent_any = True
            self._send_repeats()
        if not sent_any:
            now = self.clock()
            if self.last_send is None or now - self.last_send >= self.props["heartbeat"]:
                self._emit([], self.dropped)
        self._send_repeats()


# --------------------------------------------------------------------------
# Input side: sources
# --------------------------------------------------------------------------

def make_allow(networks):
    if not networks:
        return lambda ip: True

    def allowed(ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in networks)
    return allowed


def run_udp_source(sock, sender, props, stop):
    allowed = make_allow(props["allow_from"])
    max_line = props["max_line"]
    while not stop.is_set():
        try:
            data, peer = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            if stop.is_set():
                return
            raise
        if not allowed(peer[0]):
            sender.stats["rejected"] += 1
            continue
        msg = data.rstrip(b"\r\n\x00")
        if msg:
            sender.add(peer[0], "udp", to_text(msg, max_line))


class StreamFramer:
    """Split a TCP syslog stream into messages.

    Handles both RFC 6587 framings, per message:
      octet counting   '123 <13>1 2026-...'   (digits, space, then '<')
      non-transparent  '...\\n' (also NUL-terminated)
    A plain line that happens to start with digits is not mistaken for a
    length, because octet counting also requires the '<' of a syslog PRI.
    """

    def __init__(self, max_line):
        self.max_line = max_line
        self.buf = bytearray()
        self.skipping = False      # discarding the rest of an over-long line

    def feed(self, data):
        self.buf.extend(data)
        out = []
        while self.buf:
            if self.skipping:
                idx = self._eol()
                if idx < 0:
                    self.buf.clear()
                    break
                del self.buf[:idx + 1]
                self.skipping = False
                continue
            m = OCTET_RE.match(self.buf[:16])
            # An absurd length is not trusted; the message falls through to
            # LF framing instead of stalling the stream waiting for it.
            if m and int(m.group(1)) <= self.max_line * 4:
                length = int(m.group(1))
                start = len(m.group(1)) + 1
                if len(self.buf) < start + length:
                    break
                msg = bytes(self.buf[start:start + length])
                del self.buf[:start + length]
                out.append(msg[:self.max_line + 1])
                continue
            idx = self._eol()
            if idx < 0:
                if len(self.buf) > self.max_line:
                    out.append(bytes(self.buf[:self.max_line + 1]))
                    self.buf.clear()
                    self.skipping = True
                break
            line = bytes(self.buf[:idx]).rstrip(b"\r")
            del self.buf[:idx + 1]
            if line:
                out.append(line)
        return out

    def _eol(self):
        nl, nul = self.buf.find(b"\n"), self.buf.find(b"\x00")
        if nl < 0:
            return nul
        if nul < 0:
            return nl
        return min(nl, nul)


def run_tcp_source(listener, sender, props, stop):
    allowed = make_allow(props["allow_from"])
    clients = threading.BoundedSemaphore(props["max_tcp_clients"])

    def serve(conn, peer):
        try:
            conn.settimeout(1.0)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            framer = StreamFramer(props["max_line"])
            while not stop.is_set():
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                for msg in framer.feed(data):
                    sender.add(peer[0], "tcp", to_text(msg, props["max_line"]))
        except OSError as err:
            log.debug("tcp client %s: %s", peer[0], err)
        finally:
            conn.close()
            clients.release()

    while not stop.is_set():
        try:
            conn, peer = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            if stop.is_set():
                return
            raise
        if not allowed(peer[0]):
            sender.stats["rejected"] += 1
            conn.close()
            continue
        if not clients.acquire(blocking=False):
            log.warning("too many syslog TCP clients; refusing %s", peer[0])
            conn.close()
            continue
        threading.Thread(target=serve, args=(conn, peer), daemon=True,
                         name="logs-tcp-%s" % peer[0]).start()


class FileTailer:
    """Follow files matching glob patterns, like `tail -F`.

    * rotation by rename: the old file is read to its end before switching
      to the new one, so lines written just before rotation are not lost;
    * truncation (copytruncate): reading restarts at 0;
    * files that appear after start-up are read from their beginning;
    * optional offset persistence (tail_state), so a restart resumes where
      it stopped instead of skipping or repeating.
    """

    def __init__(self, props, sender, hostname=None):
        self.patterns = props["tail"]
        self.from_start = props["tail_from"] == "start"
        self.state_path = props.get("tail_state")
        self.max_line = props["max_line"]
        self.sender = sender
        self.host = hostname or socket.gethostname()
        self.files = {}     # path -> dict(fh, ino, partial)
        self.first_scan = True
        self.saved = self._load_state()
        self.last_save = 0.0

    def _load_state(self):
        if not self.state_path:
            return {}
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return {k: (int(v[0]), int(v[1])) for k, v in data.items()}
        except (OSError, ValueError, TypeError, IndexError):
            return {}

    def save_state(self, force=False):
        if not self.state_path:
            return
        now = time.monotonic()
        if not force and now - self.last_save < 5.0:
            return
        self.last_save = now
        data = {}
        for path, f in self.files.items():
            data[path] = [f["ino"], f["fh"].tell() - len(f["partial"])]
        tmp = self.state_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.state_path)
        except OSError as err:
            log.warning("cannot save tail state to %s: %s", self.state_path, err)

    def close(self):
        self.save_state(force=True)
        for f in self.files.values():
            f["fh"].close()
        self.files.clear()

    def _open(self, path, st, at_end):
        try:
            fh = open(path, "rb")
        except OSError as err:
            log.debug("cannot open %s: %s", path, err)
            return
        saved = self.saved.pop(path, None)
        if saved and saved[0] == st.st_ino and saved[1] <= st.st_size:
            fh.seek(saved[1])
        elif at_end:
            fh.seek(0, os.SEEK_END)
        self.files[path] = {"fh": fh, "ino": st.st_ino, "partial": b""}
        log.info("tailing %s", path)

    def _drain(self, path, f, final=False):
        chan = "file:" + path
        while True:
            chunk = f["fh"].read(65536)
            if not chunk:
                break
            data = f["partial"] + chunk
            lines = data.split(b"\n")
            f["partial"] = lines.pop()
            for line in lines:
                line = line.rstrip(b"\r")
                if line:
                    self.sender.add(self.host, chan, to_text(line, self.max_line))
            if len(f["partial"]) > self.max_line:
                self.sender.add(self.host, chan, to_text(f["partial"], self.max_line))
                f["partial"] = b""
        if final and f["partial"]:
            self.sender.add(self.host, chan, to_text(f["partial"], self.max_line))
            f["partial"] = b""

    def poll(self):
        seen = set()
        for pattern in self.patterns:
            for path in glob.glob(pattern):
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if not os.path.isfile(path):
                    continue
                seen.add(path)
                f = self.files.get(path)
                if f is None:
                    self._open(path, st, at_end=self.first_scan and not self.from_start)
                    f = self.files.get(path)
                    if f is None:
                        continue
                elif st.st_ino != f["ino"]:
                    # Renamed away: finish the old file, then start the new.
                    self._drain(path, f, final=True)
                    f["fh"].close()
                    del self.files[path]
                    self._open(path, st, at_end=False)
                    f = self.files.get(path)
                    if f is None:
                        continue
                elif st.st_size < f["fh"].tell():
                    log.info("%s was truncated; reading from the start", path)
                    f["fh"].seek(0)
                    f["partial"] = b""
                self._drain(path, f)
        for path in list(self.files):
            if path not in seen:
                f = self.files.pop(path)
                self._drain(path, f, final=True)
                f["fh"].close()
        self.first_scan = False
        self.save_state()


def run_tail_source(tailer, stop, interval=0.5):
    while not stop.is_set():
        tailer.poll()
        stop.wait(interval)
    tailer.close()


def bind_sources(props):
    """Bind the listening sockets in the calling thread, so a bad address
    or a port in use fails the module start loudly (and the supervisor
    retries) instead of killing a background thread quietly."""
    udp = tcp = None
    if props["listen_udp"]:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        udp.bind(props["listen_udp"])
        udp.settimeout(1.0)
    if props["listen_tcp"]:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind(props["listen_tcp"])
        tcp.listen(64)
        tcp.settimeout(1.0)
    return udp, tcp


def start_input(name, props, send, stop, hostname=None):
    """Start the sources. Returns (sender, threads, sockets)."""
    sender = LogSender(props, send, module=name)
    udp, tcp = bind_sources(props)
    threads = []
    if udp:
        threads.append(threading.Thread(target=run_udp_source,
                                        args=(udp, sender, props, stop),
                                        name="logs-udp", daemon=True))
    if tcp:
        threads.append(threading.Thread(target=run_tcp_source,
                                        args=(tcp, sender, props, stop),
                                        name="logs-tcp", daemon=True))
    if props["tail"]:
        tailer = FileTailer(props, sender, hostname)
        threads.append(threading.Thread(target=run_tail_source,
                                        args=(tailer, stop),
                                        name="logs-tail", daemon=True))
    for t in threads:
        t.start()
    return sender, threads, [s for s in (udp, tcp) if s]


def run_logs_input(name, props, cfg, stop=None):
    common.setup_logging(cfg.get("_log_level", "INFO"), cfg)
    stop = stop or threading.Event()
    addr = (cfg["network"]["out_ip"], props["port"])
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    auth = common.require_auth(cfg)

    def send(payload):
        common.udp_send(out, addr, payload, auth=auth)

    sender, threads, _ = start_input(name, props, send, stop)
    sources = []
    if props["listen_udp"]:
        sources.append("udp %s:%d" % props["listen_udp"])
    if props["listen_tcp"]:
        sources.append("tcp %s:%d" % props["listen_tcp"])
    if props["tail"]:
        sources.append("%d file pattern(s)" % len(props["tail"]))
    log.info("module %r: collecting logs from %s, sending to %s:%d (cap %g kbit/s, "
             "%d cop%s)", name, ", ".join(sources), addr[0], addr[1],
             props["max_kbps"], props["copies"], "y" if props["copies"] == 1 else "ies")
    last_stats = time.monotonic()
    reported_drop = 0
    while not stop.is_set():
        stop.wait(props["batch_interval"])
        for t in threads:
            if not t.is_alive():
                raise RuntimeError("log source thread %s died" % t.name)
        sender.tick()
        if sender.dropped > reported_drop:
            log.warning("module %r: buffer full, %d line(s) dropped so far "
                        "(input faster than max_kbps allows)", name, sender.dropped)
            reported_drop = sender.dropped
        if time.monotonic() - last_stats >= STATS_EVERY:
            last_stats = time.monotonic()
            common.log_event("logs_stats", module=name, **dict(sender.stats))
            sender.stats.clear()


# --------------------------------------------------------------------------
# Output side: sinks
# --------------------------------------------------------------------------

class FileSink:
    """Append lines to <out>/<out_file>, or one file per source.

    Files are re-opened when logrotate moves them away (same idea as
    WatchedFileHandler), and created 0640.
    """

    MAX_OPEN = 128

    def __init__(self, directory, out_file, split_by_source, fmt):
        self.directory = directory
        self.out_file = out_file
        self.split = split_by_source
        self.fmt = fmt
        self.handles = collections.OrderedDict()   # path -> (fh, ino)
        os.makedirs(directory, mode=0o750, exist_ok=True)

    def _path(self, src):
        if self.split:
            return os.path.join(self.directory, safe_name(src) + ".log")
        return os.path.join(self.directory, self.out_file)

    def _handle(self, path):
        entry = self.handles.get(path)
        try:
            ino = os.stat(path).st_ino
        except OSError:
            ino = None
        if entry is not None and entry[1] == ino:
            self.handles.move_to_end(path)
            return entry[0]
        if entry is not None:
            entry[0].close()
            del self.handles[path]
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        fh = os.fdopen(fd, "ab")
        self.handles[path] = (fh, os.fstat(fd).st_ino)
        while len(self.handles) > self.MAX_OPEN:
            _, (old, _) = self.handles.popitem(last=False)
            old.close()
        return fh

    def format(self, rec):
        ts, src, chan, msg = rec
        if self.fmt == "json":
            return json.dumps({"ts": iso_utc(ts), "src": src, "chan": chan,
                               "msg": msg}, ensure_ascii=True)
        msg = escape_controls(msg)
        if self.fmt == "raw":
            return msg
        return "%s %s %s" % (iso_utc(ts), src, msg)

    def write(self, records):
        groups = collections.OrderedDict()
        for rec in records:
            groups.setdefault(self._path(rec[1]), []).append(rec)
        for path, recs in groups.items():
            fh = self._handle(path)
            fh.write(b"".join(to_bytes(self.format(r)) + b"\n" for r in recs))
            fh.flush()

    def close(self):
        for fh, _ in self.handles.values():
            fh.close()
        self.handles.clear()


def syslog_wrap(rec):
    """Give a non-syslog line (e.g. from a tailed file) an RFC 5424 header,
    so the SIEM sees a proper timestamp and the original host.  Lines that
    already start with a <PRI> are passed through untouched."""
    ts, src, chan, msg = rec
    raw = to_bytes(msg)
    if SYSLOG_PRI_RE.match(raw):
        return raw
    host = safe_name(src)
    app = "dyode"
    # facility user(1), severity notice(5) -> PRI 13
    return ("<13>1 %s %s %s - - - " % (iso_utc(ts), host, app)).encode("ascii") + raw


class UdpForwarder:
    def __init__(self, host, port, wrap):
        self.target = (host, port)
        self.wrap = wrap
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.errors = 0

    def write(self, records):
        for rec in records:
            data = syslog_wrap(rec) if self.wrap else to_bytes(rec[3])
            try:
                self.sock.sendto(data[:65000], self.target)
            except OSError as err:
                self.errors += 1
                if self.errors == 1 or self.errors % 1000 == 0:
                    log.warning("forward to udp://%s:%d failed (%d so far): %s",
                                self.target[0], self.target[1], self.errors, err)

    def close(self):
        self.sock.close()


class TcpForwarder:
    """LF-framed syslog over TCP, with reconnect and a bounded queue.

    It runs in its own thread so a slow or absent SIEM never stalls the
    diode receiver.  While disconnected, lines queue up to `max_queue`;
    beyond that they are dropped and counted.  Lines in flight when a
    connection breaks can be lost: TCP syslog without RELP has no
    application-level acknowledgement.
    """

    def __init__(self, host, port, wrap, max_queue=200000):
        self.target = (host, port)
        self.wrap = wrap
        self.queue = collections.deque()
        self.max_queue = max_queue
        self.cond = threading.Condition()
        self.dropped = 0
        self.connected = False
        self.closed = False
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="logs-fwd-%s" % host)
        self.thread.start()

    def write(self, records):
        with self.cond:
            for rec in records:
                if len(self.queue) >= self.max_queue:
                    self.dropped += 1
                    continue
                data = syslog_wrap(rec) if self.wrap else to_bytes(rec[3])
                self.queue.append(to_bytes(escape_controls(
                    data.decode("utf-8", "surrogateescape"))) + b"\n")
            self.cond.notify()

    def _run(self):
        backoff = 1.0
        warned = False
        while not self.closed:
            try:
                sock = socket.create_connection(self.target, timeout=10)
            except OSError as err:
                if not warned:
                    log.warning("cannot connect to tcp://%s:%d (%s); retrying, "
                                "lines are queued meanwhile", *self.target, err)
                    warned = True
                time.sleep(backoff)
                backoff = min(30.0, backoff * 2)
                continue
            if warned:
                log.info("connected to tcp://%s:%d", *self.target)
            warned, backoff, self.connected = False, 1.0, True
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                while not self.closed:
                    with self.cond:
                        self.cond.wait_for(lambda: self.queue or self.closed, 1.0)
                        chunk = []
                        while self.queue and len(chunk) < 1000:
                            chunk.append(self.queue.popleft())
                    if chunk:
                        sock.sendall(b"".join(chunk))
            except OSError as err:
                log.warning("lost connection to tcp://%s:%d: %s", *self.target, err)
            finally:
                self.connected = False
                sock.close()

    def close(self):
        self.closed = True
        with self.cond:
            self.cond.notify()


def make_sinks(props):
    sinks = []
    if props["out"]:
        sinks.append(FileSink(props["out"], props["out_file"],
                              props["split_by_source"], props["format"]))
    for proto, host, port in props["forward"]:
        cls = UdpForwarder if proto == "udp" else TcpForwarder
        sinks.append(cls(host, port, props["forward_wrap"]))
    return sinks


# --------------------------------------------------------------------------
# Output side: sequencing
# --------------------------------------------------------------------------

class LogReceiver:
    """Turn possibly duplicated, reordered, lossy batches into an in-order
    stream of lines, and account for everything that went missing."""

    MAX_BUFFERED = 2000

    def __init__(self, sinks, props, module=None, clock=time.monotonic):
        self.sinks = sinks
        self.window = props["reorder_window"]
        self.stale_after = props["stale_after"]
        self.module = module
        self.clock = clock
        self.boot = None
        self.next_seq = None
        self.buffer = {}
        self.gap_since = None
        self.last_rx = None
        self.stale = False
        self.src_dropped = 0
        self.stats = collections.Counter()

    def feed(self, payload):
        batch = decode_batch(payload)
        if batch is None:
            self.stats["bad"] += 1
            log.warning("module %r: received something that is not a valid log "
                        "batch; ignored", self.module)
            return
        now = self.clock()
        self.last_rx = now
        if self.stale:
            self.stale = False
            log.info("module %r: log stream resumed", self.module)
            common.log_event("logs_resumed", module=self.module)
        if batch["b"] != self.boot:
            if self.boot is not None:
                self._resolve_gap(force=True)
                log.info("module %r: input side restarted (new stream)", self.module)
                common.log_event("logs_sender_restart", module=self.module)
            self.boot = batch["b"]
            self.next_seq = batch["s"]
            self.buffer = {}
            self.gap_since = None
            self.src_dropped = 0
        seq = batch["s"]
        if seq < self.next_seq or seq in self.buffer:
            self.stats["duplicates"] += 1
            return
        self.buffer[seq] = batch
        self._deliver()
        if self.buffer and self.gap_since is None:
            self.gap_since = now
        if len(self.buffer) > self.MAX_BUFFERED:
            self._resolve_gap(force=True)

    def _deliver(self):
        while self.next_seq in self.buffer:
            batch = self.buffer.pop(self.next_seq)
            self.next_seq += 1
            self.stats["batches"] += 1
            if batch["d"] > self.src_dropped:
                lost = batch["d"] - self.src_dropped
                self.src_dropped = batch["d"]
                log.warning("module %r: input side dropped %d line(s): its "
                            "buffer was full", self.module, lost)
                common.log_event("logs_dropped_at_source", module=self.module,
                                 lines=lost)
            if batch["l"]:
                self.stats["lines"] += len(batch["l"])
                for sink in self.sinks:
                    try:
                        sink.write(batch["l"])
                    except OSError as err:
                        log.error("module %r: cannot write logs: %s", self.module, err)
        if not self.buffer:
            self.gap_since = None

    def _resolve_gap(self, force=False):
        if not self.buffer:
            return
        first = min(self.buffer)
        lost = first - self.next_seq
        if lost > 0:
            self.stats["lost_batches"] += lost
            log.warning("module %r: %d log batch(es) lost in the diode "
                        "(sequence %d-%d)", self.module, lost, self.next_seq, first - 1)
            common.log_event("logs_gap", module=self.module, batches=lost,
                             first=self.next_seq, last=first - 1)
        self.next_seq = first
        self.gap_since = None
        self._deliver()
        if self.buffer:
            self.gap_since = self.clock()
            if force:
                self._resolve_gap(force=True)

    def poll(self):
        now = self.clock()
        if self.gap_since is not None and now - self.gap_since >= self.window:
            self._resolve_gap()
        if (self.last_rx is not None and not self.stale
                and now - self.last_rx > self.stale_after):
            self.stale = True
            log.warning("module %r: no log batches or heartbeats for %.0fs; the "
                        "link or the input side may be down", self.module,
                        now - self.last_rx)
            common.log_event("logs_stale", module=self.module,
                             silent_s=round(now - self.last_rx, 1))


def start_output(props, cfg, udp_bind=None, module=None, clock=time.monotonic):
    sock = common.diode_socket(cfg, props["port"], rcvbuf=8 * 1024 * 1024, bind=udp_bind)
    sock.settimeout(0.5)
    receiver = LogReceiver(make_sinks(props), props, module=module, clock=clock)
    return sock, receiver


def receive_loop(sock, receiver, stop, name=None, auth=None):
    reasm = common.Reassembler(max_pending=32, max_pieces=256)
    last_stats = time.monotonic()
    while not stop.is_set():
        try:
            data = sock.recv(common.UDP_MAX_DATAGRAM + 64)
        except socket.timeout:
            data = None
        except OSError:
            if stop.is_set():
                return
            raise
        if data:
            payload = reasm.feed(data)
            if payload is not None and auth is not None:
                payload = auth.unseal(payload, name)
            if payload is not None:
                receiver.feed(payload)
        receiver.poll()
        if time.monotonic() - last_stats >= STATS_EVERY:
            last_stats = time.monotonic()
            stats = dict(receiver.stats)
            stats["incomplete_messages"] = reasm.dropped
            common.log_event("logs_stats", module=name, **stats)
            receiver.stats.clear()


def run_logs_output(name, props, cfg, stop=None):
    common.setup_logging(cfg.get("_log_level", "INFO"), cfg)
    stop = stop or threading.Event()
    auth = common.require_auth(cfg)
    sock, receiver = start_output(props, cfg, module=name)
    where = []
    if props["out"]:
        where.append(os.path.join(props["out"], "<source>.log" if props["split_by_source"]
                                  else props["out_file"]))
    where += ["%s://%s:%d" % f for f in props["forward"]]
    log.info("module %r: receiving logs on %s:%d -> %s", name,
             *sock.getsockname()[:2], ", ".join(where))
    receive_loop(sock, receiver, stop, name, auth=auth)
