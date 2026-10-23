import json
import os
import subprocess
import sys
import random
import shutil
import socket
import tempfile
import textwrap
import threading
import time
import unittest
import zlib

import _setup
import dyode_common as common
import logstream as ls


def props(**over):
    base = {"port": 0, "listen_udp": "127.0.0.1:0", "out": "/tmp/x", "max_line": 1024}
    base.update(over)
    return ls.normalize_props("t", base, common.ConfigError)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ListSink:
    def __init__(self):
        self.records = []

    def write(self, records):
        self.records.extend(records)


# --------------------------------------------------------------------------

class ConfigTests(unittest.TestCase):
    def test_needs_a_source_and_a_sink(self):
        with self.assertRaisesRegex(common.ConfigError, "at least one source"):
            ls.normalize_props("t", {"port": 1, "out": "/x"}, common.ConfigError)
        with self.assertRaisesRegex(common.ConfigError, "somewhere to put"):
            ls.normalize_props("t", {"port": 1, "listen_udp": 514}, common.ConfigError)

    def test_parsing(self):
        p = props(listen_udp=514, listen_tcp="10.0.0.1:601",
                  forward=["udp://siem:514", "TCP://10.1.1.1:6514"],
                  allow_from=["192.168.1.0/24", "10.0.0.5"], tail="/var/log/*.log")
        self.assertEqual(p["listen_udp"], ("0.0.0.0", 514))
        self.assertEqual(p["listen_tcp"], ("10.0.0.1", 601))
        self.assertEqual(p["forward"], [("udp", "siem", 514), ("tcp", "10.1.1.1", 6514)])
        self.assertEqual(p["tail"], ["/var/log/*.log"])
        self.assertEqual(p["stale_after"], 30.0)
        allowed = ls.make_allow(p["allow_from"])
        self.assertTrue(allowed("192.168.1.77"))
        self.assertTrue(allowed("10.0.0.5"))
        self.assertFalse(allowed("10.0.0.6"))

    def test_rejects_bad_values(self):
        for bad in ({"forward": "http://x:1"}, {"forward": "udp://:514"},
                    {"tail": "relative/*.log"}, {"format": "xml"},
                    {"allow_from": "not-a-net"}, {"copies": 9},
                    {"max_line": 100000, "batch_bytes": 48000}, {"out_file": "../x"},
                    {"listen_udp": "host:99999"}):
            with self.subTest(bad=bad), self.assertRaises(common.ConfigError):
                props(**bad)

    def test_reorder_window_never_shorter_than_the_last_copy(self):
        """A window shorter than the copies' spread wastes them: the output
        side declares the batch lost before its last copy can arrive."""
        p = props(copies=3, batch_interval=0.5, reorder_window=0.1)
        self.assertGreaterEqual(p["reorder_window"], 2 * (0.5 + 0.5))
        self.assertEqual(props(reorder_window=10)["reorder_window"], 10)

    def test_config_loader_knows_the_type(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(textwrap.dedent("""
                modules:
                  ot-logs:
                    type: Logs
                    port: 9700
                    listen_udp: 514
                    out: /var/log/dyode-ot
            """))
        try:
            cfg = common.load_config(fh.name)
        finally:
            os.unlink(fh.name)
        mod = cfg["modules"]["ot-logs"]
        self.assertEqual(mod["type"], "logs")
        self.assertEqual(mod["listen_udp"], ("0.0.0.0", 514))
        self.assertEqual(mod["copies"], 2)


class WireFormatTests(unittest.TestCase):
    def test_round_trip_keeps_invalid_utf8_bytes(self):
        raw = b"caf\xc3\xa9 \xff\xfe end"
        msg = ls.to_text(raw, 100)
        batch = ls.decode_batch(ls.encode_batch(7, 3, [(1.5, "h", "udp", msg)], 2))
        self.assertEqual((batch["b"], batch["s"], batch["d"]), (7, 3, 2))
        self.assertEqual(ls.to_bytes(batch["l"][0][3]), raw)

    def test_rejects_garbage_and_bombs(self):
        self.assertIsNone(ls.decode_batch(b"\xff\xd8 a jpeg"))
        self.assertIsNone(ls.decode_batch(ls.HEADER + b"not zlib"))
        self.assertIsNone(ls.decode_batch(ls.HEADER + zlib.compress(b"[1,2]")))
        bomb = ls.HEADER + zlib.compress(b"{" + b" " * (2 * 1024 * 1024) + b"}")
        self.assertIsNone(ls.decode_batch(bomb, max_size=1024 * 1024))

    def test_truncation_is_marked(self):
        self.assertEqual(ls.to_text(b"x" * 80, 64), "x" * 64 + " [dyode: truncated 16 bytes]")

    def test_compression_pays_off_on_log_text(self):
        line = "<134>Oct 12 22:14:15 plc-%d modbusd[812]: read holding registers 400-450 ok"
        recs = [(1.0, "10.0.0.%d" % (i % 9), "udp", line % (i % 9)) for i in range(400)]
        payload = ls.encode_batch(1, 1, recs, 0)
        raw = len(common.encode_json(recs))
        self.assertLess(len(payload), raw / 5)


class FramerTests(unittest.TestCase):
    def test_both_framings_and_split_reads(self):
        f = ls.StreamFramer(1000)
        counted = b"<13>1 hello\nwith a newline inside"
        stream = (b"%d " % len(counted) + counted + b"plain\n" + b"nul term\x00"
                  + b"2026-10-12 starts with digits\n")
        out = []
        for i in range(len(stream)):                    # worst case: 1 byte per recv
            out += f.feed(stream[i:i + 1])
        self.assertEqual(out, [counted, b"plain", b"nul term",
                               b"2026-10-12 starts with digits"])

    def test_overlong_line_truncated_and_rest_skipped(self):
        f = ls.StreamFramer(100)
        out = f.feed(b"a" * 300)
        out += f.feed(b"b" * 50 + b"\nnext\n")
        self.assertEqual(len(out[0]), 101)
        self.assertEqual(out[1:], [b"next"])

    def test_absurd_octet_count_does_not_stall(self):
        f = ls.StreamFramer(100)
        self.assertEqual(f.feed(b"9999999 <13> fake\n"), [b"9999999 <13> fake"])


class SenderTests(unittest.TestCase):
    def make(self, **over):
        self.clock = FakeClock()
        self.sent = []
        p = props(**over)
        return ls.LogSender(p, lambda b: self.sent.append(ls.decode_batch(b)),
                            clock=self.clock, sleep=self.clock.sleep)

    def test_batches_by_size_and_numbers_them(self):
        s = self.make(batch_bytes=2000, copies=1)
        for i in range(100):
            s.add("h", "udp", "x" * 70, ts=1.0)
        s.tick()
        self.assertGreater(len(self.sent), 3)
        self.assertEqual([b["s"] for b in self.sent], list(range(len(self.sent))))
        self.assertEqual(sum(len(b["l"]) for b in self.sent), 100)

    def test_buffer_overflow_is_counted_and_reported(self):
        s = self.make(buffer_lines=10, copies=1)
        for _ in range(15):
            s.add("h", "udp", "m")
        s.tick()
        self.assertEqual(s.dropped, 5)
        self.assertEqual(self.sent[-1]["d"], 5)

    def test_heartbeat_only_when_idle(self):
        s = self.make(heartbeat=10, copies=1)
        s.tick()                         # first tick announces the stream
        self.assertEqual(len(self.sent), 1)
        self.clock.now += 5
        s.tick()
        self.assertEqual(len(self.sent), 1)
        self.clock.now += 6
        s.tick()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[-1]["l"], [])

    def test_repeat_is_sent_later_not_back_to_back(self):
        s = self.make(copies=2, batch_interval=0.5)
        s.add("h", "udp", "one")
        s.tick()
        self.assertEqual([b["s"] for b in self.sent], [0])
        self.clock.now += 0.6
        s.add("h", "udp", "two")
        s.tick()
        self.assertEqual([b["s"] for b in self.sent], [0, 1, 0][:2] + [0])
        self.clock.now += 0.6
        s.tick()
        self.assertEqual([b["s"] for b in self.sent][-1], 1)

    def test_rate_cap_holds_new_data_and_drops_repeats_first(self):
        s = self.make(max_kbps=64, copies=2, batch_bytes=4000)   # 8 KB/s
        start = self.clock.now
        for i in range(400):
            s.add("h", "udp", os.urandom(40).hex())             # incompressible
        s.tick()
        sent_bytes = s.stats["wire_bytes"]
        elapsed = self.clock.now - start
        self.assertEqual(sum(len(b["l"]) for b in self.sent if b), 400)
        # never faster than the cap, beyond the initial burst allowance
        self.assertLessEqual(sent_bytes - s.bucket.capacity, 8000 * elapsed + 1)
        self.assertGreater(s.stats["repeats_skipped"], 0)


