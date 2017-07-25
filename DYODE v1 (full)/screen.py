# -*- coding: utf-8 -*-

import sys
import time
import multiprocessing
import os
import logging
import struct
import pickle
import math
from socket import *
from BaseHTTPServer import BaseHTTPRequestHandler,HTTPServer
import asyncore
import pyinotify
import dyode

# Logging
logging.basicConfig()
log = logging.getLogger()
log.setLevel(logging.DEBUG)

def screen_file_copy(file, params):
    port = params[1]['port']
    buffer_size = 2048
    addr = ('10.0.1.2',port)

    f = open(file, 'rb')
    file_data = f.read()
    file_length = len(file_data)

    nb_of_packets = int(math.floor(file_length/(buffer_size - 4))) + 1
    s = socket(AF_INET,SOCK_DGRAM)

    for i in range(1, nb_of_packets + 1):
    	if (i < nb_of_packets):
    		data = file_data[((i-1)*(buffer_size-4)):(i*(buffer_size - 4))]
    		msg = struct.pack('>I', len(data)) + data
    	elif (i == nb_of_packets):
    		data = file_data[((i-1)*(buffer_size - 4)):file_length]
    		msg = struct.pack('>I', len(data)) + data
    	s.sendto(msg,addr)
    	time.sleep(0.0002)
    	i+= 1
    s.sendto('', addr)
    s.close()
    f.close()

class ScreenshotHandler(pyinotify.ProcessEvent):

    def process_IN_CLOSE_WRITE(self, event):
        current_process = multiprocessing.current_process()
        log.debug('New screenshot detected !')
        screen_file_copy(event.pathname, current_process._args)

def watch_folder(module, params):
    log.debug('Watching folder %s for screenshots ...' % params['in'])
    screen_wm = pyinotify.WatchManager()
    screen_mask = pyinotify.IN_CLOSE_WRITE
    screen_notifier = pyinotify.AsyncNotifier(screen_wm, ScreenshotHandler())
    screen_wdd = screen_wm.add_watch(params['in'], screen_mask, rec = True)
    asyncore.loop()

def get_screenshot(port):
    s = socket(AF_INET,SOCK_DGRAM)
    s.bind(('10.0.1.2',port))

    full_data = ''
    while True:
    	data, addr = s.recvfrom(2048)
    	if not data:
    		break
    	else:
    		msg_length = struct.unpack('>I', data[:4])[0]
    		full_data += data[4:(msg_length+4)]
    s.close()

    return full_data

class CamHandler(BaseHTTPRequestHandler):
  def do_GET(self):
    if self.path.endswith('.mjpg'):
      self.send_response(200)
      self.send_header('Content-type','multipart/x-mixed-replace; boundary=--jpgboundary')
      self.end_headers()
      try:
          while(True):
              image = get_screenshot(multiprocessing.current_process()._args[1]['port'])
              self.wfile.write("--jpgboundary")
              self.send_header('Content-type','image/jpeg')
              self.send_header('Content-length',len(image))
              self.end_headers()
              self.wfile.write(image)
              time.sleep(0.1)
      except KeyboardInterrupt:
        pass
      return
    else:
      self.send_response(200)
      self.send_header('Content-type','text/html')
      self.end_headers()
      self.wfile.write("""<html><head></head><body>
        <img src="/screen.mjpg"/>
      </body></html>""")
      return

def http_server(module, properties):
  try:
    server = HTTPServer(('',8080),CamHandler)
    server.serve_forever()
  except KeyboardInterrupt:
    server.socket.close()
