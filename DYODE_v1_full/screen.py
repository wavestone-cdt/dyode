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
            common.udp_send(sock, addr, data)
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


def receive_frames(sock, store, stop=None):
    """Thread: read datagrams forever and publish complete JPEG frames.
    The socket stays bound the whole time, so no frame is lost between
    viewers (the original re-bound it for every frame)."""
    reasm = common.Reassembler()
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
        if payload is None:
            continue
        if payload.startswith(JPEG_MAGIC):
            store.put(payload)
        else:
            log.warning("received a frame that is not a JPEG; ignored")


def make_handler(store):
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


def start_screen_output(props, cfg, udp_bind=None):
    """Bind the UDP receiver and HTTP server. Returns (httpd, store, sock)."""
    store = FrameStore()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(udp_bind or (cfg["network"]["out_ip"], props["port"]))
    sock.settimeout(1.0)
    threading.Thread(target=receive_frames, args=(sock, store),
                     name="screen-rx", daemon=True).start()
    http_addr = (str(props.get("http_bind", "0.0.0.0")), int(props.get("http_port", 8080)))
    httpd = ThreadingHTTPServer(http_addr, make_handler(store))
    httpd.daemon_threads = True
    return httpd, store, sock


def run_screen_output(name, props, cfg):
    """Output agent: serve received frames over HTTP. Runs forever."""
    common.setup_logging(cfg.get("_log_level", "INFO"), cfg)
    httpd, _, _ = start_screen_output(props, cfg)
    log.info("module %r: screen available at http://%s:%d/",
             name, *httpd.server_address[:2])
    httpd.serve_forever()
