#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weekly archiving and inspection for the DYODE transfer logs.

Rotation itself is logrotate's job (packaging/logrotate/dyode-transfer).
This tool runs once a week, from a systemd timer, and packs the daily files
logrotate left behind into one tarball:

    /var/log/dyode-transfer/dyode.log-20260921        rotated by logrotate
    /var/log/dyode-transfer/transfer.jsonl-20260921
      ->  /var/log/dyode-transfer/archive/dyode-logs-2026-W38.tar.gz

It only ever touches ROTATED files -- the live 'dyode.log' and
'transfer.jsonl' that the daemons hold open are never read, moved or
deleted, so it does not matter whether this runs before or after logrotate.

Packing a week together rather than letting logrotate gzip each day
compresses considerably better: consecutive days of the same log are highly
redundant and gzip sees that redundancy inside one tar stream.

  dyode_logs.py --archive              pack last week, prune old archives
  dyode_logs.py --archive --dry-run    say what it would do
  dyode_logs.py --stats                summarize transfer.jsonl
"""

import argparse
import collections
import datetime
import json
import os
import re
import sys
import tarfile

DEFAULT_DIR = "/var/log/dyode-transfer"
ARCHIVE_SUBDIR = "archive"
DEFAULT_KEEP_WEEKS = 26

# logrotate 'dateext' with 'dateformat -%Y%m%d' produces 'dyode.log-20260921'.
# The plain numeric suffixes logrotate uses without dateext ('dyode.log.1')
# are matched too, so a hand-edited logrotate config still archives.
ROTATED_RE = re.compile(r"^(?P<base>.+?)[.-](?P<stamp>\d{8}|\d+)(?P<gz>\.gz)?$")
ARCHIVE_RE = re.compile(r"^dyode-logs-(?P<year>\d{4})-W(?P<week>\d{2})\.tar\.gz$")


def week_tag(when):
    """ISO year and week, e.g. '2026-W38'. ISO weeks start on Monday."""
    iso = when.isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


def rotated_files(directory, live_names):
    """Rotated log files in `directory`, oldest suffix first.

    `live_names` are the files the daemons still have open; they are skipped
    however they sort, which is what makes this safe to run at any time.
    """
    found = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or name in live_names:
            continue
        match = ROTATED_RE.match(name)
        if match and match.group("base") in live_names:
            found.append(path)
    return found


def archive(directory=DEFAULT_DIR, live_names=("dyode.log", "transfer.jsonl"),
            keep_weeks=DEFAULT_KEEP_WEEKS, when=None, dry_run=False,
            out=sys.stdout):
    """Pack rotated logs into one tarball and prune old ones.

    Returns (tarball path or None, list of files packed, list of archives
    pruned).
    """
    live_names = set(live_names)
    when = when or datetime.date.today()
    files = rotated_files(directory, live_names)
    if not files:
        print("nothing to archive in %s" % directory, file=out)
        return None, [], prune(directory, keep_weeks, when, dry_run, out)

    archive_dir = os.path.join(directory, ARCHIVE_SUBDIR)
    tarball = os.path.join(archive_dir,
                           "dyode-logs-%s.tar.gz" % week_tag(when))
    if dry_run:
        print("would create %s with %d file(s):" % (tarball, len(files)),
              file=out)
        for path in files:
            print("  %s" % os.path.basename(path), file=out)
        return tarball, files, prune(directory, keep_weeks, when, True, out)

    os.makedirs(archive_dir, mode=0o750, exist_ok=True)
    # Write beside the target, then rename: a crash or a full disk leaves a
    # .partial behind rather than a truncated archive that looks complete.
    # gzip streams cannot be appended to, so a second run in the same ISO
    # week rewrites the tarball with the old members plus the new files
    # rather than overwriting a week of logs.
    partial = tarball + ".partial"
    try:
        with tarfile.open(partial, "w:gz") as tar:
            carried = _copy_members(tarball, tar) if os.path.exists(tarball) else 0
            for path in files:
                tar.add(path, arcname=os.path.basename(path))
    except OSError:
        if os.path.exists(partial):
            os.remove(partial)
        raise
    os.replace(partial, tarball)

    for path in files:
        os.remove(path)
    print("archived %d file(s) into %s%s"
          % (len(files), tarball,
             " (kept %d already there)" % carried if carried else ""),
          file=out)
    return tarball, files, prune(directory, keep_weeks, when, False, out)


def _copy_members(source, tar):
    """Copy every member of the `source` tarball into open tarfile `tar`."""
    copied = 0
    with tarfile.open(source, "r:gz") as old:
        for member in old.getmembers():
            if not member.isfile():
                continue
            extracted = old.extractfile(member)
            if extracted is None:
                continue
            tar.addfile(member, extracted)
            copied += 1
    return copied


def prune(directory, keep_weeks, when=None, dry_run=False, out=sys.stdout):
    """Delete archives older than `keep_weeks`. Returns the paths removed."""
    archive_dir = os.path.join(directory, ARCHIVE_SUBDIR)
    if keep_weeks <= 0 or not os.path.isdir(archive_dir):
        return []
    when = when or datetime.date.today()
    cutoff = when - datetime.timedelta(weeks=keep_weeks)
    removed = []
    for name in sorted(os.listdir(archive_dir)):
        match = ARCHIVE_RE.match(name)
        if not match:
            continue
        year, week = int(match.group("year")), int(match.group("week"))
        try:
            monday = datetime.date.fromisocalendar(year, week, 1)
        except ValueError:
            continue
        if monday >= cutoff:
            continue
        path = os.path.join(archive_dir, name)
        removed.append(path)
        if dry_run:
            print("would remove %s" % path, file=out)
        else:
            os.remove(path)
            print("removed %s" % path, file=out)
    return removed


# --------------------------------------------------------------------------
# Reading the JSON stream back
# --------------------------------------------------------------------------

def read_events(path):
    """Yield the events in a .jsonl file, skipping anything unparsable.

    A partial last line is normal if the file is read while a transfer is
    running, so a bad line is skipped rather than raising.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


