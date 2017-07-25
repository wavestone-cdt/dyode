# -*- coding: utf-8 -*-

# Imports
import asyncore
import pyinotify
import string
import random
import shutil
import threading
import ConfigParser
from ConfigParser import SafeConfigParser
import sys, time, subprocess, os
import logging
import hashlib
from math import floor
import os
import datetime
import multiprocessing
import shlex

# Logging stuff
logging.basicConfig()
global log
log = logging.getLogger()
log.setLevel(logging.DEBUG)




######################## Reception specific funcitons ##########################

def parse_manifest(file):
    parser = SafeConfigParser()
    parser.read(file)
    files = {}
    for item, value in parser.items('Files'):
        log.debug(item + ' :: ' + value)
        files[item] = value
    return files

# File reception forever loop
def file_reception_loop(params):
    while(1):
        wait_for_file(params)


        time.sleep(10)

# Launch UDPCast to receive a file
def receive_file(filepath, portbase):
    log.debug(portbase)
    command = 'udp-receiver --nosync --mcast-rdv-addr 10.0.1.1 --interface eth1 --portbase ' + str(portbase) + ' -f ' + '\'' + filepath + '\''
    log.debug(command)
    #p = subprocess.Popen(command, stdout=subprocess.PIPE, shell=True)
    p = subprocess.Popen(shlex.split(command), shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, err = p.communicate()


# File reception function
def wait_for_file(params):
    log.debug('Waiting for file ...')
    log.debug(datetime.datetime.now())
    # YOLO
    log.debug(params)
    # Use a dedicated name for each process to prevent race conditions
    process_name = multiprocessing.current_process().name
    manifest_filename = 'manifest_' + process_name + '.cfg'
    receive_file(manifest_filename, params['port'])
    files = parse_manifest(manifest_filename)
    if len(files) == 0:
        log.error('No file detected')
        return 0
    log.debug('Manifest content : %s' % files)
    for f in files:
        filename = os.path.basename(f)
        temp_file = ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(12))
        receive_file(temp_file, params['port'])
        log.info('File ' + f + ' received')
	log.debug(datetime.datetime.now())
	hash_file(temp_file)
        if hash_file(temp_file) != files[f]:
            log.error('Invalid checksum for file ' + f)
            os.remove(f)
            log.error('Calculating next file hash...')
            continue
        else:
            log.info('Hashes match !')
	    shutil.move(temp_file, params['out'] + '/' + filename)
	    log.info('File ' + filename + ' available at ' + params['out'])
    os.remove(manifest_filename)



################### Send specific functions ####################################

# Send a file using udpcast
def send_file(file, port_base, max_bitrate):
    command = 'udp-sender --async --fec 8x16/64 --max-bitrate ' \
                 + str('{:0.0f}'.format(max_bitrate)) \
                 + 'm --mcast-rdv-addr 10.0.1.2 --mcast-data-addr 10.0.1.2 ' \
                 + '--portbase ' + str(port_base) + ' --autostart 1 ' \
                 + '--interface eth0 -f ' + '\'' + str(file) + '\''
    log.debug(command)
    p = subprocess.Popen(command, stdout=subprocess.PIPE, shell=True)
    (output, err) = p.communicate()


# List all files recursively
def list_all_files(dir):
    files = []
    for root, directories, filenames in os.walk(dir):
        for directory in directories:
            files.append(os.path.join(root, directory))
        for filename in filenames:
            files.append(os.path.join(root,filename))

    return files

# TODO : Adapt to YAML-parsed params
def parse_config():
    parser = SafeConfigParser()
    parser.read('sample.folder')
    params = {}
    for item, value in parser.items('folder'):
        log.debug(item + '::' + value)
        params[item] = value
    return params

def write_manifest(files, manifest_filename):
    config = ConfigParser.RawConfigParser()
    config.add_section('Files')
    log.debug('Files...')
    log.debug(files)
    for f in files:
        config.set('Files', f, files[f])
        log.debug(f + ' :: ' + files[f])

    with open(manifest_filename, 'wb') as configfile:
        config.write(configfile)

def file_copy(params):
    log.debug('Local copy starting ...')

    files =  list_all_files(params[1]['in'])
    log.debug('List of files : ' + str(files))
    if len(files) == 0:
        log.debug('No file detected')
        return 0
    manifest_data = {}

    for f in files:
        manifest_data[f] = hash_file(f)
    log.debug('Writing manifest file')
    # Use a dedicated name for each process to prevent race conditions
    manifest_filename = 'manifest_' + str(params[0]) + '.cfg'
    write_manifest(manifest_data, manifest_filename)
    log.info('Sending manifest file : ' + manifest_filename)

    send_file(manifest_filename, params[1]['port'], params[1]['bitrate'])
    log.debug('Deleting manifest file')
    os.remove(manifest_filename)
    for f in files:
        log.info('Sending ' + f)
        send_file(f, params[1]['port'], params[1]['bitrate'])
        log.info('Deleting ' + f)
        os.remove(f)


########################### Shared functions ###################################

def hash_file(file):
    BLOCKSIZE = 65536
    hasher = hashlib.sha256()
    with open(file, 'rb') as afile:
        buf = afile.read(BLOCKSIZE)
        while len(buf) > 0:
            hasher.update(buf)
            buf = afile.read(BLOCKSIZE)

    return hasher.hexdigest()
