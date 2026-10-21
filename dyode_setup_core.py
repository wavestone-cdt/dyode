# -*- coding: utf-8 -*-
"""Logic behind dyode_setup.py, kept free of any user interface so that both
the curses and the plain-text front ends (and the tests) share it.

Nothing here prints or prompts; the wizard asks, this module computes.
"""

import datetime
import os
import re
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
V1_DIR = os.path.join(REPO_ROOT, "DYODE_v1_full")
V2_DIR = os.path.join(REPO_ROOT, "DYODE_v2_light")

# Import the runtime's own config loader so a generated file is validated by
# exactly the code that will later read it.
if V1_DIR not in sys.path:
    sys.path.insert(0, V1_DIR)
import dyode_common as common  # noqa: E402

DEFAULT_IN_IP = "10.0.1.1"
DEFAULT_OUT_IP = "10.0.1.2"
DEFAULT_PORTS = {"folder": 9600, "modbus": 9400, "screen": 9900, "logs": 9700}
ZERO_MAC = "00:00:00:00:00:00"

MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", re.I)
IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------

def valid_mac(text):
    return bool(MAC_RE.match(text.strip())) and text.strip().lower() != ZERO_MAC


def valid_ipv4(text):
    m = IPV4_RE.match(text.strip())
    return bool(m) and all(0 <= int(g) <= 255 for g in m.groups())


def valid_port(text):
    try:
        return 1 <= int(text) <= 65535
    except (TypeError, ValueError):
        return False


def valid_ranges(text):
    """Accept a comma-separated list of 'start-end' ranges, or empty."""
    text = text.strip()
    if not text:
        return True
    try:
        for part in text.split(","):
            common.parse_range(part.strip())
    except common.ConfigError:
        return False
    return True


def split_ranges(text):
    return [p.strip() for p in text.split(",") if p.strip()]


def valid_abs_path(text):
    return text.strip().startswith("/") and "\x00" not in text


def split_list(text):
    return [p.strip() for p in text.split(",") if p.strip()]


def valid_abs_path_list(text):
    """Comma-separated absolute paths or globs, or empty."""
    return all(valid_abs_path(p) for p in split_list(text))


def valid_network_list(text):
    """Empty, or comma-separated IPv4/IPv6 addresses or networks."""
    import ipaddress
    try:
        for item in split_list(text):
            ipaddress.ip_network(item, strict=False)
    except ValueError:
        return False
    return True


def valid_forward_list(text):
    """Comma-separated udp://host:port / tcp://host:port, or empty."""
    for item in split_list(text):
        m = re.match(r"^(udp|tcp)://([^\s:/]+):(\d+)$", item, re.I)
        if not m or not valid_port(m.group(3)):
            return False
    return True


# --------------------------------------------------------------------------
# Network interfaces
# --------------------------------------------------------------------------

class Iface:
    def __init__(self, name, mac, operstate, carrier, virtual, ipv4=None):
        self.name = name
        self.mac = mac
        self.operstate = operstate
        self.carrier = carrier
        self.virtual = virtual
        self.ipv4 = ipv4 or []

    def label(self):
        bits = [self.mac or "no MAC"]
        bits.append("link up" if self.carrier else "no link")
        if self.ipv4:
            bits.append(", ".join(self.ipv4))
        if self.virtual:
            bits.append("virtual")
        return "%-12s %s" % (self.name, "  |  ".join(bits))

    def __repr__(self):
        return "<Iface %s %s>" % (self.name, self.mac)


def _read(path, default=""):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def ipv4_addresses(sys_root="/sys/class/net"):
    """Map interface -> ['192.168.1.5/24', ...] using iproute2, best effort."""
    if sys_root != "/sys/class/net":       # tests use a fake tree: no live data
        return {}
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet":
            found.setdefault(parts[1], []).append(parts[3])
    return found


