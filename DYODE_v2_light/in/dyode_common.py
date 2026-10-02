# -*- coding: utf-8 -*-
"""Shared helpers for DYODE (Python 3 port).

This one file is copied, unchanged, into every deployable folder:
  DYODE v1 (full)/, DYODE v2 (light)/in/, DYODE v2 (light)/out/
tests/test_layout.py checks that the copies stay identical.

Contents:
  * configuration loading and validation (YAML)
  * logging setup
  * a process supervisor that restarts crashed modules
  * UDP datagram framing (chunking, reassembly, CRC check)
  * serial line framing (newline-delimited, CRC check)
"""

import argparse
import datetime
import json
import logging
import logging.handlers
import multiprocessing
import os
import re
import secrets
import struct
import sys
import time
import zlib

import yaml

log = logging.getLogger("dyode")

KNOWN_TYPES = ("folder", "modbus", "screen")

# Forward error correction for folder transfers (udp-sender --fec).
# The diode has no return path, so a packet the FEC cannot repair costs the
# whole file: it fails its SHA-256 on arrival and must be sent again by hand.
# The encoder is single-threaded software Reed-Solomon and becomes the
# throughput ceiling well below line rate on fast hardware -- on server-grade
# boxes it caps out a gigabit link at roughly 40%.  Set 'fec: none' on a short,
# clean, dedicated link where losing loss-recovery is an acceptable trade.
DEFAULT_FEC = "8x16/64"
FEC_DISABLED = ("none", "off", "false", "no", "0", "disabled")
FEC_RE = re.compile(r"^\d+x\d+(/\d+)?$")

DEFAULT_NETWORK = {
    "in_ip": "10.0.1.1",
    "out_ip": "10.0.1.2",
    "in_interface": "eth0",
    "out_interface": "eth1",
}

DEFAULT_SERIAL = {
    # /dev/serial0 is the Raspberry Pi alias for the GPIO UART, whichever
    # physical device (ttyAMA0 / ttyS0) it maps to on a given model.
    "device": "/dev/serial0",
    "baudrate": 57600,
}

# Logging.  Each module runs in its own process (see supervise()), so several
# processes append to these files at once.  That rules out Python's own
# Rotating/TimedRotatingFileHandler: whichever process rotates first renames
# the file, and the others keep writing to the unlinked inode.  Instead
# logrotate does the rotation and WatchedFileHandler notices the new inode
# and reopens -- see packaging/logrotate/dyode-transfer.
DEFAULT_LOGGING = {
    "dir": "/var/log/dyode-transfer",
    "human": "dyode.log",
    "json": "transfer.jsonl",
    "per_file_events": True,
}
EVENT_SCHEMA_VERSION = 1
LOG_DIR_MODE = 0o750


class ConfigError(ValueError):
    """Raised when config.yaml is missing something or has a bad value."""


# --------------------------------------------------------------------------
# Command line and logging
# --------------------------------------------------------------------------

def parse_args(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("-c", "--config", default=None,
                        help="path to config.yaml (default: ./config.yaml, "
                             "then the script's own folder)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


event_log = logging.getLogger("dyode.events")
event_log.propagate = False           # never duplicated into the human log

# Per-process state, set by setup_logging().  Each module runs in its own
# process, so every process fills these in for itself.
_side = None
_per_file_events = True


def setup_logging(level="INFO", cfg=None):
    """Log to stderr, and to the log directory when one is usable.

    stderr is always kept so `journalctl -u dyode-in` still shows everything.
    On top of that:
      <dir>/dyode.log        human-readable, every module, both variants
      <dir>/transfer.jsonl   one JSON object per line, folder transfers only

    Both use WatchedFileHandler, which re-opens the file when logrotate
    replaces it.  If the directory cannot be used (not root, read-only /var)
    the daemon still starts; it just logs a warning and stays on stderr.
    """
    global _side, _per_file_events

    cfg = cfg or {}
    log_cfg = cfg.get("logging") or dict(DEFAULT_LOGGING)
    _side = cfg.get("_side")
    _per_file_events = bool(log_cfg.get("per_file_events", True))

    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-7s [%(processName)s] %(message)s",
    )
    event_log.setLevel(logging.INFO)

    # Re-running in the same process (the test suite does) must not stack
    # handlers and write every line twice.
    for handler in list(event_log.handlers):
        event_log.removeHandler(handler)
        handler.close()
    for handler in list(logging.getLogger().handlers):
        if isinstance(handler, logging.handlers.WatchedFileHandler):
            logging.getLogger().removeHandler(handler)
            handler.close()

    directory = log_cfg.get("dir")
    if not directory:
        return
    try:
        os.makedirs(directory, mode=LOG_DIR_MODE, exist_ok=True)
        human = logging.handlers.WatchedFileHandler(
            os.path.join(directory, log_cfg.get("human", "dyode.log")),
            encoding="utf-8")
        human.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(processName)s] %(message)s"))
        events = logging.handlers.WatchedFileHandler(
            os.path.join(directory, log_cfg.get("json", "transfer.jsonl")),
            encoding="utf-8")
        events.setFormatter(logging.Formatter("%(message)s"))
    except OSError as err:
        log.warning("cannot write to %s (%s); logging to stderr only",
                    directory, err)
        return

    logging.getLogger().addHandler(human)
    event_log.addHandler(events)


