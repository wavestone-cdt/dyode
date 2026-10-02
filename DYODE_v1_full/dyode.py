# -*- coding: utf-8 -*-
"""File transfer through the diode using udpcast (Python 3 port).

Protocol, per batch:
  1. the input side sends a JSON manifest listing each file's relative
     path, size and SHA-256, in sending order;
  2. it then sends each file, in that order, and deletes it once sent.
The output side receives the manifest, then each file, verifies size and
hash, and moves good files into place (keeping subfolders).

If a batch is interrupted, the output side notices the next manifest when
it arrives in place of an expected file, and resynchronizes on it instead
of discarding everything that follows.
"""

import collections
import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import dyode_common as common

log = logging.getLogger("dyode.folder")

MANIFEST_MAGIC = "dyode-manifest-v1"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
STAGING_DIR = ".dyode_incoming"


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def hash_file(path, blocksize=65536):
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(blocksize), b""):
            hasher.update(block)
    return hasher.hexdigest()


class ChangeWatcher:
    """Wake up when something changes under `root` (recursively).

    Uses inotify through the small inotify_simple package. If it is not
    installed, falls back to plain polling, which is slower to react but
    otherwise equivalent (the sender rescans the folder either way).
    """

    def __init__(self, root, recursive=True):
        self.root = root
        self.recursive = recursive
        self.inotify = None
        try:
            from inotify_simple import INotify, flags
        except ImportError:
            log.warning("inotify_simple not installed: polling %s instead", root)
            return
        self.flags = flags
        self.inotify = INotify()
        self.mask = (flags.CLOSE_WRITE | flags.MOVED_TO | flags.CREATE
                     | flags.DELETE_SELF | flags.MOVE_SELF)
        self._add_all()

    def _add_all(self):
        dirs = [self.root]
        if self.recursive:
            for dirpath, dirnames, _ in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".dyode")]
                dirs += [os.path.join(dirpath, d) for d in dirnames]
        for d in dirs:
            try:
                self.inotify.add_watch(d, self.mask)
            except OSError as err:
                log.debug("cannot watch %s: %s", d, err)

    def wait(self, timeout):
        """Block up to `timeout` seconds. Returns True if events arrived."""
        if self.inotify is None:
            time.sleep(timeout)
            return False
        events = self.inotify.read(timeout=int(timeout * 1000))
        if any(e.mask & self.flags.ISDIR for e in events):
            self._add_all()     # watch newly created subfolders
        return bool(events)


# --------------------------------------------------------------------------
# udpcast transport
# --------------------------------------------------------------------------

class UdpCast:
    """Thin wrapper around the udp-sender / udp-receiver programs.

    Arguments are passed as a list, never through a shell, so file names
    cannot inject commands (the original passed a shell string).
    """

    def __init__(self, props, cfg):
        net = cfg["network"]
        self.port = props["port"]
        self.bitrate = int(props.get("bitrate", 8))
        # Normalized by common._normalize_module: a ratio string, or None
        # when forward error correction is switched off for this module.
        self.fec = props.get("fec", common.DEFAULT_FEC)
        self.in_ip, self.out_ip = net["in_ip"], net["out_ip"]
        self.in_if, self.out_if = net["in_interface"], net["out_interface"]

    def sender_cmd(self, path):
        cmd = ["udp-sender", "--async"]
        if self.fec:
            cmd += ["--fec", self.fec]
        cmd += ["--max-bitrate", "%dm" % self.bitrate,
                "--mcast-rdv-addr", self.out_ip, "--mcast-data-addr", self.out_ip,
                "--portbase", str(self.port), "--autostart", "1",
                "--interface", self.in_if, "-f", path]
        return cmd

    def receiver_cmd(self, path):
        return ["udp-receiver", "--nosync", "--mcast-rdv-addr", self.in_ip,
                "--interface", self.out_if, "--portbase", str(self.port), "-f", path]

    @staticmethod
    def _run(cmd):
        log.debug("running: %s", cmd)
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            common.die("%s not found: install udpcast" % cmd[0])
        if res.returncode != 0:
            log.error("%s failed (exit %d): %s", cmd[0], res.returncode,
                      res.stderr.decode(errors="replace").strip()[-500:])
            return False
        return True

    def send(self, path):
        return self._run(self.sender_cmd(path))

    def receive(self, path):
        return self._run(self.receiver_cmd(path))


