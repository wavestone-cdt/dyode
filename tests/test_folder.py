import collections
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

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
        # Files arrive as /proc/self/fd/N (the open descriptor DYODE checked);
        # the link names the real file, which fail_on matches against.
        name = os.readlink(path) if path.startswith("/proc/self/fd/") else path
        if self.fail_on is not None and self.fail_on in name:
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

    def queue_big_file(self):
        """Send a one-file batch and deliver only its manifest, leaving the
        file 'in flight' the way a long transfer is."""
        make_file(self.inp, "big.bin", b"B" * 4096)
        dyode.send_batch(dyode.scan_ready_files(self.inp, 2)[0], self.cast,
                         self.work)
        manifest = self.cast.queue.popleft()
        blob = make_file(self.staging, "m", manifest)
        self.assertEqual(self.rx.handle(blob), "manifest")
        for batch in self.rx.batches.values():
            batch.last_activity -= 301        # 5 minutes since the manifest

    def test_transfer_longer_than_batch_timeout_is_not_discarded(self):
        """Regression (field, 10 GB at ~350 Mbit/s): the batch timed out
        while its only file was still arriving, and the intact file was then
        thrown away as having no manifest."""
        self.queue_big_file()
        partial = make_file(self.staging, "partial", b"B" * 100, age=0)
        with self.assertNoLogs("dyode.folder", "WARNING"):
            self.rx.sweep(300, dyode.last_data_time(partial))
        self.assertEqual(len(self.rx.batches), 1)
        os.remove(partial)
        results = self.cast.deliver_all(self.rx, self.staging)
        self.assertEqual(results, ["stored"])
        self.assertEqual(self.read_out("big.bin"), b"B" * 4096)

    def test_batch_still_times_out_once_data_stops_arriving(self):
        self.queue_big_file()
        stale = make_file(self.staging, "stale", b"B" * 100, age=301)
        with self.assertLogs("dyode.folder", "WARNING") as logs:
            self.rx.sweep(300, dyode.last_data_time(stale))
        self.assertIn("big.bin never arrived", "\n".join(logs.output))
        self.assertEqual(self.rx.batches, {})

    def test_nothing_being_received_means_plain_timeout(self):
        self.queue_big_file()
        empty = make_file(self.staging, "empty", b"", age=0)
        self.assertIsNone(dyode.last_data_time(empty))      # listening only
        self.assertIsNone(dyode.last_data_time(None))
        self.assertIsNone(dyode.last_data_time(empty + ".gone"))
        with self.assertLogs("dyode.folder", "WARNING"):
            self.rx.sweep(300, dyode.last_data_time(empty))
        self.assertEqual(self.rx.batches, {})

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


class UnattendedUdpcastTests(unittest.TestCase):
    """Regression: the service worked by hand but moved nothing under
    systemd, where stdin is /dev/null and the box may boot before the
    diode NIC is configured."""

    NET = {"network": {"in_ip": "10.9.0.1", "out_ip": "10.9.0.2",
                       "in_interface": "eth0", "out_interface": "eth1"}}

    def cast(self, **extra):
        props = {"port": 9600, "bitrate": 900}
        props.update(extra)
        return dyode.UdpCast(props, self.NET)

    def test_both_commands_disable_keyboard_input(self):
        """udp-sender(1)/udp-receiver(1) otherwise read a start signal from
        the keyboard and print a 'press any key' prompt."""
        self.assertIn("--nokbd", self.cast().sender_cmd("/in/f"))
        self.assertIn("--nokbd", self.cast().receiver_cmd("/out/f"))

    def test_autostart_counts_hello_retransmissions_not_receivers(self):
        cmd = self.cast().sender_cmd("/in/f")
        self.assertEqual(cmd[cmd.index("--autostart") + 1], "5")
        cmd = self.cast(autostart=12).sender_cmd("/in/f")
        self.assertEqual(cmd[cmd.index("--autostart") + 1], "12")

    def test_receiver_start_timeout_is_explicit_and_can_be_disabled(self):
        cmd = self.cast().receiver_cmd("/out/f")
        self.assertEqual(cmd[cmd.index("--start-timeout") + 1], "300")
        self.assertNotIn("--start-timeout",
                         self.cast(start_timeout=0).receiver_cmd("/out/f"))

    def test_output_path_is_still_the_last_argument(self):
        for cmd in (self.cast().sender_cmd("/in/f"),
                    self.cast().receiver_cmd("/out/f")):
            self.assertEqual(cmd[-2:], ["-f", cmd[-1]])


