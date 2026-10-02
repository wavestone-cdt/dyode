import collections
import os
import shutil
import tempfile
import time
import unittest

import _setup  # noqa: F401
import dyode
import dyode_common as common


class LoopbackCast:
    """Stands in for udpcast: send() copies bytes into a queue, receive()
    pops the next one into the destination file, like the optical link."""

    def __init__(self):
        self.queue = collections.deque()
        self.sent_paths = []
        self.fail_on = None

    def send(self, path):
        if self.fail_on is not None and self.fail_on in path:
            return False
        self.sent_paths.append(path)
        with open(path, "rb") as fh:
            self.queue.append(fh.read())
        return True

    def deliver_all(self, receiver, staging):
        results = []
        while self.queue:
            fd, blob = tempfile.mkstemp(dir=staging)
            with os.fdopen(fd, "wb") as fh:
                fh.write(self.queue.popleft())
            results.append(receiver.handle(blob))
        return results


def make_file(root, rel, data, age=10):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    past = time.time() - age
    os.utime(path, (past, past))
    return path


class FolderTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.inp = os.path.join(self.tmp, "in")
        self.out = os.path.join(self.tmp, "out")
        self.work = os.path.join(self.tmp, "work")
        self.staging = os.path.join(self.out, dyode.STAGING_DIR)
        for d in (self.inp, self.work, self.staging):
            os.makedirs(d)
        self.cast = LoopbackCast()
        self.rx = dyode.FolderReceiver(self.out)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def transfer(self):
        ready, waiting = dyode.scan_ready_files(self.inp, settle=2)
        sent = dyode.send_batch(ready, self.cast, self.work) if ready else 0
        return sent, waiting, self.cast.deliver_all(self.rx, self.staging)

    def read_out(self, rel):
        with open(os.path.join(self.out, *rel.split("/")), "rb") as fh:
            return fh.read()

    def test_batch_with_subfolders_and_odd_names(self):
        """Names that broke the original: quotes (shell), ':' and '=' (INI),
        '%' (interpolation), capitals (INI lowercases keys)."""
        names = {"Report.PDF": b"A" * 5000, "sub/dir/deep.bin": os.urandom(70_000),
                 "it's a:weird=name%.txt": b"hello", "empty.dat": b""}
        for rel, data in names.items():
            make_file(self.inp, rel, data)
        sent, _, results = self.transfer()
        self.assertEqual(sent, 4)
        self.assertEqual(results, ["manifest"] + ["stored"] * 4)
        for rel, data in names.items():
            self.assertEqual(self.read_out(rel), data)
        self.assertEqual(os.listdir(self.inp), ["sub"])     # files deleted, dirs kept
        self.assertEqual(os.listdir(self.staging), [])

    def test_half_written_file_waits_for_next_pass(self):
        make_file(self.inp, "old.txt", b"old")
        make_file(self.inp, "fresh.txt", b"still being written", age=0)
        sent, waiting, _ = self.transfer()
        self.assertEqual((sent, waiting), (1, 1))
        self.assertTrue(os.path.exists(os.path.join(self.inp, "fresh.txt")))

    def test_symlinks_are_never_sent(self):
        secret = make_file(self.tmp, "secret.txt", b"root password")
        os.symlink(secret, os.path.join(self.inp, "innocent.txt"))
        os.symlink(self.tmp, os.path.join(self.inp, "linkdir"))
        make_file(self.inp, "real.txt", b"ok")
        sent, _, _ = self.transfer()
        self.assertEqual(sent, 1)
        self.assertFalse(os.path.exists(os.path.join(self.out, "innocent.txt")))

    def test_tampered_file_rejected_and_rest_still_stored(self):
        """Original bug: os.remove(f) on the INPUT path crashed the receiver."""
        make_file(self.inp, "a.txt", b"aaaa")
        make_file(self.inp, "b.txt", b"bbbb")
        dyode.send_batch(dyode.scan_ready_files(self.inp, 2)[0], self.cast, self.work)
        self.cast.queue[1] = b"XXXX"          # corrupt a.txt in transit
        with self.assertLogs("dyode.folder", "ERROR"):
            results = self.cast.deliver_all(self.rx, self.staging)
        self.assertEqual(results, ["manifest", "rejected", "stored"])
        self.assertEqual(self.read_out("b.txt"), b"bbbb")
        self.assertEqual(os.listdir(self.staging), [])

    def test_receiver_resyncs_after_interrupted_batch(self):
        for n in "abc":
            make_file(self.inp, n + ".txt", n.encode() * 10)
        self.cast.fail_on = "b.txt"
        with self.assertLogs("dyode.folder", "ERROR"):
            self.assertEqual(dyode.send_batch(dyode.scan_ready_files(self.inp, 2)[0],
                                              self.cast, self.work), 1)
        self.cast.fail_on = None
        _, _, results = self.transfer()
        self.assertEqual(results, ["manifest", "stored", "manifest", "stored", "stored"])
        for n in "abc":
            self.assertEqual(self.read_out(n + ".txt"), n.encode() * 10)
        # The re-sent files supersede the first batch's outstanding entries,
        # so nothing is left open to be reported missing later.
        self.assertEqual(self.rx.pending, [])

    def test_one_lost_file_does_not_destroy_the_rest_of_the_batch(self):
        """Regression: identity used to come from arrival order.

        A single dropped transfer shifted every later file by one, so each
        was checked against the previous file's hash and the whole tail of
        the batch was discarded.  Files are matched by content now.
        """
        names = ["f%02d.txt" % i for i in range(8)]
        for i, name in enumerate(names):
            make_file(self.inp, name, b"payload-%02d" % i)
        dyode.send_batch(dyode.scan_ready_files(self.inp, 2)[0], self.cast,
                         self.work)
        del self.cast.queue[3]                # lose one transfer outright
        results = self.cast.deliver_all(self.rx, self.staging)

        self.assertEqual(results.count("stored"), 7)
        lost = names[2]
        for i, name in enumerate(names):
            if name == lost:
                self.assertFalse(os.path.exists(os.path.join(self.out, name)))
            else:
                self.assertEqual(self.read_out(name), b"payload-%02d" % i)

    def test_duplicate_content_in_one_batch_lands_under_both_names(self):
        make_file(self.inp, "one.txt", b"same bytes")
        make_file(self.inp, "two.txt", b"same bytes")
        make_file(self.inp, "three.txt", b"other")
        sent, _, _ = self.transfer()
        self.assertEqual(sent, 3)
        self.assertEqual(self.read_out("one.txt"), b"same bytes")
        self.assertEqual(self.read_out("two.txt"), b"same bytes")
        self.assertEqual(self.read_out("three.txt"), b"other")

    def test_damaged_transfer_is_discarded_and_named_as_missing(self):
        make_file(self.inp, "a.txt", b"aaaa")
        make_file(self.inp, "b.txt", b"bbbb")
        dyode.send_batch(dyode.scan_ready_files(self.inp, 2)[0], self.cast,
                         self.work)
        self.cast.queue[1] = b"XX"            # a.txt damaged in flight
        with self.assertLogs("dyode.folder", "ERROR"):
            self.cast.deliver_all(self.rx, self.staging)
        self.assertEqual(self.read_out("b.txt"), b"bbbb")
        self.assertFalse(os.path.exists(os.path.join(self.out, "a.txt")))
        # a.txt is still outstanding; the sweep names it rather than
        # silently blaming whichever blob arrived next.
        self.assertEqual([e["path"] for e in self.rx.pending], ["a.txt"])
        time.sleep(0.02)
        with self.assertLogs("dyode.folder", "WARNING") as logs:
            self.rx.sweep(batch_timeout=0.001)
        self.assertIn("a.txt never arrived", "\n".join(logs.output))

    def test_file_without_manifest_is_discarded(self):
        blob = make_file(self.staging, "x", b"stray")
        with self.assertLogs("dyode.folder", "WARNING"):
            self.assertEqual(self.rx.handle(blob), "orphan")
        self.assertFalse(os.path.exists(blob))

    def test_hostile_manifest_paths_rejected(self):
        for bad in ("/etc/passwd", "../escape", "a/../../b", "a//b", "./x", "",
                    ".dyode_incoming/x", "a\x00b"):
            self.assertIsNone(dyode.safe_join(self.out, bad), bad)
        self.assertEqual(dyode.safe_join(self.out, "a/b.txt"),
                         os.path.join(self.out, "a", "b.txt"))

    def test_ordinary_json_file_is_not_mistaken_for_manifest(self):
        blob = make_file(self.staging, "cfg", b'{"files": [], "hello": 1}')
        self.assertIsNone(dyode.read_manifest(blob))