# --------------------------------------------------------------------------
# Input side
# --------------------------------------------------------------------------

def scan_ready_files(root, settle):
    """List regular files under root whose last change is at least `settle`
    seconds old (so half-written files are left for the next pass).

    Returns (ready, waiting): ready is a sorted list of (relpath, abspath);
    waiting counts files skipped because they are still changing.
    Symlinks are skipped on purpose: a link dropped in the input folder
    must not be able to exfiltrate an arbitrary file from the input box.
    """
    ready, waiting = [], 0
    now = time.time()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".dyode")
                             and not os.path.islink(os.path.join(dirpath, d)))
        for fname in sorted(filenames):
            path = os.path.join(dirpath, fname)
            try:
                st = os.lstat(path)
            except OSError:
                continue                      # vanished meanwhile
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            if now - st.st_mtime < settle:
                waiting += 1
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            ready.append((rel, path))
    return ready, waiting


def build_manifest(files):
    """files: list of (relpath, abspath). Hashes each file."""
    entries = []
    for rel, path in files:
        entries.append({"path": rel, "size": os.path.getsize(path),
                        "sha256": hash_file(path)})
    return {"magic": MANIFEST_MAGIC, "batch": uuid.uuid4().hex, "files": entries}


SENT_DIR = ".dyode_sent"


def retire_sent_file(root, rel, path, keep_sent_hours):
    """Move a file out of the watched folder once it has been transmitted.

    The original unlinked it as soon as udp-sender exited 0, but on a diode
    that exit code only means "transmitted" -- there is no acknowledgement,
    so a file lost in flight was gone for good.  Keeping it under
    .dyode_sent for a while makes a failed transfer recoverable: copy it
    back into the watched folder and it is sent again.  scan_ready_files
    skips folders starting with '.dyode', so it is not picked up again.
    """
    if not keep_sent_hours or root is None:
        try:
            os.remove(path)
        except OSError as err:
            log.error("sent %s but could not delete it: %s", rel, err)
        return
    dest = os.path.join(root, SENT_DIR, rel)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(path, dest)
    except OSError as err:
        log.error("sent %s but could not retire it to %s: %s",
                  rel, SENT_DIR, err)
        try:
            os.remove(path)
        except OSError:
            pass


def prune_sent(root, keep_sent_hours):
    """Delete retired files older than keep_sent_hours."""
    if not keep_sent_hours:
        return
    base = os.path.join(root, SENT_DIR)
    if not os.path.isdir(base):
        return
    cutoff = time.time() - keep_sent_hours * 3600
    for dirpath, _dirnames, filenames in os.walk(base, topdown=False):
        for fname in filenames:
            path = os.path.join(dirpath, fname)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
        if dirpath != base:
            try:
                os.rmdir(dirpath)             # only succeeds when empty
            except OSError:
                pass


def send_batch(files, transport, workdir, module=None, root=None,
               file_gap=0.0, keep_sent_hours=24):
    """Send one batch. Returns the number of files sent."""
    manifest = build_manifest(files)
    batch = manifest["batch"]
    manifest_path = os.path.join(workdir, "manifest_%s.json" % batch)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    started = time.monotonic()
    try:
        log.info("sending manifest for %d file(s), batch %s",
                 len(files), batch[:8])
        if not transport.send(manifest_path):
            common.log_event("batch_failed", module=module, batch=batch[:8],
                             files=len(files), reason="manifest_send_failed")
            return 0
    finally:
        os.remove(manifest_path)

    sent = 0
    sent_bytes = 0
    for (rel, path), entry in zip(files, manifest["files"]):
        log.info("sending %s (%d bytes)", rel, entry["size"])
        if not transport.send(path):
            log.error("aborting batch; %d file(s) left for the next batch",
                      len(files) - sent)
            break
        retire_sent_file(root, rel, path, keep_sent_hours)
        sent += 1
        sent_bytes += entry["size"]
        common.log_event("file_sent", module=module, per_file=True,
                         batch=batch[:8], path=rel, bytes=entry["size"])
        # The receiver needs a moment to restart udp-receiver between
        # transfers, and a diode gives it no way to ask for one.
        if file_gap:
            time.sleep(file_gap)

    elapsed = time.monotonic() - started
    common.log_event("batch_sent", module=module, batch=batch[:8],
                     files=sent, files_failed=len(files) - sent,
                     bytes=sent_bytes, duration_s=round(elapsed, 3),
                     mbps=round(sent_bytes * 8 / elapsed / 1e6, 1)
                     if elapsed > 0 else None)
    return sent


