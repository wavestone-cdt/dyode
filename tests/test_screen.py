import os
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request

import _setup  # noqa: F401
import dyode_common as common
import screen

FAKE_JPEG = b"\xff\xd8\xff\xe0" + os.urandom(30_000) + b"\xff\xd9"


class ScreenTests(unittest.TestCase):
    def setUp(self):
        props = {"port": 0, "http_port": 0, "http_bind": "127.0.0.1"}
        self.httpd, self.store, self.sock = screen.start_screen_output(
            props, {}, udp_bind=("127.0.0.1", 0))
        self.udp_addr = self.sock.getsockname()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def send(self, payload):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            common.udp_send(s, self.udp_addr, payload)

    def wait_frame(self, n):
        for _ in range(100):
            if self.store.frame_id >= n:
                return
            time.sleep(0.02)
        self.fail("frame %d never arrived" % n)

    def test_index_and_still_image(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"/screen.mjpg", r.read())
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/screen.jpg")
        self.assertEqual(cm.exception.code, 503)
        self.send(FAKE_JPEG)
        self.wait_frame(1)
        with urllib.request.urlopen(self.base + "/screen.jpg") as r:
            self.assertEqual(r.read(), FAKE_JPEG)

    def test_non_jpeg_payload_ignored(self):
        with self.assertLogs("dyode.screen", "WARNING"):
            self.send(b"<script>not an image</script>")
            time.sleep(0.2)
        self.assertEqual(self.store.frame_id, 0)

    def test_two_viewers_stream_at_once(self):
        """The original single-threaded server let only one viewer in."""
        def read_part(results):
            with urllib.request.urlopen(self.base + "/screen.mjpg", timeout=5) as r:
                self.assertIn("boundary=jpgboundary", r.headers["Content-Type"])
                head = r.readline() + r.readline() + r.readline() + r.readline()
                length = int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0])
                results.append((head, r.read(length)))

        results = []
        viewers = [threading.Thread(target=read_part, args=(results,)) for _ in range(2)]
        for v in viewers:
            v.start()
        time.sleep(0.3)
        self.send(FAKE_JPEG)
        for v in viewers:
            v.join(timeout=5)
        self.assertEqual(len(results), 2)
        for head, body in results:
            self.assertTrue(head.startswith(b"--jpgboundary\r\n"))
            self.assertEqual(body, FAKE_JPEG)

    def test_newest_image_ignores_non_jpeg_and_symlinks(self):
        import tempfile
        d = tempfile.mkdtemp()
        for name, age in (("a.jpg", 5), ("b.JPG", 1), ("c.png", 0)):
            p = os.path.join(d, name)
            open(p, "wb").close()
            os.utime(p, (time.time() - age,) * 2)
        os.symlink(os.path.join(d, "a.jpg"), os.path.join(d, "z.jpg"))
        self.assertEqual(os.path.basename(screen.newest_image(d)[0]), "b.JPG")


if __name__ == "__main__":
    unittest.main()
