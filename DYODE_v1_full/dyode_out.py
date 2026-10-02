# -*- coding: utf-8 -*-
"""DYODE v1 - output side (Python 3 port).

Usage:  python3 dyode_out.py [-c config.yaml] [--log-level DEBUG]
Serving Modbus on port 502 needs root (or CAP_NET_BIND_SERVICE).
"""

import logging

import dyode
import dyode_common as common
import modbus
import screen

log = logging.getLogger("dyode")

AGENTS = {
    "folder": dyode.run_folder_output,
    "modbus": modbus.run_udp_output,
    "screen": screen.run_screen_output,
}


def run_agent(name, props, cfg):
    """Process entry point. Module-level so it can be pickled."""
    common.setup_logging(cfg["_log_level"], cfg)
    AGENTS[props["type"]](name, props, cfg)


def main():
    args = common.parse_args("DYODE v1 output side")
    common.setup_logging(args.log_level)
    try:
        cfg = common.load_config(common.find_config(args.config, __file__))
    except (common.ConfigError, OSError) as err:
        common.die("configuration error: %s" % err)
    cfg["_log_level"] = args.log_level
    cfg["_side"] = "out"
    common.setup_logging(args.log_level, cfg)

    log.info("configuration %r, version %s, dated %s", cfg.get("config_name"),
             cfg.get("config_version"), cfg.get("config_date"))
    net = cfg["network"]
    log.info("receiving from %s on %s (%s)", net["in_ip"], net["out_ip"],
             net["out_interface"])

    targets = [(name, run_agent, (name, props, cfg))
               for name, props in cfg["modules"].items()]
    common.supervise(targets)


if __name__ == "__main__":
    main()
