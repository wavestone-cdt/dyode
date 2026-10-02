# -*- coding: utf-8 -*-
"""Modbus transfer through the diode (Python 3 port).

Input side:  poll a PLC with a Modbus TCP client and push the values out.
Output side: receive the values and serve them from a Modbus TCP server.

Two transports are provided:
  * UDP    (DYODE v1): one process per module, one UDP port per module
  * serial (DYODE v2): one process for all modules, messages tagged by name

This same file is copied into DYODE v1 (full)/, DYODE v2 (light)/in/ and
DYODE v2 (light)/out/.

Every pymodbus call lives in the "pymodbus shim" section so that an upgrade
(e.g. to pymodbus 4.x) only has to touch that section. Tested API range:
pymodbus 3.11 - 3.12 (see requirements.txt).
"""

import asyncio
import inspect
import logging
import socket
import threading
import time

import dyode_common as common

log = logging.getLogger("dyode.modbus")

# Modbus protocol limits per request.
MAX_REGISTERS_PER_READ = 125
MAX_COILS_PER_READ = 2000

# Modbus function codes used by the datastore.
FC_COILS = 1
FC_HOLDING_REGISTERS = 3


# ==========================================================================
# pymodbus shim
# ==========================================================================

def _device_kwarg(method):
    """pymodbus renamed the device-address keyword twice:
    unit= (<3.3), slave= (3.3-3.9), device_id= (3.10+)."""
    params = inspect.signature(method).parameters
    for name in ("device_id", "slave", "unit"):
        if name in params:
            return name
    return None


def make_tcp_client(host, port, timeout=3.0):
    from pymodbus.client import ModbusTcpClient
    return ModbusTcpClient(host, port=port, timeout=timeout)


def make_server_context(size, unit):
    """Datastore holding coils and holding registers 0..size-1."""
    from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext
    try:
        from pymodbus.datastore import ModbusDeviceContext as DeviceContext
    except ImportError:                        # pymodbus < 3.10
        from pymodbus.datastore import ModbusSlaveContext as DeviceContext
    # +1: some pymodbus versions shift datastore addresses by one internally.
    store = DeviceContext(co=ModbusSequentialDataBlock(0, [False] * (size + 1)),
                          hr=ModbusSequentialDataBlock(0, [0] * (size + 1)))
    return ModbusServerContext(store, single=True)


def make_identity(module_name):
    try:
        from pymodbus import ModbusDeviceIdentification
    except ImportError:
        from pymodbus.device import ModbusDeviceIdentification
    identity = ModbusDeviceIdentification()
    identity.VendorName = "ASO+AKO"
    identity.ProductCode = "DYODE"
    identity.VendorUrl = "https://github.com/wavestone-cdt/dyode"
    identity.ProductName = "DYODE"
    identity.ModelName = module_name[:64]
    identity.MajorMinorRevision = "2.0-py3"
    return identity


async def serve_tcp(context, identity, bind, port):
    from pymodbus.server import StartAsyncTcpServer
    await StartAsyncTcpServer(context, identity=identity, address=(bind, port))


def datastore_write(context, unit, fc, address, values):
    context[unit].setValues(fc, address, values)


# ==========================================================================
# Input side: polling the PLC
# ==========================================================================

class ModbusReadError(Exception):
    pass


class PlcPoller:
    """Reads the configured ranges from one PLC. Keeps the TCP connection
    open between polls and reconnects after any failure."""

    def __init__(self, name, props, client=None):
        self.name = name
        self.props = props
        self.client = client or make_tcp_client(props["ip"], props["plc_port"])
        self.unit = props["unit"]
        self.seq = 0
        self._kw_regs = _device_kwarg(self.client.read_holding_registers)
        self._kw_coils = _device_kwarg(self.client.read_coils)

    def _read(self, method, kw, address, count):
        kwargs = {"count": count}
        if kw:
            kwargs[kw] = self.unit
        rr = method(address=address, **kwargs)
        if rr is None or rr.isError():
            raise ModbusReadError("read of %d value(s) at %d failed: %s"
                                  % (count, address, rr))
        return rr

    def poll(self):
        """Return one message dict with all configured values."""
        try:
            if not self.client.connected and not self.client.connect():
                raise ModbusReadError("cannot connect to %s:%s"
                                      % (self.props["ip"], self.props["plc_port"]))
            registers = {}
            for start, end in self.props["registers"]:
                values = []
                for addr in range(start, end, MAX_REGISTERS_PER_READ):
                    count = min(MAX_REGISTERS_PER_READ, end - addr)
                    rr = self._read(self.client.read_holding_registers,
                                    self._kw_regs, addr, count)
                    values.extend(int(v) for v in rr.registers[:count])
                registers[str(start)] = values

            coils = {}
            for start, end in self.props["coils"]:
                values = []
                for addr in range(start, end, MAX_COILS_PER_READ):
                    count = min(MAX_COILS_PER_READ, end - addr)
                    rr = self._read(self.client.read_coils,
                                    self._kw_coils, addr, count)
                    # .bits is padded to a multiple of 8: keep only `count`.
                    values.extend(bool(b) for b in rr.bits[:count])
                coils[str(start)] = values
        except Exception:
            # Drop the connection so the next poll starts clean.
            try:
                self.client.close()
            except Exception:
                pass
            raise

        self.seq += 1
        return {"module": self.name, "seq": self.seq, "time": time.time(),
                "registers": registers, "coils": coils}


