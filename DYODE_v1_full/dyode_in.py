# -*- coding: utf-8 -*-
"""DYODE v1 - input side (Python 3 port).

Usage:  python3 dyode_in.py [-c config.yaml] [--log-level DEBUG]
Setting the static ARP entry needs root; everything else does not.
"""

import logging
import subprocess

import dyode
import dyode_common as common
import modbus
import screen

log = logging.getLogger("dyode")

# Empirical udpcast ceiling through the optical link, shared by all
# folder modules (Modbus and screen modules use their own UDP sockets).
MAX_BITRATE_MBPS = 8

AGENTS = {
    "folder": dyode.run_folder_input,
    "modbus": modbus.run_udp_input,
    "screen": screen.run_screen_input,
}


def run_agent(name, props, cfg):
    """Process entry point. Module-level so it can be pickled."""
    common.setup_logging(cfg["_log_level"], cfg)
    AGENTS[props["type"]](name, props, cfg)


def set_static_arp(net):
    """Nothing can answer ARP through a one-way link, so the output box's
    MAC must be pinned. Uses iproute2 (net-tools' `arp` is not installed by
    default on current Debian / Raspberry Pi OS)."""
    if not net.get("out_mac"):
        log.warning("no dyode_out.mac in config: skipping static ARP entry")
        return
    cmd = ["ip", "neigh", "replace", net["out_ip"], "lladdr", net["out_mac"],
           "dev", net["in_interface"], "nud", "permanent"]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        log.error("'ip' command not found: install iproute2")
        return
    if res.returncode == 0:
        log.info("static ARP: %s -> %s on %s", net["out_ip"], net["out_mac"],
                 net["in_interface"])
    else:
        log.error("static ARP failed (run as root?): %s",
                  res.stderr.decode(errors="replace").strip())


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
    set_static_arp(net)

    folders = common.modules_of_type(cfg, "folder")
    if folders:
        share = max(1, MAX_BITRATE_MBPS // len(folders))
        for props in folders.values():
            props.setdefault("bitrate", share)

    targets = [(name, run_agent, (name, props, cfg))
               for name, props in cfg["modules"].items()]
    common.supervise(targets)


if __name__ == "__main__":
    main()
