"""1.5.0 security fixes: message authentication, symlink-safe file handling,
bounded reassembly, the read-only Modbus mirror, the diode-port firewall and
the key handling in the setup wizard.

Each test names the attack it stops.  The Modbus test at the end talks to a
real pymodbus 3.12 server when one is available (DYODE_TEST_PYTHON, or a
python3 that imports pymodbus); the rest need only the standard library.
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

import _setup
import dyode_common as common
import dyode
import modbus
import screen

sys.path.insert(0, _setup.REPO)
import dyode_setup_core as core  # noqa: E402

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
FIREWALL = os.path.join(_setup.REPO, "lib", "dyode_firewall.py")
INSTALL = os.path.join(_setup.REPO, "install.sh")

CONFIG = """\
config_name: test
dyode_in: {ip: 10.0.1.1, mac: "b8:27:eb:89:1e:f3", interface: eth0}
dyode_out: {ip: 10.0.1.2, mac: "b8:27:eb:b1:ff:ab", interface: %(iface)s}
%(auth)s
modules:
  files: {type: folder, port: 9600, in: /tmp/in, out: /tmp/out}
  plc: {type: modbus, port: 9400, ip: 192.168.1.150, registers: [0-100], coils: [0-10]}
  logs: {type: logs, port: 9700, listen_udp: 514, out: /tmp/logs}