class ReceiverTests(unittest.TestCase):
    def make(self, **over):
        self.clock = FakeClock()
        self.sink = ListSink()
        p = props(reorder_window=3, heartbeat=10, **over)
        return ls.LogReceiver([self.sink], p, module="t", clock=self.clock)

    def batch(self, seq, lines=(), boot=1, dropped=0):
        return ls.encode_batch(boot, seq, [(1.0, "h", "udp", m) for m in lines], dropped)

    def msgs(self):
        return [r[3] for r in self.sink.records]

    def test_duplicates_and_reordering(self):
        r = self.make()
        r.feed(self.batch(0, ["a"]))
        r.feed(self.batch(2, ["c"]))         # 1 is late
        r.feed(self.batch(0, ["a"]))         # repeat
        self.assertEqual(self.msgs(), ["a"])
        r.feed(self.batch(1, ["b"]))         # 1's repeat arrives
        self.assertEqual(self.msgs(), ["a", "b", "c"])
        self.assertEqual(r.stats["duplicates"], 1)
        self.assertEqual(r.stats["lost_batches"], 0)

    def test_gap_is_declared_after_the_window_and_reported(self):
        r = self.make()
        r.feed(self.batch(0, ["a"]))
        r.feed(self.batch(3, ["d"]))
        self.clock.now += 2
        r.poll()
        self.assertEqual(self.msgs(), ["a"])
        self.clock.now += 1.5
        with self.assertLogs("dyode.logs", "WARNING") as cm:
            r.poll()
        self.assertIn("2 log batch(es) lost", cm.output[0])
        self.assertIn("sequence 1-2", cm.output[0])
        self.assertEqual(self.msgs(), ["a", "d"])
        r.feed(self.batch(1, ["b"]))         # too late now: ignored
        self.assertEqual(self.msgs(), ["a", "d"])

    def test_sender_restart_starts_a_new_stream(self):
        r = self.make()
        r.feed(self.batch(0, ["a"], boot=1))
        r.feed(self.batch(5, ["b"], boot=1))
        r.feed(self.batch(0, ["new"], boot=2))
        self.assertEqual(self.msgs(), ["a", "b", "new"])
        self.assertEqual(r.stats["lost_batches"], 4)

    def test_source_drops_reported_once(self):
        r = self.make()
        with self.assertLogs("dyode.logs", "WARNING") as cm:
            r.feed(self.batch(0, ["a"], dropped=0))
            r.feed(self.batch(1, ["b"], dropped=7))
            r.feed(self.batch(2, ["c"], dropped=7))
        self.assertEqual(len(cm.output), 1)
        self.assertIn("dropped 7 line", cm.output[0])

    def test_stale_and_resume(self):
        r = self.make()
        r.feed(self.batch(0))
        self.clock.now += 31
        with self.assertLogs("dyode.logs", "WARNING"):
            r.poll()
        self.assertTrue(r.stale)
        with self.assertLogs("dyode.logs", "INFO"):
            r.feed(self.batch(1))
        self.assertFalse(r.stale)

    def test_bad_payload_ignored(self):
        r = self.make()
        with self.assertLogs("dyode.logs", "WARNING"):
            r.feed(b"\xff\xd8 not a batch")
        self.assertEqual(r.stats["bad"], 1)


class TailerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "app.log")
        self.sink = []
        self.sender = type("S", (), {"add": lambda _s, src, chan, msg: self.sink.append(msg)})()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def tailer(self, **over):
        p = props(tail=[os.path.join(self.dir, "*.log")], **over)
        tailer = ls.FileTailer(p, self.sender, hostname="box")
        self.addCleanup(tailer.close)
        return tailer

    def append(self, text, path=None):
        with open(path or self.path, "a") as fh:
            fh.write(text)

    def test_starts_at_end_and_follows(self):
        self.append("old\n")
        t = self.tailer()
        t.poll()
        self.append("new 1\npart")
        t.poll()
        self.append("ial\n")
        t.poll()
        self.assertEqual(self.sink, ["new 1", "partial"])

    def test_rename_rotation_loses_nothing(self):
        t = self.tailer()
        self.append("")
        t.poll()
        self.append("before rotation\n")
        os.rename(self.path, self.path + ".1")          # logrotate's rename ...
        self.append("after rotation\n")                 # ... then a new file
        t.poll()
        self.assertEqual(self.sink, ["before rotation", "after rotation"])

    def test_truncation_restarts_at_zero(self):
        self.append("x" * 100 + "\n")
        t = self.tailer()
        t.poll()
        open(self.path, "w").close()
        self.append("fresh\n")
        t.poll()
        self.assertEqual(self.sink, ["fresh"])

    def test_new_file_read_from_start(self):
        t = self.tailer()
        t.poll()
        self.append("first line\n", os.path.join(self.dir, "other.log"))
        t.poll()
        self.assertEqual(self.sink, ["first line"])

    def test_state_file_resumes_after_restart(self):
        state = os.path.join(self.dir, "state", "tail.json")
        self.append("")
        t = self.tailer(tail_state=state)
        t.poll()
        self.append("one\n")
        t.poll()
        t.close()
        self.append("written while down\n")
        t2 = self.tailer(tail_state=state)
        t2.poll()
        self.assertEqual(self.sink, ["one", "written while down"])


class SinkTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_formats_and_escaping(self):
        rec = (0.0, "10.0.0.1", "udp", "evil\n\x1b[2Jline")
        for fmt, expected in (("raw", "evil\\x0a\\x1b[2Jline"),
                              ("prefixed", "1970-01-01T00:00:00.000Z 10.0.0.1 evil\\x0a\\x1b[2Jline")):
            sink = ls.FileSink(self.dir, fmt + ".log", False, fmt)
            sink.write([rec])
            sink.close()
            with open(os.path.join(self.dir, fmt + ".log")) as fh:
                self.assertEqual(fh.read(), expected + "\n")
        sink = ls.FileSink(self.dir, "j.log", False, "json")
        sink.write([rec])
        sink.close()
        with open(os.path.join(self.dir, "j.log")) as fh:
            self.assertEqual(json.loads(fh.read())["msg"], rec[3])

    def test_split_by_source_cannot_escape_the_directory(self):
        sink = ls.FileSink(self.dir, "x", True, "raw")
        self.addCleanup(sink.close)
        sink.write([(0.0, "../../etc/passwd", "udp", "m"), (0.0, "10.0.0.9", "udp", "n")])
        self.assertEqual(sorted(os.listdir(self.dir)),
                         ["10.0.0.9.log", "_.._etc_passwd.log"])

    def test_reopens_after_logrotate(self):
        path = os.path.join(self.dir, "r.log")
        sink = ls.FileSink(self.dir, "r.log", False, "raw")
        self.addCleanup(sink.close)
        sink.write([(0.0, "h", "udp", "one")])
        os.rename(path, path + ".1")
        sink.write([(0.0, "h", "udp", "two")])
        with open(path) as fh:
            self.assertEqual(fh.read(), "two\n")
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o640")

    def test_syslog_wrap(self):
        self.assertEqual(ls.syslog_wrap((0.0, "h", "udp", "<13>already syslog")),
                         b"<13>already syslog")
        self.assertEqual(ls.syslog_wrap((0.0, "plc gw", "file:/x", "plain")),
                         b"<13>1 1970-01-01T00:00:00.000Z plc_gw dyode - - - plain")

    def test_tcp_forwarder_queues_while_siem_is_down(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()                                     # nothing listening yet
        fwd = ls.TcpForwarder("127.0.0.1", port, wrap=False)
        try:
            with self.assertLogs("dyode.logs", "WARNING"):
                fwd.write([(0.0, "h", "udp", "queued %d" % i) for i in range(3)])
                time.sleep(0.3)
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", port))
            srv.listen(1)
            srv.settimeout(5)
            conn, _ = srv.accept()
            conn.settimeout(3)
            data = b""
            while data.count(b"\n") < 3:
                data += conn.recv(4096)
            self.assertEqual(data, b"queued 0\nqueued 1\nqueued 2\n")
            conn.close()
            srv.close()
        finally:
            fwd.close()


class LossyEndToEndTests(unittest.TestCase):
    """Real sockets, with a relay in the middle that drops datagrams.

    The property under test: every batch the input side numbered is either
    delivered or reported lost.  Nothing goes missing silently.
    """

    def run_link(self, loss, copies, lines=3000, seed=7):
        rng = random.Random(seed)
        stop = threading.Event()
        p = props(copies=copies, batch_interval=0.05, batch_bytes=8000,
                  reorder_window=0.5, max_kbps=50000)
        sink = ListSink()
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(0.2)
        receiver = ls.LogReceiver([sink], p, module="t")
        rx_thread = threading.Thread(target=ls.receive_loop,
                                     args=(rx, receiver, stop, "t"), daemon=True)
        rx_thread.start()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(tx.close)
        self.addCleanup(rx.close)
        dest = rx.getsockname()

        def lossy_send(payload):
            for dg in common.udp_chunks(payload):
                if rng.random() >= loss:
                    tx.sendto(dg, dest)
                time.sleep(0.0001)

        sender = ls.LogSender(p, lossy_send, module="t")
        for i in range(lines):
            sender.add("10.0.0.%d" % (i % 5), "udp",
                       "<134>line %06d %s" % (i, os.urandom(12).hex()))
        for _ in range(10):
            sender.tick()
            time.sleep(0.06)
        time.sleep(1.0)
        stop.set()
        rx_thread.join(3)
        receiver._resolve_gap(force=True)
        total = sender.seq
        got = receiver.stats["batches"]
        lost = receiver.stats["lost_batches"]
        seqs = [int(r[3].split()[1]) for r in sink.records]
        return total, got, lost, seqs

    def test_no_silent_loss_and_order_kept(self):
        for loss, copies in ((0.0, 1), (0.02, 1), (0.02, 2)):
            with self.subTest(loss=loss, copies=copies):
                total, got, lost, seqs = self.run_link(loss, copies)
                # head/tail batches lost on BOTH copies cannot be seen as a
                # gap until something newer arrives; everything in between
                # must be accounted for exactly
                self.assertEqual(seqs, sorted(seqs))
                self.assertEqual(len(seqs), len(set(seqs)))
                self.assertLessEqual(got + lost, total)
                self.assertGreaterEqual(got + lost, total - 2)
                if loss == 0:
                    self.assertEqual(len(seqs), 3000)

    def test_repeat_copy_recovers_most_loss(self):
        _, _, lost1, seqs1 = self.run_link(0.03, copies=1, seed=11)
        _, _, lost2, seqs2 = self.run_link(0.03, copies=2, seed=11)
        self.assertGreater(lost1, 0)
        self.assertLess(lost2, lost1)
        self.assertGreater(len(seqs2), len(seqs1))


class IntegrationTests(unittest.TestCase):
    def test_wizard_writes_a_logs_module_the_runtime_accepts(self):
        if _setup.REPO not in sys.path:
            sys.path.insert(0, _setup.REPO)
        import dyode_setup_core as core
        model = core.ConfigModel("v1")
        model.add_module("OT syslog", {
            "type": "logs", "port": model.next_port("logs"), "listen_udp": 514,
            "tail": ["/var/log/plc/*.log"], "out": "/var/log/dyode-ot",
            "forward": ["udp://10.20.0.5:514", "tcp://siem.corp:601"]})
        self.assertIsNone(model.validate())
        text = model.to_yaml_text()
        again = core.ConfigModel.from_yaml_text(text, "v1")
        self.assertEqual(again.to_yaml_text().split("\n")[3:], text.split("\n")[3:])
        self.assertIn("port: 9700", text)
        self.assertTrue(core.valid_forward_list("udp://a:514, TCP://b.c:601"))
        self.assertFalse(core.valid_forward_list("http://a:80"))
        self.assertFalse(core.valid_abs_path_list("/ok, relative/x"))

    def test_dyode_v2_ignores_a_logs_module_without_logstream(self):
        """Both variants read the same dyode_common.py; v2 ships no
        logstream.py and must still load a config that mentions 'logs'."""
        self.assertFalse(os.path.exists(os.path.join(_setup.V2_IN, "logstream.py")))
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("logging: {dir: ''}\n"
                     "modules:\n  l: {type: logs, port: 9700, listen_udp: 514}\n")
        self.addCleanup(os.unlink, fh.name)
        res = subprocess.run([sys.executable, "dyode_in.py", "-c", fh.name],
                             cwd=_setup.V2_IN, capture_output=True, text=True,
                             env=dict(os.environ, PYTHONPATH=_setup.FAKES), timeout=30)
        self.assertIn("not supported by DYODE v2; ignored", res.stderr)
        self.assertIn("no modbus modules configured", res.stderr)
        self.assertNotIn("Traceback", res.stderr)

    def test_v1_entry_points_run_logs_modules(self):
        sys.path.insert(0, _setup.V1)
        import dyode_in
        import dyode_out
        self.assertIs(dyode_in.AGENTS["logs"], ls.run_logs_input)
        self.assertIs(dyode_out.AGENTS["logs"], ls.run_logs_output)


if __name__ == "__main__":
    unittest.main()
