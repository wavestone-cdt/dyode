# -*- coding: utf-8 -*-
"""DYODE v1 - input side (Python 3 port).

Usage:  python3 dyode_in.py [-c config.yaml] [--log-level DEBUG]
Setting the static ARP entry needs root; everything else does not.
"""

import logging

import dyode
import dyode_common as common
import modbus
import logstream
import screen

log = logging.getLogger("dyode")

# Empirical udpcast ceiling through the optical link, shared by all
# folder modules (Modbus and screen modules use their own UDP sockets).
MAX_BITRATE_MBPS = 8

AGENTS = {
    "folder": dyode.run_folder_input,
    "modbus": modbus.run_udp_input,
    "screen": screen.run_screen_input,
    "logs": logstream.run_logs_input,
}


def run_agent(name, props, cfg):
    """Process entry point. Module-level so it can be pickled."""
    common.setup_logging(cfg["_log_level"], cfg)
    AGENTS[props["type"]](name, props, cfg)


def set_static_arp(net):
    """Kept for backwards compatibility; the real work is in dyode.py so it
    can also run as a supervised keeper process."""
    return dyode.set_static_arp(net)


def main():
    args = common.parse_args("DYODE v1 input side")
    common.setup_logging(args.log_level)
    try:
        cfg = common.load_config(common.find_config(args.config, __file__))
    except (common.ConfigError, OSError) as err:
        common.die("configuration error: %s" % err)
    cfg["_log_level"] = args.log_level
    cfg["_side"] = "in"
    common.setup_logging(args.log_level, cfg)

    log.info("configuration %r, version %s, dated %s", cfg.get("config_name"),
             cfg.get("config_version"), cfg.get("config_date"))
    net = cfg["network"]
    log.info("input %s (%s) -> output %s (%s)", net["in_ip"], net["in_interface"],
             net["out_ip"], net["out_mac"] or "MAC not set")

    # Refuse to come up pretending to work.  Without the ARP entry every
    # transfer is silently discarded by our own kernel while udp-sender
    # still exits 0, so a service that starts before the diode NIC is
    # configured looks healthy and moves nothing.  Exiting lets systemd's
    # Restart=always retry until the interface is ready.
    if net.get("out_mac") and not dyode.set_static_arp(net):
        common.die("cannot set the static ARP entry for %s on %s; refusing to "
                   "start, because nothing would reach the output side"
                   % (net["out_ip"], net["in_interface"]))

    folders = common.modules_of_type(cfg, "folder")
    if folders:
        share = max(1, MAX_BITRATE_MBPS // len(folders))
        for props in folders.values():
            props.setdefault("bitrate", share)

    targets = [(name, run_agent, (name, props, cfg))
               for name, props in cfg["modules"].items()]
    if net.get("out_mac"):
        targets.append(("arp-keeper", dyode.run_arp_keeper,
                        (net, float(net.get("arp_interval", 60.0)))))
    common.supervise(targets)


if __name__ == "__main__":
    main()