def log_event(event, module=None, per_file=False, **fields):
    """Append one JSON object to transfer.jsonl.

    Each record is written as a single line and flushed, so appends from the
    several module processes do not interleave.  `per_file` marks the
    high-volume per-file records that 'per_file_events: false' suppresses.
    """
    if per_file and not _per_file_events:
        return
    if not event_log.handlers:
        return
    record = {"v": EVENT_SCHEMA_VERSION, "ts": utc_now(), "event": event}
    if _side:
        record["side"] = _side
    if module:
        record["module"] = module
    for key, value in fields.items():
        if value is not None:
            record[key] = value
    try:
        event_log.info(json.dumps(record))
    except Exception as err:                       # logging must never crash
        log.debug("could not write event %s: %s", event, err)


def utc_now():
    """ISO-8601 UTC, milliseconds, e.g. 2026-09-25T20:32:14.108Z.

    UTC rather than local time: it sorts lexically and has no repeated hour
    when the clocks go back, both of which matter to whatever consumes this.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return "%s.%03dZ" % (now.strftime("%Y-%m-%dT%H:%M:%S"),
                         now.microsecond // 1000)


def find_config(explicit, script_file):
    """Return the config path: explicit > ./config.yaml > next to script."""
    if explicit:
        return explicit
    candidates = ["config.yaml",
                  os.path.join(os.path.dirname(os.path.abspath(script_file)),
                               "config.yaml")]
    for path in candidates:
        if os.path.isfile(path):
            return path
    raise ConfigError("config.yaml not found (looked in: %s)" % ", ".join(candidates))


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def parse_range(text):
    """Parse a 'start-end' range. End is EXCLUSIVE, as in the original code:
    '0-100' means addresses 0..99 (100 values)."""
    try:
        start_s, end_s = str(text).split("-")
        start, end = int(start_s), int(end_s)
    except ValueError:
        raise ConfigError("bad range %r (expected 'start-end', e.g. '0-100')" % (text,))
    if start < 0 or end <= start or end > 65536:
        raise ConfigError("bad range %r (need 0 <= start < end <= 65536)" % (text,))
    return start, end


def load_config(path):
    """Load and validate config.yaml. Returns a normalized dict.

    Normalization:
      * module 'type' is lower-cased ('Modbus' -> 'modbus')
      * modbus 'registers'/'coils' become lists of (start, end) tuples
      * a 'network' section is built from dyode_in/dyode_out (with defaults)
      * a 'serial' section gets defaults (used by DYODE v2)
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ConfigError("%s: top level must be a mapping" % path)

    modules = raw.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise ConfigError("%s: 'modules' must be a non-empty mapping" % path)

    cfg = dict(raw)
    cfg["modules"] = {}
    for name, props in modules.items():
        cfg["modules"][str(name)] = _normalize_module(str(name), props)

    d_in = raw.get("dyode_in") or {}
    d_out = raw.get("dyode_out") or {}
    net = dict(DEFAULT_NETWORK)
    if d_in.get("ip"):
        net["in_ip"] = str(d_in["ip"])
    if d_out.get("ip"):
        net["out_ip"] = str(d_out["ip"])
    if d_in.get("interface"):
        net["in_interface"] = str(d_in["interface"])
    if d_out.get("interface"):
        net["out_interface"] = str(d_out["interface"])
    net["out_mac"] = str(d_out["mac"]) if d_out.get("mac") else None
    cfg["network"] = net

    serial_cfg = dict(DEFAULT_SERIAL)
    serial_cfg.update(raw.get("serial") or {})
    serial_cfg["baudrate"] = int(serial_cfg["baudrate"])
    cfg["serial"] = serial_cfg

    cfg["logging"] = _normalize_logging(raw.get("logging"))
    return cfg


