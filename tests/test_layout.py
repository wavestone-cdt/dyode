import filecmp
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

import _setup

PY_FILES = []
for folder in (_setup.V1, _setup.V2_IN, _setup.V2_OUT, _setup.REPO):
    PY_FILES += [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                 if f.endswith(".py")]


def run(args, env_extra=None, **kw):
    env = dict(os.environ, PYTHONPATH=_setup.FAKES)
    env.update(env_extra or {})
    return subprocess.run([sys.executable] + args, capture_output=True, text=True,
                          env=env, timeout=60, **kw)


class LayoutTests(unittest.TestCase):
    def test_shared_copies_are_identical(self):
        for name in ("dyode_common.py", "modbus.py"):
            for folder in (_setup.V2_IN, _setup.V2_OUT):
                self.assertTrue(filecmp.cmp(os.path.join(_setup.V1, name),
                                            os.path.join(folder, name), shallow=False),
                                "%s in %s differs from v1 copy" % (name, folder))

    def test_every_file_compiles_with_warnings_as_errors(self):
        for path in PY_FILES:
            res = run(["-W", "error", "-m", "py_compile", path])
            self.assertEqual(res.returncode, 0, res.stderr)

    def test_no_python2_leftovers(self):
        patterns = [r"\.iteritems\(", r"^\s*print [\"']", r"except \w+, \w+:",
                    r"\bimport (asyncore|ConfigParser|BaseHTTPServer)\b",
                    r"pymodbus\.server\.async\b", r"\bimport pickle\b|pickle\.(loads|dumps)",
                    r"shell=True",
                    r"yaml\.load\(", r"\._args\b", r"\t"]
        for path in PY_FILES:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for pat in patterns:
                self.assertIsNone(re.search(pat, text, re.M),
                                  "%s matches %r" % (os.path.basename(path), pat))

    def test_entry_points_start_and_validate_config(self):
        bad = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        bad.write("modules:\n  x: {type: nonsense, port: 1}\n")
        bad.close()
        for folder, script in ((_setup.V1, "dyode_in.py"), (_setup.V1, "dyode_out.py"),
                               (_setup.V2_IN, "dyode_in.py"), (_setup.V2_OUT, "dyode_out.py"),
                               (_setup.REPO, "dyode_setup.py"),
                               (_setup.REPO, "dyode_logs.py")):
            with self.subTest(script=os.path.join(os.path.basename(folder), script)):
                self.assertEqual(run([script, "--help"], cwd=folder).returncode, 0)
                if script in ("dyode_setup.py", "dyode_logs.py"):
                    continue              # neither takes a -c config flag
                res = run([script, "-c", bad.name], cwd=folder)
                self.assertEqual(res.returncode, 1)
                self.assertIn("unknown type 'nonsense'", res.stderr)


class SupervisorTests(unittest.TestCase):
    def test_restarts_crashed_module_under_spawn(self):
        """Python 3.14 defaults to 'forkserver' on Linux; 'spawn' is the
        strictest case: nothing is inherited, everything must pickle."""
        marker = tempfile.mktemp()
        script = textwrap.dedent("""
            import multiprocessing, sys, threading, os, time
            sys.path[:0] = [%r, %r]
            import dyode_common, agents_for_tests
            multiprocessing.set_start_method("spawn")
            dyode_common.setup_logging("INFO")
            def stop():
                [c.kill() for c in multiprocessing.active_children()]
                os._exit(0)
            threading.Timer(4, stop).start()
            dyode_common.supervise([("crashy", agents_for_tests.crash_first_time, (%r,))],
                                   check_every=0.2, restart_delay=0.2)
        """) % (_setup.V1, _setup.TESTS, marker)
        res = run(["-c", script])
        self.assertIn("module 'crashy' exited (code 3)", res.stderr)
        with open(marker) as fh:
            self.assertIn("restarted", fh.read())


if __name__ == "__main__":
    unittest.main()
