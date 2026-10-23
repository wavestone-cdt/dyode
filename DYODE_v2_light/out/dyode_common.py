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
  * message authentication (HMAC-SHA256, shared key file)
  * serial line framing (newline-delimited, CRC check)
"""

import argparse
import collections
import datetime
import hashlib
import ipaddress
import hmac
import json
import logging
import logging.handlers
import multiprocessing
import os
import re
import secrets
import socket
import struct
import sys
import time
import zlib

import yaml

log = logging.getLogger("dyode")

KNOWN_TYPES = ("folder", "modbus", "screen", "logs")

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

# Message authentication.  Both boxes hold the same random key; every
# Modbus, log and screen message and every folder manifest is signed with
# it, so nothing that merely reaches the output box's diode ports -- from
# the IT network, say -- can pass for data from the input box.
DEFAULT_AUTH = {
    "key_file": "dyode.key",   # relative to the folder holding config.yaml
    "required": True,
}
AUTH_KEY_BYTES = 32


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
    # How often the input box re-asserts the static ARP entry.  It is lost
    # when the diode NIC goes down and comes back, and nothing on a one-way
    # link will ever tell us so.
    net["arp_interval"] = _positive_number(
        "dyode_in", "arp_interval", d_in.get("arp_interval", 60.0))
    cfg["network"] = net

    serial_cfg = dict(DEFAULT_SERIAL)
    serial_cfg.update(raw.get("serial") or {})
    serial_cfg["baudrate"] = int(serial_cfg["baudrate"])
    cfg["serial"] = serial_cfg

    cfg["logging"] = _normalize_logging(raw.get("logging"))
    cfg["auth"] = _normalize_auth(raw.get("auth"), os.path.dirname(os.path.abspath(path)))
    return cfg


def _normalize_auth(raw, base_dir):
    """Validate the optional 'auth' section; the key file path becomes
    absolute (relative paths are taken from the config file's folder)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("'auth' must be a mapping")
    unknown = set(raw) - set(DEFAULT_AUTH)
    if unknown:
        raise ConfigError("'auth': unknown key(s) %s" % ", ".join(sorted(unknown)))
    auth = dict(DEFAULT_AUTH)
    auth.update(raw)
    if not isinstance(auth["required"], bool):
        raise ConfigError("'auth.required' must be true or false")
    key_file = str(auth["key_file"])
    if not os.path.isabs(key_file):
        key_file = os.path.join(base_dir, key_file)
    auth["key_file"] = key_file
    return auth


def generate_key_file(path):
    """Write a new random key (hex) to path, mode 0600.  Refuses to
    overwrite: replacing the key on one box only breaks the link."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secrets.token_hex(AUTH_KEY_BYTES) + "\n")
    return path


def read_key_file(path):
    """Return the key bytes from a hex key file, or raise ConfigError."""
    try:
        with open(path) as fh:
            text = fh.read().strip()
    except OSError as err:
        raise ConfigError("cannot read authentication key %s: %s" % (path, err.strerror))
    try:
        key = bytes.fromhex(text)
    except ValueError:
        raise ConfigError("authentication key %s is not hex" % path)
    if len(key) < 16:
        raise ConfigError("authentication key %s is too short (need at least 16 bytes)" % path)
    return key


def message_auth(cfg):
    """MessageAuth for this config, or None when authentication is off.
    A missing key with auth.required (the default) is a configuration
    error: running unsigned would silently undo the protection."""
    auth = cfg.get("auth")
    if auth is None:            # a config dict not built by load_config (tests)
        return None
    if not os.path.exists(auth["key_file"]):
        if auth.get("required", True):
            raise ConfigError(
                "no message authentication key at %s. Create one on ONE box with "
                "'python3 dyode_setup.py --new-key' (in the DYODE "
                "folder) and copy it to the other box beside config.yaml (or set auth.required: false to run "
                "unauthenticated)" % auth["key_file"])
        log.warning("auth.required is false and %s is missing: messages are NOT "
                    "authenticated", auth["key_file"])
        return None
    return MessageAuth(read_key_file(auth["key_file"]))


def require_auth(cfg):
    """message_auth(), exiting with the error instead of raising it."""
    try:
        return message_auth(cfg)
    except ConfigError as err:
        die("configuration error: %s" % err)


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
            # udp-sender --autostart: hello retransmissions before data
            # starts, which on a diode is the only grace period the output
            # box gets to have udp-receiver listening.
            props["autostart"] = _positive_number(
                name, "autostart", props.get("autostart", 5), integer=True)
            # udp-receiver --start-timeout; 0 waits indefinitely.
            props["start_timeout"] = _positive_number(
                name, "start_timeout", props.get("start_timeout", 300),
                allow_zero=True, integer=True)
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
        if mtype == "screen":
            # Viewers at once; each holds a thread and a connection open.
            props["max_streams"] = _positive_number(
                name, "max_streams", props.get("max_streams", 8), integer=True)
            if props.get("http_bind") not in (None, ""):
                try:
                    props["http_bind"] = str(ipaddress.IPv4Address(str(props["http_bind"])))
                except ValueError:
                    raise ConfigError("module %r: 'http_bind' must be an IPv4 address, "
                                      "got %r" % (name, props["http_bind"]))
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
        # How the output box's Modbus server answers for an address that is
        # not in any configured range: Modbus exception 02 (Illegal Data
        # Address), or 0 as before 1.5.0.
        props["unmapped"] = str(props.get("unmapped", "exception")).lower()
        if props["unmapped"] not in ("exception", "zero"):
            raise ConfigError("module %r: 'unmapped' must be exception or zero" % name)
    elif mtype == "logs":
        # Validation lives with the module.  DYODE v2 has no logstream.py
        # (it ignores 'logs' modules with a warning), so a missing module
        # just skips the detailed checks there.
        try:
            import logstream
        except ImportError:
            return props
        props = logstream.normalize_props(name, props, ConfigError)
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


def udp_send(sock, addr, payload, pace=0.0002, auth=None):
    """Send one message as framed datagrams, pacing to avoid overrunning
    the receiver (there is no flow control on a diode).  With `auth`, the
    message is sealed (signed) first."""
    if auth is not None:
        payload = auth.seal(payload)
    for datagram in udp_chunks(payload):
        sock.sendto(datagram, addr)
        if pace:
            time.sleep(pace)


class Reassembler:
    """Rebuild messages from framed datagrams. Pure logic, no sockets.

    Bounded: a piece larger than DYODE ever sends, a message announcing more
    pieces than this listener's largest real message, more than `max_bytes`
    held in total, or a partial message older than `max_age` seconds is
    dropped.  Without these, anything able to send to the port could make it
    hold gigabytes of never-completed messages.
    """

    def __init__(self, max_pending=8, max_pieces=1024, max_bytes=32 * 1024 * 1024,
                 max_age=10.0, clock=time.monotonic):
        self.max_pending = max_pending
        self.max_pieces = max_pieces
        self.max_bytes = max_bytes
        self.max_age = max_age
        self.clock = clock
        self.pending = {}   # msg_id -> [total, crc, {index: piece}, first_seen]
        self.held = 0
        self.dropped = 0

    def _forget(self, msg_id):
        entry = self.pending.pop(msg_id)
        self.held -= sum(len(p) for p in entry[2].values())

    def _expire(self):
        cutoff = self.clock() - self.max_age
        for msg_id in [k for k, e in self.pending.items() if e[3] < cutoff]:
            self._forget(msg_id)
            self.dropped += 1

    def feed(self, datagram):
        """Feed one datagram. Returns the complete payload, or None."""
        if len(datagram) < UDP_HEADER.size or len(datagram) > UDP_MAX_DATAGRAM:
            return None
        magic, msg_id, index, total, crc = UDP_HEADER.unpack_from(datagram)
        if magic != UDP_MAGIC or total == 0 or index >= total or total > self.max_pieces:
            return None
        piece = datagram[UDP_HEADER.size:]
        self._expire()

        entry = self.pending.get(msg_id)
        if entry is None:
            if len(self.pending) >= self.max_pending:
                # Oldest incomplete message will never finish: drop it.
                oldest = min(self.pending, key=lambda k: self.pending[k][3])
                self._forget(oldest)
                self.dropped += 1
            entry = [total, crc, {}, self.clock()]
            self.pending[msg_id] = entry
        if entry[0] != total or entry[1] != crc or index in entry[2]:
            return None
        while self.held + len(piece) > self.max_bytes and self.pending:
            oldest = min(self.pending, key=lambda k: self.pending[k][3])
            self._forget(oldest)
            self.dropped += 1
            if oldest == msg_id:
                return None
        entry[2][index] = piece
        self.held += len(piece)
        if len(entry[2]) < total:
            return None

        self._forget(msg_id)
        payload = b"".join(entry[2][i] for i in range(total))
        if zlib.crc32(payload) != crc:
            self.dropped += 1
            return None
        # A complete message supersedes older partial ones (sender sends in
        # order), so clear them to free memory.
        self.pending.clear()
        self.held = 0
        return payload


# --------------------------------------------------------------------------
# Message authentication
#
# sealed = b"DYA1" | boot id (8) | counter (u64) | message | HMAC-SHA256 (32)
# The HMAC covers everything before it.  The boot id is random per sender
# process and the counter rises with every message, so a captured message
# cannot be replayed into the same stream.  Reordering within `window` is
# tolerated (log batches are sent twice and may arrive out of order).
# --------------------------------------------------------------------------

AUTH_MAGIC = b"DYA1"
AUTH_HEAD = struct.Struct(">4s8sQ")
AUTH_TAG = 32


class MessageAuth:
    def __init__(self, key, window=4096, max_streams=8):
        self.key = key
        self.boot = secrets.token_bytes(8)
        self.counter = 0
        self.window = window
        self.max_streams = max_streams
        self.streams = collections.OrderedDict()   # boot -> [highest, set(recent)]
        self.rejected = 0
        self._warned_at = None
        self._warned_count = 0

    def _tag(self, data):
        return hmac.new(self.key, data, hashlib.sha256).digest()

    def seal(self, payload):
        self.counter += 1
        head = AUTH_HEAD.pack(AUTH_MAGIC, self.boot, self.counter)
        return head + payload + self._tag(head + payload)

    def sign_text(self, text):
        """Hex HMAC of a text, for fields inside a JSON document."""
        return self._tag(text.encode("utf-8")).hex()

    def verify_text(self, text, mac):
        return isinstance(mac, str) and hmac.compare_digest(self.sign_text(text), mac)

    def open(self, sealed):
        """Return the message if `sealed` is genuine and new, else None."""
        if len(sealed) < AUTH_HEAD.size + AUTH_TAG:
            self.rejected += 1
            return None
        magic, boot, counter = AUTH_HEAD.unpack_from(sealed)
        body, tag = sealed[:-AUTH_TAG], sealed[-AUTH_TAG:]
        if magic != AUTH_MAGIC or not hmac.compare_digest(self._tag(body), tag):
            self.rejected += 1
            return None
        stream = self.streams.get(boot)
        if stream is None:
            stream = [0, set()]
            self.streams[boot] = stream
            while len(self.streams) > self.max_streams:
                self.streams.popitem(last=False)
        else:
            self.streams.move_to_end(boot)
        highest, recent = stream
        if counter <= highest - self.window or counter in recent:
            self.rejected += 1          # replayed, or far too old
            return None
        recent.add(counter)
        if counter > highest:
            stream[0] = counter
            floor = counter - self.window
            stream[1] = {c for c in recent if c > floor}
        return body[AUTH_HEAD.size:]

    def unseal(self, sealed, module=None, every=60.0):
        """open(), logging rejections at most once per `every` seconds."""
        msg = self.open(sealed)
        if msg is None:
            now = time.monotonic()
            if self._warned_at is None or now - self._warned_at >= every:
                log.warning("module %r: %d message(s) rejected: not signed with "
                            "this box's key, or replayed", module,
                            self.rejected - self._warned_count)
                self._warned_at, self._warned_count = now, self.rejected
        return msg


# --------------------------------------------------------------------------
# Receiving sockets
# --------------------------------------------------------------------------

def diode_socket(cfg, port, rcvbuf=None, bind=None):
    """UDP socket for traffic from the input box: bound to out_ip and, where
    the kernel allows it, to the diode NIC itself (SO_BINDTODEVICE), so a
    datagram that reaches out_ip through another interface is not seen.
    `bind` overrides the address (tests) and skips the device binding."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if rcvbuf:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    if bind is None:
        iface = cfg["network"].get("out_interface")
        try:
            if not iface:
                raise OSError("no out_interface configured")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                            iface.encode() + b"\0")
        except (OSError, AttributeError) as err:
            log.warning("cannot bind to interface %s (%s); relying on the "
                        "firewall to keep other interfaces out", iface, err)
        bind = (cfg["network"]["out_ip"], port)
    sock.bind(bind)
    return sock


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