def run_folder_input(name, props, cfg):
    """Input agent: watch props['in'] and send files as they settle."""
    common.setup_logging(cfg.get("_log_level", "INFO"), cfg)
    root = props["in"]
    os.makedirs(root, exist_ok=True)
    settle = float(props.get("settle", 2.0))
    rescan = float(props.get("rescan", 30.0))
    file_gap = float(props.get("file_gap", 0.5))
    keep_sent_hours = props.get("keep_sent_hours", 24)
    transport = UdpCast(props, cfg)
    watcher = ChangeWatcher(root)
    workdir = tempfile.mkdtemp(prefix="dyode_%s_" % props["port"])
    log.info("module %r: watching %s (port %d, %d Mbit/s, FEC %s, gap %.2fs, "
             "sent files kept %s)", name, root, props["port"],
             transport.bitrate, transport.fec or "off", file_gap,
             ("%dh" % keep_sent_hours) if keep_sent_hours else "not at all")
    try:
        while True:
            # Existing files are picked up at start-up too (the original
            # only noticed them once a new file arrived).
            ready, waiting = scan_ready_files(root, settle)
            if ready:
                send_batch(ready, transport, workdir, module=name, root=root,
                           file_gap=file_gap, keep_sent_hours=keep_sent_hours)
                continue
            prune_sent(root, keep_sent_hours)
            watcher.wait(settle if waiting else rescan)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Output side
# --------------------------------------------------------------------------

def safe_join(root, rel):
    """Join a manifest path under root, or return None if it tries to escape
    (absolute path, '..', empty or odd components)."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or rel.startswith("/"):
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".dyode") for p in parts):
        return None
    path = os.path.join(root, *parts)
    root_abs = os.path.abspath(root)
    if os.path.commonpath([root_abs, os.path.abspath(path)]) != root_abs:
        return None
    return path


def read_manifest(path):
    """Return the manifest dict if the file at path is a valid manifest,
    otherwise None."""
    try:
        if os.path.getsize(path) > MAX_MANIFEST_BYTES:
            return None
        with open(path, "rb") as fh:
            if fh.read(1) != b"{":
                return None
            fh.seek(0)
            data = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("magic") != MANIFEST_MAGIC:
        return None
    files = data.get("files")
    if not isinstance(files, list):
        return None
    for e in files:
        if not (isinstance(e, dict) and isinstance(e.get("path"), str)
                and isinstance(e.get("size"), int)
                and isinstance(e.get("sha256"), str) and len(e["sha256"]) == 64):
            return None
    return data


class _Batch:
    """One manifest's worth of files still waiting to arrive."""

    def __init__(self, manifest):
        self.id = manifest.get("batch") or "?"
        self.started = time.monotonic()
        self.last_activity = self.started
        self.expected = len(manifest["files"])
        self.stored = 0
        self.rejected = 0
        self.bytes = 0
        # sha256 -> entries still outstanding.  A list, because two files
        # in one batch may legitimately have identical content.
        self.by_hash = {}
        self.sizes = set()
        self.paths = set()
        for entry in manifest["files"]:
            self.by_hash.setdefault(entry["sha256"], []).append(entry)
            self.sizes.add(entry["size"])
            self.paths.add(entry["path"])

    def drop_path(self, path):
        """Forget an outstanding entry because a newer batch supersedes it."""
        if path not in self.paths:
            return False
        dropped = False
        for digest, entries in list(self.by_hash.items()):
            keep = [e for e in entries if e["path"] != path]
            if len(keep) != len(entries):
                dropped = True
                if keep:
                    self.by_hash[digest] = keep
                else:
                    del self.by_hash[digest]
        self.paths.discard(path)
        return dropped

    @property
    def outstanding(self):
        return sum(len(v) for v in self.by_hash.values())

    def take(self, digest):
        """Claim an entry matching this content, or None."""
        entries = self.by_hash.get(digest)
        if not entries:
            return None
        entry = entries.pop(0)
        if not entries:
            del self.by_hash[digest]
        return entry


