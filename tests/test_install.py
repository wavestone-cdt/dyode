"""install.sh and the bundled wheelhouse.

The installer tests run the real script against a scratch copy of the repo
and a wheelhouse of tiny stand-in wheels carrying the real distribution and
module names, so they need neither root nor a network.  OS packages are
skipped here (--skip-os-packages); the .deb path needs root and apt, and was
verified by hand on Ubuntu 24.04.
"""

import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

import _setup

REPO = _setup.REPO
WHEELS = os.path.join(REPO, "packaging", "wheels")
INSTALL = os.path.join(REPO, "install.sh")

# What the offline bundle promises to cover.
PYTHONS = ("3.11", "3.12", "3.13", "3.14")
ARCHES = ("x86_64", "aarch64")
REQUIREMENTS = [os.path.join(REPO, *p) for p in (
    ("DYODE_v1_full", "requirements.txt"),
    ("DYODE_v2_light", "in", "requirements.txt"),
    ("DYODE_v2_light", "out", "requirements.txt"))]

STAND_INS = [("PyYAML", "6.0.2", "yaml"), ("pymodbus", "3.12.1", "pymodbus"),
             ("inotify_simple", "1.3.5", "inotify_simple"),
             ("pyserial", "3.5", "serial")]


def make_wheel(out, name, version, module):
    """A minimal but valid pure-Python wheel."""
    def digest(data):
        return "sha256=" + base64.urlsafe_b64encode(
            hashlib.sha256(data).digest()).rstrip(b"=").decode()
    dist = name.replace("-", "_")
    info = "%s-%s.dist-info" % (dist, version)
    files = {
        "%s/__init__.py" % module: ("__version__ = %r\n" % version).encode(),
        info + "/METADATA": ("Metadata-Version: 2.1\nName: %s\nVersion: %s\n"
                             % (name, version)).encode(),
        info + "/WHEEL": (b"Wheel-Version: 1.0\nGenerator: dyode-test\n"
                          b"Root-Is-Purelib: true\nTag: py3-none-any\n"),
    }
    record = "\n".join("%s,%s,%d" % (p, digest(b), len(b))
                       for p, b in files.items())
    files[info + "/RECORD"] = (record + "\n%s/RECORD,,\n" % info).encode()
    path = os.path.join(out, "%s-%s-py3-none-any.whl" % (dist, version))
    with zipfile.ZipFile(path, "w") as zf:
        for p, b in files.items():
            zf.writestr(p, b)


def requirement_names(path):
    names = []
    with open(path) as fh:
        for line in fh:
            line = line.split("#")[0].strip()
            if line:
                names.append(re.split(r"[\s<>=!~;\[]", line)[0])
    return names


def normalize(name):
    return re.sub(r"[-_.]+", "_", name).lower()


@unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"),
                     "needs bash and coreutils")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dyode_install_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        shutil.copytree(REPO, self.repo, ignore=shutil.ignore_patterns(
            ".git", "venv", "__pycache__", "wheels", "debs", "tests"))
        self.wheels = os.path.join(self.repo, "packaging", "wheels")
        os.makedirs(self.wheels)
        for spec in STAND_INS:
            make_wheel(self.wheels, *spec)
        self.write_sums()

    def write_sums(self):
        subprocess.run("sha256sum -- *.whl > SHA256SUMS", shell=True,
                       cwd=self.wheels, check=True)

    def install(self, *args):
        return subprocess.run(
            ["bash", os.path.join(self.repo, "install.sh"), *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=180, cwd=self.repo)

    def offline_v2_out(self, *extra):
        return self.install("--offline", "--variant", "v2", "--side", "out",
                            "--skip-os-packages", "--yes", "--no-wizard",
                            "--python", sys.executable, *extra)

    def test_offline_install_builds_a_working_venv_from_the_bundle(self):
        res = self.offline_v2_out()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("ok  pymodbus", res.stdout)
        self.assertIn("ok  pyserial", res.stdout)
        python = os.path.join(self.repo, "DYODE_v2_light", "out", "venv",
                              "bin", "python")
        out = subprocess.run([python, "-c", "import yaml, pymodbus, serial;"
                              "print(pymodbus.__version__)"],
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "3.12.1")

    def test_offline_never_uses_an_index(self):
        """With a requirement the bundle cannot meet, pip must fail rather
        than reach for PyPI."""
        os.remove(os.path.join(self.wheels, "pyserial-3.5-py3-none-any.whl"))
        self.write_sums()
        res = self.offline_v2_out()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("bundled wheels do not cover this system", res.stderr)
        # pip announces every source it consults: the bundle, never an index.
        self.assertIn("Looking in links", res.stdout)
        self.assertNotIn("Looking in indexes", res.stdout + res.stderr)

    def test_damaged_bundle_is_refused_before_installing(self):
        with open(os.path.join(self.wheels,
                               "pymodbus-3.12.1-py3-none-any.whl"), "ab") as fh:
            fh.write(b"x")
        res = self.offline_v2_out()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("do not match SHA256SUMS", res.stderr)
        self.assertFalse(os.path.exists(os.path.join(
            self.repo, "DYODE_v2_light", "out", "venv", "lib")))

    def test_venv_carried_over_from_another_folder_is_rebuilt(self):
        self.assertEqual(self.offline_v2_out().returncode, 0)
        cfg = os.path.join(self.repo, "DYODE_v2_light", "out", "venv",
                           "pyvenv.cfg")
        with open(cfg) as fh:
            text = fh.read()
        if "command = " not in text:
            self.skipTest("this Python does not record the venv command")
        with open(cfg, "w") as fh:
            fh.write(re.sub(r"-m venv .*", "-m venv /old/revision/venv", text))
        res = self.offline_v2_out()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("replacing an existing virtualenv", res.stdout)
        res = self.offline_v2_out()
        self.assertIn("reusing the existing virtualenv", res.stdout)

    def test_refuses_to_guess_without_a_terminal(self):
        for args, message in (
                (("--variant", "v1"), "choose --offline or --online"),
                (("--offline",), "choose --variant v1 or v2"),
                (("--offline", "--variant", "v2"), "v2 needs --side"),
                (("--offline", "--variant", "v1", "--skip-os-packages",
                  "--python", sys.executable), "pass --yes")):
            res = self.install(*args)
            self.assertNotEqual(res.returncode, 0, args)
            self.assertIn(message, res.stderr, args)

    def test_bad_option_values_are_rejected(self):
        for args in (("--mode", "sideways"), ("--variant", "v3"),
                     ("--side", "up"), ("--bogus",)):
            res = self.install(*args)
            self.assertNotEqual(res.returncode, 0, args)

    def test_dry_run_changes_nothing(self):
        res = self.install("--online", "--variant", "v1", "--skip-os-packages",
                           "--python", sys.executable, "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("would run", res.stdout)
        self.assertIn("dyode_setup.py --variant v1 --install-mode online",
                      res.stdout)
        self.assertFalse(os.path.exists(
            os.path.join(self.repo, "DYODE_v1_full", "venv")))

    def services_dry_run(self, value):
        return self.install("--online", "--variant", "v2", "--side", "out",
                            "--with", value, "--python", sys.executable,
                            "--dry-run", "--no-wizard")

    def test_with_installs_the_requested_services(self):
        res = self.services_dry_run("SSH, smb")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("services:     ssh, smb", res.stdout)
        # Package names may be listed as already present on some hosts, but
        # NFS was not asked for and must not appear at all.
        self.assertNotIn("nfs-kernel-server", res.stdout)
        self.assertIn("/etc/samba/smb.conf", res.stdout)

    def test_with_all_and_none(self):
        res = self.services_dry_run("all")
        self.assertIn("services:     ssh, nfs, smb", res.stdout)
        res = self.services_dry_run("none")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("services:", res.stdout)

    def test_with_rejects_unknown_services(self):
        res = self.services_dry_run("ssh,ftp")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("got 'ftp'", res.stderr)

    def test_with_conflicts_with_skip_os_packages(self):
        res = self.install("--offline", "--variant", "v1", "--with", "nfs",
                           "--skip-os-packages")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("cannot be combined with --skip-os-packages", res.stderr)

    def test_help(self):
        res = self.install("--help")
        self.assertEqual(res.returncode, 0)
        for flag in ("--offline", "--online", "--variant", "--side"):
            self.assertIn(flag, res.stdout)


class WheelhouseCoverageTests(unittest.TestCase):
    """Every requirement, for every Python and CPU the bundle promises.

    Skipped until tools/build_wheelhouse.sh has been run; after that a
    missing wheel fails here rather than on an air-gapped box.
    """

    def setUp(self):
        if not os.path.isdir(WHEELS) or not any(
                f.endswith(".whl") for f in os.listdir(WHEELS)):
            self.skipTest("packaging/wheels/ not built yet")
        self.wheels = [f for f in os.listdir(WHEELS) if f.endswith(".whl")]

    def candidates(self, name, py, arch):
        want = normalize(name)
        cp = "cp" + py.replace(".", "")
        found = []
        for wheel in self.wheels:
            parts = wheel[:-4].split("-")
            dist, pytag, abi, plat = parts[0], parts[-3], parts[-2], parts[-1]
            if normalize(dist) != want:
                continue
            if plat == "any" or (arch in plat and (cp in pytag.split(".")
                                                   or abi == "abi3")):
                found.append(wheel)
        return found

    def test_every_requirement_for_every_target(self):
        missing = []
        names = sorted({n for req in REQUIREMENTS for n in requirement_names(req)})
        for name in names:
            for py in PYTHONS:
                for arch in ARCHES:
                    if not self.candidates(name, py, arch):
                        missing.append("%s for Python %s on %s" % (name, py, arch))
        self.assertEqual(missing, [], "rebuild with tools/build_wheelhouse.sh")

    def test_checksums_cover_every_wheel(self):
        with open(os.path.join(WHEELS, "SHA256SUMS")) as fh:
            listed = {line.split()[-1] for line in fh if line.strip()}
        self.assertEqual(listed, set(self.wheels))


if __name__ == "__main__":
    unittest.main()