def _normalize_logging(raw):
    """Validate the optional 'logging' section."""
    log_cfg = dict(DEFAULT_LOGGING)
    if raw is None:
        return log_cfg
    if not isinstance(raw, dict):
        raise ConfigError("'logging' must be a mapping")
    unknown = set(raw) - set(DEFAULT_LOGGING)
    if unknown:
        raise ConfigError("'logging': unknown key(s) %s (expected %s)"
                          % (", ".join(sorted(unknown)),
                             ", ".join(sorted(DEFAULT_LOGGING))))
    for key in ("dir", "human", "json"):
        if key in raw:
            # An empty 'dir' is the documented way to keep stderr only.
            value = raw[key]
            if value is None or value is False:
                log_cfg[key] = "" if key == "dir" else DEFAULT_LOGGING[key]
                continue
            value = str(value).strip()
            if key != "dir" and ("/" in value or not value):
                raise ConfigError("'logging.%s' must be a bare file name, "
                                  "got %r" % (key, raw[key]))
            log_cfg[key] = value
    if "per_file_events" in raw:
        if not isinstance(raw["per_file_events"], bool):
            raise ConfigError("'logging.per_file_events' must be true or false,"
                              " got %r" % (raw["per_file_events"],))
        log_cfg["per_file_events"] = raw["per_file_events"]
    if log_cfg["dir"] and not os.path.isabs(log_cfg["dir"]):
        raise ConfigError("'logging.dir' must be an absolute path, got %r"
                          % log_cfg["dir"])
    if log_cfg["human"] == log_cfg["json"]:
        raise ConfigError("'logging.human' and 'logging.json' must differ")
    return log_cfg


def _normalize_module(name, props):
    if not isinstance(props, dict):
        raise ConfigError("module %r: properties must be a mapping" % name)
    props = dict(props)
    mtype = str(props.get("type", "")).strip().lower()
    if mtype not in KNOWN_TYPES:
        raise ConfigError("module %r: unknown type %r (expected one of %s)"
                          % (name, props.get("type"), ", ".join(KNOWN_TYPES)))
    props["type"] = mtype

    if "port" not in props:
        raise ConfigError("module %r: 'port' is required" % name)
    props["port"] = int(props["port"])

    if mtype in ("folder", "screen"):
        for key in ("in", "out"):
            if key not in props:
                raise ConfigError("module %r: '%s' folder is required" % (name, key))
        if mtype == "folder":
            if "bitrate" in props:
                try:
                    props["bitrate"] = int(props["bitrate"])
                except (TypeError, ValueError):
                    raise ConfigError("module %r: 'bitrate' must be a whole "
                                      "number of Mbit/s, got %r"
                                      % (name, props["bitrate"]))
                if props["bitrate"] < 1:
                    raise ConfigError("module %r: 'bitrate' must be at least 1"
                                      % name)
            props["fec"] = _normalize_fec(name, props.get("fec", DEFAULT_FEC))
            # Pause between files: the receiver must restart udp-receiver
            # between transfers and a diode gives it no way to say so.
            props["file_gap"] = _positive_number(
                name, "file_gap", props.get("file_gap", 0.5), allow_zero=True)
            # Sent files are retired under .dyode_sent rather than deleted,
            # because udp-sender exiting 0 is not proof of delivery.
            props["keep_sent_hours"] = _positive_number(
                name, "keep_sent_hours", props.get("keep_sent_hours", 24),
                allow_zero=True, integer=True)
            # How long an incomplete batch stays open before it is closed
            # and its missing files reported.
            props["batch_timeout"] = _positive_number(
                name, "batch_timeout", props.get("batch_timeout", 300.0),
                allow_zero=True)
            if props.get("staging") is not None:
                staging = str(props["staging"]).strip()
                if not os.path.isabs(staging):
                    raise ConfigError("module %r: 'staging' must be an "
                                      "absolute path, got %r"
                                      % (name, props["staging"]))
                out_abs = os.path.abspath(str(props["out"]))
                if os.path.commonpath([os.path.abspath(staging), out_abs]) \
                        == out_abs:
                    raise ConfigError(
                        "module %r: 'staging' must not be inside the output "
                        "folder, or anything serving that folder will see "
                        "partial files" % name)
                props["staging"] = staging
    elif mtype == "modbus":
        if "ip" not in props:
            raise ConfigError("module %r: PLC 'ip' is required" % name)
        props["port_out"] = int(props.get("port_out", 502))
        props["plc_port"] = int(props.get("plc_port", 502))
        props["unit"] = int(props.get("unit", 1))
        props["registers"] = [parse_range(r) for r in (props.get("registers") or [])]
        props["coils"] = [parse_range(c) for c in (props.get("coils") or [])]
        if not props["registers"] and not props["coils"]:
            raise ConfigError("module %r: no registers or coils configured" % name)
    return props


