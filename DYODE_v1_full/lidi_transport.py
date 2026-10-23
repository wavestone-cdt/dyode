# -*- coding: utf-8 -*-
"""lidi (ANSSI-FR/lidi) as the folder transport: an alternative to udpcast.

lidi moves files through a one-way UDP link with RaptorQ forward error
correction.  Unlike udp-sender / udp-receiver, which DYODE started once per
file, lidi is a pair of long-running daemons on each box:

    input box:   lidi-file-send --> [unix socket] --> lidi-send   ==UDP==>
    output box:  ==UDP==> lidi-receive --> [unix socket] --> lidi-file-receive

lidi-send and lidi-receive are the diode itself.  lidi-file-send sends one
file per call and exits; lidi-file-receive stays up and writes each file it
completes into the staging folder.  Because the receiver is always listening,
there is no start-up hello, no start timeout and no deaf window between
files.

The protocol above this layer is unchanged.  lidi does not authenticate
anything, and nothing in it says whether a file arrived intact, so DYODE's
signed manifest and per-file SHA-256 remain the only integrity check.

Everything is passed as an argument list, never through a shell.  Local
endpoints are unix sockets in a private directory, so no extra TCP port is
opened on either box.

Options are the lidi 3.0.0 command-line parameters.  The sender and receiver
values of --ports, --mtu, --block and --repair must match on both boxes.
"""

import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid

import dyode_common as common

log = logging.getLogger("dyode.lidi")

LIDI_SEND = "lidi-send"
LIDI_RECEIVE = "lidi-receive"
LIDI_FILE_SEND = "lidi-file-send"
LIDI_FILE_RECEIVE = "lidi-file-receive"

# Assumed slowest rate for a whole file, to bound how long a send may take
# before it is treated as stuck.  Deliberately low: a 10 GB file at 100 Mbit/s
# already takes over ten minutes.
MIN_RATE_BYTES_PER_S = 12_500_000       # 100 Mbit/s
SEND_TIMEOUT_BASE_S = 300.0

SOCKET_WAIT_S = 10.0
PARTIAL_DIR = ".partial"


def lidi_ports(props):
    """The UDP ports lidi uses for this module, starting at its 'port'."""
    return [props["port"] + i for i in range(props["lidi_ports"])]


def shared_args(props):
    """Options that must be identical on the sending and receiving box."""
    return ["--ports", ",".join(str(p) for p in lidi_ports(props)),
            "--mtu", str(props["lidi_mtu"]),
            "--block", str(props["lidi_block"]),
            "--repair", str(props["lidi_repair"])]


def _run(cmd, timeout):
    """Run a lidi client to completion. Returns (ok, stderr_text)."""
    log.debug("running: %s", cmd)
    try:
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=timeout)
    except FileNotFoundError:
        common.die("%s not found: install lidi (transport: lidi)" % cmd[0])
    except subprocess.TimeoutExpired:
        log.error("%s did not finish within %.0fs and was abandoned",
                  cmd[0], timeout)
        return False, "timeout"
    text = res.stderr.decode(errors="replace").strip()
    if res.returncode != 0:
        log.error("%s failed (exit %d): %s", cmd[0], res.returncode, text[-500:])
        return False, text
    return True, text


def _send_timeout(size):
    return SEND_TIMEOUT_BASE_S + size / MIN_RATE_BYTES_PER_S


class _Daemon:
    """One long-running lidi process. The owner decides when to restart it."""

    def __init__(self, argv):
        self.argv = argv
        self.proc = None

    def start(self):
        log.debug("starting: %s", self.argv)
        try:
            # stdout/stderr are inherited, so lidi's own log lines reach the
            # journal, and a full pipe can never block it.
            self.proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            common.die("%s not found: install lidi (transport: lidi)" % self.argv[0])

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def _wait_for_socket(path, timeout=SOCKET_WAIT_S):
    end = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > end:
            return False
        time.sleep(0.05)
    return True


def _remove_stale_socket(path):
    # A socket left by a daemon that died would make the next bind fail.
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _private_dir(prefix):
    # mkdtemp creates the directory 0700: only this user can reach the sockets.
    return tempfile.mkdtemp(prefix=prefix)


# --------------------------------------------------------------------------
# Input side
# --------------------------------------------------------------------------

