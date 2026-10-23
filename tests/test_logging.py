"""Transfer logging: the JSON event stream, and the archiver.

The two claims the design rests on are tested here for real rather than
asserted: that several processes can append concurrently without corrupting
a line, and that the daemons follow the file when logrotate replaces it.
"""

import datetime
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

import _setup  # noqa: F401
import dyode
import dyode_common as common

REPO = _setup.REPO
LOGS_TOOL = os.path.join(REPO, "dyode_logs.py")
sys.path.insert(0, REPO)
import dyode_logs  # noqa: E402


def read_events(directory, name="transfer.jsonl"):
    path = os.path.join(directory, name)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class LogSetupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dyode_log_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.addCleanup(common.setup_logging, "INFO", None)

    def cfg(self, side="in", **over):
        log_cfg = dict(common.DEFAULT_LOGGING, dir=self.dir)
        log_cfg.update(over)
        return {"_side": side, "logging": log_cfg}

    def test_both_files_are_written(self):
        common.setup_logging("INFO", self.cfg())
        common.log.info("a human line")
        common.log_event("batch_sent", module="m", files=3)
        with open(os.path.join(self.dir, "dyode.log"), encoding="utf-8") as fh:
            self.assertIn("a human line", fh.read())
        self.assertEqual(len(read_events(self.dir)), 1)

    def test_every_line_is_one_complete_json_object(self):
        common.setup_logging("INFO", self.cfg())
        for i in range(50):
            common.log_event("file_sent", module="m", path="f%d" % i, bytes=i)
        events = read_events(self.dir)
        self.assertEqual(len(events), 50)
        self.assertEqual([e["path"] for e in events],
                         ["f%d" % i for i in range(50)])
        for event in events:
            self.assertEqual(event["v"], common.EVENT_SCHEMA_VERSION)
            self.assertEqual(event["side"], "in")
            self.assertTrue(event["ts"].endswith("Z"), event["ts"])
            datetime.datetime.strptime(event["ts"], "%Y-%m-%dT%H:%M:%S.%fZ")

    def test_human_log_is_never_polluted_with_events(self):
        common.setup_logging("INFO", self.cfg())
        common.log_event("batch_sent", module="m", files=1)
        with open(os.path.join(self.dir, "dyode.log"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "")

    def test_per_file_events_can_be_switched_off(self):
        common.setup_logging("INFO", self.cfg(per_file_events=False))
        common.log_event("file_sent", module="m", per_file=True, path="f")
        common.log_event("batch_sent", module="m", files=1)
        names = [e["event"] for e in read_events(self.dir)]
        self.assertEqual(names, ["batch_sent"])

    def test_repeated_setup_does_not_duplicate_lines(self):
        for _ in range(3):
            common.setup_logging("INFO", self.cfg())
        common.log_event("batch_sent", module="m", files=1)
        self.assertEqual(len(read_events(self.dir)), 1)

    def test_unusable_directory_falls_back_to_stderr(self):
        """A daemon must still start when /var/log is not available.

        The log directory is placed under a regular file, so makedirs fails
        with ENOTDIR whatever the uid -- unlike a mode-based test, which
        root would sail straight through.
        """
        blocker = os.path.join(self.dir, "iam-a-file")
        open(blocker, "w").close()
        common.setup_logging("INFO", self.cfg(dir=os.path.join(blocker, "sub")))
        common.log.info("still logging to stderr")
        common.log_event("batch_sent", module="m", files=1)   # must not raise
        self.assertEqual(common.event_log.handlers, [])

    def test_empty_dir_means_stderr_only(self):
        common.setup_logging("INFO", self.cfg(dir=""))
        common.log_event("batch_sent", module="m", files=1)
        self.assertEqual(common.event_log.handlers, [])
        self.assertEqual(os.listdir(self.dir), [])

    def test_follows_the_file_when_logrotate_replaces_it(self):
        """WatchedFileHandler must reopen after an external rename."""
        common.setup_logging("INFO", self.cfg())
        common.log_event("batch_sent", module="m", files=1)
        live = os.path.join(self.dir, "transfer.jsonl")
        os.rename(live, live + "-20260921")          # what logrotate does
        common.log_event("batch_sent", module="m", files=2)
        self.assertEqual([e["files"] for e in read_events(self.dir)], [2])
        with open(live + "-20260921", encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().strip().splitlines()), 1)


