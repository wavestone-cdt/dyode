# -*- coding: utf-8 -*-
"""Screen sharing through the diode (Python 3 port).

Input side:  watch a folder where screenshots (JPEG) are written, e.g. by
             screenshot.ps1, and send the newest one over UDP.
Output side: receive frames and serve them over HTTP as an MJPEG stream
             (http://<output-ip>:8080/) and as a still (/screen.jpg).
"""

import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dyode
import dyode_common as common

log = logging.getLogger("dyode.screen")

JPEG_MAGIC = b"\xff\xd8"
BOUNDARY = "jpgboundary"
IMAGE_EXTS = (".jpg", ".jpeg")

PAGE = b"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>DYODE screen</title>
<style>body{margin:0;background:#111}img{display:block;max-width:100%;margin:auto}</style>
</head><body><img src="/screen.mjpg" alt="screen"></body></html>
"""


# --------------------------------------------------------------------------
# Input side
# --------------------------------------------------------------------------

def newest_image(folder):
    """Return (path, mtime, size) of the newest JPEG in folder, or None."""
    best = None
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return None
    for entry in entries:
        if not entry.name.lower().endswith(IMAGE_EXTS):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if best is None or st.st_mtime > best[1]:
            best = (entry.path, st.st_mtime, st.st_size)
    return best


def run_screen_input(name, props, cfg):
    """Input agent: send each new screenshot, at most max_fps per second."""
    common.setup_logging(cfg.get("_log_level", "INFO"), cfg)
    folder = props["in"]
    os.makedirs(folder, exist_ok=True)
    min_gap = 1.0 / float(props.get("max_fps", 10))
    addr = (cfg["network"]["out_ip"], props["port"])
    watcher = dyode.ChangeWatcher(folder, recursive=False)
    auth = common.require_auth(cfg)
    last_sent = None
    log.info("module %r: watching %s for screenshots, sending to %s:%s",
             name, folder, *addr)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        while True:
            watcher.wait(1.0)
            found = newest_image(folder)
            if found is None or found[1:] == last_sent:
                continue
            path, mtime, size = found
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError as err:
                log.debug("cannot read %s: %s", path, err)
                continue
            if not data.startswith(JPEG_MAGIC):
                log.debug("%s is not a complete JPEG yet; skipped", path)
                continue
            common.udp_send(sock, addr, data, auth=auth)
            last_sent = (mtime, size)
            time.sleep(min_gap)


# --------------------------------------------------------------------------
# Output side
# --------------------------------------------------------------------------

class FrameStore:
    """Latest frame, shared between the UDP receiver and HTTP viewers."""

    def __init__(self):
        self.cond = threading.Condition()
        self.frame = None
        self.frame_id = 0

    def put(self, frame):
        with self.cond:
            self.frame = frame
            self.frame_id += 1
            self.cond.notify_all()

    def wait_newer(self, seen_id, timeout):
        """Return (frame_id, frame) newer than seen_id, or (seen_id, None)."""
        with self.cond:
            self.cond.wait_for(lambda: self.frame_id != seen_id, timeout=timeout)
            if self.frame_id == seen_id:
                return seen_id, None
            return self.frame_id, self.frame


def receive_frames(sock, store, stop=None, auth=None):
    """Thread: read datagrams forever and publish complete JPEG frames.
    The socket stays bound the whole time, so no frame is lost between
    viewers (the original re-bound it for every frame)."""
    reasm = common.Reassembler(max_pieces=4096, max_bytes=64 * 1024 * 1024)
    while stop is None or not stop.is_set():
        try:
            data = sock.recv(common.UDP_MAX_DATAGRAM + 64)
        except socket.timeout:
            continue
        except OSError:
            if stop is not None and stop.is_set():
                return
            raise
        payload = reasm.feed(data)
        if payload is not None and auth is not None:
            payload = auth.unseal(payload, "screen")
        if payload is None:
            continue
        if payload.startswith(JPEG_MAGIC):
            store.put(payload)
        else:
            log.warning("received a frame that is not a JPEG; ignored")


def make_handler(store, max_streams=8):
    streams = threading.BoundedSemaphore(max_streams)

    class ScreenHandler(BaseHTTPRequestHandler):
        server_version = "DYODE"

        def log_message(self, fmt, *args):
            log.debug("http %s: " + fmt, self.client_address[0], *args)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PAGE)
            elif path == "/screen.jpg":
                _, frame = store.wait_newer(-1, 0)
                if frame is None:
                    self._send(503, "text/plain", b"no frame received yet\n")
                else:
                    self._send(200, "image/jpeg", frame)
            elif path.endswith(".mjpg"):
                self._stream()
            else:
                self._send(404, "text/plain", b"not found\n")

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _stream(self):
            # Each viewer holds a thread for as long as it watches: without a
            # cap, anyone who can reach the port could open them until the
            # box ran out of threads.
            if not streams.acquire(blocking=False):
                log.warning("viewer %s refused: %d streams already open (max_streams)",
                            self.client_address[0], max_streams)
                self._send(503, "text/plain", b"too many viewers\n")
                return
            try:
                self._stream_frames()
            finally:
                streams.release()

        def _stream_frames(self):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=%s" % BOUNDARY)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen = -1
            try:
                while True:
                    seen, frame = store.wait_newer(seen, timeout=5.0)
                    if frame is None:
                        continue
                    self.wfile.write(("--%s\r\nContent-Type: image/jpeg\r\n"
                                      "Content-Length: %d\r\n\r\n"
                                      % (BOUNDARY, len(frame))).encode("ascii"))
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                log.debug("viewer %s disconnected", self.client_address[0])

    return ScreenHandler


def interface_addresses():
    """{interface: IPv4 address} for every interface that has one (Linux)."""
    import fcntl
    import struct
    found = {}
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in socket.if_nameindex():
            try:
                packed = fcntl.ioctl(probe.fileno(), 0x8915,       # SIOCGIFADDR
                                     struct.pack("256s", name[:15].encode()))
            except OSError:
                continue
            found[name] = socket.inet_ntoa(packed[20:24])
    finally:
        probe.close()
    return found


def default_http_bind(cfg, addresses=None):
    """Where the viewer page listens when http_bind is not set: the first
    address that is neither loopback nor on the diode NIC, i.e. the IT-side
    network the viewers are on.  0.0.0.0 (with a warning) if none is found."""
    net = cfg.get("network", {})
    try:
        addresses = interface_addresses() if addresses is None else addresses
    except (OSError, ImportError, AttributeError):
        addresses = {}
    for name in sorted(addresses):
        ip = addresses[name]
        if name == net.get("out_interface") or ip == net.get("out_ip") \
                or ip.startswith("127."):
            continue
        return ip
    log.warning("screen: no non-diode address found; the viewer page listens on "
                "every address (set http_bind to choose one)")
    return "0.0.0.0"


def start_screen_output(props, cfg, udp_bind=None, auth=None):
    """Bind the UDP receiver and HTTP server. Returns (httpd, store, sock)."""
    store = FrameStore()
    sock = common.diode_socket(cfg, props["port"], rcvbuf=4 * 1024 * 1024, bind=udp_bind)
    sock.settimeout(1.0)
    threading.Thread(target=receive_frames, args=(sock, store, None, auth),
                     name="screen-rx", daemon=True).start()
    bind = props.get("http_bind") or default_http_bind(cfg)
    http_addr = (bind, int(props.get("http_port", 8080)))
    httpd = ThreadingHTTPServer(http_addr, make_handler(store, props.get("max_streams", 8)))
    httpd.daemon_threads = True
    return httpd, store, sock


def run_screen_output(name, props, cfg):
    """Output agent: serve received frames over HTTP. Runs forever."""
    common.setup_logging(cfg.get("_log_level", "INFO"), cfg)
    httpd, _, _ = start_screen_output(props, cfg, auth=common.require_auth(cfg))
    log.info("module %r: screen available at http://%s:%d/",
             name, *httpd.server_address[:2])
    httpd.serve_forever()
