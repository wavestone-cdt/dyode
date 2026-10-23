"""lidi transport (1.5.1-beta).

These tests cover DYODE's side of lidi: configuration, the command lines it
builds, the hard-link send path, and the receive loop feeding the same
verifier as udpcast.  lidi itself is replaced by a stand-in, because the lidi
binaries are not in the test environment.  Running them against real lidi
on a diode pair is a separate, manual step (see CHANGELOG 1.5.1-beta).
"""

import os
import shutil
import tempfile
import threading
import time
import unittest
import unittest.mock

import _setup  # noqa: F401
import dyode
import dyode_common as common
import lidi_transport

NET = {"in_ip": "10.0.1.1", "out_ip": "10.0.1.2",
       "in_interface": "eth0", "out_interface": "eth1"}


def folder_props(**extra):
    props = {"type": "folder", "port": 9600, "in": "/x/in", "out": "/x/out"}
    props.update(extra)
    return common._normalize_module("m", props)


def wait_until(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class ConfigTests(unittest.TestCase):
    def test_default_transport_is_still_udpcast(self):
        props = folder_props()
        self.assertEqual(props["transport"], "udpcast")
        self.assertEqual(props["lidi_ports"], 4)
        self.assertEqual(props["lidi_block"], 220000)

    def test_lidi_transport_accepted(self):
        self.assertEqual(folder_props(transport="LiDi")["transport"], "lidi")

    def test_unknown_transport_rejected(self):
        with self.assertRaises(common.ConfigError):
            folder_props(transport="udp-sender")

    def test_lidi_limits_enforced(self):
        for bad in ({"lidi_ports": 0}, {"lidi_ports": 17},
                    {"lidi_repair": 100}, {"lidi_repair": 0},
                    {"lidi_mtu": 1279}, {"lidi_mtu": 9001},
                    {"lidi_block": "big"}):
            with self.assertRaises(common.ConfigError, msg=str(bad)):
                folder_props(transport="lidi", **bad)

    def test_lidi_keys_checked_even_under_udpcast(self):
        with self.assertRaises(common.ConfigError):
            folder_props(lidi_repair=150)

    def test_diode_ports(self):
        self.assertEqual(common.diode_udp_ports(folder_props()), [9600, 9601])
        lidi = folder_props(transport="lidi", lidi_ports=3)
        self.assertEqual(common.diode_udp_ports(lidi), [9600, 9601, 9602])
        modbus = common._normalize_module("p", {"type": "modbus", "port": 9400,
                                                "ip": "192.168.1.150",
                                                "registers": ["0-1"]})
        self.assertEqual(common.diode_udp_ports(modbus), [9400])


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"network": NET}
        self.props = folder_props(transport="lidi", lidi_ports=2, lidi_repair=25,
                                  lidi_block=100000, lidi_mtu=9000)
        self.tx = lidi_transport.LidiSender(self.props, self.cfg)

    def tearDown(self):
        self.tx.close()

    def test_shared_options_match_on_both_sides(self):
        shared = lidi_transport.shared_args(self.props)
        self.assertEqual(shared, ["--ports", "9600,9601", "--mtu", "9000",
                                  "--block", "100000", "--repair", "25"])

    def test_sender_daemon_binds_to_diode_nic(self):
        tx = self.tx
        argv = tx.daemon_argv()
        self.assertEqual(argv[0], "lidi-send")
        self.assertEqual(argv[argv.index("--from") + 1], "unix:" + tx.sock)
        self.assertEqual(argv[argv.index("--to") + 1], NET["out_ip"])
        self.assertEqual(argv[argv.index("--to-bind") + 1], "10.0.1.1:0")
        for flag in ("--ports", "--mtu", "--block", "--repair"):
            self.assertIn(flag, argv)

    def test_receiver_daemons_are_chained_through_one_socket(self):
        base = tempfile.mkdtemp()
        partial = os.path.join(base, "partial")
        rx = lidi_transport.LidiReceiver(self.props, self.cfg,
                                         os.path.join(base, "out"), partial)
        try:
            fr = rx.file_receive.argv
            self.assertEqual(fr[0], "lidi-file-receive")
            self.assertEqual(fr[fr.index("--from-unix") + 1], rx.sock)
            self.assertEqual(fr[fr.index("--tmp-dir") + 1], partial)
            self.assertEqual(fr[-1], os.path.join(base, "out"))
            lr = rx.receive.argv
            self.assertEqual(lr[0], "lidi-receive")
            self.assertEqual(lr[lr.index("--from") + 1], NET["out_ip"])
            self.assertEqual(lr[lr.index("--to") + 1], "unix:" + rx.sock)
            self.assertIn("--ports", lr)
        finally:
            rx.close()
            shutil.rmtree(base)

    def test_send_timeout_grows_with_file_size(self):
        small = lidi_transport._send_timeout(1_000)
        big = lidi_transport._send_timeout(20 * 10**9)
        self.assertGreater(big, small)
        self.assertGreater(big, 1600)     # 20 GB at 100 Mbit/s is 1600 s


class SendPathTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"network": NET}
        self.props = folder_props(transport="lidi")
        self.root = tempfile.mkdtemp()
        self.tx = lidi_transport.LidiSender(self.props, self.cfg)
        self.tx.ensure_running = lambda: None      # no daemon in tests
        self.seen = []

    def tearDown(self):
        self.tx.close()
        shutil.rmtree(self.root)

    def recording_file_send(self, paths, size, pass_fds=()):
        # Reads through the offered name, as lidi-file-send would.
        target = os.readlink(paths[0])
        with open(paths[0], "rb") as fh:
            self.seen.append((paths[0], fh.read(), size, target))
        return True

    def send_file(self, rel, data):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        fd = os.open(path, os.O_RDONLY)
        try:
            with unittest.mock.patch.object(self.tx, "_file_send", self.recording_file_send):
                return self.tx.send_fd(fd)
        finally:
            os.close(fd)

    def test_send_fd_offers_the_open_file_under_a_unique_name(self):
        self.assertTrue(self.send_file("a.txt", b"hello"))
        (name, content, size, target), = self.seen
        self.assertEqual(content, b"hello")
        self.assertEqual(size, 5)
        self.assertNotEqual(os.path.basename(name), "a.txt")
        self.assertEqual(target, "/proc/%d/fd/%s" % (os.getpid(), target.rsplit("/", 1)[1]))
        self.assertEqual(os.listdir(self.tx.link_dir), [])    # link removed after send

    def test_same_basename_gets_distinct_names(self):
        self.send_file("x/data.bin", b"one")
        self.send_file("y/data.bin", b"two")
        first, second = self.seen[0][0], self.seen[1][0]
        self.assertNotEqual(first, second)
        self.assertEqual([self.seen[0][1], self.seen[1][1]], [b"one", b"two"])

    def test_symlink_failure_is_a_failed_send_not_a_crash(self):
        path = os.path.join(self.root, "b.txt")
        with open(path, "wb") as fh:
            fh.write(b"zz")
        fd = os.open(path, os.O_RDONLY)
        try:
            with unittest.mock.patch("os.symlink", side_effect=OSError("EMFILE")):
                self.assertFalse(self.tx.send_fd(fd))
        finally:
            os.close(fd)
        self.assertEqual(self.seen, [])


class PartialFileTests(unittest.TestCase):
    def test_prune_removes_only_stale_partials(self):
        d = tempfile.mkdtemp()
        try:
            old = os.path.join(d, "old")
            new = os.path.join(d, "new")
            for p in (old, new):
                open(p, "w").close()
            past = time.time() - 3600
            os.utime(old, (past, past))
            lidi_transport.prune_partial(d, older_than=600)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(new))
            lidi_transport.prune_partial(d, older_than=0)
            self.assertEqual(os.listdir(d), [])
        finally:
            shutil.rmtree(d)

    def test_newest_mtime(self):
        d = tempfile.mkdtemp()
        try:
            self.assertIsNone(lidi_transport.newest_mtime(d))
            p = os.path.join(d, "f")
            open(p, "w").close()
            self.assertAlmostEqual(lidi_transport.newest_mtime(d), os.stat(p).st_mtime)
        finally:
            shutil.rmtree(d)