"""


def tmpdir(test):
    path = tempfile.mkdtemp(prefix="dyode_sec_")
    test.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


def write_config(folder, iface="eth1", auth=""):
    path = os.path.join(folder, "config.yaml")
    with open(path, "w") as fh:
        fh.write(CONFIG % {"iface": iface, "auth": auth})
    return path


# --------------------------------------------------------------------------
# Message authentication
# --------------------------------------------------------------------------

class MessageAuthTests(unittest.TestCase):
    def setUp(self):
        self.sender = common.MessageAuth(KEY)
        self.receiver = common.MessageAuth(KEY)

    def test_genuine_message_round_trips(self):
        self.assertEqual(self.receiver.open(self.sender.seal(b"hello")), b"hello")

    def test_forged_by_a_host_without_the_key(self):
        forger = common.MessageAuth(OTHER_KEY)
        self.assertIsNone(self.receiver.open(forger.seal(b"set coil 1")))

    def test_unsigned_dyode_1_4_message(self):
        self.assertIsNone(self.receiver.open(b'{"values": [1, 2, 3]}' + b"\0" * 40))

    def test_tampered_payload_or_header(self):
        sealed = bytearray(self.sender.seal(b"value=10"))
        sealed[-40] ^= 1                                   # payload byte
        self.assertIsNone(self.receiver.open(bytes(sealed)))
        sealed = bytearray(self.sender.seal(b"value=10"))
        sealed[15] ^= 1                                    # counter byte
        self.assertIsNone(self.receiver.open(bytes(sealed)))

    def test_captured_message_replayed(self):
        sealed = self.sender.seal(b"old reading")
        self.assertIsNotNone(self.receiver.open(sealed))
        self.assertIsNone(self.receiver.open(sealed))
        self.assertEqual(self.receiver.rejected, 1)

    def test_reordering_inside_the_window_is_fine(self):
        a, b, c = (self.sender.seal(x) for x in (b"a", b"b", b"c"))
        self.assertEqual([self.receiver.open(m) for m in (c, a, b)], [b"c", b"a", b"b"])

    def test_message_older_than_the_window_refused(self):
        receiver = common.MessageAuth(KEY, window=8)
        old = self.sender.seal(b"old")
        for _ in range(20):
            receiver.open(self.sender.seal(b"new"))
        self.assertIsNone(receiver.open(old))

    def test_sender_restart_starts_a_new_stream(self):
        self.receiver.open(self.sender.seal(b"before"))
        restarted = common.MessageAuth(KEY)                # new boot id, counter 1
        self.assertEqual(self.receiver.open(restarted.seal(b"after")), b"after")

    def test_short_garbage(self):
        self.assertIsNone(self.receiver.open(b"DYA1"))
        self.assertIsNone(self.receiver.open(b""))

    def test_signed_text(self):
        mac = self.sender.sign_text("abc")
        self.assertTrue(self.receiver.verify_text("abc", mac))
        self.assertFalse(self.receiver.verify_text("abd", mac))
        self.assertFalse(self.receiver.verify_text("abc", None))

    def test_sealed_message_through_udp_framing(self):
        payload = os.urandom(5000)
        reasm = common.Reassembler()
        out = None
        for dgram in common.udp_chunks(self.sender.seal(payload)):
            out = reasm.feed(dgram) or out
        self.assertEqual(self.receiver.open(out), payload)


class KeyFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)

    def test_generated_key_is_private_and_never_overwritten(self):
        path = os.path.join(self.dir, "dyode.key")
        common.generate_key_file(path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(len(common.read_key_file(path)), common.AUTH_KEY_BYTES)
        with self.assertRaises(FileExistsError):
            common.generate_key_file(path)

    def test_relative_key_file_is_beside_config(self):
        cfg = common.load_config(write_config(self.dir))
        self.assertEqual(cfg["auth"]["key_file"], os.path.join(self.dir, "dyode.key"))

    def test_missing_key_stops_dyode_by_default(self):
        cfg = common.load_config(write_config(self.dir))
        with self.assertRaises(common.ConfigError) as ctx:
            common.message_auth(cfg)
        self.assertIn("--new-key", str(ctx.exception))

    def test_running_unauthenticated_needs_an_explicit_opt_out(self):
        cfg = common.load_config(write_config(self.dir, auth="auth: {required: false}"))
        with self.assertLogs("dyode", "WARNING"):
            self.assertIsNone(common.message_auth(cfg))

    def test_present_key_is_used(self):
        common.generate_key_file(os.path.join(self.dir, "dyode.key"))
        cfg = common.load_config(write_config(self.dir))
        self.assertIsInstance(common.message_auth(cfg), common.MessageAuth)

    def test_bad_auth_section(self):
        for bad in ("auth: {required: maybe}", "auth: {keyfile: x}", "auth: [1]"):
            with self.assertRaises(common.ConfigError, msg=bad):
                common.load_config(write_config(self.dir, auth=bad))


# --------------------------------------------------------------------------
# Folder transfers: signed manifests, symlinks on both boxes
# --------------------------------------------------------------------------

class SignedManifestTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)
        self.out = os.path.join(self.dir, "out")
        os.mkdir(self.out)
        self.auth = common.MessageAuth(KEY)

    def blob(self, data, name="blob"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def manifest_blob(self, auth, content=b"payload", rel="a.txt"):
        src = self.blob(content, "src")
        fd = os.open(src, os.O_RDONLY)
        try:
            manifest = dyode.build_manifest([(rel, src, fd)], auth=auth)
        finally:
            os.close(fd)
        return manifest, self.blob(json.dumps(manifest).encode(), "manifest")

    def test_signed_batch_is_stored(self):
        receiver = dyode.FolderReceiver(self.out, auth=self.auth)
        _, m = self.manifest_blob(common.MessageAuth(KEY))
        self.assertEqual(receiver.handle(m), "manifest")
        self.assertEqual(receiver.handle(self.blob(b"payload")), "stored")
        with open(os.path.join(self.out, "a.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"payload")

    def test_forged_manifest_cannot_announce_a_planted_file(self):
        receiver = dyode.FolderReceiver(self.out, auth=self.auth)
        _, m = self.manifest_blob(common.MessageAuth(OTHER_KEY), b"evil", "run.sh")
        self.assertEqual(receiver.handle(m), "rejected")
        self.assertEqual(receiver.handle(self.blob(b"evil")), "orphan")
        self.assertFalse(os.path.exists(os.path.join(self.out, "run.sh")))

    def test_unsigned_manifest_refused_when_auth_is_on(self):
        receiver = dyode.FolderReceiver(self.out, auth=self.auth)
        _, m = self.manifest_blob(None)
        self.assertEqual(receiver.handle(m), "rejected")

    def test_edited_manifest_refused(self):
        manifest, _ = self.manifest_blob(common.MessageAuth(KEY))
        manifest["files"][0]["path"] = "../../etc/cron.d/x"
        m = self.blob(json.dumps(manifest).encode(), "edited")
        self.assertEqual(dyode.read_manifest(m, self.auth), "forged")

    def test_replayed_manifest_refused(self):
        receiver = dyode.FolderReceiver(self.out, auth=self.auth)
        manifest, m = self.manifest_blob(common.MessageAuth(KEY))
        self.assertEqual(receiver.handle(m), "manifest")
        again = self.blob(json.dumps(manifest).encode(), "again")
        self.assertEqual(receiver.handle(again), "rejected")

    def test_manifest_signature_ignores_key_order(self):
        manifest, _ = self.manifest_blob(common.MessageAuth(KEY))
        shuffled = dict(reversed(list(manifest.items())))
        m = self.blob(json.dumps(shuffled, indent=2).encode(), "shuffled")
        self.assertIsInstance(dyode.read_manifest(m, self.auth), dict)


class PlaceFileTests(unittest.TestCase):
    """A symlink inside out/ (from a read-write share, say) must not take a
    received file, written as root, anywhere else."""

    def setUp(self):
        self.dir = tmpdir(self)
        self.out = os.path.join(self.dir, "out")
        self.elsewhere = os.path.join(self.dir, "elsewhere")
        os.mkdir(self.out)
        os.mkdir(self.elsewhere)

    def blob(self):
        path = os.path.join(self.dir, "blob")
        with open(path, "wb") as fh:
            fh.write(b"data")
        return path

    def test_plain_and_nested_paths(self):
        self.assertEqual(dyode.place_file(self.out, "a/b/c.txt", self.blob()),
                         os.path.join(self.out, "a", "b", "c.txt"))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "a", "b", "c.txt")))

    def test_symlinked_folder_at_the_top(self):
        os.symlink(self.elsewhere, os.path.join(self.out, "sub"))
        self.assertIsNone(dyode.place_file(self.out, "sub/x.txt", self.blob()))
        self.assertEqual(os.listdir(self.elsewhere), [])

    def test_symlinked_folder_further_down(self):
        os.makedirs(os.path.join(self.out, "a"))
        os.symlink(self.elsewhere, os.path.join(self.out, "a", "b"))
        self.assertIsNone(dyode.place_file(self.out, "a/b/x.txt", self.blob()))
        self.assertEqual(os.listdir(self.elsewhere), [])

    def test_symlink_at_the_final_name_is_replaced_not_followed(self):
        target = os.path.join(self.elsewhere, "victim")
        with open(target, "w") as fh:
            fh.write("keep")
        os.symlink(target, os.path.join(self.out, "x.txt"))
        dyode.place_file(self.out, "x.txt", self.blob())
        with open(target) as fh:
            self.assertEqual(fh.read(), "keep")
        self.assertFalse(os.path.islink(os.path.join(self.out, "x.txt")))

    def test_folder_at_the_final_name(self):
        os.mkdir(os.path.join(self.out, "x.txt"))
        self.assertIsNone(dyode.place_file(self.out, "x.txt", self.blob()))

    def test_receiver_reports_unsafe_destination(self):
        os.symlink(self.elsewhere, os.path.join(self.out, "sub"))
        receiver = dyode.FolderReceiver(self.out)
        src = os.path.join(self.dir, "src")
        with open(src, "wb") as fh:
            fh.write(b"data")
        fd = os.open(src, os.O_RDONLY)
        manifest = dyode.build_manifest([("sub/x.txt", src, fd)])
        os.close(fd)
        mpath = os.path.join(self.dir, "m")
        with open(mpath, "w") as fh:
            json.dump(manifest, fh)
        receiver.handle(mpath)
        self.assertEqual(receiver.handle(self.blob()), "rejected")
        self.assertEqual(os.listdir(self.elsewhere), [])


class OpenReadyFileTests(unittest.TestCase):
    """A drop-folder user must not be able to have another file on the input
    box sent, by swapping a file or folder for a link after the scan."""

    def setUp(self):
        self.dir = tmpdir(self)
        self.root = os.path.join(self.dir, "in")
        os.mkdir(self.root)
        self.secret = os.path.join(self.dir, "secret")
        with open(self.secret, "w") as fh:
            fh.write("secret")

    def test_regular_file(self):
        with open(os.path.join(self.root, "a"), "w") as fh:
            fh.write("a")
        fd = dyode.open_ready_file(self.root, "a")
        self.assertIsNotNone(fd)
        os.close(fd)

    def test_file_swapped_for_a_link(self):
        os.symlink(self.secret, os.path.join(self.root, "a"))
        self.assertIsNone(dyode.open_ready_file(self.root, "a"))

    def test_folder_swapped_for_a_link(self):
        os.symlink(self.dir, os.path.join(self.root, "sub"))
        self.assertIsNone(dyode.open_ready_file(self.root, "sub/secret"))

    def test_fifo_is_not_read(self):
        os.mkfifo(os.path.join(self.root, "pipe"))
        self.assertIsNone(dyode.open_ready_file(self.root, "pipe"))

    def test_open_descriptor_keeps_the_file_even_if_the_path_is_swapped(self):
        path = os.path.join(self.root, "a")
        with open(path, "w") as fh:
            fh.write("genuine")
        fd = dyode.open_ready_file(self.root, "a")
        os.remove(path)
        os.symlink(self.secret, path)              # swapped after opening
        try:
            digest, _ = dyode.hash_fd(fd)
            with open("/proc/self/fd/%d" % fd, "rb") as fh:
                self.assertEqual(fh.read(), b"genuine")
        finally:
            os.close(fd)
        import hashlib
        self.assertEqual(digest, hashlib.sha256(b"genuine").hexdigest())


# --------------------------------------------------------------------------
# Reassembly limits
# --------------------------------------------------------------------------

class ReassemblerLimitTests(unittest.TestCase):
    def header(self, msg_id, index, total, crc=0):
        return common.UDP_HEADER.pack(common.UDP_MAGIC, msg_id, index, total, crc)

    def test_oversized_datagram_dropped(self):
        r = common.Reassembler()
        self.assertIsNone(r.feed(self.header(1, 0, 2) + b"x" * (common.UDP_CHUNK + 1)))
        self.assertEqual(r.pending, {})

    def test_message_announcing_too_many_pieces(self):
        r = common.Reassembler(max_pieces=16)
        self.assertIsNone(r.feed(self.header(1, 0, 17) + b"x"))
        self.assertEqual(r.pending, {})

    def test_total_memory_is_capped(self):
        r = common.Reassembler(max_pending=1000, max_bytes=10 * common.UDP_CHUNK)
        for msg_id in range(100):
            r.feed(self.header(msg_id, 0, 2) + b"x" * common.UDP_CHUNK)
            self.assertLessEqual(r.held, 10 * common.UDP_CHUNK)
        self.assertGreater(r.dropped, 0)

    def test_partial_messages_expire(self):
        now = [0.0]
        r = common.Reassembler(max_age=10, clock=lambda: now[0])
        r.feed(self.header(1, 0, 2) + b"x")
        now[0] = 11
        r.feed(self.header(2, 0, 2) + b"y")
        self.assertNotIn(1, r.pending)
        self.assertEqual(r.held, 1)

    def test_duplicate_piece_does_not_count_twice(self):
        r = common.Reassembler()
        dgrams = common.udp_chunks(b"z" * 3000, msg_id=7)
        r.feed(dgrams[0])
        r.feed(dgrams[0])
        self.assertEqual(r.held, len(dgrams[0]) - common.UDP_HEADER.size)
        r.feed(dgrams[1])
        self.assertEqual(r.feed(dgrams[2]), b"z" * 3000)
        self.assertEqual(r.held, 0)


class SignedModbusEndToEndTests(unittest.TestCase):
    """Poll -> sign -> UDP -> verify -> mirror, with a forger and a replay
    mixed in: only the genuine updates reach the mirror."""

    def test_only_genuine_updates_reach_the_mirror(self):
        import asyncio
        import test_modbus as tm
        tm.reset_plc({a: 500 + a for a in range(10)}, {})
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        p = tm.props(registers=[(0, 10)], coils=[], port=port)
        cfg = {"network": {"out_ip": "127.0.0.1", "out_interface": "lo"}}
        mirror = modbus.ModbusMirror("m", p)
        sender, forger = common.MessageAuth(KEY), common.MessageAuth(OTHER_KEY)
        receiver = common.MessageAuth(KEY)

        async def scenario():
            server = asyncio.create_task(modbus.udp_output(
                "m", p, cfg, mirror=mirror, serve=False, auth=receiver))
            await asyncio.sleep(0.1)
            poller = modbus.PlcPoller("m", p)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                send = lambda payload, auth: asyncio.to_thread(
                    common.udp_send, s, ("127.0.0.1", port), payload, auth=auth)
                genuine = common.encode_json(poller.poll())
                await send(genuine, sender)
                forged = dict(json.loads(genuine), seq=99)
                forged["registers"] = {"0": [6666] * 10}
                await send(common.encode_json(forged), forger)
                await send(common.encode_json(forged), None)
                captured = sender.seal(common.encode_json(poller.poll()))   # seq 2
                for _ in range(2):                             # sent, then replayed
                    for dgram in common.udp_chunks(captured):
                        s.sendto(dgram, ("127.0.0.1", port))
                    await asyncio.sleep(0.05)
            for _ in range(50):
                if receiver.rejected >= 3 and mirror.last_seq == 2:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)
            server.cancel()

        with self.assertLogs("dyode", "WARNING"):
            asyncio.run(scenario())
        self.assertEqual(mirror.last_seq, 2)
        self.assertEqual(tm.read_back(mirror, 3, 0, 10), [500 + a for a in range(10)])
        self.assertEqual(receiver.rejected, 3)        # forged, unsigned, replayed


class DiodeSocketTests(unittest.TestCase):
    def test_explicit_bind_skips_the_device(self):
        sock = common.diode_socket({"network": {}}, 0, bind=("127.0.0.1", 0))
        self.addCleanup(sock.close)
        self.assertEqual(sock.getsockname()[0], "127.0.0.1")

    def test_unknown_interface_warns_and_still_listens(self):
        cfg = {"network": {"out_ip": "127.0.0.1", "out_interface": "nosuchnic0"}}
        with self.assertLogs("dyode", "WARNING"):
            sock = common.diode_socket(cfg, 0)
        sock.close()


# --------------------------------------------------------------------------
# Read-only Modbus mirror
# --------------------------------------------------------------------------

class ReadOnlyMirrorTests(unittest.TestCase):
    """Logic, against the pymodbus stand-in.  The real protocol is checked
    by RealPymodbusTests below."""

    def run_async(self, coro):
        import asyncio
        return asyncio.run(coro)

    def device(self, unmapped="exception"):
        ctx = modbus.make_server_context(200, 1, {"h": [(0, 100)], "c": [(0, 10)]},
                                         unmapped=unmapped)
        return ctx[1]

    def test_every_write_is_illegal_function(self):
        from pymodbus.constants import ExcCodes
        dev = self.device()
        with self.assertLogs("dyode", "WARNING"):
            for fc in (5, 6, 15, 16):
                self.assertEqual(self.run_async(dev.async_setValues(fc, 0, [1])),
                                 ExcCodes.ILLEGAL_FUNCTION)
        self.assertEqual(dev.refused_writes, 4)

    def test_unmapped_read_is_illegal_address(self):
        from pymodbus.constants import ExcCodes
        dev = self.device()
        self.assertEqual(self.run_async(dev.async_getValues(3, 150, 1)),
                         ExcCodes.ILLEGAL_ADDRESS)
        self.assertEqual(self.run_async(dev.async_getValues(3, 95, 10)),
                         ExcCodes.ILLEGAL_ADDRESS)     # runs off the end
        self.assertEqual(self.run_async(dev.async_getValues(1, 10, 1)),
                         ExcCodes.ILLEGAL_ADDRESS)

    def test_mapped_read_returns_values(self):
        dev = self.device()
        dev.setValues(3, 5, [42])                      # the diode path
        self.assertEqual(self.run_async(dev.async_getValues(3, 5, 1)), [42])

    def test_zero_mode_answers_unmapped_reads_with_zero(self):
        dev = self.device("zero")
        self.assertEqual(self.run_async(dev.async_getValues(3, 150, 1)), [0])

    def test_unmapped_setting_is_validated(self):
        d = tmpdir(self)
        path = write_config(d, auth="")
        text = open(path).read().replace("coils: [0-10]}", "coils: [0-10], unmapped: maybe}")
        with open(path, "w") as fh:
            fh.write(text)
        with self.assertRaises(common.ConfigError):
            common.load_config(path)


def _real_pymodbus_python():
    candidates = [os.environ.get("DYODE_TEST_PYTHON"), shutil.which("python3")]
    for py in filter(None, candidates):
        probe = subprocess.run(
            [py, "-I", "-c", "import pymodbus, yaml; assert pymodbus.__version__.startswith('3.12')"],
            capture_output=True)
        if probe.returncode == 0:
            return py
    return None


REAL_PY = _real_pymodbus_python()

MODBUS_SCRIPT = textwrap.dedent("""
    import asyncio, sys
    sys.path.insert(0, sys.argv[1])
    import modbus
    from pymodbus.client import AsyncModbusTcpClient

    async def main(port, unmapped):
        ctx = modbus.make_server_context(200, 1, {"h": [(0, 100)], "c": [(0, 10)]},
                                         unmapped=unmapped)
        ctx[1].setValues(3, 7, [1234])
        server = asyncio.create_task(modbus.serve_tcp(ctx, modbus.make_identity("t"),
                                                      "127.0.0.1", port))
        client = AsyncModbusTcpClient("127.0.0.1", port=port)
        for _ in range(50):
            await asyncio.sleep(0.1)
            if await client.connect():
                break
        def code(r):
            return r.exception_code if r.isError() else r.registers if hasattr(r, "registers") and r.registers else "ok"
        out = [code(await client.read_holding_registers(7, count=1, device_id=1)),
               code(await client.write_register(7, 1, device_id=1)),
               code(await client.write_coil(0, True, device_id=1)),
               code(await client.read_holding_registers(150, count=1, device_id=1)),
               code(await client.read_holding_registers(7, count=1, device_id=1))]
        print(out)
        client.close()
        server.cancel()

    asyncio.run(main(int(sys.argv[2]), sys.argv[3]))