class IdleReceiverTests(unittest.TestCase):
    """An idle diode hits udp-receiver's start timeout constantly. Treating
    that as a failure logged errors and, worse, slept a second before
    listening again -- a window in which a transfer is missed outright.

    Classification is by elapsed time and bytes received, never by udpcast's
    wording: the first attempt looked for "timeout" in stderr, and the real
    udpcast says nothing of the kind.
    """

    # What udpcast 20120424 actually printed on an idle start timeout,
    # reported from a production journal.  No mention of "timeout".
    REAL_IDLE_STDERR = "udp-receiver 20120424\nReceiver Error"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dyode_idle_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.blob = os.path.join(self.dir, "blob")
        self.cast = self.make_cast()

    @staticmethod
    def make_cast(**props):
        base = {"port": 9600}
        base.update(props)
        return dyode.UdpCast(
            base,
            {"network": {"in_ip": "10.9.0.1", "out_ip": "10.9.0.2",
                         "in_interface": "eth0", "out_interface": "eth1"}})

    def fake_run(self, ok, stderr, elapsed, blob_bytes=b"", cast=None):
        with open(self.blob, "wb") as fh:
            fh.write(blob_bytes)

        def runner(cmd, quiet=False):
            return ok, stderr, elapsed
        (cast or self.cast)._run = runner

    def test_real_udpcast_idle_message_is_idle(self):
        """Regression: this exact output was logged as an ERROR every five
        minutes, and each one bought a one-second deaf window."""
        self.fake_run(False, self.REAL_IDLE_STDERR, elapsed=300.0)
        with self.assertNoLogs("dyode.folder", "WARNING"):
            self.assertEqual(self.cast.receive(self.blob), "idle")

    def test_classification_ignores_the_wording(self):
        for stderr in (self.REAL_IDLE_STDERR, "", "something new in 2031",
                       "timeout"):
            self.fake_run(False, stderr, elapsed=299.5)
            self.assertEqual(self.cast.receive(self.blob), "idle", stderr)

    def test_slightly_early_expiry_still_counts_as_idle(self):
        """udpcast's timer and ours do not start at the same instant."""
        self.fake_run(False, self.REAL_IDLE_STDERR, elapsed=241.0)
        self.assertEqual(self.cast.receive(self.blob), "idle")

    def test_same_message_but_fast_exit_is_a_real_error(self):
        """A bad interface also prints 'Receiver Error' -- immediately."""
        self.fake_run(False, self.REAL_IDLE_STDERR, elapsed=0.05)
        with self.assertLogs("dyode.folder", "ERROR") as logs:
            self.assertEqual(self.cast.receive(self.blob), "error")
        self.assertIn("check the interface", "\n".join(logs.output))

    def test_word_timeout_does_not_make_a_fast_failure_idle(self):
        """The old heuristic would have swallowed this real failure."""
        self.fake_run(False, "udp-receiver: socket timeout binding eth9",
                      elapsed=0.2)
        with self.assertLogs("dyode.folder", "ERROR"):
            self.assertEqual(self.cast.receive(self.blob), "error")

    def test_bytes_received_then_stopped_is_a_real_error(self):
        """A stalled transfer must not be mistaken for an idle link, even
        after a full start_timeout's worth of waiting."""
        self.fake_run(False, self.REAL_IDLE_STDERR, elapsed=300.0,
                      blob_bytes=b"partial")
        with self.assertLogs("dyode.folder", "ERROR") as logs:
            self.assertEqual(self.cast.receive(self.blob), "error")
        self.assertIn("started and then stopped", "\n".join(logs.output))

    SNMP = ("Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors "
            "SndbufErrors InCsumErrors IgnoredMulti MemErrors\n"
            "Udp: 900 0 %d 10 %d 0 0 0 0\n")

    def stall_with_drops(self, before, after):
        """A stalled receive, with the kernel's RcvbufErrors counter at
        `before` when udp-receiver starts and `after` when it exits."""
        snmp = os.path.join(self.dir, "snmp")
        with open(snmp, "w") as fh:
            fh.write(self.SNMP % (before, before))
        self.cast.snmp_path = snmp
        with open(self.blob, "wb") as fh:
            fh.write(b"partial")

        def runner(cmd, quiet=False):
            with open(snmp, "w") as fh:
                fh.write(self.SNMP % (after, after))
            return False, "Missed blocks: 400,401", 39.0
        self.cast._run = runner
        with self.assertLogs("dyode.folder", "ERROR") as logs:
            self.assertEqual(self.cast.receive(self.blob), "error")
        return "\n".join(logs.output)

    def test_stall_with_buffer_overflow_says_so_and_points_at_the_fix(self):
        """Field case (CAVEATS.md): writeback bursts on an encrypted disk
        stalled the receiver and overflowed its socket buffer."""
        msg = self.stall_with_drops(1000, 4321)
        self.assertIn("3321 UDP packets were dropped on this box", msg)
        self.assertIn("--tuning-only", msg)
        self.assertIn("Missed blocks", msg)        # udpcast's own text kept

    def test_stall_without_overflow_points_away_from_this_box(self):
        msg = self.stall_with_drops(1000, 1000)
        self.assertIn("lost before reaching it", msg)
        self.assertNotIn("--tuning-only", msg)

    def test_stall_hint_is_skipped_when_the_counter_is_unreadable(self):
        self.cast.snmp_path = os.path.join(self.dir, "missing")
        self.fake_run(False, "x", elapsed=39.0, blob_bytes=b"partial")
        with self.assertLogs("dyode.folder", "ERROR") as logs:
            self.cast.receive(self.blob)
        msg = "\n".join(logs.output)
        self.assertIn("started and then stopped", msg)
        self.assertNotIn("dropped", msg)
        self.assertNotIn("lost before", msg)

    def test_reads_rcvbuf_errors_by_column_name(self):
        path = os.path.join(self.dir, "snmp")
        with open(path, "w") as fh:
            fh.write("Ip: Forwarding\nIp: 1\n" + self.SNMP % (7, 42))
        self.assertEqual(dyode.udp_rcvbuf_errors(path), 42)
        self.assertIsNone(dyode.udp_rcvbuf_errors(path + ".nope"))

    def test_no_start_timeout_means_any_exit_is_an_error(self):
        cast = self.make_cast(start_timeout=0)
        self.fake_run(False, self.REAL_IDLE_STDERR, elapsed=86400.0,
                      cast=cast)
        with self.assertLogs("dyode.folder", "ERROR") as logs:
            self.assertEqual(cast.receive(self.blob), "error")
        self.assertIn("no start timeout was set", "\n".join(logs.output))

    def test_first_idle_is_noted_once_then_quiet(self):
        self.fake_run(False, self.REAL_IDLE_STDERR, elapsed=300.0)
        with self.assertLogs("dyode.folder", "DEBUG") as logs:
            for _ in range(5):
                self.assertEqual(self.cast.receive(self.blob), "idle")
        infos = [r for r in logs.records if r.levelname == "INFO"]
        self.assertEqual(len(infos), 1)
        self.assertFalse([r for r in logs.records
                          if r.levelno >= logging.WARNING])

    def test_success_is_ok(self):
        self.fake_run(True, "", elapsed=12.0)
        self.assertEqual(self.cast.receive(self.blob), "ok")

    def test_run_reports_elapsed_time(self):
        ok, text, elapsed = dyode.UdpCast._run(
            [sys.executable, "-c", "import time; time.sleep(0.2)"])
        self.assertTrue(ok)
        self.assertGreaterEqual(elapsed, 0.2)