class LidiSender:
    """Sends files through lidi-send.  Same interface as UdpCast:
    send(path), send_fd(fd) and close()."""

    def __init__(self, props, cfg):
        net = cfg["network"]
        self.props = props
        self.in_ip, self.out_ip = net["in_ip"], net["out_ip"]
        self.sock_dir = _private_dir("dyode_lidi_tx_")
        self.sock = os.path.join(self.sock_dir, "send.sock")
        self.link_dir = os.path.join(self.sock_dir, "links")
        os.makedirs(self.link_dir)
        self.daemon = _Daemon(self.daemon_argv())

    def daemon_argv(self):
        # --to-bind pins the source address, and with it the NIC facing the
        # diode, so packets cannot leave by the box's other interface.
        return ([LIDI_SEND, "--from", "unix:" + self.sock,
                 "--to", self.out_ip, "--to-bind", "%s:0" % self.in_ip]
                + shared_args(self.props) + ["--max-clients", "2"])

    def start(self):
        _remove_stale_socket(self.sock)
        self.daemon.start()
        if not _wait_for_socket(self.sock):
            raise RuntimeError("lidi-send did not create %s" % self.sock)

    def ensure_running(self):
        if not self.daemon.running():
            log.error("lidi-send is not running; restarting it")
            self.daemon.stop()
            self.start()

    def close(self):
        self.daemon.stop()
        shutil.rmtree(self.sock_dir, ignore_errors=True)

    def _file_send(self, paths, size):
        self.ensure_running()
        cmd = [LIDI_FILE_SEND, "--to-unix", self.sock] + list(paths)
        ok, _text = _run(cmd, _send_timeout(size))
        return ok

    def send(self, path):
        return self._file_send([path], os.path.getsize(path))

    def send_fd(self, fd):
        """Send the file already open as `fd`.

        lidi names each file on the wire by its basename, and two files in one
        batch can share a name, so the file is offered to lidi-file-send under
        a unique name: a symlink in this process's private directory that
        points at the open descriptor (/proc/<pid>/fd/<fd>).  The descriptor
        stays open for the whole send, so the link always reaches the file
        that was checked, even if its path has since changed.  The receiver
        matches files by SHA-256, so the name does not matter to it.

        A hard link would avoid /proc, but linkat() through /proc/self/fd
        fails with EXDEV on this kernel, so it is not used.
        """
        size = os.fstat(fd).st_size
        link = os.path.join(self.link_dir, uuid.uuid4().hex)
        try:
            os.symlink("/proc/%d/fd/%d" % (os.getpid(), fd), link)
        except OSError as err:
            log.error("cannot offer a file to lidi (%s); it stays for the next pass", err)
            return False
        try:
            return self._file_send([link], size)
        finally:
            try:
                os.remove(link)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Output side
# --------------------------------------------------------------------------

class LidiReceiver:
    """Keeps lidi-file-receive and lidi-receive running.

    Files appear in out_dir (the staging folder) only once complete: lidi
    writes them into partial_dir and moves them across, so anything in the
    staging folder is safe to verify.
    """

    def __init__(self, props, cfg, out_dir, partial_dir):
        net = cfg["network"]
        self.props = props
        self.in_ip, self.out_ip = net["in_ip"], net["out_ip"]
        self.out_dir = out_dir
        self.partial_dir = partial_dir
        self.sock_dir = _private_dir("dyode_lidi_rx_")
        self.sock = os.path.join(self.sock_dir, "recv.sock")
        self.file_receive = _Daemon(
            [LIDI_FILE_RECEIVE, "--from-unix", self.sock,
             "--tmp-dir", partial_dir, out_dir])
        self.receive = _Daemon(
            [LIDI_RECEIVE, "--from", self.out_ip, "--to", "unix:" + self.sock]
            + shared_args(props) + ["--max-clients", "2"])

    def _start_all(self):
        # The file client listens; lidi-receive connects to it, so it goes first.
        _remove_stale_socket(self.sock)
        self.file_receive.start()
        if not _wait_for_socket(self.sock):
            raise RuntimeError("lidi-file-receive did not create %s" % self.sock)
        self.receive.start()

    def start(self):
        os.makedirs(self.partial_dir, exist_ok=True)
        prune_partial(self.partial_dir, older_than=0)
        self._start_all()

    def ensure_running(self):
        if self.file_receive.running() and self.receive.running():
            return
        log.error("lidi receiver stopped (file receive %s, lidi-receive %s); "
                  "restarting both", "up" if self.file_receive.running() else "down",
                  "up" if self.receive.running() else "down")
        common.log_event("lidi_restarted")
        self.receive.stop()
        self.file_receive.stop()
        self._start_all()

    def close(self):
        self.receive.stop()
        self.file_receive.stop()
        shutil.rmtree(self.sock_dir, ignore_errors=True)


def newest_mtime(path):
    """Latest modification time of any file under path, or None."""
    newest = None
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                mtime = entry.stat().st_mtime
                newest = mtime if newest is None else max(newest, mtime)
    except OSError:
        return None
    return newest


def prune_partial(partial_dir, older_than):
    """Delete partial files not written to for older_than seconds.

    A file lidi is still receiving keeps changing, so only one that stopped
    (its sender died mid-file) is removed.  older_than=0 clears everything;
    it is used at start-up, when nothing can be in progress.
    """
    if not os.path.isdir(partial_dir):
        return
    cutoff = time.time() - older_than
    for entry in os.scandir(partial_dir):
        try:
            if older_than == 0 or entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
        except OSError:
            pass
