# -*- coding: utf-8 -*-
"""DYODE v2 (light) - input side (Python 3 port).

Polls the PLC(s) and writes the values to the serial/optocoupler link.
Usage:  python3 dyode_in.py [-c config.yaml] [--log-level DEBUG]
"""

import logging

import dyode_common as common
import modbus

log = logging.getLogger("dyode")


def run_agent(modules, cfg):
    """Process entry point. Module-level so it can be pickled."""
    common.setup_logging(cfg["_log_level"], cfg)
    modbus.run_serial_input(modules, cfg)


def main():
    args = common.parse_args("DYODE v2 input side")
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
    for name, props in cfg["modules"].items():
        if props["type"] != "modbus":
            log.warning("module %r: type %r is not supported by DYODE v2; ignored",
                        name, props["type"])
    modules = common.modules_of_type(cfg, "modbus")
    if not modules:
        common.die("no modbus modules configured")
    # One process owns the serial port; the supervisor restarts it on a crash.
    common.supervise([("modbus-serial-in", run_agent, (modules, cfg))])


if __name__ == "__main__":
    main()
