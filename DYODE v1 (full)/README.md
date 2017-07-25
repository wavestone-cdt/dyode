# DYODE : Do Your Own DyodE
A low-cost (~200 €) data diode aimed at Industrial Control Systems.

## Hardware
We use very standard hardware:
* 3 Copper-Optical converters
* 2 Raspberry Pis
* A few additional components, such as USB-Ethernet NICs, RJ45 cables, ...


## Software
We use ``udpcast`` to transfer files over a unidirectional channel. Modbsu and screen sharing work over a very simple Python UDP socket implementation.

## Features
At the moment, DYODE can be used for the following usages:
* File transfer
* Modbus data transfer
* Screen sharing

You can take a look at the video to learn more about the usage, and ``INSTALL.md`` for installation guidelines.
