class _Resp:
    def __init__(self, registers=None, bits=None, error=False):
        self.registers = registers or []
        self.bits = bits or []
        self._error = error
    def isError(self):
        return self._error
    def __repr__(self):
        return "<FakeResp error=%s>" % self._error


class ModbusTcpClient:
    """Fake PLC client. `memory` maps address -> value for registers and coils."""
    registers = {}
    coils = {}
    fail_connect = False
    calls = []

    def __init__(self, host, port=502, timeout=3):
        self.host, self.port = host, port
        self.connected = False

    def connect(self):
        self.connected = not type(self).fail_connect
        return self.connected

    def close(self):
        self.connected = False

    def read_holding_registers(self, address, *, count=1, device_id=1):
        type(self).calls.append(("hr", address, count, device_id))
        if count > 125:
            return _Resp(error=True)
        return _Resp(registers=[type(self).registers.get(a, 0) for a in range(address, address + count)])

    def read_coils(self, address, *, count=1, device_id=1):
        type(self).calls.append(("co", address, count, device_id))
        bits = [type(self).coils.get(a, False) for a in range(address, address + count)]
        bits += [False] * (-len(bits) % 8)          # real pymodbus pads to 8
        return _Resp(bits=bits)
