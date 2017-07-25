# -*- coding: utf-8 -*-

from pymodbus.client.sync import ModbusTcpClient as ModbusClient
import os
import sys
import time
import logging
import random
import string
import yaml
import pickle
import struct
import multiprocessing
import math
from socket import *
from twisted.internet.task import LoopingCall
import dyode

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

def modbus_send(data, properties):
    port = properties['port']
    buffer_size = 2048
    addr = ('10.0.1.2',port)
    modbus_data = pickle.dumps(data)
    data_length = len(modbus_data)

    nb_of_packets = int(math.floor(data_length/(buffer_size - 4))) + 1
    s = socket(AF_INET,SOCK_DGRAM)

    for i in range(1, nb_of_packets + 1):
    	if (i < nb_of_packets):
    		data = modbus_data[((i-1)*(buffer_size-4)):(i*(buffer_size - 4))]
    		msg = struct.pack('>I', len(data)) + data
    	elif (i == nb_of_packets):
    		data = modbus_data[((i-1)*(buffer_size - 4)):data_length]
    		msg = struct.pack('>I', len(data)) + data
    	s.sendto(msg,addr)
    	time.sleep(0.0002)
    	i+= 1
    s.sendto('', addr)
    s.close()

def modbus_loop(module, properties):
    while(1):
        try:
            data = get_modbus(properties)
            modbus_send(data, properties)
        except Exception, error:
            log.debug('Error while updating modbus values')
        time.sleep(WAIT_TIME)

def get_modbus_data(port):
    s = socket(AF_INET,SOCK_DGRAM)
    s.bind(('10.0.1.2',port))

    full_data = ''
    i = 0
    while True:
    	data, addr = s.recvfrom(2048)
    	if not data:
    		break
    	else:
    		msg_length = struct.unpack('>I', data[:4])[0]
    		full_data += data[4:(msg_length+4)]
    		i+=1

    s.close()
    return pickle.loads(full_data)

def modbus_master_update(a):
    log.debug('Updating : %s' % a[0])
    log.debug(str(a[1]))
    log.debug(str(a[1]['registers']))

    try:
        data = get_modbus_data(multiprocessing.current_process()._args[1]['port'])
        for coil_start, values in data['coils'].iteritems():
            log.debug('Coil start at : %s with values : %s' % (coil_start, values))
            a[2][0x01].setValues(1, int(coil_start), values)
        for register_start, values in data['registers'].iteritems():
            log.debug('Register start at : %s with values : %s' % (register_start, values))
            a[2][0x01].setValues(3, int(register_start), values)
    except Exception, error:
        log.error('Error obtaining Modbus values')
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
    identity.ProductCode = 'DYODE'
    identity.VendorUrl   = 'yoloswag'
    identity.ProductName = 'DYODE'
    identity.ModelName   = 'BSides LV release'
    identity.MajorMinorRevision = '0.9'

    #--------------------------------------------------------------------------#
    # run the server you want
    #--------------------------------------------------------------------------#
    time = 1 # 5 seconds delay
    loop = LoopingCall(f=modbus_master_update, a=(module, properties, context))
    loop.start(time, now=False) # initially delay by time
    StartTcpServer(context, identity=identity, address=("0.0.0.0", \
                   properties['port_out']))
