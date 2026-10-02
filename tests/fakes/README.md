Minimal stand-ins for pymodbus and pyserial, used ONLY by the unit tests so
they run without network access or hardware. They mimic the small API
surface DYODE uses (pymodbus 3.11-style names). They do not prove
compatibility with the real libraries: test on real hardware too.