def _positive_number(name, key, value, allow_zero=False, integer=False):
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise ConfigError("module %r: %r must be a number, got %r"
                          % (name, key, value))
    if number < 0 or (number == 0 and not allow_zero):
        raise ConfigError("module %r: %r must be %s, got %r"
                          % (name, key,
                             "zero or more" if allow_zero else "more than zero",
                             value))
    return number


def _normalize_fec(name, value):
    """Return a udp-sender FEC ratio string, or None when disabled.

    Accepts a ratio ('8x16/64', '8x8'), or any of FEC_DISABLED / YAML null /
    YAML false to turn forward error correction off entirely.
    """
    if value is None or value is False:
        return None
    text = str(value).strip()
    if text.lower() in FEC_DISABLED:
        return None
    if not FEC_RE.match(text):
        raise ConfigError("module %r: 'fec' must look like '8x16/64' or be one "
                          "of %s, got %r"
                          % (name, "/".join(FEC_DISABLED[:3]), value))
    return text


def modules_of_type(cfg, mtype):
    return {n: p for n, p in cfg["modules"].items() if p["type"] == mtype}


# --------------------------------------------------------------------------
# Process supervision
# --------------------------------------------------------------------------

def supervise(targets, check_every=5.0, restart_delay=5.0):
    """Run each (name, function, args) in its own process; restart crashes.

    `function` must be a module-level function so it can be pickled: that
    keeps this working with the 'forkserver' and 'spawn' start methods
    (Python 3.14 made 'forkserver' the Linux default).
    """
    procs = {}

    def start(name, func, args):
        p = multiprocessing.Process(name=name, target=func, args=args, daemon=True)
        p.start()
        log.info("started module %r (pid %s)", name, p.pid)
        procs[name] = (p, func, args, time.monotonic())

    for name, func, args in targets:
        start(name, func, args)

    try:
        while True:
            time.sleep(check_every)
            for name, (p, func, args, started) in list(procs.items()):
                if p.is_alive():
                    continue
                log.error("module %r exited (code %s)", name, p.exitcode)
                # Avoid a tight crash loop if it dies right after starting.
                if time.monotonic() - started < restart_delay:
                    time.sleep(restart_delay)
                start(name, func, args)
    except KeyboardInterrupt:
        log.info("shutting down")
        for p, *_ in procs.values():
            p.terminate()
        for p, *_ in procs.values():
            p.join(timeout=5)


# --------------------------------------------------------------------------
# JSON messages
# --------------------------------------------------------------------------