def list_interfaces(sys_root="/sys/class/net", include_virtual=False):
    """List network interfaces, real ones first. Reads sysfs directly so the
    installer needs no extra dependency."""
    try:
        names = sorted(os.listdir(sys_root))
    except OSError:
        return []
    addresses = ipv4_addresses(sys_root)
    out = []
    for name in names:
        base = os.path.join(sys_root, name)
        virtual = "/virtual/" in os.path.realpath(base) + "/"
        mac = _read(os.path.join(base, "address"))
        if mac.lower() == ZERO_MAC:
            mac = ""
        if virtual and not include_virtual:
            continue
        out.append(Iface(name=name, mac=mac,
                         operstate=_read(os.path.join(base, "operstate"), "unknown"),
                         carrier=_read(os.path.join(base, "carrier")) == "1",
                         virtual=virtual, ipv4=addresses.get(name, [])))
    out.sort(key=lambda i: (i.virtual, not i.carrier, i.name))
    return out


def serial_devices(dev_root="/dev"):
    """Candidate serial ports for DYODE v2, best guess first."""
    preferred = ["serial0", "serial1", "ttyAMA0", "ttyS0"]
    found = []
    for name in preferred:
        if os.path.exists(os.path.join(dev_root, name)):
            found.append(os.path.join(dev_root, name))
    try:
        for name in sorted(os.listdir(dev_root)):
            if name.startswith(("ttyUSB", "ttyACM")):
                path = os.path.join(dev_root, name)
                if path not in found:
                    found.append(path)
    except OSError:
        pass
    return found


# --------------------------------------------------------------------------
# Configuration model
# --------------------------------------------------------------------------

class Module:
    def __init__(self, name, props):
        self.name = name
        self.props = props

    @property
    def type(self):
        return str(self.props.get("type", "")).lower()


