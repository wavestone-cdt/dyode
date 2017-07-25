# -*- coding: utf-8 -*-

from pymodbus.client.sync import ModbusTcpClient as ModbusClient
import os
import sys
import time
import logging
import random
import base64
import string
import yaml
import pickle
import struct
import multiprocessing
import serial
from twisted.internet.task import LoopingCall

#---------------------------------------------------------------------------#
# import the modbus libraries we need
#---------------------------------------------------------------------------#
from pymodbus.server.async import StartTcpServer
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.datastore import ModbusSequentialDataBlock
from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
from pymodbus.transaction import ModbusRtuFramer, ModbusAsciiFramer
from twisted.internet.task import LoopingCall
from ConfigParser import SafeConfigParser
import subprocess
#---------------------------------------------------------------------------#

# Logging
logging.basicConfig()
log = logging.getLogger()
log.setLevel(logging.DEBUG)

# Frequency at which to get modbus values
WAIT_TIME = 1

def get_modbus(properties):
    try:
        print "Performing an action which may throw an exception."
        client = ModbusClient(properties['ip'], port=502)
        client.connect()
        log.debug(properties['registers'])
        log.debug(properties['coils'])
        modbus_values = {}
        
        # Get holding registers values
        modbus_registers = {}
        for i in properties['registers']:
            register_start_nb = i.split('-')[0]
            register_end_nb = i.split('-')[1]
            log.debug('Register start number : %s' % register_start_nb)
            log.debug('Register end number : %s' % register_end_nb)
            register_count = int(register_end_nb) - int(register_start_nb)
            log.debug('Number of registers to read : %s' % register_count)
            rr = client.read_holding_registers(int(register_start_nb),register_count, unit=0x01)
            modbus_registers[register_start_nb] = rr.registers
            log.debug('Registers values : %s' % rr.registers)

        # Get coils values
        modbus_coils = {}
        for i in properties['coils']:
            coil_start_nb = i.split('-')[0]
            coil_end_nb = i.split('-')[1]
            log.debug('Coil start number : ' + register_start_nb)
            log.debug('Coil end number : ' + register_end_nb)
            coil_count = int(coil_end_nb) - int(coil_start_nb)
            log.debug('Number of coils to read : ' + str(coil_count))
            rr = client.read_coils(int(coil_start_nb),coil_count, unit=0x01)
            modbus_coils[coil_start_nb] = rr.bits
            log.debug('Coils values : ' + str(rr.bits))
            log.debug('Modbus coils values : ' + str(modbus_coils))

        client.close()
        modbus_values['registers'] = modbus_registers
        modbus_values['coils'] = modbus_coils
        log.debug(str(modbus_values))
        return modbus_values

    except Exception, error:
        log.debug('Error connecting to %s' % properties['ip'])
        log.debug(str(error))
def modbus_send_serial(data, properties):
    modbus_data = pickle.dumps(data)
    data_length = len(modbus_data)
    encoded_data = base64.b64encode(modbus_data)
    data_size = sys.getsizeof(encoded_data)
    log.debug('Data size : %s' % data_size)

    ser = serial.Serial(
        port='/dev/ttyAMA0',
        baudrate = 57600,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        bytesize=serial.EIGHTBITS,
        timeout=0.5
        )

    ser.write(encoded_data)
    log.debug(encoded_data)

def modbus_loop(module, properties):
    log.debug('Modbus Loop')
    while(1):
        try:
            data = get_modbus(properties)
            modbus_send_serial(data, properties)
        except Exception, error:
            log.debug('Error while updating modbus values')
            log.debug(str(error))
        time.sleep(WAIT_TIME)