class ReceiveLoopTests(unittest.TestCase):
    """End to end through the real manifest, verifier and FolderReceiver.

    The stand-in for lidi delivers each file into the staging folder the way
    lidi-file-receive does: written to the partial folder, then moved in
    whole.  The sender is the real LidiSender, with only its process call
    replaced.
    """

    def setUp(self):
        self.cfg = {"network": NET}
        self.props = folder_props(transport="lidi")
        self.root = tempfile.mkdtemp()
        self.inp = os.path.join(self.root, "in")
        self.out = os.path.join(self.root, "out")
        self.staging = os.path.join(self.root, "out.incoming")
        self.partial = os.path.join(self.staging, lidi_transport.PARTIAL_DIR)
        for d in (self.inp, self.out, self.staging, self.partial):
            os.makedirs(d)
        self.work_dir = os.path.join(self.root, "work")
        os.makedirs(self.work_dir)
        self.stop = threading.Event()

    def tearDown(self):
        self.stop.set()
        shutil.rmtree(self.root)

    def fake_lidi_delivery(self, paths, size, pass_fds=()):
        """Stand-in for lidi-file-send + lidi: deliver the named file."""
        name = os.path.basename(paths[0])          # the unique offered name
        staged = os.path.join(self.partial, "tmp-" + name)
        with open(paths[0], "rb") as fh, open(staged, "wb") as out:
            out.write(fh.read())
        os.replace(staged, os.path.join(self.staging, name))
        return True

    def test_files_arrive_verified_and_placed(self):
        # Two files with the same basename in different folders.
        for rel, data in (("a/report.txt", b"first"), ("b/report.txt", b"second")):
            path = os.path.join(self.inp, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
            past = time.time() - 10
            os.utime(path, (past, past))

        receiver = dyode.FolderReceiver(self.out, module="m")
        work = dyode.queue.Queue(maxsize=4)
        queued = set()
        receiving = {"blob": None, "last_data": lambda: None}
        dyode.start_verifier(receiver, work, 300.0, receiving, queued)

        transport = lidi_transport.LidiReceiver(self.props, self.cfg, self.out, self.partial)
        transport.start = lambda: None
        transport.ensure_running = lambda: None
        transport.close = lambda: None
        loop = threading.Thread(
            target=dyode.lidi_receive_loop,
            args=(transport, self.staging, work, queued, self.partial, 300.0),
            kwargs={"stop": self.stop}, daemon=True)
        loop.start()

        ready, _ = dyode.scan_ready_files(self.inp, settle=2)
        tx = lidi_transport.LidiSender(self.props, self.cfg)
        tx.ensure_running = lambda: None
        tx._file_send = self.fake_lidi_delivery
        try:
            sent = dyode.send_batch(ready, tx, self.work_dir, module="m",
                                    root=self.inp, file_gap=0.0,
                                    keep_sent_hours=0, auth=None)
            self.assertEqual(sent, 2)
            self.assertTrue(wait_until(lambda: os.path.isfile(
                os.path.join(self.out, "a", "report.txt"))
                and os.path.isfile(os.path.join(self.out, "b", "report.txt"))))
            with open(os.path.join(self.out, "a", "report.txt"), "rb") as fh:
                self.assertEqual(fh.read(), b"first")
            with open(os.path.join(self.out, "b", "report.txt"), "rb") as fh:
                self.assertEqual(fh.read(), b"second")
            self.assertTrue(wait_until(lambda: not any(
                not f.startswith(".") for f in os.listdir(self.staging))))
            self.assertEqual(os.listdir(tx.link_dir), [])
        finally:
            tx.close()

    def test_unfinished_file_in_partial_is_not_verified(self):
        # A file still being received lives in the partial folder, not in staging,
        # so the loop must not see it.
        with open(os.path.join(self.partial, "half-written"), "wb") as fh:
            fh.write(b"partial data")
        transport = lidi_transport.LidiReceiver(self.props, self.cfg, self.out, self.partial)
        transport.start = lambda: None
        transport.ensure_running = lambda: None
        work = dyode.queue.Queue()
        queued = set()
        loop = threading.Thread(
            target=dyode.lidi_receive_loop,
            args=(transport, self.staging, work, queued, self.partial, 300.0),
            kwargs={"stop": self.stop}, daemon=True)
        loop.start()
        time.sleep(0.3)
        self.assertTrue(work.empty())
        self.assertEqual(queued, set())


if __name__ == "__main__":
    unittest.main()