def encode_json(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def decode_json(data):
    return json.loads(data.decode("ascii"))


# --------------------------------------------------------------------------
# UDP framing
#
# Each message is split into chunks. Every datagram carries a header:
#   magic(4) | msg_id(u32) | index(u16) | total(u16) | crc32 of message(u32)
# The receiver reassembles by msg_id, tolerates reordering, drops messages
# with missing chunks, and verifies the CRC before accepting. Unlike the
# original "empty datagram = end" scheme, a lost packet can no longer
# silently corrupt a message.
# --------------------------------------------------------------------------

UDP_MAGIC = b"DYO1"
UDP_HEADER = struct.Struct(">4sIHHI")
# 1400 bytes keeps each datagram inside a standard 1500-byte Ethernet frame,
# so there is no IP fragmentation (which multiplies the effect of loss).
UDP_CHUNK = 1400
UDP_MAX_DATAGRAM = UDP_HEADER.size + UDP_CHUNK


def udp_chunks(payload, msg_id=None, chunk_size=UDP_CHUNK):
    """Split payload bytes into framed datagrams."""
    if msg_id is None:
        msg_id = secrets.randbits(32)
    crc = zlib.crc32(payload)
    pieces = [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)] or [b""]
    if len(pieces) > 0xFFFF:
        raise ValueError("message too large (%d bytes)" % len(payload))
    total = len(pieces)
    return [UDP_HEADER.pack(UDP_MAGIC, msg_id, i, total, crc) + piece
            for i, piece in enumerate(pieces)]


def udp_send(sock, addr, payload, pace=0.0002):
    """Send one message as framed datagrams, pacing to avoid overrunning
    the receiver (there is no flow control on a diode)."""
    for datagram in udp_chunks(payload):
        sock.sendto(datagram, addr)
        if pace:
            time.sleep(pace)


class Reassembler:
    """Rebuild messages from framed datagrams. Pure logic, no sockets."""

    def __init__(self, max_pending=8):
        self.max_pending = max_pending
        self.pending = {}   # msg_id -> [total, crc, {index: piece}, first_seen]
        self.dropped = 0

    def feed(self, datagram):
        """Feed one datagram. Returns the complete payload, or None."""
        if len(datagram) < UDP_HEADER.size:
            return None
        magic, msg_id, index, total, crc = UDP_HEADER.unpack_from(datagram)
        if magic != UDP_MAGIC or total == 0 or index >= total:
            return None
        piece = datagram[UDP_HEADER.size:]

        entry = self.pending.get(msg_id)
        if entry is None:
            if len(self.pending) >= self.max_pending:
                # Oldest incomplete message will never finish: drop it.
                oldest = min(self.pending, key=lambda k: self.pending[k][3])
                del self.pending[oldest]
                self.dropped += 1
            entry = [total, crc, {}, time.monotonic()]
            self.pending[msg_id] = entry
        if entry[0] != total or entry[1] != crc:
            return None
        entry[2][index] = piece
        if len(entry[2]) < total:
            return None

        del self.pending[msg_id]
        payload = b"".join(entry[2][i] for i in range(total))
        if zlib.crc32(payload) != crc:
            self.dropped += 1
            return None
        # A complete message supersedes older partial ones (sender sends in
        # order), so clear them to free memory.
        self.pending.clear()
        return payload


# --------------------------------------------------------------------------
# Serial framing (DYODE v2)
#
# One message per line:  <crc32 as 8 hex digits> <space> <JSON> <\n>
# Compact JSON never contains a raw newline, so '\n' is a reliable
# delimiter. The original code sent base64 with no delimiter and relied on
# readline() timing out, which split and glued messages at random.
# --------------------------------------------------------------------------

def serial_encode(obj):
    body = encode_json(obj)
    return b"%08x " % zlib.crc32(body) + body + b"\n"


def serial_decode(line):
    """Decode one line (without or with trailing newline). Returns the object,
    or None if the line is damaged."""
    line = line.strip()
    if len(line) < 10 or line[8:9] != b" ":
        return None
    try:
        crc = int(line[:8], 16)
    except ValueError:
        return None
    body = line[9:]
    if zlib.crc32(body) != crc:
        return None
    try:
        return decode_json(body)
    except ValueError:
        return None


class LineBuffer:
    """Accumulate raw serial bytes and yield complete lines."""

    def __init__(self, max_line=4 * 1024 * 1024):
        self.buf = bytearray()
        self.max_line = max_line
        self.overflows = 0

    def feed(self, data):
        self.buf.extend(data)
        lines = []
        while True:
            idx = self.buf.find(b"\n")
            if idx < 0:
                break
            lines.append(bytes(self.buf[:idx]))
            del self.buf[:idx + 1]
        if len(self.buf) > self.max_line:
            # Garbage with no newline: discard rather than grow forever.
            self.buf.clear()
            self.overflows += 1
        return lines


def die(message):
    log.error(message)
    sys.exit(1)