def _writer(directory, side, count):
    """Child process for the concurrency test."""
    log_cfg = dict(common.DEFAULT_LOGGING, dir=directory)
    common.setup_logging("INFO", {"_side": side, "logging": log_cfg})
    for i in range(count):
        common.log_event("file_sent", module="m", path="%s-%d" % (side, i),
                         bytes=i, filler="x" * 200)


class ConcurrencyTests(unittest.TestCase):
    """Several module processes append to one file; nothing may interleave."""

    def test_four_processes_append_without_corrupting_a_line(self):
        directory = tempfile.mkdtemp(prefix="dyode_conc_")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        per_process = 150
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_writer,
                             args=(directory, "w%d" % n, per_process))
                 for n in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            self.assertEqual(p.exitcode, 0)

        path = os.path.join(directory, "transfer.jsonl")
        with open(path, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 4 * per_process)
        seen = set()
        for line in lines:
            event = json.loads(line)          # raises if a line was torn
            seen.add(event["path"])
        self.assertEqual(len(seen), 4 * per_process)


class ArchiveTests(unittest.TestCase):
    LIVE = ("dyode.log", "transfer.jsonl")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dyode_arch_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.out = open(os.devnull, "w")
        self.addCleanup(self.out.close)
        for name in self.LIVE:
            self.write(name, "live\n")

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_packs_rotated_files_and_leaves_live_ones_alone(self):
        for stamp in ("20260921", "20260922"):
            self.write("dyode.log-%s" % stamp, "day %s\n" % stamp)
            self.write("transfer.jsonl-%s" % stamp, "{}\n")
        self.write("unrelated.txt", "not ours\n")

        tarball, packed, _ = dyode_logs.archive(
            self.dir, keep_weeks=0, when=datetime.date(2026, 9, 25),
            out=self.out)

        self.assertEqual(len(packed), 4)
        self.assertTrue(tarball.endswith("dyode-logs-2026-W39.tar.gz"))
        with tarfile.open(tarball, "r:gz") as tar:
            self.assertEqual(sorted(tar.getnames()), [
                "dyode.log-20260921", "dyode.log-20260922",
                "transfer.jsonl-20260921", "transfer.jsonl-20260922"])
        left = sorted(os.listdir(self.dir))
        self.assertEqual(left, ["archive", "dyode.log", "transfer.jsonl",
                                "unrelated.txt"])

    def test_nothing_to_do_is_not_an_error(self):
        tarball, packed, pruned = dyode_logs.archive(
            self.dir, keep_weeks=0, when=datetime.date(2026, 9, 25),
            out=self.out)
        self.assertIsNone(tarball)
        self.assertEqual((packed, pruned), ([], []))

    def test_second_run_in_the_same_week_keeps_earlier_members(self):
        when = datetime.date(2026, 9, 25)
        self.write("dyode.log-20260921", "first\n")
        dyode_logs.archive(self.dir, keep_weeks=0, when=when, out=self.out)
        self.write("dyode.log-20260922", "second\n")
        tarball, _, _ = dyode_logs.archive(self.dir, keep_weeks=0, when=when,
                                           out=self.out)
        with tarfile.open(tarball, "r:gz") as tar:
            self.assertEqual(sorted(tar.getnames()),
                             ["dyode.log-20260921", "dyode.log-20260922"])

    def test_prune_removes_only_archives_past_the_window(self):
        archive_dir = os.path.join(self.dir, dyode_logs.ARCHIVE_SUBDIR)
        os.makedirs(archive_dir)
        for name in ("dyode-logs-2025-W10.tar.gz", "dyode-logs-2026-W20.tar.gz",
                     "dyode-logs-2026-W39.tar.gz", "keep-me.txt"):
            open(os.path.join(archive_dir, name), "w").close()
        removed = dyode_logs.prune(self.dir, keep_weeks=26,
                                   when=datetime.date(2026, 9, 25),
                                   out=self.out)
        self.assertEqual([os.path.basename(p) for p in removed],
                         ["dyode-logs-2025-W10.tar.gz"])
        self.assertIn("keep-me.txt", os.listdir(archive_dir))

    def test_dry_run_changes_nothing(self):
        self.write("dyode.log-20260921", "day\n")
        before = sorted(os.listdir(self.dir))
        dyode_logs.archive(self.dir, keep_weeks=0,
                           when=datetime.date(2026, 9, 25), dry_run=True,
                           out=self.out)
        self.assertEqual(sorted(os.listdir(self.dir)), before)

    def test_week_tag_uses_iso_weeks(self):
        self.assertEqual(dyode_logs.week_tag(datetime.date(2026, 9, 25)),
                         "2026-W39")
        # 1 January 2027 falls in ISO week 53 of 2026.
        self.assertEqual(dyode_logs.week_tag(datetime.date(2027, 1, 1)),
                         "2026-W53")

    def test_stats_skips_unparsable_lines(self):
        self.write("transfer.jsonl", "\n".join([
            '{"v":1,"event":"batch_sent","files":3,"bytes":3000}',
            'this is not json',
            '{"v":1,"event":"batch_received","files_stored":2,'
            '"files_rejected":1,"bytes":2000}',
            '{"v":1,"event":"batch_sent","files":1,"bytes":10}',
        ]) + "\n")
        stats = dyode_logs.summarize(
            dyode_logs.read_events(os.path.join(self.dir, "transfer.jsonl")))
        self.assertEqual(stats["files_sent"], 4)
        self.assertEqual(stats["bytes_sent"], 3010)
        self.assertEqual(stats["files_stored"], 2)
        self.assertEqual(stats["files_rejected"], 1)

    def test_command_line_runs(self):
        self.write("dyode.log-20260921", "day\n")
        res = subprocess.run(
            [sys.executable, LOGS_TOOL, "--dir", self.dir, "--archive",
             "--stats"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(res.returncode, 0, res.stderr.decode())
        self.assertIn(b"archived 1 file", res.stdout)


class TransferEventTests(unittest.TestCase):
    """The events emitted by a real send and a real receive."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dyode_ev_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.addCleanup(common.setup_logging, "INFO", None)
        self.logs = os.path.join(self.dir, "logs")
        self.work = os.path.join(self.dir, "work")
        self.stage = os.path.join(self.dir, "stage")
        self.out = os.path.join(self.dir, "out")
        for path in (self.work, self.stage, self.out):
            os.makedirs(path)

    def use_side(self, side):
        common.setup_logging("INFO", {
            "_side": side,
            "logging": dict(common.DEFAULT_LOGGING, dir=self.logs)})

    def make_files(self, spec):
        files = []
        for rel, data in spec:
            path = os.path.join(self.dir, rel.replace("/", "_"))
            with open(path, "wb") as fh:
                fh.write(data)
            files.append((rel, path))
        return files

    def test_send_and_receive_events_share_a_batch_id(self):
        import test_folder
        cast = test_folder.LoopbackCast()
        self.use_side("in")
        files = self.make_files([("a.txt", b"a" * 100),
                                 ("sub/b.bin", b"b" * 250)])
        self.assertEqual(dyode.send_batch(files, cast, self.work,
                                          module="transfer"), 2)
        self.use_side("out")
        receiver = dyode.FolderReceiver(self.out, module="transfer")
        cast.deliver_all(receiver, self.stage)

        events = read_events(self.logs)
        by_name = {}
        for event in events:
            by_name.setdefault(event["event"], []).append(event)

        sent = by_name["batch_sent"][0]
        received = by_name["batch_received"][0]
        self.assertEqual(sent["batch"], received["batch"])
        self.assertEqual(sent["side"], "in")
        self.assertEqual(received["side"], "out")
        self.assertEqual(sent["files"], 2)
        self.assertEqual(sent["bytes"], 350)
        self.assertEqual(received["files_stored"], 2)
        self.assertEqual(received["files_rejected"], 0)
        self.assertEqual(received["files_missing"], 0)
        self.assertEqual(received["bytes"], 350)
        self.assertEqual(len(by_name["file_sent"]), 2)
        self.assertEqual(len(by_name["file_stored"]), 2)
        self.assertEqual([e["module"] for e in events], ["transfer"] * len(events))

    def test_a_corrupted_file_is_logged_as_rejected(self):
        import test_folder
        cast = test_folder.LoopbackCast()
        self.use_side("in")
        files = self.make_files([("good.txt", b"g" * 40),
                                 ("bad.txt", b"h" * 40)])
        dyode.send_batch(files, cast, self.work, module="transfer")

        self.use_side("out")
        receiver = dyode.FolderReceiver(self.out, module="transfer")
        # Corrupt the second payload in flight, as a lost packet would.
        cast.queue[2] = b"h" * 39
        cast.deliver_all(receiver, self.stage)

        # A damaged blob cannot be attributed to a file any more -- that
        # guess is exactly what used to corrupt the rest of the batch.  It
        # is logged by size, and the file it was meant to be is named when
        # the batch closes.
        rejected = [e for e in read_events(self.logs)
                    if e["event"] == "file_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reason"], "no_matching_file")
        self.assertEqual(rejected[0]["bytes"], 39)
        self.assertNotIn("path", rejected[0])

        receiver.sweep(batch_timeout=0.0001)
        missing = [e for e in read_events(self.logs)
                   if e["event"] == "file_missing"]
        self.assertEqual([e["path"] for e in missing], ["bad.txt"])
        summary = [e for e in read_events(self.logs)
                   if e["event"] == "batch_received"][0]
        self.assertEqual(summary["files_stored"], 1)
        self.assertEqual(summary["files_missing"], 1)
        self.assertEqual(summary["reason"], "timeout")

    def test_an_interrupted_batch_reports_the_missing_files(self):
        import test_folder
        cast = test_folder.LoopbackCast()
        self.use_side("out")
        receiver = dyode.FolderReceiver(self.out, module="transfer")

        self.use_side("in")
        first = self.make_files([("x.txt", b"x" * 10), ("y.txt", b"y" * 10)])
        dyode.send_batch(first, cast, self.work, module="transfer")
        self.use_side("out")
        # Deliver the manifest and only the first of the two files.
        for _ in range(2):
            fd, blob = tempfile.mkstemp(dir=self.stage)
            os.close(fd)
            with open(blob, "wb") as fh:
                fh.write(cast.queue.popleft())
            receiver.handle(blob)
        cast.queue.clear()

        # A new batch for a DIFFERENT file arrives. The incomplete batch is
        # kept open rather than written off, so that a late y.txt would
        # still be stored; the sweep is what finally reports it.
        self.use_side("in")
        second = self.make_files([("z.txt", b"z" * 10)])
        dyode.send_batch(second, cast, self.work, module="transfer")
        self.use_side("out")
        cast.deliver_all(receiver, self.stage)

        summaries = [e for e in read_events(self.logs)
                     if e["event"] == "batch_received"]
        self.assertEqual(len(summaries), 1)             # only z.txt's batch
        self.assertEqual(summaries[0]["files_missing"], 0)
        self.assertEqual([e["path"] for e in receiver.pending], ["y.txt"])

        receiver.sweep(batch_timeout=0.0001)
        summaries = [e for e in read_events(self.logs)
                     if e["event"] == "batch_received"]
        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[1]["files_missing"], 1)
        self.assertEqual(summaries[1]["files_stored"], 1)
        missing = [e["path"] for e in read_events(self.logs)
                   if e["event"] == "file_missing"]
        self.assertEqual(missing, ["y.txt"])


if __name__ == "__main__":
    unittest.main()