class StaticArpTests(unittest.TestCase):
    """The entry is lost on a link flap and cannot be set before the NIC has
    an address, and nothing on a one-way link reports either."""

    NET = {"in_ip": "10.9.0.1", "out_ip": "10.9.0.2",
           "in_interface": "eth0", "out_interface": "eth1",
           "out_mac": "b8:27:eb:b1:ff:ab"}

    def test_missing_mac_is_not_a_success(self):
        with self.assertLogs("dyode.folder", "WARNING"):
            self.assertFalse(dyode.set_static_arp(dict(self.NET, out_mac=None)))

    def test_command_is_an_argument_list_with_permanent_nud(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        with unittest.mock.patch.object(subprocess, "run", fake_run):
            self.assertTrue(dyode.set_static_arp(dict(self.NET)))
        self.assertEqual(seen["cmd"][:3], ["ip", "neigh", "replace"])
        self.assertEqual(seen["cmd"][-2:], ["nud", "permanent"])
        self.assertIn("b8:27:eb:b1:ff:ab", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("dev") + 1], "eth0")

    def test_failure_is_reported_not_swallowed(self):
        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(
                cmd, 2, b"", b"Cannot find device \"eth0\"")
        with unittest.mock.patch.object(subprocess, "run", fake_run):
            with self.assertLogs("dyode.folder", "ERROR") as logs:
                self.assertFalse(dyode.set_static_arp(dict(self.NET)))
        self.assertIn("Cannot find device", "\n".join(logs.output))

    def test_keeper_retries_and_logs_only_transitions(self):
        results = [False, False, True, True]
        calls = []

        def fake_set(net):
            if not results:
                raise KeyboardInterrupt        # ends the keeper loop
            calls.append(net)
            return results.pop(0)
        with unittest.mock.patch.object(dyode, "set_static_arp", fake_set), \
                unittest.mock.patch.object(dyode.time, "sleep", lambda s: None):
            with self.assertLogs("dyode.folder") as logs:
                with self.assertRaises(KeyboardInterrupt):
                    dyode.run_arp_keeper(dict(self.NET), interval=0)
        self.assertEqual(len(calls), 4)
        text = "\n".join(logs.output)
        # One failure transition and one recovery, not one line per pass.
        self.assertEqual(text.count("is NOT in place"), 1)
        self.assertEqual(text.count("entry in place"), 1)


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