def summarize(events):
    """Totals per event type, plus files and bytes moved."""
    totals = collections.Counter()
    files_sent = files_stored = files_rejected = 0
    bytes_sent = bytes_stored = 0
    for event in events:
        name = event.get("event", "?")
        totals[name] += 1
        if name == "batch_sent":
            files_sent += event.get("files", 0)
            bytes_sent += event.get("bytes", 0)
        elif name == "batch_received":
            files_stored += event.get("files_stored", 0)
            files_rejected += event.get("files_rejected", 0)
            bytes_stored += event.get("bytes", 0)
    return {"events": dict(totals), "files_sent": files_sent,
            "files_stored": files_stored, "files_rejected": files_rejected,
            "bytes_sent": bytes_sent, "bytes_stored": bytes_stored}


def human_bytes(count):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if count < 1024 or unit == "TiB":
            return "%.1f %s" % (count, unit) if unit != "B" else "%d B" % count
        count /= 1024.0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Archive and inspect the DYODE transfer logs.")
    parser.add_argument("--dir", default=DEFAULT_DIR,
                        help="log directory (default: %s)" % DEFAULT_DIR)
    parser.add_argument("--archive", action="store_true",
                        help="pack rotated logs into this week's tarball")
    parser.add_argument("--stats", action="store_true",
                        help="summarize the JSON event log")
    parser.add_argument("--keep-weeks", type=int, default=DEFAULT_KEEP_WEEKS,
                        help="archives to keep (default: %d, 0 = forever)"
                             % DEFAULT_KEEP_WEEKS)
    parser.add_argument("--json", dest="json_name", default="transfer.jsonl",
                        help="event log file name, for --stats")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would happen, change nothing")
    args = parser.parse_args(argv)

    if not args.archive and not args.stats:
        parser.error("choose --archive or --stats")

    if not os.path.isdir(args.dir):
        print("no such directory: %s" % args.dir, file=sys.stderr)
        return 1

    if args.archive:
        archive(args.dir, keep_weeks=args.keep_weeks, dry_run=args.dry_run)

    if args.stats:
        path = os.path.join(args.dir, args.json_name)
        if not os.path.isfile(path):
            print("no event log at %s" % path, file=sys.stderr)
            return 1
        stats = summarize(read_events(path))
        print("%s" % path)
        for name, count in sorted(stats["events"].items()):
            print("  %-20s %d" % (name, count))
        print("  files sent           %d (%s)"
              % (stats["files_sent"], human_bytes(stats["bytes_sent"])))
        print("  files stored         %d (%s)"
              % (stats["files_stored"], human_bytes(stats["bytes_stored"])))
        print("  files rejected       %d" % stats["files_rejected"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
