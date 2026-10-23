import asyncio
import random
import socket
import threading
import time
import unittest

import _setup  # noqa: F401
import dyode_common as common
import modbus
import serial as fake_serial
from pymodbus.client import ModbusTcpClient as FakeClient


def props(registers=((0, 10),), coils=((0, 4),), **extra):
    p = {"type": "modbus", "port": 9400, "ip": "192.0.2.1", "plc_port": 502,
         "port_out": 1502, "unit": 1, "registers": list(registers), "coils": list(coils)}
    p.update(extra)
    return p


def reset_plc(registers=None, coils=None):
    FakeClient.registers = registers or {}
    FakeClient.coils = coils or {}
    FakeClient.calls = []
    FakeClient.fail_connect = False


def read_back(mirror, fc, start, count):
    return mirror.context[1].getValues(fc, start, count)


class PollerTests(unittest.TestCase):
    def setUp(self):
        reset_plc({a: a * 2 for a in range(500)}, {1: True, 3: True})

    def test_poll_reads_ranges_and_trims_padded_bits(self):
        msg = modbus.PlcPoller("m", props()).poll()
        self.assertEqual(msg["registers"]["0"], [a * 2 for a in range(10)])
        # 4 coils requested; the fake (like pymodbus) returns 8 padded bits.
        self.assertEqual(msg["coils"]["0"], [False, True, False, True])
        self.assertEqual(msg["module"], "m")
        self.assertEqual(msg["seq"], 1)

    def test_large_range_split_into_legal_requests(self):
        """The original sent one request per range; >125 registers fails."""
        msg = modbus.PlcPoller("m", props(registers=[(0, 300)], coils=[])).poll()
        self.assertEqual(len(msg["registers"]["0"]), 300)
        counts = [c[2] for c in FakeClient.calls if c[0] == "hr"]
        self.assertEqual(counts, [125, 125, 50])

    def test_device_id_keyword_detected(self):
        modbus.PlcPoller("m", props(unit=7)).poll()
        self.assertTrue(all(c[3] == 7 for c in FakeClient.calls))

    def test_older_slave_keyword_supported(self):
        class OldClient(FakeClient):
            def read_holding_registers(self, address, *, count=1, slave=1):
                return super().read_holding_registers(address, count=count, device_id=slave)
            def read_coils(self, address, *, count=1, slave=1):
                return super().read_coils(address, count=count, device_id=slave)
        poller = modbus.PlcPoller("m", props(unit=5), client=OldClient("x"))
        poller.poll()
        self.assertEqual(poller._kw_regs, "slave")
        self.assertTrue(all(c[3] == 5 for c in FakeClient.calls))

    def test_connection_failure_raises_instead_of_returning_none(self):
        """The original returned None, which was then sent and crashed the
        output side."""
        FakeClient.fail_connect = True
        with self.assertRaises(modbus.ModbusReadError):
            modbus.PlcPoller("m", props()).poll()

    def test_error_response_raises_and_resets_connection(self):
        poller = modbus.PlcPoller("m", props())
        poller.client.read_holding_registers = lambda **kw: type(
            "E", (), {"isError": lambda s: True})()
        with self.assertRaises(modbus.ModbusReadError):
            poller.poll()
        self.assertFalse(poller.client.connected)


class MirrorTests(unittest.TestCase):
    def test_apply_writes_registers_and_coils(self):
        m = modbus.ModbusMirror("m", props(registers=[(0, 5), (400, 450)], coils=[(0, 4)]))
        self.assertEqual(m.size, 450)          # sized from config, not fixed 100
        self.assertTrue(m.apply({"registers": {"0": [1, 2, 3], "400": [9] * 50},
                                 "coils": {"0": [True, False, True, True]}}))
        self.assertEqual(read_back(m, 3, 0, 3), [1, 2, 3])
        self.assertEqual(read_back(m, 3, 400, 50), [9] * 50)
        self.assertEqual(read_back(m, 1, 0, 4), [True, False, True, True])

    def test_out_of_range_values_clipped_not_misplaced(self):
        m = modbus.ModbusMirror("m", props(registers=[(0, 10)], coils=[]))
        with self.assertLogs("dyode.modbus", "WARNING"):
            self.assertTrue(m.apply({"registers": {"8": [1, 2, 3, 4], "50": [7]}}))
        self.assertEqual(read_back(m, 3, 8, 2), [1, 2])

    def test_malformed_messages_rejected(self):
        m = modbus.ModbusMirror("m", props())
        for bad in (None, [], {"registers": [1]}, {"registers": {"0": "x"}},
                    {"registers": {"0": [70000]}}, {"registers": {"0": [1.5]}},
                    {"registers": {"0": [True]}}, {"registers": {"zz": [1]}}):
            with self.subTest(bad=bad), self.assertLogs("dyode.modbus", "ERROR"):
                self.assertFalse(m.apply(bad))


