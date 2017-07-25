# Install notes for DYODE

## Hardware setup
DYODE is composed of two hardware types:
* 2 x  Computers (for example Raspberry Pis)
* 3 x Copper/Optical converters (TP-LINK MC100CM for example)
* Additional NICs (3 per computer are needed)
* 2 optical cables
* Some RJ-45 cables

## Software setup
DYODE is developed in Python and heavily relies on open-source libraries.

First of all, you'll need to install [udpcast](https://www.udpcast.linux.lu/), which is the tool used to transfer files through the diode.

Then, you'll require Python 2 and the following modules:
* pymodbus
* pyinotify
* YAML
* asyncore

### Configuration file
Configuration is based on a YAML file, which must be copied to both input and output diodes.

Below is an example of such a configuration file
```yaml
config_name: "Dyode test"
config_version : 1.0
config_date: 2016-05-04

dyode_in:
  ip: 10.0.1.1
  mac: b8:27:eb:89:1e:f3
dyode_out:
  ip: 10.0.1.2
  mac: b8:27:eb:b1:ff:ab

modules:
  "Partage de fichier 1":
     type: folder
     port: 9600
     in: /home/pi/in
     out: /home/pi/out
  "Partage 2":
     type: folder
     port: 9700
     in: /home/pi/in2
     out: /home/pi/out2
  "Automate Modbus 1":
     type: Modbus
     port: 9400
     ip: 192.168.1.150
     port_out: 502
     registers:
       - 0-100
       - 400-450
     coils:
       - 0-10
       - 100-110
  "Automate Modbus 2":
    type: modbus
    port: 9500
    ip: 127.0.0.1
    port_out: 503
    registers:
      - 0-10
      - 400-402
    coils:
      - 0-10
      - 100-110
  "Partage d'ecran presta":
     type: screen
     port: 9900
     in: /home/pi/screenz
     out: /home/pi/screenz
```
It's supposed to be straight-forward.
Each entry is defined by its type (folder, Modbus or screen), and some properties:
* port is the base port that will be used by udpcast to send the data
* in is the path of the folder on the input diode where the files to be transfered are located
* out is the path on the output diode where the received files will be stored
* for modbus, you need to specify the coils and registers range that you want to transfer


Copy all files in the two computers. To launch the diode, launch :
* dyode_in.py on the input computer
* dyode_out.py on the output computer
