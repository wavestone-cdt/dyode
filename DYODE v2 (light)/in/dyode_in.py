# -*- coding: utf-8 -*-
import time
import sys
import yaml
import logging
import subprocess
import multiprocessing
import asyncore
import modbus
import os

# Logging
logging.basicConfig()
log = logging.getLogger()
log.setLevel(logging.DEBUG)

def launch_agents(module, properties):
    log.debug(module)
    log.debug(properties)
    if properties['type'] == 'folder':
        exit()
    elif properties['type'] == 'modbus':
        log.debug('Modbus agent : %s' % module)
        modbus.modbus_loop(module, properties)
    elif properties['type'] == 'screen':
        exit()


if __name__ == '__main__':
    with open('config.yaml', 'r') as config_file:
        config = yaml.load(config_file)

    # Log infos about the configuration file
    log.info('Loading config file')
    log.info('Configuration name : %s' % config['config_name'])
    log.info('Configuration version : %s' % config['config_version'])
    log.info('Configuration date : %s' % config['config_date'])
 
    # Iterate on modules
    modules = config.get('modules')
    for module, properties in modules.iteritems():
        log.debug('Parsing %s' % module)
        log.debug('Trying to launch a new process for module %s' % module)
        p = multiprocessing.Process(name=str(module), target=launch_agents, args=(module, properties))
        p.start()

    # TODO : Check if all modules are still alive and restart the ones that are not