class _FailureLog:
    """Log the first failure loudly, repeats quietly, and the recovery."""

    def __init__(self, what):
        self.what = what
        self.failing = False

    def fail(self, err):
        if not self.failing:
            log.warning("%s: %s", self.what, err)
            self.failing = True
        else:
            log.debug("%s (still failing): %s", self.what, err)

    def ok(self):
        if self.failing:
            log.info("%s: recovered", self.what)
            self.failing = False


def run_udp_input(name, props, cfg):
    """DYODE v1 input agent: poll one PLC, send over UDP. Runs forever."""
    common.setup_logging(cfg.get("_log_level", "INFO"), cfg)
    poller = PlcPoller(name, props)
    interval = float(props.get("interval", 1.0))
    addr = (cfg["network"]["out_ip"], props["port"])
    status = _FailureLog("module %r" % name)
    log.info("module %r: polling %s:%s every %ss, sending to %s:%s",
             name, props["ip"], props["plc_port"], interval, *addr)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        while True:
            started = time.monotonic()
            try:
                common.udp_send(sock, addr, common.encode_json(poller.poll()))
                status.ok()
            except Exception as err:
                status.fail(err)
            time.sleep(max(0.0, interval - (time.monotonic() - started)))


def _open_serial(serial_cfg, timeout):
    import serial
    return serial.Serial(port=serial_cfg["device"], baudrate=serial_cfg["baudrate"],
                         timeout=timeout, write_timeout=5)


def run_serial_input(modules, cfg):
    """DYODE v2 input: poll every modbus module, write to the serial link.
    A single process owns the serial port (the original opened it once per
    module per second, which cannot work with more than one module)."""
    pollers = [PlcPoller(n, p) for n, p in modules.items()]
    interval = min(float(p.get("interval", 1.0)) for p in modules.values())
    serial_cfg = cfg["serial"]
    # 8N1 = 10 bits on the wire per byte.
    budget = serial_cfg["baudrate"] / 10.0 * interval
    link = _FailureLog("serial port %s" % serial_cfg["device"])
    statuses = {pl.name: _FailureLog("module %r" % pl.name) for pl in pollers}
    ser = None
    log.info("polling %d module(s) every %ss, writing to %s @ %d baud",
             len(pollers), interval, serial_cfg["device"], serial_cfg["baudrate"])
    while True:
        started = time.monotonic()
        if ser is None:
            try:
                ser = _open_serial(serial_cfg, timeout=0.5)
                link.ok()
            except Exception as err:
                link.fail(err)
        sent = 0
        for poller in pollers:
            try:
                frame = common.serial_encode(poller.poll())
                statuses[poller.name].ok()
            except Exception as err:
                statuses[poller.name].fail(err)
                continue
            if ser is None:
                continue
            try:
                ser.write(frame)
                ser.flush()
                sent += len(frame)
            except Exception as err:
                link.fail(err)
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
        if sent > 0.8 * budget:
            log.warning("sending %d bytes per %ss uses >80%% of the serial "
                        "bandwidth; raise 'interval' or reduce ranges", sent, interval)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


# ==========================================================================
# Output side: mirroring values in a Modbus server
# ==========================================================================

