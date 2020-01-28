# DYODE : Do Your Own Diode

A DIY, low-cost data diode for ICS
This project aims at creating a working data diode for a fraction of the price of the commercial ones.

This project includes two versions of DYODE :
* DYODE full : a 19" rack-sized data diode

![dyodev1 picture](https://github.com/wavestone-cdt/dyode/blob/master/DYODE%20v1%20(full)/dyodev1.jpg)
* DYODE light : a very compact and ultra low-cost version with performance limitations

![dyodev2 picture](https://github.com/wavestone-cdt/dyode/blob/master/DYODE%20v2%20(light)/dyode_v2_final.JPG)

For detailed information, including steps to make your own, take a look at the [wiki](https://github.com/wavestone-cdt/dyode/wiki).
You may also take a look at the [public talks](https://github.com/wavestone-cdt/dyode/tree/master/Talks) done on this project.

## Hardware
The DYODE project is composed of 3 main parts:
* An INPUT counter
* A unidirectional, light-based data transfer mechanism
* An OUTPUT counter

The full version relies on optical-copper converters to transmit data, while the light version uses an optocoupler.

Hardware for DYODE light is open-source: PCB Gerber files are provided, as well as `.stl` files to 3D print the case.

## Software
DYODE is an open-source project developed in Python.

Features
* Modbus data transfer
* File transfer (*DYODE full only*)
* Screen sharing (*DYODE full only*)


## License
This project is published under GPLv3.
Take a look at [the full license](LICENSE).
