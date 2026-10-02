class ModbusSequentialDataBlock:
    def __init__(self, address, values):
        self.address = address
        self.values = list(values)
    def setValues(self, address, values):
        start = address - self.address
        if start < 0 or start + len(values) > len(self.values):
            raise IndexError("write outside block")
        self.values[start:start + len(values)] = values
    def getValues(self, address, count=1):
        start = address - self.address
        return self.values[start:start + count]


class ModbusDeviceContext:
    def __init__(self, co=None, hr=None):
        self.store = {1: co, 3: hr}
    def setValues(self, fc, address, values):
        self.store[fc].setValues(address + 1, values)   # mimic pymodbus' +1
    def getValues(self, fc, address, count=1):
        return self.store[fc].getValues(address + 1, count)


class ModbusServerContext:
    def __init__(self, devices, single=True):
        self.device = devices
    def __getitem__(self, unit):
        return self.device
