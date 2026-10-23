import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import _setup
import dyode_common as common

sys.path.insert(0, _setup.REPO)
import dyode_setup as wizard          # noqa: E402
import dyode_setup_core as core       # noqa: E402


def fake_sysfs(spec):
    """Build a /sys/class/net lookalike. spec: name -> (mac, carrier, virtual)."""
    root = tempfile.mkdtemp()
    net = os.path.join(root, "class", "net")
    os.makedirs(net)
    for name, (mac, carrier, virtual) in spec.items():
        kind = "virtual" if virtual else "pci0000:00"
        real = os.path.join(root, "devices", kind, "net", name)
        os.makedirs(real)
        for fname, value in (("address", mac), ("carrier", "1" if carrier else "0"),
                             ("operstate", "up" if carrier else "down")):
            with open(os.path.join(real, fname), "w") as fh:
                fh.write(value + "\n")
        os.symlink(real, os.path.join(net, name))
    return net


class ScriptedUi:
    """Answers the wizard from a script, so the flow can be tested without
    a terminal. Each entry is (kind, value); choose values match a label."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def _next(self, kind, context):
        if not self.script:
            raise AssertionError("ran out of answers at %s: %r" % (kind, context))
        want, value = self.script.pop(0)
        assert want == kind, "expected %s, wizard asked %s (%r)" % (want, kind, context)
        self.seen.append((kind, context, value))
        return value

    def intro(self, title, body):
        self.seen.append(("intro", title, None))

    info = intro

    def error(self, message):
        self.seen.append(("error", message, None))

    def choose(self, title, body, options):
        value = self._next("choose", title)
        labels = [label for label, _ in options]
        matches = [i for i, label in enumerate(labels) if value.lower() in label.lower()]
        assert matches, "%r not among %r (at %r)" % (value, labels, title)
        return matches[0]

    def text(self, title, label, default="", validator=None, help_text=""):
        value = self._next("text", label)
        value = default if value is None else value
        assert validator is None or validator(value), "%r rejected for %r" % (value, label)
        return value

    def confirm(self, title, body, default=True):
        return self._next("confirm", title)


class InterfaceTests(unittest.TestCase):
    def test_lists_real_interfaces_link_up_first(self):
        net = fake_sysfs({"eth0": ("b8:27:eb:00:00:01", False, False),
                          "eth1": ("b8:27:eb:00:00:02", True, False),
                          "lo": ("00:00:00:00:00:00", True, True)})
        names = [i.name for i in core.list_interfaces(net)]
        self.assertEqual(names, ["eth1", "eth0"])          # link up first, no lo
        self.assertIn("lo", [i.name for i in core.list_interfaces(net, include_virtual=True)])

    def test_mac_is_read_from_sysfs(self):
        net = fake_sysfs({"enx0011": ("aa:bb:cc:dd:ee:ff", True, False)})
        iface = core.list_interfaces(net)[0]
        self.assertEqual(iface.mac, "aa:bb:cc:dd:ee:ff")
        self.assertIn("link up", iface.label())

    def test_placeholder_mac_ignored(self):
        net = fake_sysfs({"eth0": ("00:00:00:00:00:00", True, False)})
        self.assertEqual(core.list_interfaces(net)[0].mac, "")

    def test_serial_devices_prefers_serial0(self):
        d = tempfile.mkdtemp()
        for name in ("ttyAMA0", "serial0", "ttyUSB3"):
            open(os.path.join(d, name), "w").close()
        self.assertEqual(core.serial_devices(d),
                         [os.path.join(d, n) for n in ("serial0", "ttyAMA0", "ttyUSB3")])


class ValidatorTests(unittest.TestCase):
    def test_macs(self):
        self.assertTrue(core.valid_mac("B8:27:EB:1a:2b:3c"))
        for bad in ("", "00:00:00:00:00:00", "b8:27:eb:1a:2b", "zz:27:eb:1a:2b:3c"):
            self.assertFalse(core.valid_mac(bad), bad)

    def test_ips_and_ranges(self):
        self.assertTrue(core.valid_ipv4("10.0.1.2"))
        for bad in ("10.0.1", "10.0.1.256", "abc"):
            self.assertFalse(core.valid_ipv4(bad), bad)
        self.assertTrue(core.valid_ranges("0-100, 400-450"))
        self.assertTrue(core.valid_ranges(""))
        for bad in ("100-0", "0-", "5"):
            self.assertFalse(core.valid_ranges(bad), bad)


class ConfigModelTests(unittest.TestCase):
    def v1_model(self):
        m = core.ConfigModel("v1")
        m.set_local("in", "eth0", "b8:27:eb:00:00:01", "10.0.1.1")
        m.set_peer("in", "eth1", "b8:27:eb:00:00:02", "10.0.1.2")
        m.add_module("Automate 1", {"type": "modbus", "port": 9400, "ip": "192.168.1.10",
                                    "port_out": 502, "registers": ["0-100", "400-450"],
                                    "coils": ["0-10"]})
        m.add_module("Files", {"type": "folder", "port": 9600,
                               "in": "/home/pi/in", "out": "/home/pi/out"})
        return m

    def test_generated_config_passes_the_runtime_loader(self):
        model = self.v1_model()
        self.assertIsNone(model.validate())
        path = os.path.join(tempfile.mkdtemp(), "config.yaml")
        core.backup_and_write(path, model.to_yaml_text())
        cfg = common.load_config(path)
        self.assertEqual(cfg["network"]["out_mac"], "b8:27:eb:00:00:02")
        self.assertEqual(cfg["network"]["in_interface"], "eth0")
        self.assertEqual(cfg["modules"]["Automate 1"]["registers"], [(0, 100), (400, 450)])
        self.assertEqual(cfg["modules"]["Files"]["in"], "/home/pi/in")

    def test_roundtrip_through_yaml(self):
        text = self.v1_model().to_yaml_text()
        again = core.ConfigModel.from_yaml_text(text, "v1")
        self.assertEqual(again.to_yaml_text().split("\n")[3:], text.split("\n")[3:])

    def test_second_box_imports_and_keeps_peer_details(self):
        """The core of the two-box flow: box B imports box A's file, adds its
        own interface and MAC, and both sides end up complete."""
        first = core.ConfigModel("v1")
        first.set_local("in", "eth0", "b8:27:eb:00:00:01", "10.0.1.1")
        first.add_module("PLC", {"type": "modbus", "port": 9400, "ip": "192.168.1.10",
                                 "port_out": 502, "registers": ["0-10"], "coils": []})
        self.assertFalse(first.peer_complete("in"))
        self.assertIn("MAC address is not set", " ".join(first.warnings("in")))

        second = core.ConfigModel.from_yaml_text(first.to_yaml_text(), "v1")
        second.set_local("out", "eth1", "b8:27:eb:00:00:02", "10.0.1.2")
        self.assertTrue(second.peer_complete("out"))
        self.assertEqual(second.in_mac, "b8:27:eb:00:00:01")
        self.assertEqual([m.name for m in second.modules], ["PLC"])
        self.assertEqual(second.warnings("out"), [])
        cfg_dir = tempfile.mkdtemp()
        path = os.path.join(cfg_dir, "config.yaml")
        core.backup_and_write(path, second.to_yaml_text())
        cfg = common.load_config(path)
        self.assertEqual(cfg["network"]["out_mac"], "b8:27:eb:00:00:02")

    def test_v2_config(self):
        m = core.ConfigModel("v2")
        m.serial_device, m.serial_baud = "/dev/ttyS0", 115200
        m.add_module("PLC", {"type": "modbus", "port": 9400, "ip": "192.168.0.5",
                             "port_out": 502, "registers": ["0-105"], "coils": ["0-1"]})
        self.assertIsNone(m.validate())
        self.assertNotIn("dyode_in", m.to_yaml_text())
        path = os.path.join(tempfile.mkdtemp(), "config.yaml")
        core.backup_and_write(path, m.to_yaml_text())
        self.assertEqual(common.load_config(path)["serial"],
                         {"device": "/dev/ttyS0", "baudrate": 115200})

    def test_ports_do_not_collide(self):
        m = core.ConfigModel("v1")
        for i in range(3):
            m.add_module("m%d" % i, {"type": "modbus", "port": m.next_port("modbus")})
        self.assertEqual([mod.props["port"] for mod in m.modules], [9400, 9401, 9402])

    def test_existing_file_is_backed_up(self):
        path = os.path.join(tempfile.mkdtemp(), "config.yaml")
        core.backup_and_write(path, "first\n")
        backup = core.backup_and_write(path, "second\n")
        self.assertIsNotNone(backup)
        with open(backup) as fh:
            self.assertEqual(fh.read(), "first\n")
        with open(path) as fh:
            self.assertEqual(fh.read(), "second\n")

    def test_systemd_unit(self):
        unit = core.systemd_unit_text("v1", "out", "/opt/dyode/DYODE_v1_full")
        self.assertIn("ExecStart=/opt/dyode/DYODE_v1_full/venv/bin/python dyode_out.py", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("port 502", unit)
        # udp-sender/udp-receiver behave differently on a terminal.
        self.assertIn("StandardInput=null", unit)

    def test_unit_waits_for_the_diode_nic(self):
        """network-online.target does not mean the hand-configured diode
        interface has its address; starting without it silently moves
        nothing."""
        unit = core.systemd_unit_text("v1", "in", "/opt/dyode/DYODE_v1_full",
                                      interface="eth0")
        device = "sys-subsystem-net-devices-eth0.device"
        self.assertIn("After=%s" % device, unit)
        self.assertIn("BindsTo=%s" % device, unit)
        # BindsTo only propagates the stop; this restarts after a flap.
        self.assertIn("WantedBy=%s" % device, unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertIn("ExecStartPre=", unit)
        self.assertIn("ip -4 addr show dev eth0", unit)
        # A long outage must not leave the unit permanently dead.
        self.assertIn("StartLimitIntervalSec=0", unit)

    def test_unit_without_an_interface_has_no_device_dependency(self):
        unit = core.systemd_unit_text("v1", "in", "/opt/dyode/DYODE_v1_full")
        self.assertNotIn("sys-subsystem-net-devices", unit)
        self.assertNotIn("ExecStartPre=", unit)

    def test_every_directive_is_in_a_section_and_well_formed(self):
        unit = core.systemd_unit_text("v1", "in", "/opt/dyode/DYODE_v1_full",
                                      interface="eth0")
        section = None
        for line in unit.splitlines():
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line
                continue
            self.assertIsNotNone(section, "directive before any section: %r" % line)
            self.assertIn("=", line, "not a directive: %r" % line)

    def test_device_unit_names_are_escaped(self):
        self.assertEqual(core.device_unit("eth0"),
                         "sys-subsystem-net-devices-eth0.device")
        # A VLAN keeps its dot; a dash must be escaped or the unit never
        # matches and the dependency quietly does nothing.
        self.assertEqual(core.device_unit("eth0.100"),
                         "sys-subsystem-net-devices-eth0.100.device")
        self.assertEqual(core.device_unit("br-lan"),
                         "sys-subsystem-net-devices-br\\x2dlan.device")

    def test_generated_unit_uses_the_side_s_own_interface(self):
        model = core.ConfigModel()
        model.variant, model.in_if, model.out_if = "v1", "enp1s0", "enp2s0"
        model.in_ip, model.out_ip = "10.0.1.1", "10.0.1.2"
        model.out_mac = "b8:27:eb:b1:ff:ab"
        model.modules = [core.Module("m", {"type": "folder", "port": 9600,
                                           "in": "/srv/in",
                                           "out": "/srv/out"})]
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        for sub in ("DYODE_v1_full",):
            os.makedirs(os.path.join(repo, sub), exist_ok=True)
        for side, expected in (("in", "enp1s0"), ("out", "enp2s0")):
            plan = core.Plan(model, side, core.target_dir("v1", side, repo))
            plan.write_unit = True
            plan.unit_path = os.path.join(plan.workdir, core.unit_name(side))
            plan.apply()
            with open(plan.unit_path) as fh:
                self.assertIn("dev %s" % expected, fh.read())


class WizardFlowTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        for sub in ("DYODE_v1_full", "DYODE_v2_light/in", "DYODE_v2_light/out"):
            os.makedirs(os.path.join(self.repo, sub))
        self.net = fake_sysfs({"eth0": ("b8:27:eb:00:00:01", True, False),
                               "eth1": ("b8:27:eb:00:00:02", False, False)})
        self.args = wizard.parse_args(["--repo", self.repo, "--sys-root", self.net,
                                  "--install-mode", "offline"])

    def run_wizard(self, script, args=None):
        ui = ScriptedUi(script)
        plan = wizard.run_wizard(ui, args or self.args)
        return ui, plan

    def test_first_box_input_side(self):
        ui, plan = self.run_wizard([
            ("choose", "v1"), ("choose", "Input"),
            ("choose", "Start a new"), ("text", "Lab diode"),
            ("choose", "eth0"), ("text", "10.0.1.1"),
            ("text", "10.0.1.2"), ("text", ""), ("text", "eth1"),
            ("choose", "Add"), ("choose", "modbus"), ("text", "PLC one"),
            ("text", "192.168.1.10"), ("text", "502"), ("text", "0-100"), ("text", "0-10"),
            ("choose", "Done"), ("confirm", True)])
        self.assertEqual(plan.side, "in")
        self.assertEqual(plan.model.in_mac, "b8:27:eb:00:00:01")   # read from sysfs
        self.assertEqual(plan.model.out_mac, "")                   # left for later
        self.assertTrue(plan.write_unit)
        self.assertEqual(plan.config_path,
                         os.path.join(self.repo, "DYODE_v1_full", "config.yaml"))
        self.assertTrue(wizard.review_and_apply(ScriptedUi([("confirm", True)]), plan))
        cfg = common.load_config(plan.config_path)
        self.assertEqual(cfg["modules"]["PLC one"]["registers"], [(0, 100)])
        self.assertTrue(os.path.exists(os.path.join(
            self.repo, "DYODE_v1_full", "dyode-in.service")))

    V1_INPUT_SCRIPT = [
        ("choose", "Start a new"), ("text", "Lab diode"),
        ("choose", "eth0"), ("text", "10.0.1.1"),
        ("text", "10.0.1.2"), ("text", ""), ("text", "eth1"),
        ("choose", "Add"), ("choose", "modbus"), ("text", "PLC one"),
        ("text", "192.168.1.10"), ("text", "502"), ("text", "0-100"), ("text", "0-10"),
        ("choose", "Done"), ("confirm", True)]

    def args_without_mode(self):
        return wizard.parse_args(["--repo", self.repo, "--sys-root", self.net])

    def test_install_question_asked_when_nothing_is_installed(self):
        ui, plan = self.run_wizard(
            [("choose", "v1"), ("choose", "Input"), ("choose", "Offline")]
            + self.V1_INPUT_SCRIPT, args=self.args_without_mode())
        self.assertEqual(plan.install_mode, "offline")
        self.assertIn("Installing software", [s[1] for s in ui.seen])
        steps = "\n".join(core.next_steps(plan))
        self.assertIn("sudo ./install.sh --offline --variant v1", steps)

    def test_install_question_online_answer(self):
        _, plan = self.run_wizard(
            [("choose", "v1"), ("choose", "Input"), ("choose", "Online")]
            + self.V1_INPUT_SCRIPT, args=self.args_without_mode())
        self.assertEqual(plan.install_mode, "online")
        self.assertIn("sudo ./install.sh --online --variant v1",
                      "\n".join(core.next_steps(plan)))

    def test_install_question_skipped_once_installed(self):
        venv_bin = os.path.join(self.repo, "DYODE_v1_full", "venv", "bin")
        os.makedirs(venv_bin)
        python = os.path.join(venv_bin, "python")
        with open(python, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(python, 0o755)
        ui, plan = self.run_wizard(
            [("choose", "v1"), ("choose", "Input")] + self.V1_INPUT_SCRIPT,
            args=self.args_without_mode())
        self.assertIsNone(plan.install_mode)
        self.assertNotIn("Installing software", [s[1] for s in ui.seen])

    def test_install_command_shapes(self):
        self.assertEqual(core.install_command("offline", "v2", "out"),
                         "sudo ./install.sh --offline --variant v2 --side out")
        self.assertEqual(core.install_command("online", "v1", "in"),
                         "sudo ./install.sh --online --variant v1")
        # No choice made: leave it to install.sh to ask.
        self.assertEqual(core.install_command(None, "v1", "in"),
                         "sudo ./install.sh --variant v1")

    def test_second_box_imports_first_box_config(self):
        _, first = self.run_wizard([
            ("choose", "v1"), ("choose", "Input"),
            ("choose", "Start a new"), ("text", "Lab diode"),
            ("choose", "eth0"), ("text", "10.0.1.1"),
            ("text", "10.0.1.2"), ("text", ""), ("text", "eth1"),
            ("choose", "Add"), ("choose", "modbus"), ("text", "PLC one"),
            ("text", "192.168.1.10"), ("text", "502"), ("text", "0-100"), ("text", ""),
            ("choose", "Done"), ("confirm", False)])
        wizard.review_and_apply(ScriptedUi([("confirm", True)]), first)

        args = wizard.parse_args(["--repo", self.repo, "--sys-root", self.net,
                                  "--install-mode", "offline",
                                  "--import-config", first.config_path])
        _, second = self.run_wizard([
            ("choose", "v1"), ("choose", "Output"),
            ("choose", "eth1"), ("text", "10.0.1.2"),
            ("choose", "Done"), ("confirm", False)], args)
        self.assertEqual(second.model.out_mac, "b8:27:eb:00:00:02")
        self.assertEqual(second.model.in_mac, "b8:27:eb:00:00:01")
        self.assertEqual([m.name for m in second.model.modules], ["PLC one"])
        self.assertEqual(second.model.warnings("out"), [])

    def test_v2_serial_flow(self):
        dev = tempfile.mkdtemp()
        open(os.path.join(dev, "serial0"), "w").close()
        args = wizard.parse_args(["--repo", self.repo, "--sys-root", self.net,
                                  "--install-mode", "offline",
                                  "--dev-root", dev])
        _, plan = self.run_wizard([
            ("choose", "v2"), ("choose", "Output"),
            ("choose", "Start a new"), ("text", "Serial diode"),
            ("choose", "serial0"), ("choose", "115200"),
            ("choose", "Add"), ("choose", "modbus"), ("text", "PLC"),
            ("text", "192.168.0.5"), ("text", "1502"), ("text", "0-105"), ("text", "0-1"),
            ("choose", "Done"), ("confirm", False)], args)
        self.assertEqual(plan.workdir,
                         os.path.join(self.repo, "DYODE_v2_light", "out"))
        self.assertEqual(plan.model.serial_baud, 115200)
        self.assertTrue(wizard.review_and_apply(ScriptedUi([("confirm", True)]), plan))
        self.assertEqual(common.load_config(plan.config_path)["serial"]["baudrate"], 115200)

    def test_refusing_at_the_review_writes_nothing(self):
        _, plan = self.run_wizard([
            ("choose", "v1"), ("choose", "Input"),
            ("choose", "Start a new"), ("text", "x"),
            ("choose", "eth0"), ("text", "10.0.1.1"),
            ("text", "10.0.1.2"), ("text", "b8:27:eb:00:00:09"), ("text", "eth1"),
            ("choose", "Add"), ("choose", "folder"), ("text", "Files"),
            ("text", "/home/pi/in"), ("text", "/home/pi/out"),
            ("choose", "Done"), ("confirm", False)])
        self.assertFalse(wizard.review_and_apply(ScriptedUi([("confirm", False)]), plan))
        self.assertFalse(os.path.exists(plan.config_path))

    def test_module_removal(self):
        _, plan = self.run_wizard([
            ("choose", "v1"), ("choose", "Output"),
            ("choose", "Start a new"), ("text", "x"),
            ("choose", "eth0"), ("text", "10.0.1.2"),
            ("text", "10.0.1.1"), ("text", "b8:27:eb:00:00:09"), ("text", "eth0"),
            ("choose", "Add"), ("choose", "screen"), ("text", "Screen"),
            ("text", "/home/pi/s"), ("text", "/home/pi/s"), ("text", "8080"),
            ("choose", "Add"), ("choose", "folder"), ("text", "Files"),
            ("text", "/home/pi/in"), ("text", "/home/pi/out"),
            ("choose", "Remove"), ("choose", "Screen"),
            ("choose", "Done"), ("confirm", False)])
        self.assertEqual([m.name for m in plan.model.modules], ["Files"])


class FrontEndTests(unittest.TestCase):
    """Drive the real programs, not just the wizard functions."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        for sub in ("DYODE_v1_full", "DYODE_v2_light/in", "DYODE_v2_light/out"):
            os.makedirs(os.path.join(self.repo, sub))
        self.net = fake_sysfs({"eth0": ("b8:27:eb:00:00:01", True, False)})
        shutil.copy(os.path.join(_setup.V1, "dyode_common.py"),
                    os.path.join(self.repo, "DYODE_v1_full"))

    def run_plain(self, answers, extra=()):
        cmd = [sys.executable, os.path.join(_setup.REPO, "dyode_setup.py"), "--plain",
               "--install-mode", "offline",
               "--repo", self.repo, "--sys-root", self.net] + list(extra)
        return subprocess.run(cmd, input="\n".join(answers) + "\n", text=True,
                              capture_output=True, timeout=60)

    ANSWERS = ["1",              # DYODE v1
               "1",              # input side
               "1",              # start a new configuration
               "Test diode",     # name
               "1",              # interface eth0
               "",               # IP 10.0.1.1
               "",               # peer IP 10.0.1.2
               "b8:27:eb:ff:ff:ff",   # peer MAC
               "",               # peer interface
               "1", "1",         # add, modbus
               "", "", "", "", "",    # name, PLC ip, port_out, registers, coils
               "3",              # done
               "n",              # no systemd unit
               "y"]              # confirm review

    def test_plain_mode_writes_a_valid_config(self):
        res = self.run_plain(self.ANSWERS)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        path = os.path.join(self.repo, "DYODE_v1_full", "config.yaml")
        cfg = common.load_config(path)
        self.assertEqual(cfg["network"]["in_mac"] if "in_mac" in cfg["network"]
                         else cfg["network"]["out_mac"], "b8:27:eb:ff:ff:ff")
        self.assertEqual(cfg["config_name"], "Test diode")
        self.assertIn("Next steps", res.stdout)
        self.assertIn("ip addr add 10.0.1.1/24 dev eth0", res.stdout)

    def test_dry_run_writes_nothing(self):
        res = self.run_plain(self.ANSWERS, extra=["--dry-run"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "DYODE_v1_full",
                                                     "config.yaml")))

    def test_invalid_input_is_rejected_then_accepted(self):
        answers = list(self.ANSWERS)
        answers[7:7] = ["not-a-mac"]        # rejected, then the real one
        res = self.run_plain(answers)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("not valid", res.stdout)

    def test_curses_interface_starts_in_a_terminal(self):
        """Run the real curses UI on a pseudo-terminal and quit from it."""
        import pty
        pid, fd = pty.fork()
        if pid == 0:                        # child: becomes the wizard
            os.environ.update(TERM="xterm", LINES="40", COLUMNS="100")
            os.execv(sys.executable,
                     [sys.executable, os.path.join(_setup.REPO, "dyode_setup.py"),
                      "--repo", self.repo, "--sys-root", self.net])
        try:
            os.write(fd, b"q")              # quit from the welcome screen
            output = b""
            deadline = __import__("time").time() + 20
            while __import__("time").time() < deadline:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if b"Cancelled" in output:
                    break
            self.assertIn(b"DYODE setup", output)
            self.assertIn(b"Welcome", output)
            self.assertIn(b"Cancelled", output)
        finally:
            os.close(fd)
            os.waitpid(pid, 0)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "DYODE_v1_full",
                                                     "config.yaml")))


if __name__ == "__main__":
    unittest.main()