class ModbusMirror:
    """Datastore + server for one module, fed by received messages."""

    def __init__(self, name, props, context=None):
        self.name = name
        self.props = props
        ends = [e for _, e in props["registers"]] + [e for _, e in props["coils"]]
        self.size = max(ends)
        self.unit = props["unit"]
        self.context = context if context is not None else make_server_context(self.size, self.unit)
        self.last_update = None
        self.last_seq = None
        self.stale_after = float(props.get("stale_after", 10.0))
        self._stale_logged = False

    def _write_block(self, fc, blocks, kind):
        if not isinstance(blocks, dict):
            raise ValueError("%s must be an object" % kind)
        for start_s, values in blocks.items():
            start = int(start_s)
            if not isinstance(values, list):
                raise ValueError("%s at %s: values must be a list" % (kind, start))
            if kind == "registers":
                if not all(type(v) is int and 0 <= v <= 0xFFFF for v in values):
                    raise ValueError("register values at %d out of range" % start)
            else:
                values = [bool(v) for v in values]
            if start < 0 or start >= self.size:
                log.warning("module %r: %s at %d outside datastore (0-%d), ignored. "
                            "Do both config.yaml files match?",
                            self.name, kind, start, self.size - 1)
                continue
            if start + len(values) > self.size:
                log.warning("module %r: %s at %d truncated to fit datastore (0-%d)",
                            self.name, kind, start, self.size - 1)
                values = values[:self.size - start]
            datastore_write(self.context, self.unit, fc, start, values)

    def apply(self, msg):
        """Apply one decoded message. Returns True if accepted."""
        try:
            if not isinstance(msg, dict):
                raise ValueError("message is not an object")
            self._write_block(FC_HOLDING_REGISTERS, msg.get("registers", {}), "registers")
            self._write_block(FC_COILS, msg.get("coils", {}), "coils")
        except (ValueError, TypeError) as err:
            log.error("module %r: rejected malformed message: %s", self.name, err)
            return False
        seq = msg.get("seq")
        if isinstance(seq, int) and self.last_seq is not None and seq != self.last_seq + 1:
            log.debug("module %r: seq jumped %s -> %s (lost updates or input restart)",
                      self.name, self.last_seq, seq)
        self.last_seq = seq if isinstance(seq, int) else None
        if self._stale_logged:
            log.info("module %r: updates resumed", self.name)
            self._stale_logged = False
        self.last_update = time.monotonic()
        return True

    async def serve(self):
        bind = str(self.props.get("bind_out", "0.0.0.0"))
        log.info("module %r: Modbus server on %s:%s (unit %d, addresses 0-%d)",
                 self.name, bind, self.props["port_out"], self.unit, self.size - 1)
        await serve_tcp(self.context, make_identity(self.name), bind, self.props["port_out"])

    async def watch_staleness(self):
        """Warn when no update has arrived for `stale_after` seconds. The
        server keeps serving the last values: clients cannot tell they are
        old, so the log is the only signal."""
        started = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            ref = self.last_update if self.last_update is not None else started
            if not self._stale_logged and time.monotonic() - ref > self.stale_after:
                log.warning("module %r: no update for %.0fs, serving stale values",
                            self.name, time.monotonic() - ref)
                self._stale_logged = True


class _UdpReceiver(asyncio.DatagramProtocol):
    def __init__(self, mirror):
        self.mirror = mirror
        self.reassembler = common.Reassembler()

    def datagram_received(self, data, addr):
        payload = self.reassembler.feed(data)
        if payload is None:
            return
        try:
            msg = common.decode_json(payload)
        except ValueError:
            log.error("module %r: undecodable message", self.mirror.name)
            return
        self.mirror.apply(msg)


async def udp_output(name, props, cfg, mirror=None, serve=True):
    mirror = mirror or ModbusMirror(name, props)
    loop = asyncio.get_running_loop()
    listen = (cfg["network"]["out_ip"], props["port"])
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _UdpReceiver(mirror), local_addr=listen)
    log.info("module %r: listening for updates on %s:%s", name, *listen)
    try:
        tasks = [mirror.watch_staleness()]
        if serve:
            tasks.append(mirror.serve())
        await asyncio.gather(*tasks)
    finally:
        transport.close()


def run_udp_output(name, props, cfg):
    """DYODE v1 output agent. Runs forever."""
    common.setup_logging(cfg.get("_log_level", "INFO"), cfg)
    asyncio.run(udp_output(name, props, cfg))


def _serial_reader(serial_cfg, loop, on_line, stop):
    """Thread: keep the serial port open and hand complete lines to the loop.
    The port stays open, so no bytes are lost between reads (the original
    reopened it every second and lost whatever arrived meanwhile)."""
    link = _FailureLog("serial port %s" % serial_cfg["device"])
    buf = common.LineBuffer()
    ser = None
    while not stop.is_set():
        if ser is None:
            try:
                ser = _open_serial(serial_cfg, timeout=0.2)
                link.ok()
            except Exception as err:
                link.fail(err)
                stop.wait(5.0)
                continue
        try:
            data = ser.read(max(1, ser.in_waiting))
        except Exception as err:
            link.fail(err)
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            continue
        for line in buf.feed(data):
            loop.call_soon_threadsafe(on_line, line)


async def serial_output(modules, cfg, mirrors=None, serve=True):
    mirrors = mirrors or {n: ModbusMirror(n, p) for n, p in modules.items()}
    loop = asyncio.get_running_loop()

    def on_line(line):
        msg = common.serial_decode(line)
        if msg is None:
            log.warning("serial: damaged frame discarded (%d bytes)", len(line))
            return
        mirror = mirrors.get(msg.get("module")) if isinstance(msg, dict) else None
        if mirror is None:
            log.warning("serial: update for unknown module %r ignored",
                        msg.get("module") if isinstance(msg, dict) else None)
            return
        mirror.apply(msg)

    stop = threading.Event()
    reader = threading.Thread(target=_serial_reader, name="serial-reader",
                              args=(cfg["serial"], loop, on_line, stop), daemon=True)
    reader.start()
    try:
        tasks = [m.watch_staleness() for m in mirrors.values()]
        if serve:
            tasks += [m.serve() for m in mirrors.values()]
        await asyncio.gather(*tasks)
    finally:
        stop.set()


def run_serial_output(modules, cfg):
    """DYODE v2 output: one serial reader, one Modbus server per module."""
    asyncio.run(serial_output(modules, cfg))
