"""install.sh file shares (lib/install-shares.sh).

Runs the real script with --shares-only against a scratch /etc (DYODE_ETC)
and stand-ins for exportfs, testparm, smbpasswd, pdbedit and systemctl that
record what they were asked to do.  The real tools were exercised by hand
on Ubuntu 24.04, including an SMB login to the share over localhost.
"""

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import _setup

INSTALL = os.path.join(_setup.REPO, "install.sh")
BEGIN = "# BEGIN DYODE"
END = "# END DYODE"

FAKES = {
    # exit status chosen by the test; records each call
    "exportfs": 'echo "exportfs $*" >> "$LOG"; exit "${EXPORTFS_RC:-0}"',
    "testparm": 'echo "testparm $*" >> "$LOG"; '
                'grep -q "BROKEN" "$2" && { echo "bad line" >&2; exit 1; }; exit 0',
    "smbpasswd": 'echo "smbpasswd $* stdin=$(cat | tr "\\n" "|")" >> "$LOG"',
    "pdbedit": 'case " ${SMB_USERS:-} " in *" $3 "*) exit 0;; esac; exit 1',
    "systemctl": 'echo "systemctl $*" >> "$LOG"; '
                 '[ "$1" = is-active ] && exit "${SMBD_ACTIVE_RC:-3}"; exit 0',
}


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class ShareTests(unittest.TestCase):
    USER = "nobody"            # exists everywhere, and is not root

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dyode_shares_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.etc = os.path.join(self.tmp, "etc")
        os.makedirs(os.path.join(self.etc, "samba"))
        self.exports = os.path.join(self.etc, "exports")
        self.smbconf = os.path.join(self.etc, "samba", "smb.conf")
        with open(self.exports, "w") as fh:
            fh.write("# site exports\n/srv/site  192.168.9.0/24(rw,sync)\n")
        with open(self.smbconf, "w") as fh:
            fh.write("[global]\n   workgroup = WORKGROUP\n\n[site]\n   path = /srv/site\n")
        self.out = os.path.join(self.tmp, "srv", "out")
        self.drop = os.path.join(self.tmp, "srv", "in")
        os.makedirs(self.out); os.makedirs(self.drop)
        os.makedirs(self.out + ".incoming")       # DYODE's staging sibling
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name, body in FAKES.items():
            self.fake(name, body)
        self.log = os.path.join(self.tmp, "calls.log")
        self.pwfile = os.path.join(self.tmp, "smbpw")
        with open(self.pwfile, "w") as fh:
            fh.write("%s:s3cret pass\n" % self.USER)
        os.chmod(self.pwfile, 0o600)

    def fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def run_shares(self, *args, env=None, cwd=None):
        e = dict(os.environ, DYODE_ETC=self.etc, LOG=self.log,
                 PATH=self.bin + ":/usr/bin:/bin")
        e.update(env or {})
        return subprocess.run(["bash", INSTALL, "--shares-only", *args],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, env=e, timeout=60, cwd=cwd or self.tmp)

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def block(self, path):
        text = self.read(path)
        return text[text.index(BEGIN):text.index(END) + len(END)]

    def calls(self):
        return self.read(self.log) if os.path.exists(self.log) else ""

    def both(self, *extra, env=None):
        return self.run_shares(
            "--share-nfs", "%s=10.0.2.0/24,10.0.3.7" % self.out,
            "--share-nfs", "%s=10.0.2.0/24:rw" % self.drop,
            "--share-smb", "%s=%s" % (self.out, self.USER),
            "--smb-password-file", self.pwfile, *extra, env=env)

    # -- writing ------------------------------------------------------------

    def test_writes_nfs_and_smb_blocks(self):
        res = self.both()
        self.assertEqual(res.returncode, 0, res.stderr)
        nfs = self.block(self.exports)
        self.assertIn("%s  10.0.2.0/24(ro,sync,no_subtree_check,root_squash) "
                      "10.0.3.7(ro,sync,no_subtree_check,root_squash)" % self.out, nfs)
        self.assertIn("%s  10.0.2.0/24(rw," % self.drop, nfs)
        smb = self.block(self.smbconf)
        self.assertIn("[dyode-out]", smb)
        self.assertIn("read only = yes", smb)
        self.assertIn("guest ok = no", smb)
        self.assertIn("valid users = %s" % self.USER, smb)
        self.assertIn("exportfs -ra", self.calls())
        # The password goes to smbpasswd on stdin, twice, never as an argument.
        self.assertIn("smbpasswd -s -a %s stdin=s3cret pass|s3cret pass|" % self.USER,
                      self.calls())

    def test_existing_configuration_outside_the_block_is_kept(self):
        self.both()
        exports = self.read(self.exports)
        self.assertTrue(exports.startswith("# site exports\n/srv/site  192.168.9.0/24(rw,sync)\n"))
        self.assertIn("[site]\n   path = /srv/site", self.read(self.smbconf))

    def test_repeating_the_same_command_changes_nothing(self):
        self.assertEqual(self.both().returncode, 0)
        before = (self.read(self.exports), self.read(self.smbconf),
                  sorted(os.listdir(self.etc)))
        res = self.both()
        self.assertIn("already up to date", res.stdout)
        self.assertEqual(before, (self.read(self.exports), self.read(self.smbconf),
                                  sorted(os.listdir(self.etc))))

    def test_block_is_replaced_not_appended(self):
        self.both()
        res = self.run_shares("--share-nfs", "%s=10.9.9.9" % self.out)
        self.assertEqual(res.returncode, 0, res.stderr)
        text = self.read(self.exports)
        self.assertEqual(text.count(BEGIN), 1)
        self.assertNotIn("10.0.2.0/24", text)
        self.assertIn("10.9.9.9(ro", text)
        # An NFS-only run leaves smb.conf alone.
        self.assertIn("[dyode-out]", self.read(self.smbconf))

    def test_backups_are_kept(self):
        self.both()
        names = os.listdir(self.etc) + os.listdir(os.path.join(self.etc, "samba"))
        self.assertTrue(any(n.startswith("exports.dyode-bak-") for n in names))
        self.assertTrue(any(n.startswith("smb.conf.dyode-bak-") for n in names))

    def test_missing_folder_from_the_command_line_is_created(self):
        new = os.path.join(self.tmp, "srv", "fresh")
        res = self.run_shares("--share-nfs", "%s=10.0.2.0/24" % new)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(os.path.isdir(new))

    def test_reloads_smbd_when_it_is_running(self):
        res = self.both(env={"SMBD_ACTIVE_RC": "0"})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("systemctl reload smbd", self.calls())

    def test_dry_run_writes_nothing(self):
        before = (self.read(self.exports), self.read(self.smbconf))
        res = self.both("--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("would write", res.stdout)
        self.assertEqual(before, (self.read(self.exports), self.read(self.smbconf)))
        self.assertNotIn("smbpasswd -s", self.calls())

    # -- rollback -------------------------------------------------------------

    def test_rejected_export_is_rolled_back(self):
        original = self.read(self.exports)
        res = self.run_shares("--share-nfs", "%s=10.0.2.0/24" % self.out,
                              env={"EXPORTFS_RC": "1"})
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("previous", res.stderr)
        self.assertEqual(self.read(self.exports), original)

    def test_smb_conf_failing_testparm_is_never_installed(self):
        original = self.read(self.smbconf)
        with open(self.smbconf, "a") as fh:
            fh.write("BROKEN\n")
        original = self.read(self.smbconf)
        res = self.run_shares("--share-smb", "%s=%s" % (self.out, self.USER),
                              "--smb-password-file", self.pwfile)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("testparm rejected", res.stderr)
        self.assertEqual(self.read(self.smbconf), original)

    # -- refusals -------------------------------------------------------------

    def refused(self, *args, message, cwd=None):
        before = (self.read(self.exports), self.read(self.smbconf))
        res = self.run_shares(*args, cwd=cwd)
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn(message, res.stderr)
        self.assertEqual(before, (self.read(self.exports), self.read(self.smbconf)))

    def test_never_exports_to_everyone(self):
        for clients in ("*", "*.*", "10.0.2.0/24,*", "0.0.0.0/0", "::/0"):
            self.refused("--share-nfs", "%s=%s" % (self.out, clients),
                         message="refusing to export to everyone")

    def test_star_is_not_expanded_to_local_file_names(self):
        """Regression: an unquoted split turned * into the names of the
        files in the current folder, each of which became an export."""
        cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(cwd)
        for name in ("README.md", "notes.txt"):
            open(os.path.join(cwd, name), "w").close()
        self.refused("--share-nfs", "%s=*" % self.out,
                     message="refusing to export to everyone", cwd=cwd)

    def test_never_exposes_the_staging_folder(self):
        parent = os.path.dirname(self.out)
        self.refused("--share-nfs", "%s=10.0.2.0/24" % parent,
                     message="contains the staging folder")
        self.refused("--share-smb", "%s.incoming=%s" % (self.out, self.USER),
                     message="is a DYODE staging folder")

    def test_smb_never_as_root_or_unknown_account(self):
        self.refused("--share-smb", "%s=root" % self.out, message="as root")
        self.refused("--share-smb", "%s=nosuchuser9" % self.out,
                     message="does not exist")

    def test_unattended_smb_needs_a_password(self):
        self.refused("--share-smb", "%s=%s" % (self.out, self.USER),
                     message="no SMB password")

    def test_smb_account_with_existing_password_is_kept(self):
        res = self.run_shares("--share-smb", "%s=%s" % (self.out, self.USER),
                              env={"SMB_USERS": self.USER})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("already has an SMB password; kept", res.stdout)
        self.assertNotIn("smbpasswd", self.calls())

    def test_config_injection_is_refused(self):
        self.refused("--share-smb", "%s\n   guest ok = yes=%s" % (self.out, self.USER),
                     message="may contain only")
        self.refused("--share-nfs", "%s=10.0.2.0/24(rw,no_root_squash)" % self.out,
                     message="not a valid NFS client")
        self.refused("--share-nfs", "/srv/with space=10.0.2.0/24",
                     message="may contain only")

    def test_malformed_specs(self):
        self.refused("--share-nfs", self.out, message="needs PATH=CLIENTS")
        self.refused("--share-nfs", "relative/dir=10.0.2.0/24",
                     message="absolute path")
        self.refused("--share-nfs", "/=10.0.2.0/24", message="refusing to share /")

    def test_needs_the_server_installed(self):
        os.remove(os.path.join(self.bin, "exportfs"))
        e = {"PATH": self.bin + ":/usr/bin:/bin"}      # no /usr/sbin exportfs
        res = self.run_shares("--share-nfs", "%s=10.0.2.0/24" % self.out, env=e)
        if shutil.which("exportfs", path=e["PATH"]):
            self.skipTest("a real exportfs is on the plain PATH")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--with nfs", res.stderr)

    def test_no_specs_and_no_terminal(self):
        res = self.run_shares()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no terminal to ask on", res.stderr)


if __name__ == "__main__":
    unittest.main()