class UdpCastCommandTests(unittest.TestCase):
    def test_commands_are_argument_lists_with_config_values(self):
        props = {"port": 9600, "bitrate": 4}
        cfg = {"network": {"in_ip": "10.9.0.1", "out_ip": "10.9.0.2",
                           "in_interface": "enxAA", "out_interface": "enxBB"}}
        cast = dyode.UdpCast(props, cfg)
        evil = "/in/x'; rm -rf ~; '.txt"
        send = cast.sender_cmd(evil)
        self.assertEqual(send[-1], evil)            # one argument, no shell parsing
        self.assertIn("4m", send)
        self.assertEqual(send[send.index("--interface") + 1], "enxAA")
        self.assertEqual(send[send.index("--mcast-rdv-addr") + 1], "10.9.0.2")
        recv = cast.receiver_cmd("/out/f")
        self.assertEqual(recv[recv.index("--interface") + 1], "enxBB")
        self.assertEqual(recv[recv.index("--mcast-rdv-addr") + 1], "10.9.0.1")


class SentFileRetentionTests(unittest.TestCase):
    """udp-sender exiting 0 is not proof of delivery, so a sent file is
    retired rather than unlinked and can be re-sent by hand."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dyode_sent_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_sent_file_is_moved_under_dyode_sent(self):
        path = make_file(self.root, "sub/report.csv", b"data")
        dyode.retire_sent_file(self.root, "sub/report.csv", path,
                               keep_sent_hours=24)
        self.assertFalse(os.path.exists(path))
        kept = os.path.join(self.root, dyode.SENT_DIR, "sub", "report.csv")
        self.assertTrue(os.path.isfile(kept))

    def test_retired_files_are_not_picked_up_again(self):
        path = make_file(self.root, "a.txt", b"data")
        dyode.retire_sent_file(self.root, "a.txt", path, keep_sent_hours=24)
        ready, _ = dyode.scan_ready_files(self.root, settle=0)
        self.assertEqual(ready, [])

    def test_zero_hours_deletes_as_before(self):
        path = make_file(self.root, "a.txt", b"data")
        dyode.retire_sent_file(self.root, "a.txt", path, keep_sent_hours=0)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.isdir(os.path.join(self.root, dyode.SENT_DIR)))

    def test_prune_removes_only_files_past_the_window(self):
        old = make_file(self.root, "old.txt", b"o")
        new = make_file(self.root, "new.txt", b"n")
        dyode.retire_sent_file(self.root, "old.txt", old, keep_sent_hours=24)
        dyode.retire_sent_file(self.root, "new.txt", new, keep_sent_hours=24)
        kept = os.path.join(self.root, dyode.SENT_DIR)
        stale = time.time() - 48 * 3600
        os.utime(os.path.join(kept, "old.txt"), (stale, stale))
        dyode.prune_sent(self.root, keep_sent_hours=24)
        self.assertFalse(os.path.exists(os.path.join(kept, "old.txt")))
        self.assertTrue(os.path.exists(os.path.join(kept, "new.txt")))


class StagingLocationTests(unittest.TestCase):
    """Staging must be outside the output folder: NFS, SFTP and rsync all
    walk it, and udp-receiver writes there for the whole transfer."""

    def test_default_is_a_sibling_of_the_output_folder(self):
        staging = dyode.staging_dir({"out": "/srv/dyode/out"})
        self.assertEqual(staging, "/srv/dyode/out.incoming")
        self.assertNotEqual(
            os.path.commonpath([staging, "/srv/dyode/out"]), "/srv/dyode/out")

    def test_trailing_separator_does_not_nest_it(self):
        self.assertEqual(dyode.staging_dir({"out": "/srv/out/"}),
                         "/srv/out.incoming")

    def test_explicit_staging_is_used(self):
        self.assertEqual(
            dyode.staging_dir({"out": "/srv/out", "staging": "/var/tmp/in"}),
            "/var/tmp/in")

    def test_config_rejects_staging_inside_the_output_folder(self):
        with self.assertRaises(common.ConfigError):
            common._normalize_module("m", {
                "type": "folder", "port": 9600, "in": "/srv/in",
                "out": "/srv/out", "staging": "/srv/out/.incoming"})


class FecOptionTests(unittest.TestCase):
    """The FEC encoder is the throughput ceiling on fast links, so it has to
    be switchable from config -- without breaking existing configs."""

    NET = {"network": {"in_ip": "10.9.0.1", "out_ip": "10.9.0.2",
                       "in_interface": "eth0", "out_interface": "eth1"}}

    def module(self, **extra):
        props = {"type": "folder", "port": 9600, "in": "/in", "out": "/out"}
        props.update(extra)
        return common._normalize_module("m", props)

    def test_default_is_unchanged_for_existing_configs(self):
        props = self.module()
        self.assertEqual(props["fec"], common.DEFAULT_FEC)
        cmd = dyode.UdpCast(props, self.NET).sender_cmd("/in/f")
        self.assertEqual(cmd[cmd.index("--fec") + 1], "8x16/64")

    def test_custom_ratio_is_passed_through(self):
        cmd = dyode.UdpCast(self.module(fec="8x8/128"),
                            self.NET).sender_cmd("/in/f")
        self.assertEqual(cmd[cmd.index("--fec") + 1], "8x8/128")

    def test_disabling_omits_the_flag_and_its_value(self):
        for value in ("none", "OFF", "no", False, None):
            props = self.module(fec=value)
            self.assertIsNone(props["fec"], value)
            cmd = dyode.UdpCast(props, self.NET).sender_cmd("/in/f")
            self.assertNotIn("--fec", cmd)
            # the ratio must not survive as a stray argument either
            self.assertNotIn("None", cmd)
            self.assertEqual(cmd[cmd.index("--max-bitrate") + 1], "8m")

    def test_invalid_ratio_is_rejected_at_load_time(self):
        for bad in ("8x", "fast", "8/16", "8x16/64/2", ""):
            with self.assertRaises(common.ConfigError):
                self.module(fec=bad)

    def test_bitrate_is_coerced_and_checked(self):
        self.assertEqual(self.module(bitrate="900")["bitrate"], 900)
        for bad in ("fast", 0, -1, None):
            with self.assertRaises(common.ConfigError):
                self.module(bitrate=bad)

    def test_fec_is_only_meaningful_for_folder_modules(self):
        screen = common._normalize_module(
            "s", {"type": "screen", "port": 9700, "in": "/i", "out": "/o"})
        self.assertNotIn("fec", screen)


if __name__ == "__main__":
    unittest.main()