class FolderReceiver:
    """Output-side state machine. Feed it each received file with handle().

    Files are matched to manifest entries BY CONTENT, not by arrival order.
    The earlier version popped the next manifest entry for each blob, which
    meant one lost transfer shifted every later file by one and destroyed
    the rest of the batch.  Here the blob's own SHA-256 says which file it
    is, so a lost transfer costs exactly that file.  The hash is computed
    either way, so this is free.

    Several recent batches are kept open at once, so a lost manifest or two
    overlapping batches cannot cascade either.
    """

    RETAIN_BATCHES = 4

    def __init__(self, out_dir, module=None):
        self.out_dir = out_dir
        self.module = module
        self.batches = collections.OrderedDict()

    # -- batch bookkeeping ------------------------------------------------

    def _summarize(self, batch, reason):
        # Name every file that never arrived.  Because a damaged transfer
        # can no longer be attributed to a particular file, this is where
        # you learn WHICH files are missing -- more useful than the old
        # per-blob mismatch line, which was often blaming the wrong name.
        for entries in batch.by_hash.values():
            for entry in entries:
                log.warning("file %s never arrived (batch %s, %s)",
                            entry["path"], batch.id[:8], reason)
                common.log_event("file_missing", module=self.module,
                                 per_file=True, batch=batch.id[:8],
                                 path=entry["path"], bytes=entry["size"],
                                 reason=reason)
        common.log_event("batch_received", module=self.module,
                         batch=batch.id[:8], files=batch.expected,
                         files_stored=batch.stored,
                         files_rejected=batch.rejected,
                         files_missing=batch.outstanding,
                         bytes=batch.bytes, reason=reason,
                         duration_s=round(time.monotonic() - batch.started, 3))
        if batch.outstanding:
            log.warning("batch %s closed with %d file(s) never received (%s)",
                        batch.id[:8], batch.outstanding, reason)

    def sweep(self, batch_timeout):
        """Close batches that have gone quiet.

        Without this an incomplete batch would sit open until four more
        manifests pushed it out, so on a quiet link its summary -- and the
        list of files that never arrived -- could be days late.
        """
        if not batch_timeout:
            return
        now = time.monotonic()
        for batch in list(self.batches.values()):
            if now - batch.last_activity >= batch_timeout:
                self._close(batch, "timeout")

    def _close(self, batch, reason):
        self.batches.pop(batch.id, None)
        self._summarize(batch, reason)

    def _add_batch(self, manifest):
        batch = _Batch(manifest)
        # A re-sent file supersedes the outstanding entry in any older open
        # batch, so one file is not reported missing twice.
        for older in list(self.batches.values()):
            for path in batch.paths:
                older.drop_path(path)
            if not older.outstanding:
                self._close(older, "superseded")
        self.batches[batch.id] = batch
        while len(self.batches) > self.RETAIN_BATCHES:
            _, oldest = self.batches.popitem(last=False)
            self._summarize(oldest, "evicted")
        return batch

    def close_all(self, reason="shutdown"):
        for batch in list(self.batches.values()):
            self._close(batch, reason)

    @property
    def pending(self):
        """Entries still outstanding across every open batch."""
        return [e for b in self.batches.values()
                for v in b.by_hash.values() for e in v]

    # -- the state machine ------------------------------------------------

    def handle(self, blob):
        """Process one received file. Always consumes (moves or deletes) it.
        Returns one of: 'manifest', 'stored', 'rejected', 'orphan'."""
        manifest = read_manifest(blob)
        if manifest is not None:
            batch = self._add_batch(manifest)
            log.info("manifest received: %d file(s), batch %s",
                     batch.expected, batch.id[:8])
            common.log_event("manifest_received", module=self.module,
                             batch=batch.id[:8], files=batch.expected)
            os.remove(blob)
            return "manifest"

        size = os.path.getsize(blob)
        if not self.batches:
            log.warning("received a file with no manifest pending; discarded")
            common.log_event("file_orphaned", module=self.module, bytes=size)
            os.remove(blob)
            return "orphan"

        # Skip hashing a blob whose length matches nothing we are waiting
        # for: that is the common case for a transfer damaged in flight.
        if any(size in b.sizes for b in self.batches.values()):
            digest = hash_file(blob)
        else:
            digest = None

        batch = entry = None
        if digest is not None:
            for candidate in self.batches.values():
                entry = candidate.take(digest)
                if entry is not None:
                    batch = candidate
                    break

        if entry is None:
            log.error("received %d bytes matching no expected file; discarded",
                      size)
            common.log_event("file_rejected", module=self.module, per_file=True,
                             reason="no_matching_file", bytes=size)
            for candidate in self.batches.values():
                candidate.rejected += 1
                break
            os.remove(blob)
            return "rejected"

        dest = safe_join(self.out_dir, entry["path"])
        if dest is None:
            log.error("unsafe path %r in manifest; file discarded", entry["path"])
            batch.rejected += 1
            common.log_event("file_rejected", module=self.module, per_file=True,
                             batch=batch.id[:8], path=entry["path"],
                             reason="unsafe_path")
            os.remove(blob)
            self._finish_if_done(batch)
            return "rejected"

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(blob, dest)            # atomic: same filesystem
        log.info("file %s available at %s", entry["path"], dest)
        batch.stored += 1
        batch.bytes += size
        batch.last_activity = time.monotonic()
        common.log_event("file_stored", module=self.module, per_file=True,
                         batch=batch.id[:8], path=entry["path"], bytes=size)
        self._finish_if_done(batch)
        return "stored"

    def _finish_if_done(self, batch):
        if not batch.outstanding:
            self._close(batch, "complete")