""")


@unittest.skipUnless(REAL_PY, "needs a python3 with pymodbus 3.12 (set DYODE_TEST_PYTHON)")
class RealPymodbusTests(unittest.TestCase):
    def run_server(self, unmapped):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        d = tmpdir(self)
        script = os.path.join(d, "mb.py")
        with open(script, "w") as fh:
            fh.write(MODBUS_SCRIPT)
        res = subprocess.run([REAL_PY, "-I", script, _setup.V1, str(port), unmapped],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.strip().splitlines()[-1]

    def test_exception_mode(self):
        # read ok; writes -> 01 (value unchanged); unmapped read -> 02
        self.assertEqual(self.run_server("exception"), "[[1234], 1, 1, 2, [1234]]")

    def test_zero_mode(self):
        self.assertEqual(self.run_server("zero"), "[[1234], 1, 1, [0], [1234]]")


# --------------------------------------------------------------------------
# Firewall
# --------------------------------------------------------------------------

class FirewallRulesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)

    def rules(self, *args, iface="eth1"):
        return subprocess.run([sys.executable, FIREWALL, write_config(self.dir, iface), *args],
                              capture_output=True, text=True, timeout=30)

    def test_ports_include_udpcast_second_port(self):
        self.assertEqual(self.rules("--ports").stdout.split(),
                         ["9400", "9600", "9601", "9700"])

    def test_ruleset_is_scoped_to_its_own_table(self):
        text = self.rules().stdout
        self.assertIn("table inet dyode {", text)
        self.assertIn('iifname != "eth1"', text)
        self.assertIn("ip saddr != 10.0.1.1", text)
        self.assertIn("meta nfproto ipv6", text)
        self.assertNotIn("flush ruleset", text)
        self.assertIn("policy accept", text)

    def test_interface_name_cannot_inject_rules(self):
        res = self.rules(iface='"eth1\\"; flush ruleset; #"')
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")
        self.assertIn("unsafe interface", res.stderr)

    @unittest.skipUnless(shutil.which("nft") and os.geteuid() == 0, "needs nft as root")
    def test_nft_accepts_the_rules(self):
        path = os.path.join(self.dir, "fw.nft")
        with open(path, "w") as fh:
            fh.write(self.rules().stdout)
        res = subprocess.run(["nft", "-c", "-f", path], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class FirewallInstallTests(unittest.TestCase):
    """install.sh --firewall-only against a scratch /etc, with stand-ins for
    nft, sysctl and systemctl that record their calls."""

    def setUp(self):
        self.dir = tmpdir(self)
        self.etc = os.path.join(self.dir, "etc")
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        self.log = os.path.join(self.dir, "calls.log")
        for name in ("nft", "sysctl", "systemctl"):
            self.fake(name, 'echo "%s $*" >> "$LOG"; exit 0' % name)
        self.config = write_config(self.dir)

    def fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def install(self, *args):
        env = dict(os.environ, DYODE_ETC=self.etc, LOG=self.log, PYTHON=sys.executable,
                   PATH=self.bin + ":" + os.path.dirname(sys.executable) + ":/usr/bin:/bin")
        return subprocess.run(["bash", INSTALL, *args], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, env=env, timeout=60,
                              cwd=self.dir)

    def calls(self):
        return open(self.log).read() if os.path.exists(self.log) else ""

    def test_installs_rules_unit_and_rp_filter(self):
        res = self.install("--firewall-only", "--config", self.config)
        self.assertEqual(res.returncode, 0, res.stderr)
        rules = open(os.path.join(self.etc, "dyode", "firewall.nft")).read()
        self.assertIn("ip saddr != 10.0.1.1", rules)
        unit = open(os.path.join(self.etc, "systemd", "system",
                                 "dyode-firewall.service")).read()
        self.assertIn("After=nftables.service", unit)
        self.assertIn("Before=network-pre.target dyode-out.service", unit)
        sysctl = open(os.path.join(self.etc, "sysctl.d", "92-dyode-firewall.conf")).read()
        self.assertIn("net.ipv4.conf.eth1.rp_filter = 1", sysctl)
        calls = self.calls()
        self.assertIn("nft -c -f", calls)
        self.assertIn("nft -f %s" % os.path.join(self.etc, "dyode", "firewall.nft"), calls)
        self.assertIn("systemctl enable dyode-firewall.service", calls)

    def test_repeat_run_changes_nothing(self):
        self.install("--firewall-only", "--config", self.config)
        res = self.install("--firewall-only", "--config", self.config)
        self.assertIn("already up to date", res.stdout)
        self.assertEqual([f for f in os.listdir(os.path.join(self.etc, "dyode"))
                          if "backup" in f], [])

    def test_dry_run_writes_and_loads_nothing(self):
        res = self.install("--firewall-only", "--config", self.config, "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ip saddr != 10.0.1.1 -> drop", res.stdout)
        self.assertFalse(os.path.exists(self.etc))
        self.assertEqual(self.calls(), "")

    def test_rejected_rules_change_nothing(self):
        self.fake("nft", 'echo "nft $*" >> "$LOG"; echo "Error: syntax" >&2; exit 1')
        res = self.install("--firewall-only", "--config", self.config)
        self.assertIn("nft rejected", res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.etc, "dyode", "firewall.nft")))

    def test_contradictions_and_sending_box_refused(self):
        self.assertNotEqual(self.install("--firewall-only", "--no-firewall",
                                         "--config", self.config).returncode, 0)
        self.assertNotEqual(self.install("--firewall-only", "--side", "in",
                                         "--config", self.config).returncode, 0)

    def test_opt_out_is_documented_in_help(self):
        res = self.install("--help")
        self.assertIn("--no-firewall", res.stdout + res.stderr)


# --------------------------------------------------------------------------
# Smaller items: screen viewer page, syslog senders (D-11, D-12)
# --------------------------------------------------------------------------

class ScreenExposureTests(unittest.TestCase):
    CFG = {"network": {"out_interface": "eth1", "out_ip": "10.0.1.2"}}

    def test_default_bind_skips_loopback_and_the_diode_nic(self):
        addrs = {"lo": "127.0.0.1", "eth1": "10.0.1.2", "eth0": "192.168.50.4"}
        self.assertEqual(screen.default_http_bind(self.CFG, addrs), "192.168.50.4")

    def test_no_it_address_falls_back_to_all_with_a_warning(self):
        with self.assertLogs("dyode", "WARNING"):
            self.assertEqual(screen.default_http_bind(self.CFG, {"lo": "127.0.0.1"}),
                             "0.0.0.0")

    def test_viewer_streams_are_capped(self):
        import http.client
        import threading
        store = screen.FrameStore()
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), screen.make_handler(store, 1))
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]
        first = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        first.request("GET", "/stream.mjpg")
        self.assertEqual(first.getresponse().status, 200)
        self.addCleanup(first.close)
        second = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        with self.assertLogs("dyode", "WARNING"):
            second.request("GET", "/stream.mjpg")
            self.assertEqual(second.getresponse().status, 503)
        second.close()

    def test_bad_settings_refused(self):
        d = tmpdir(self)
        base = CONFIG % {"iface": "eth1", "auth": ""}
        for extra in ("max_streams: 0", "http_bind: everywhere"):
            path = os.path.join(d, "config.yaml")
            with open(path, "w") as fh:
                fh.write(base + "  scr: {type: screen, port: 9900, in: /a, out: /b, %s}\n"
                         % extra)
            with self.assertRaises(common.ConfigError, msg=extra):
                common.load_config(path)


class SyslogSenderWarningTests(unittest.TestCase):
    def test_open_syslog_listener_is_flagged(self):
        model = core.ConfigModel("v1")
        model.add_module("logs", {"type": "logs", "port": 9700, "listen_udp": 514,
                                  "out": "/var/log/x"})
        self.assertTrue(any("allow_from" in w for w in model.warnings("in")))
        model.modules[0].props["allow_from"] = ["192.168.1.0/24"]
        self.assertFalse(any("allow_from" in w for w in model.warnings("in")))

    def test_network_list_validation(self):
        self.assertTrue(core.valid_network_list(""))
        self.assertTrue(core.valid_network_list("192.168.1.0/24, 10.0.0.5"))
        self.assertFalse(core.valid_network_list("plc-1"))


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class NfsSubtreeTests(unittest.TestCase):
    def test_fs_root_detection(self):
        lib = os.path.join(_setup.REPO, "lib", "install-shares.sh")
        d = tmpdir(self)
        script = 'die(){ exit 9; }; . "%s"; fs_root / && echo root; fs_root "%s" || echo sub' % (lib, d)
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(res.stdout.split(), ["root", "sub"])


# --------------------------------------------------------------------------
# Setup wizard: the key
# --------------------------------------------------------------------------

class WizardKeyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir(self)
        self.work = os.path.join(self.dir, "work")
        os.mkdir(self.work)

    def plan(self, model):
        return core.Plan(model, "in", self.work)

    def test_first_box_creates_a_key(self):
        plan = self.plan(core.ConfigModel("v1"))
        self.assertEqual(plan.key_action, "create")
        plan.model.add_module("f", {"type": "folder", "port": 9600,
                                    "in": "/tmp/a", "out": "/tmp/b"})
        plan.apply()
        key = os.path.join(self.work, "dyode.key")
        self.assertEqual(stat.S_IMODE(os.stat(key).st_mode), 0o600)
        self.assertTrue(any("dyode.key" in s for s in core.next_steps(plan)))

    def test_import_copies_the_key_beside_the_imported_config(self):
        other = os.path.join(self.dir, "usb")
        os.mkdir(other)
        common.generate_key_file(os.path.join(other, "dyode.key"))
        model = core.ConfigModel.from_yaml_text(open(write_config(other)).read(), "v1")
        model.imported_from = os.path.join(other, "config.yaml")
        plan = self.plan(model)
        self.assertEqual(plan.key_action, "copy")
        plan.apply()
        self.assertEqual(common.read_key_file(os.path.join(self.work, "dyode.key")),
                         common.read_key_file(os.path.join(other, "dyode.key")))

    def test_import_without_a_key_never_makes_a_second_one(self):
        other = os.path.join(self.dir, "usb")
        os.mkdir(other)
        model = core.ConfigModel.from_yaml_text(open(write_config(other)).read(), "v1")
        model.imported_from = os.path.join(other, "config.yaml")
        plan = self.plan(model)
        self.assertEqual(plan.key_action, "missing")
        plan.apply()
        self.assertFalse(os.path.exists(os.path.join(self.work, "dyode.key")))
        self.assertTrue(any("from the other box" in s for s in core.next_steps(plan)))

    def test_existing_key_kept(self):
        common.generate_key_file(os.path.join(self.work, "dyode.key"))
        self.assertEqual(self.plan(core.ConfigModel("v1")).key_action, "keep")

    def test_v2_serial_needs_no_key(self):
        self.assertIsNone(self.plan(core.ConfigModel("v2")).key_action)

    def test_auth_section_and_unmapped_survive_a_round_trip(self):
        text = open(write_config(self.dir, auth="auth: {required: false}")).read()
        text = text.replace("coils: [0-10]}", "coils: [0-10], unmapped: zero}")
        model = core.ConfigModel.from_yaml_text(text, "v1")
        again = model.to_yaml_text()
        self.assertIn("required: false", again)
        self.assertIn("unmapped: zero", again)
        self.assertIsNone(model.validate())

    def test_new_key_command(self):
        key = os.path.join(self.dir, "k")
        run = [sys.executable, os.path.join(_setup.REPO, "dyode_setup.py"),
               "--new-key", "--key-file", key]
        first = subprocess.run(run, capture_output=True, text=True, timeout=30)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = open(key).read()
        second = subprocess.run(run, capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 1)
        self.assertIn("already exists", second.stdout)
        self.assertEqual(open(key).read(), before)


if __name__ == "__main__":
    unittest.main()
