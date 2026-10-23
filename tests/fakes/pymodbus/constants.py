import enum


class ExcCodes(enum.IntEnum):
    ILLEGAL_FUNCTION = 0x01
    ILLEGAL_ADDRESS = 0x02
    ILLEGAL_VALUE = 0x03
    DEVICE_FAILURE = 0x04