def staging_dir(props):
    """Where incoming transfers are written before they are verified.

    This must NOT be inside the output folder.  udp-receiver writes here for
    the whole duration of a transfer, so anything serving the output folder
    -- NFS, SFTP, rsync, an indexer -- would otherwise hand out growing,
    unverified partial files.  The default is a sibling directory, which
    keeps it on the same filesystem so the final os.replace stays atomic.
    """
    configured = props.get("staging")
    if configured:
        return configured
    out_dir = props["out"].rstrip(os.sep)
    return out_dir + ".incoming"


def run_folder_output(name, props, cfg):
    """Output agent: receive files forever into props['out'].

    Verification runs in a worker thread so that udp-receiver is restarted
    immediately after each transfer.  The earlier version hashed and moved
    the file before re-opening the socket, and a diode has no flow control:
    at line rate the sender had already pushed hundreds of megabytes of the
    next file into a closed socket by the time we were listening again.
    """
    common.setup_logging(cfg.get("_log_level", "INFO"), cfg)
    out_dir = props["out"]
    staging = staging_dir(props)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(staging, exist_ok=True)
    if os.path.commonpath([os.path.abspath(staging), os.path.abspath(out_dir)]) \
            == os.path.abspath(out_dir):
        log.warning("staging folder %s is inside the output folder: anything "
                    "serving %s will see partial files", staging, out_dir)
    # Clear leftovers from a previous crash.
    for leftover in os.listdir(staging):
        try:
            os.remove(os.path.join(staging, leftover))
        except OSError:
            pass

    transport = UdpCast(props, cfg)
    receiver = FolderReceiver(out_dir, module=name)
    log.info("module %r: receiving into %s via %s (port %d)",
             name, out_dir, staging, props["port"])

    # Bounded, so a slow disk applies backpressure instead of filling it.
    work = queue.Queue(maxsize=4)

    batch_timeout = float(props.get("batch_timeout", 300.0))

    def verify_loop():
        while True:
            try:
                blob = work.get(timeout=min(30.0, batch_timeout or 30.0))
            except queue.Empty:
                receiver.sweep(batch_timeout)   # report what never arrived
                continue
            try:
                receiver.handle(blob)
            except OSError as err:
                log.error("could not store received file: %s", err)
                if os.path.exists(blob):
                    try:
                        os.remove(blob)
                    except OSError:
                        pass
            finally:
                work.task_done()

    threading.Thread(target=verify_loop, name="verify", daemon=True).start()

    while True:
        fd, blob = tempfile.mkstemp(dir=staging)
        os.close(fd)
        if not transport.receive(blob):
            os.remove(blob)
            time.sleep(1.0)
            continue
        work.put(blob)