class ConfigModel:
    """Everything that goes into config.yaml, in a form the wizard can edit."""

    def __init__(self, variant="v1"):
        self.variant = variant                 # "v1" or "v2"
        self.name = "DYODE"
        self.version = 1.0
        self.date = datetime.date.today().isoformat()
        self.in_ip = DEFAULT_IN_IP
        self.out_ip = DEFAULT_OUT_IP
        self.in_mac = ""
        self.out_mac = ""
        self.in_if = "eth0"
        self.out_if = "eth1"
        self.serial_device = common.DEFAULT_SERIAL["device"]
        self.serial_baud = common.DEFAULT_SERIAL["baudrate"]
        self.modules = []
        self.auth = {}                         # config.yaml 'auth' section, kept as is
        self.imported_from = None              # path of an imported config.yaml

    # -- side helpers ------------------------------------------------------

    def set_local(self, side, interface, mac, ip):
        """Record the box the wizard is running on."""
        if side == "in":
            self.in_if, self.in_mac, self.in_ip = interface, mac, ip
        else:
            self.out_if, self.out_mac, self.out_ip = interface, mac, ip

    def set_peer(self, side, interface, mac, ip):
        return self.set_local(peer_side(side), interface, mac, ip)

    def peer_complete(self, side):
        """Has the other box's detail been filled in?"""
        other = peer_side(side)
        mac = self.out_mac if other == "out" else self.in_mac
        return valid_mac(mac or "")

    # -- module helpers ----------------------------------------------------

    def next_port(self, mtype):
        used = {int(m.props.get("port", 0)) for m in self.modules}
        port = DEFAULT_PORTS.get(mtype, 9000)
        while port in used:
            port += 1
        return port

    def add_module(self, name, props):
        self.modules.append(Module(name, props))

    def remove_module(self, index):
        del self.modules[index]

    # -- serialization -----------------------------------------------------

    @classmethod
    def from_yaml_text(cls, text, variant=None):
        import yaml
        raw = yaml.safe_load(text) or {}
        if not isinstance(raw, dict):
            raise ValueError("not a DYODE configuration file")
        model = cls(variant or ("v1" if "dyode_in" in raw or "dyode_out" in raw else "v2"))
        model.name = str(raw.get("config_name", model.name))
        model.version = raw.get("config_version", model.version)
        date = raw.get("config_date", model.date)
        model.date = date.isoformat() if hasattr(date, "isoformat") else str(date)
        d_in, d_out = raw.get("dyode_in") or {}, raw.get("dyode_out") or {}
        model.in_ip = str(d_in.get("ip", model.in_ip))
        model.out_ip = str(d_out.get("ip", model.out_ip))
        model.in_mac = str(d_in.get("mac", "") or "")
        model.out_mac = str(d_out.get("mac", "") or "")
        model.in_if = str(d_in.get("interface", model.in_if))
        model.out_if = str(d_out.get("interface", model.out_if))
        if isinstance(raw.get("auth"), dict):
            model.auth = dict(raw["auth"])
        serial_cfg = raw.get("serial") or {}
        model.serial_device = str(serial_cfg.get("device", model.serial_device))
        model.serial_baud = int(serial_cfg.get("baudrate", model.serial_baud))
        modules = raw.get("modules")
        if not isinstance(modules, dict) or not modules:
            raise ValueError("no modules found in that file")
        for name, props in modules.items():
            model.add_module(str(name), dict(props or {}))
        return model

    def to_yaml_text(self):
        """Emit a commented config.yaml. Written by hand rather than with
        yaml.dump so the comments survive."""
        L = ["# DYODE configuration, generated by dyode_setup.py on %s."
             % time.strftime("%Y-%m-%d %H:%M"),
             "# The SAME file must be present on both boxes.",
             "config_name: %s" % _quote(self.name),
             "config_version: %s" % self.version,
             "config_date: %s" % self.date,
             ""]
        if self.variant == "v1":
            L += ["dyode_in:",
                  "  ip: %s" % self.in_ip,
                  "  mac: %s" % (self.in_mac or "''"),
                  "  interface: %s      # NIC facing the diode on the input box" % self.in_if,
                  "dyode_out:",
                  "  ip: %s" % self.out_ip,
                  "  mac: %s" % (self.out_mac or "''"),
                  "  interface: %s      # NIC facing the diode on the output box" % self.out_if,
                  ""]
            if self.auth:
                L.append("auth:")
                for key in sorted(self.auth):
                    L.append("  %s: %s" % (key, _scalar(self.auth[key])))
                L.append("")
        else:
            L += ["serial:",
                  "  device: %s" % self.serial_device,
                  "  baudrate: %d" % self.serial_baud,
                  ""]
        L.append("modules:")
        if not self.modules:
            L.append("  {}")
        for mod in self.modules:
            L.append("  %s:" % _quote(mod.name))
            L.append("    type: %s" % mod.type)
            for key in ("port", "ip", "port_out", "plc_port", "unit", "interval",
                        "in", "out", "http_port", "max_fps", "settle", "bitrate",
                        "listen_udp", "listen_tcp", "copies", "max_kbps",
                        "format", "split_by_source", "unmapped",
                        "http_bind", "max_streams"):
                if key in mod.props:
                    L.append("    %s: %s" % (key, _scalar(mod.props[key])))
            for key in ("tail", "forward", "allow_from"):
                values = mod.props.get(key)
                if isinstance(values, str):
                    values = [values]
                if values:
                    L.append("    %s:" % key)
                    L += ["      - %s" % _quote(str(v)) for v in values]
            for key in ("registers", "coils"):
                values = mod.props.get(key)
                if values:
                    L.append("    %s:   # end excluded: 0-100 means 0..99" % key)
                    L += ["      - %s" % v for v in values]
        return "\n".join(L) + "\n"

    def validate(self):
        """Run the generated file through the runtime's own loader.
        Returns None if fine, else the error message."""
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".yaml")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(self.to_yaml_text())
            common.load_config(path)
        except (common.ConfigError, OSError, ValueError) as err:
            return str(err)
        finally:
            os.unlink(path)
        return None

    def warnings(self, side):
        """Non-fatal problems worth telling the user about."""
        out = []
        if self.variant == "v1" and not self.peer_complete(side):
            out.append("The %s box's MAC address is not set yet. Without it the "
                       "input box cannot send through the diode, because nothing "
                       "can answer ARP on a one-way link. Copy this config.yaml to "
                       "the other box, run this installer there, and bring the "
                       "updated file back."
                       % ("output" if side == "in" else "input"))
        if not self.modules:
            out.append("No modules are defined, so DYODE will not transfer anything.")
        for mod in self.modules:
            if mod.type == "logs" and (mod.props.get("listen_udp") or mod.props.get("listen_tcp")) \
                    and not mod.props.get("allow_from"):
                out.append("Logs module %r accepts syslog from any sender. Set "
                           "allow_from to the OT devices' networks, or anything that "
                           "can reach the input box can write into the logs." % mod.name)
        if self.variant == "v1" and self.in_if == self.out_if:
            out.append("Both sides use the interface %r. That is only correct if "
                       "the two boxes happen to name their NICs the same way."
                       % self.in_if)
        return out


