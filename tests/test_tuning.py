"""install.sh receive-side kernel settings (lib/install-tuning.sh).

Runs the real script with --tuning-only against a scratch /etc (DYODE_ETC)
and a stand-in sysctl that records what it was asked to load.  The settings
themselves were verified in the field: with them, 1.3 GB and 5 GB transfers
over a gigabit diode to a LUKS-encrypted receiver arrived intact; without
them, everything over ~1 GB failed (CAVEATS.md).
"""

import os
import shutil
import subprocess
import tempfile
import unittest

import _setup

INSTALL = os.path.join(_setup.REPO, "install.sh")
MIB = 1024 * 1024
# On the field box (256 GB of RAM, LUKS): what fixed 1.3, 5 and 10 GB.
SETTINGS = {
    "vm.dirty_background_bytes": str(64 * MIB),
    "vm.dirty_bytes": str(2048 * MIB),
    "net.core.rmem_default": "8388608",
    "net.core.rmem_max": "16777216",
}


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TuningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dyode_tuning_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.etc = os.path.join(self.tmp, "etc")
        self.conf = os.path.join(self.etc, "sysctl.d", "90-dyode-receive.conf")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "calls.log")
        self.fake("sysctl", 'echo "sysctl $*" >> "$LOG"; exit "${SYSCTL_RC:-0}"')
        self.meminfo = os.path.join(self.tmp, "meminfo")
        self.set_ram_mib(256 * 1024)

    def set_ram_mib(self, mib):
        with open(self.meminfo, "w") as fh:
            fh.write("MemTotal:       %d kB\nMemFree: 1 kB\n" % (mib * 1024))

    def fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def run_tuning(self, *args, env=None):
        e = dict(os.environ, DYODE_ETC=self.etc, LOG=self.log,
                 DYODE_MEMINFO=self.meminfo, PATH=self.bin + ":/usr/bin:/bin")
        e.update(env or {})
        return subprocess.run(["bash", INSTALL, "--tuning-only", *args],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, env=e, timeout=60, cwd=self.tmp)

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def calls(self):
        return self.read(self.log) if os.path.exists(self.log) else ""

    def settings(self):
        found = {}
        for line in self.read(self.conf).splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = (part.strip() for part in line.split("=", 1))
                found[key] = value
        return found

    def test_writes_exactly_the_field_proven_settings_and_loads_them(self):
        res = self.run_tuning()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.settings(), SETTINGS)
        self.assertIn("sysctl -q -p %s" % self.conf, self.calls())
        self.assertIn("wrote", res.stdout)

    def test_ceiling_scales_with_ram_and_is_capped(self):
        """20% of RAM like the kernel's own default, at most 2 GB, at least
        128 MB; writeback starts at 64 MB or a quarter of the ceiling."""
        for ram, dirty, background in ((256 * 1024, 2048 * MIB, 64 * MIB),
                                       (16 * 1024, 2048 * MIB, 64 * MIB),
                                       (4 * 1024, 4096 * MIB // 5, 64 * MIB),
                                       (1024, 1024 * MIB // 5,
                                        1024 * MIB // 20),
                                       (256, 128 * MIB, 32 * MIB)):
            self.set_ram_mib(ram)
            res = self.run_tuning()
            self.assertEqual(res.returncode, 0, res.stderr)
            got = self.settings()
            self.assertEqual(int(got["vm.dirty_bytes"]), dirty, ram)
            self.assertEqual(int(got["vm.dirty_background_bytes"]),
                             background, ram)
            self.assertLess(background, dirty)

    def test_unreadable_meminfo_falls_back_to_a_safe_middle(self):
        os.remove(self.meminfo)
        res = self.run_tuning()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(int(self.settings()["vm.dirty_bytes"]),
                         4096 * MIB // 5)

    def test_repeat_run_changes_nothing_and_keeps_no_backup(self):
        self.run_tuning()
        res = self.run_tuning()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("already up to date", res.stdout)
        self.assertEqual(os.listdir(os.path.dirname(self.conf)),
                         ["90-dyode-receive.conf"])

    def test_hand_edited_file_is_backed_up_then_replaced(self):
        self.run_tuning()
        with open(self.conf, "a") as fh:
            fh.write("vm.swappiness = 1\n")
        res = self.run_tuning()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("previous copy", res.stdout)
        self.assertEqual(self.settings(), SETTINGS)
        backups = [n for n in os.listdir(os.path.dirname(self.conf))
                   if ".dyode-bak-" in n]
        self.assertEqual(len(backups), 1)
        self.assertIn("vm.swappiness", self.read(os.path.join(
            os.path.dirname(self.conf), backups[0])))

    def test_dry_run_writes_and_loads_nothing(self):
        res = self.run_tuning("--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("would write", res.stdout)
        self.assertIn("net.core.rmem_default = 8388608", res.stdout)
        self.assertFalse(os.path.exists(self.conf))
        self.assertEqual(self.calls(), "")

    def test_sysctl_failure_warns_but_keeps_the_file_for_next_boot(self):
        res = self.run_tuning(env={"SYSCTL_RC": "1"})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("next boot", res.stderr)
        self.assertTrue(os.path.exists(self.conf))

    def test_sending_box_and_contradictions_are_refused(self):
        for args in (("--side", "in"), ("--no-tuning",)):
            res = self.run_tuning(*args)
            self.assertNotEqual(res.returncode, 0, args)
            self.assertFalse(os.path.exists(self.conf), args)