class UdpEndToEndTests(unittest.TestCase):
    def test_poll_send_receive_serve(self):
        reset_plc({a: 1000 + a for a in range(20)}, {2: True})
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        p = props(registers=[(0, 20)], coils=[(0, 3)], port=port)
        cfg = {"network": {"out_ip": "127.0.0.1"}}
        mirror = modbus.ModbusMirror("m", p)

        async def scenario():
            server = asyncio.create_task(modbus.udp_output("m", p, cfg, mirror=mirror, serve=False))
            await asyncio.sleep(0.1)
            poller = modbus.PlcPoller("m", p)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                for _ in range(3):
                    await asyncio.to_thread(common.udp_send, s, ("127.0.0.1", port),
                                            common.encode_json(poller.poll()))
            for _ in range(50):
                if mirror.last_seq == 3:
                    break
                await asyncio.sleep(0.02)
            server.cancel()

        asyncio.run(scenario())
        self.assertEqual(mirror.last_seq, 3)
        self.assertEqual(read_back(mirror, 3, 0, 20), [1000 + a for a in range(20)])
        self.assertEqual(read_back(mirror, 1, 0, 3), [False, False, True])


class SerialEndToEndTests(unittest.TestCase):
    def test_two_modules_over_one_noisy_serial_link(self):
        device = "/dev/fake-%s" % random.random()
        cfg = {"serial": {"device": device, "baudrate": 57600}}
        mods = {"a": props(registers=[(0, 5)], coils=[]),
                "b": props(registers=[(10, 13)], coils=[(0, 2)])}
        mirrors = {n: modbus.ModbusMirror(n, p) for n, p in mods.items()}
        frames = [common.serial_encode({"module": "a", "seq": 1, "registers": {"0": [5, 4, 3, 2, 1]}}),
                  b"line noise that is not a frame\n",
                  common.serial_encode({"module": "ghost", "seq": 1, "registers": {}}),
                  common.serial_encode({"module": "b", "seq": 1, "registers": {"10": [7, 8, 9]},
                                        "coils": {"0": [True, True]}})]
        stream = b"".join(frames)

        def writer():
            time.sleep(0.2)
            port = fake_serial.Serial(port=device)
            rng = random.Random(3)
            pos = 0
            while pos < len(stream):          # dribble bytes in random chunks
                step = rng.randint(1, 40)
                port.write(stream[pos:pos + step])
                pos += step
                time.sleep(0.005)

        async def scenario():
            task = asyncio.create_task(modbus.serial_output(mods, cfg, mirrors=mirrors, serve=False))
            await asyncio.to_thread(writer)
            for _ in range(100):
                if all(m.last_seq == 1 for m in mirrors.values()):
                    break
                await asyncio.sleep(0.02)
            task.cancel()

        with self.assertLogs("dyode.modbus", "WARNING") as logs:
            asyncio.run(scenario())
        self.assertEqual(read_back(mirrors["a"], 3, 0, 5), [5, 4, 3, 2, 1])
        self.assertEqual(read_back(mirrors["b"], 3, 10, 3), [7, 8, 9])
        self.assertEqual(read_back(mirrors["b"], 1, 0, 2), [True, True])
        joined = "\n".join(logs.output)
        self.assertIn("damaged frame", joined)
        self.assertIn("unknown module 'ghost'", joined)

    def test_serial_input_loop_writes_framed_messages(self):
        reset_plc({0: 11, 1: 22})
        device = "/dev/fake-in-%s" % random.random()
        cfg = {"serial": {"device": device, "baudrate": 57600}}
        mods = {"a": props(registers=[(0, 2)], coils=[], interval=0.05)}
        t = threading.Thread(target=modbus.run_serial_input, args=(mods, cfg), daemon=True)
        t.start()
        time.sleep(0.3)
        raw = bytes(fake_serial.channel(device).buf)
        lines = common.LineBuffer().feed(raw)
        self.assertGreaterEqual(len(lines), 2)
        msgs = [common.serial_decode(line) for line in lines]
        self.assertTrue(all(m["registers"]["0"] == [11, 22] for m in msgs))
        self.assertEqual([m["seq"] for m in msgs], list(range(1, len(msgs) + 1)))


if __name__ == "__main__":
    unittest.main()