def _quote(text):
    return '"%s"' % str(text).replace('\\', '\\\\').replace('"', '\\"')


def _scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    return text if re.match(r"^[A-Za-z0-9_./:-]+$", text) else _quote(text)


def peer_side(side):
    return "out" if side == "in" else "in"


def side_word(side):
    return "input (sending)" if side == "in" else "output (receiving)"


# --------------------------------------------------------------------------
# Where things go, and what gets written
# --------------------------------------------------------------------------

def target_dir(variant, side, repo_root=REPO_ROOT):
    if variant == "v1":
        return os.path.join(repo_root, "DYODE_v1_full")
    return os.path.join(repo_root, "DYODE_v2_light", "in" if side == "in" else "out")


def systemd_escape(text):
    """systemd-escape(1) for a path component used in a unit name.

    Interface names are usually plain, but a VLAN ('eth0.100') or an altname
    with a dash has to be escaped or the .device unit never matches and the
    dependency silently does nothing.
    """
    out = []
    for i, ch in enumerate(text):
        if ch == "/":
            out.append("-")
        elif ch.isalnum() or ch == "_":
            out.append(ch)
        elif ch == "." and i > 0:
            out.append(ch)
        else:
            out.append("\\x%02x" % ord(ch))
    return "".join(out)


def device_unit(interface):
    """The .device unit systemd creates for a network interface."""
    return "sys-subsystem-net-devices-%s.device" % systemd_escape(interface)


def wait_for_address_cmd(interface, seconds=60):
    """Shell that waits for the interface to have an IPv4 address.

    network-online.target only promises that *some* network is up.  The
    diode NIC is configured by hand, so at boot this service can easily
    start before that address exists -- and then the static ARP entry
    cannot be set and every transfer afterwards goes nowhere while both
    sides report success.  Waiting here is what makes the unit honest.
    """
    return ("/bin/sh -c 'for i in $(seq 1 %d); do "
            "ip -4 addr show dev %s 2>/dev/null | grep -q \"inet \" && exit 0; "
            "sleep 1; done; "
            "echo \"%s still has no IPv4 address after %ds\" >&2; exit 1'"
            % (seconds, interface, interface, seconds))


