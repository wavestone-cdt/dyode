#!/usr/bin/env python3
"""Generate DYODE's nftables rules from a config.yaml (used by install.sh).

    python3 lib/dyode_firewall.py CONFIG          print the ruleset
    python3 lib/dyode_firewall.py CONFIG --ports  print the ports, one line

The rules keep the diode ports for traffic from the input box only: a UDP
datagram for one of them is dropped unless it arrives on the diode NIC
(dyode_out.interface) from dyode_in.ip.  Every other port and rule on the box
is left alone.  Linux accepts a packet for any local address on any interface,
so without this a host on the IT network could reach the output box's diode
ports through its IT-facing NIC.

The config is loaded with DYODE's own dyode_common.load_config() -- it sits
beside config.yaml in every deployable folder -- so the rules always match
what DYODE itself listens on.
"""

import ipaddress
import os
import re
import sys

IFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")


def load(config_path):
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:0] = [os.path.dirname(os.path.abspath(config_path)),
                    os.path.join(here, "..", "DYODE_v1_full")]
    import dyode_common
    return dyode_common.load_config(config_path)


def diode_ports(cfg):
    """Every UDP port a module receives on through the diode."""
    ports = set()
    for props in cfg["modules"].values():
        port = int(props["port"])
        ports.add(port)
        if props["type"] == "folder":
            ports.add(port + 1)          # udpcast uses portbase and portbase+1
    return sorted(p for p in ports if 0 < p < 65536)


def ruleset(cfg):
    net = cfg["network"]
    iface = net["out_interface"]
    if not IFACE_RE.match(iface):
        raise ValueError("unsafe interface name %r" % iface)
    in_ip = str(ipaddress.IPv4Address(net["in_ip"]))
    ports = diode_ports(cfg)
    if not ports:
        raise ValueError("no diode ports in the configuration")
    plist = ", ".join(str(p) for p in ports)
    return """\
# Managed by DYODE install.sh (rewritten on each run; --no-firewall to skip).
# Diode ports {plist}: accepted only on {iface} from the input box {in_ip}.
# Everything else on this box is untouched.  See CAVEATS.md.
table inet dyode
delete table inet dyode
table inet dyode {{
	chain input {{
		type filter hook input priority -10; policy accept;
		meta nfproto ipv6 udp dport {{ {plist} }} counter drop comment "DYODE: diode ports are IPv4 only"
		udp dport {{ {plist} }} iifname != "{iface}" counter drop comment "DYODE: diode ports only on the diode NIC"
		udp dport {{ {plist} }} ip saddr != {in_ip} counter drop comment "DYODE: diode ports only from the input box"
	}}
}}
""".format(plist=plist, iface=iface, in_ip=in_ip)


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        cfg = load(argv[1])
        if "--ports" in argv:
            print(" ".join(str(p) for p in diode_ports(cfg)))
        elif "--interface" in argv:
            print(cfg["network"]["out_interface"])
        else:
            sys.stdout.write(ruleset(cfg))
    except Exception as err:            # reported by install.sh
        print("error: %s" % err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
