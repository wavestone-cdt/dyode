# -*- coding: utf-8 -*-

from pymodbus.client.sync import ModbusTcpClient as ModbusClient
import os
import sys
import time
import logging
import random
import string
import yaml
import base64
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

def get_modbus_data_serial():
    ser = serial.Serial(
        port='/dev/ttyAMA0',
        baudrate = 57600,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        bytesize=serial.EIGHTBITS,
        timeout=1
        )

    x=ser.readline()
    if len(x) > 0:
        decoded_data = base64.b64decode(x)
        log.debug(pickle.loads(decoded_data))
	return pickle.loads(decoded_data)

def modbus_master_update(a):
    log.debug('Updating : %s' % a[0])
    log.debug(str(a[1]))
    log.debug(str(a[1]['registers']))

    try:
        data = get_modbus_data_serial()
        for coil_start, values in data['coils'].iteritems():
            log.debug('Coil start at : %s with values : %s' % (coil_start, values))
            a[2][0x01].setValues(1, int(coil_start), values)
        for register_start, values in data['registers'].iteritems():
            log.debug('Register start at : %s with values : %s' % (register_start, values))
            a[2][0x01].setValues(3, int(register_start), values)
    except Exception, error:
        log.error('Error obtaining Modbus values')
        log.error(str(error))
        return 0


def modbus_master(module, properties):
    log.debug('Modbus master module : '  + str(module))
    # Modbus Master
    #--------------------------------------------------------------------------#
    # initialize your data store
    #--------------------------------------------------------------------------#
    store = ModbusSlaveContext(
        co = ModbusSequentialDataBlock(0, [0]*100),
        hr = ModbusSequentialDataBlock(0, [0]*100))
    context = ModbusServerContext(slaves=store, single=True)

    #--------------------------------------------------------------------------#
    # initialize the server information
    #--------------------------------------------------------------------------#
    identity = ModbusDeviceIdentification()
    identity.VendorName  = 'ASO+AKO'
    identity.ProductCode = 'DYODEv2'
    identity.VendorUrl   = 'http://github.com/wavestone-cdt/dyode'
    identity.ProductName = 'DYODE'
    identity.ModelName   = 'Very Low Cost @ BlackHat Arsenal'
    identity.MajorMinorRevision = '1.0'

    #--------------------------------------------------------------------------#
    # run the server you want
    #--------------------------------------------------------------------------#
    time = 1 # 1 seconds delay
    loop = LoopingCall(f=modbus_master_update, a=(module, properties, context))
    loop.start(time, now=False) # initially delay by time
    StartTcpServer(context, identity=identity, address=("0.0.0.0", \
                   properties['port_out']))