def systemd_unit_text(variant, side, workdir, python_exe=None, interface=None):
    script = "dyode_in.py" if side == "in" else "dyode_out.py"
    python_exe = python_exe or os.path.join(workdir, "venv", "bin", "python")
    reason = ("the static ARP entry" if side == "in"
              else "binding the Modbus server to port 502")
    lines = [
        "[Unit]",
        "Description=DYODE %s side (%s)" % (side_word(side), variant),
        "After=network-online.target",
        "Wants=network-online.target",
    ]
    if interface:
        lines += [
            "# Stop and restart with the diode NIC, and do not start without it.",
            "After=%s" % device_unit(interface),
            "BindsTo=%s" % device_unit(interface),
        ]
    lines += [
        # A diode may sit behind a network that takes a long time to come
        # back.  Without this, five quick restarts leave the unit dead for
        # good, and nothing is watching to notice.
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        "WorkingDirectory=%s" % workdir,
    ]
    if interface:
        lines.append("ExecStartPre=%s" % wait_for_address_cmd(interface))
    lines += [
        "ExecStart=%s %s" % (python_exe, script),
        "Restart=always",
        "RestartSec=5",
        # udpcast and the agents never read stdin; be explicit, because
        # udp-sender/udp-receiver change behaviour when it is a terminal.
        "StandardInput=null",
        # Creates /var/log/dyode-transfer with the right mode before start.
        "LogsDirectory=dyode-transfer",
        "LogsDirectoryMode=0750",
        "# root is needed for %s." % reason,
        "User=root",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    if interface:
        # BindsTo only propagates the STOP when the NIC disappears.  This is
        # what brings DYODE back when it reappears, so a link flap does not
        # leave a dead service nobody is watching.
        lines.append("WantedBy=%s" % device_unit(interface))
    lines.append("")
    return "\n".join(lines)


def unit_name(side):
    return "dyode-%s.service" % ("in" if side == "in" else "out")


def backup_and_write(path, text):
    """Write text to path, keeping a timestamped backup of any existing file.
    Returns the backup path, or None."""
    backup = None
    if os.path.exists(path):
        backup = "%s.backup-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return backup


class Plan:
    """What the wizard decided to do, so it can be shown before anything is
    written and then applied in one step."""

    def __init__(self, model, side, workdir, install_mode=None):
        self.model = model
        self.side = side
        self.workdir = workdir
        # "offline", "online", or None when the box is already installed.
        self.install_mode = install_mode
        self.config_path = os.path.join(workdir, "config.yaml")
        self.write_unit = False
        self.unit_path = None
        self.created = []
        self.backups = []
        self.key_path, self.key_action, self.key_source = self._plan_key()

    def _plan_key(self):
        """What to do about the message authentication key (v1 only):
        'keep' an existing one, 'copy' it from beside an imported
        config.yaml, 'create' one on the first box, or report it 'missing'
        (imported without a key: it must come from the other box)."""
        if self.model.variant != "v1":
            return None, None, None
        name = str(self.model.auth.get("key_file", common.DEFAULT_AUTH["key_file"]))
        path = name if os.path.isabs(name) else os.path.join(self.workdir, name)
        if os.path.exists(path):
            return path, "keep", None
        if self.model.imported_from:
            source = os.path.join(os.path.dirname(self.model.imported_from),
                                  os.path.basename(path))
            if os.path.isfile(source):
                return path, "copy", source
            return path, "missing", None
        return path, "create", None

    def describe(self):
        lines = ["Write %s" % self.config_path]
        if self.write_unit:
            lines.append("Write %s" % self.unit_path)
        if self.key_action == "create":
            lines.append("Create a new message authentication key %s" % self.key_path)
        elif self.key_action == "copy":
            lines.append("Copy the key %s to %s" % (self.key_source, self.key_path))
        elif self.key_action == "missing":
            lines.append("No key yet: copy dyode.key from the other box to %s"
                         % self.key_path)
        return lines

    def apply(self):
        backup = backup_and_write(self.config_path, self.model.to_yaml_text())
        self.created.append(self.config_path)
        if backup:
            self.backups.append(backup)
        if self.key_action == "create":
            common.generate_key_file(self.key_path)
            self.created.append(self.key_path)
        elif self.key_action == "copy":
            install_key_copy(self.key_source, self.key_path)
            self.created.append(self.key_path)
        if self.write_unit:
            iface = (self.model.in_if if self.side == "in"
                     else self.model.out_if)
            text = systemd_unit_text(self.model.variant, self.side,
                                     self.workdir, interface=iface)
            backup = backup_and_write(self.unit_path, text)
            self.created.append(self.unit_path)
            if backup:
                self.backups.append(backup)
        return self.created


def install_key_copy(source, dest):
    """Copy a key file, checking it first, readable by root only."""
    common.read_key_file(source)                 # raises if it is not a key
    with open(source, encoding="ascii") as fh:
        text = fh.read()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return dest


def venv_ready(workdir):
    """True when this folder already has a working virtualenv."""
    python = os.path.join(workdir, "venv", "bin", "python")
    return os.path.isfile(python) and os.access(python, os.X_OK)


def install_command(mode, variant, side):
    """The install.sh line for this box.  Offline uses only the files
    bundled in packaging/; online uses apt and PyPI."""
    parts = ["sudo ./install.sh"]
    if mode in ("offline", "online"):
        parts.append("--" + mode)        # otherwise install.sh asks
    parts += ["--variant", variant]
    if variant == "v2":
        parts += ["--side", side]
    return " ".join(parts)


def next_steps(plan):
    """Instructions shown after a successful run."""
    model, side, workdir = plan.model, plan.side, plan.workdir
    local_ip = model.in_ip if side == "in" else model.out_ip
    iface = model.in_if if side == "in" else model.out_if
    steps = []
    needs_udpcast = model.variant == "v1" and not shutil.which("udp-sender")
    if not venv_ready(workdir) or needs_udpcast:
        steps.append("Install the software%s:\n    cd %s\n    %s"
                     % (" (including udpcast)" if needs_udpcast else "",
                        _shell_quote(REPO_ROOT),
                        install_command(plan.install_mode, model.variant, side)))
    steps.append("Give the diode interface its address (make it permanent in your\n"
                 "network configuration; this command only lasts until reboot):\n"
                 "    sudo ip addr add %s/24 dev %s && sudo ip link set %s up"
                 % (local_ip, iface, iface))
    if model.variant == "v1" and not model.peer_complete(side):
        steps.append("Copy %s to the other box and run this installer there, then\n"
                     "bring the updated file back so both boxes match."
                     % _shell_quote(plan.config_path))
    if plan.key_action == "create":
        steps.append("Copy the new key %s to the other box, into the same\n"
                     "folder as its config.yaml (beside it, so importing the config\n"
                     "there picks it up). Both boxes need the SAME key: treat it like\n"
                     "a password, keep it off file shares and out of e-mail."
                     % _shell_quote(plan.key_path))
    elif plan.key_action == "missing":
        steps.append("Copy dyode.key from the other box to %s\n"
                     "(chmod 600). DYODE will not start without it."
                     % _shell_quote(plan.key_path))
    if plan.write_unit:
        steps.append("Enable the service:\n"
                     "    sudo cp %s /etc/systemd/system/%s\n"
                     "    sudo systemctl daemon-reload && sudo systemctl enable --now %s\n"
                     "    journalctl -u %s -f"
                     % (_shell_quote(plan.unit_path), unit_name(side),
                        unit_name(side), unit_name(side)))
    else:
        script = "dyode_in.py" if side == "in" else "dyode_out.py"
        steps.append("Start DYODE:\n    cd %s && sudo venv/bin/python %s --log-level DEBUG"
                     % (_shell_quote(workdir), script))
    steps.append("Set up log rotation and weekly archiving (logs go to\n"
                 "/var/log/dyode-transfer; edit the paths in the .service\n"
                 "first if DYODE is not installed in /opt/dyode):\n"
                 "    sudo cp packaging/logrotate/dyode-transfer /etc/logrotate.d/\n"
                 "    sudo cp packaging/systemd/dyode-log-archive.* /etc/systemd/system/\n"
                 "    sudo systemctl daemon-reload\n"
                 "    sudo systemctl enable --now dyode-log-archive.timer")
    return steps


def _shell_quote(path):
    return "'%s'" % path if " " in path else path
