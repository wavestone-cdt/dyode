import os
import random
import tempfile
import textwrap
import unittest

import _setup  # noqa: F401
import dyode_common as common


def write_config(text):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as fh:
        fh.write(textwrap.dedent(text))
    return path


class ConfigTests(unittest.TestCase):
    def test_install_md_example_loads(self):
        """The example from the original INSTALL.md, including 'type: Modbus'
        with a capital M, which the original code silently ignored."""
        path = os.path.join(_setup.V1, "config.example.yaml")
        cfg = common.load_config(path)
        mods = cfg["modules"]
        self.assertEqual(mods["Automate Modbus 1"]["type"], "modbus")
        self.assertEqual(mods["Automate Modbus 1"]["registers"], [(0, 100), (400, 450)])
        self.assertEqual(cfg["network"]["out_ip"], "10.0.1.2")
        self.assertEqual(cfg["network"]["out_mac"], "b8:27:eb:b1:ff:ab")
        self.assertEqual(cfg["network"]["in_interface"], "eth0")

    def test_original_v2_configs_still_load(self):
        for side in ("in", "out"):
            cfg = common.load_config(os.path.join(_setup.REPO, "DYODE_v2_light",
                                                  side, "config.yaml"))
            self.assertEqual(cfg["serial"]["device"], "/dev/serial0")
            self.assertEqual(cfg["network"]["out_ip"], "10.0.1.2")   # defaults

    def test_ranges_are_end_exclusive(self):
        self.assertEqual(common.parse_range("0-100"), (0, 100))
        for bad in ("100-0", "5-5", "a-b", "7", "-1-5", "0-70000"):
            with self.assertRaises(common.ConfigError, msg=bad):
                common.parse_range(bad)

    def test_bad_configs_are_rejected_clearly(self):
        cases = {
            "unknown type": """
                modules:
                  x: {type: ftp, port: 1}
            """,
            "missing port": """
                modules:
                  x: {type: modbus, ip: 1.2.3.4, registers: ["0-1"]}
            """,
            "no ranges": """
                modules:
                  x: {type: modbus, port: 1, ip: 1.2.3.4}
            """,
            "no modules": "config_name: empty\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                with self.assertRaises(common.ConfigError):
                    common.load_config(write_config(text))

    def test_interfaces_and_serial_overrides(self):
        cfg = common.load_config(write_config("""
            dyode_in: {ip: 192.168.9.1, interface: enx001122}
            dyode_out: {ip: 192.168.9.2}
            serial: {device: /dev/ttyS0, baudrate: "115200"}
            modules:
              m: {type: modbus, port: 1, ip: 1.2.3.4, coils: ["0-8"]}
        """))
        self.assertEqual(cfg["network"]["in_interface"], "enx001122")
        self.assertEqual(cfg["network"]["out_ip"], "192.168.9.2")
        self.assertEqual(cfg["serial"], {"device": "/dev/ttyS0", "baudrate": 115200})


class UdpFramingTests(unittest.TestCase):
    def test_roundtrip_small_and_large(self):
        for size in (0, 1, 1400, 1401, 250_000):
            payload = os.urandom(size)
            r = common.Reassembler()
            out = [r.feed(d) for d in common.udp_chunks(payload)]
            self.assertEqual(out[-1], payload)
            self.assertTrue(all(o is None for o in out[:-1]))

    def test_datagrams_fit_in_ethernet_frame(self):
        for d in common.udp_chunks(os.urandom(10_000)):
            self.assertLessEqual(len(d) + 28, 1500)   # + IP/UDP headers

    def test_reordering_is_tolerated(self):
        payload = os.urandom(20_000)
        chunks = common.udp_chunks(payload)
        random.Random(1).shuffle(chunks)
        r = common.Reassembler()
        results = [x for x in (r.feed(d) for d in chunks) if x is not None]
        self.assertEqual(results, [payload])

    def test_lost_chunk_drops_message_instead_of_corrupting(self):
        """The original concatenated whatever arrived. Now a message with a
        missing piece is never delivered, and the next one still is."""
        first, second = os.urandom(5000), os.urandom(5000)
        c1 = common.udp_chunks(first)
        del c1[1]
        r = common.Reassembler()
        got = [x for x in (r.feed(d) for d in c1 + common.udp_chunks(second)) if x]
        self.assertEqual(got, [second])

    def test_corruption_detected_by_crc(self):
        chunks = common.udp_chunks(b"x" * 3000)
        bad = bytearray(chunks[0])
        bad[-1] ^= 0xFF
        r = common.Reassembler()
        got = [r.feed(bytes(bad))] + [r.feed(d) for d in chunks[1:]]
        self.assertTrue(all(g is None for g in got))

    def test_garbage_and_foreign_datagrams_ignored(self):
        r = common.Reassembler()
        for junk in (b"", b"abc", os.urandom(100), b"DYO1" + b"\x00" * 20):
            self.assertIsNone(r.feed(junk))


class SerialFramingTests(unittest.TestCase):
    MSG = {"module": "m", "seq": 3, "registers": {"0": [1, 2, 65535]}}

    def test_roundtrip(self):
        frame = common.serial_encode(self.MSG)
        self.assertTrue(frame.endswith(b"\n"))
        self.assertEqual(frame.count(b"\n"), 1)
        self.assertEqual(common.serial_decode(frame), self.MSG)

    def test_damaged_frames_rejected(self):
        frame = bytearray(common.serial_encode(self.MSG))
        frame[20] ^= 0x01
        for bad in (bytes(frame), b"", b"hello", b"zzzzzzzz {}", b"00000000 {}"):
            self.assertIsNone(common.serial_decode(bad))

    def test_linebuffer_handles_split_and_glued_frames(self):
        """The exact failure of the original: reads that cut a message in
        half, or return two messages at once."""
        frames = [common.serial_encode(dict(self.MSG, seq=i)) for i in range(5)]
        stream = b"".join(frames)
        buf = common.LineBuffer()
        rng = random.Random(7)
        got, pos = [], 0
        while pos < len(stream):
            step = rng.randint(1, 90)
            got += buf.feed(stream[pos:pos + step])
            pos += step
        self.assertEqual([common.serial_decode(line)["seq"] for line in got], list(range(5)))

    def test_linebuffer_recovers_after_garbage(self):
        buf = common.LineBuffer(max_line=100)
        self.assertEqual(buf.feed(b"x" * 500), [])
        self.assertEqual(buf.overflows, 1)
        frame = common.serial_encode(self.MSG)
        self.assertEqual(buf.feed(b"noise\n" + frame), [b"noise", frame[:-1]])


if __name__ == "__main__":
    unittest.main()
