#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DYODE installer / configuration wizard.

    python3 dyode_setup.py               curses interface
    python3 dyode_setup.py --plain       plain text (also used automatically
                                         when the terminal cannot run curses)

It asks which side of the diode this box is, which network interface faces
the diode, reads that interface's MAC address itself, and writes a
config.yaml that the runtime validates before anything is saved.
"""

import argparse
import os
import sys

import dyode_setup_core as core


# ==========================================================================
# User interfaces
# ==========================================================================

class PlainUi:
    """Plain question-and-answer, for pipes, logs and terminals without curses."""

    def __init__(self, stream=None, stdin=None):
        self.out = stream or sys.stdout
        self.stdin = stdin or sys.stdin

    def _w(self, text=""):
        self.out.write(text + "\n")
        self.out.flush()

    def _read(self, prompt):
        self.out.write(prompt)
        self.out.flush()
        line = self.stdin.readline()
        if not line:
            raise EOFError
        return line.rstrip("\n")

    def intro(self, title, body):
        self._w("\n=== %s ===" % title)
        for line in body:
            self._w(line)

    def info(self, title, body):
        self.intro(title, body)

    def error(self, message):
        self._w("  ! %s" % message)

    def choose(self, title, body, options):
        while True:
            self.intro(title, body)
            for i, (label, desc) in enumerate(options, 1):
                self._w("  %d) %s%s" % (i, label, "   - " + desc if desc else ""))
            answer = self._read("Choice [1]: ").strip() or "1"
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            self.error("Enter a number between 1 and %d." % len(options))

    def text(self, title, label, default="", validator=None, help_text=""):
        self.intro(title, [help_text] if help_text else [])
        while True:
            prompt = "%s%s: " % (label, " [%s]" % default if default else "")
            answer = self._read(prompt).strip() or default
            if validator is None or validator(answer):
                return answer
            self.error("That value is not valid.")

    def confirm(self, title, body, default=True):
        self.intro(title, body)
        answer = self._read("[%s] " % ("Y/n" if default else "y/N")).strip().lower()
        if not answer:
            return default
        return answer.startswith("y")


class CursesUi:
    """Full-screen curses interface: arrow keys or j/k, Enter to accept."""

    def __init__(self, screen):
        import curses
        self.curses = curses
        self.screen = screen
        curses.curs_set(0)
        self.colors = False
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)     # header
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selection
            curses.init_pair(3, curses.COLOR_YELLOW, -1)   # warnings
            self.colors = True
        except self.curses.error:
            pass

    # -- drawing helpers ---------------------------------------------------

    def _pair(self, n):
        return self.curses.color_pair(n) if self.colors else 0

    def _wrap(self, body, width):
        import textwrap
        lines = []
        for para in body:
            lines += textwrap.wrap(para, width) if para else [""]
        return lines

    def _frame(self, title, body):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        inner = max(20, width - 4)
        self._put(0, 1, "DYODE setup - %s" % title, self._pair(1) | self.curses.A_BOLD)
        self._put(1, 1, "-" * min(inner, width - 2))
        row = 3
        for line in self._wrap(body, inner):
            if row >= height - 2:
                break
            self._put(row, 2, line)
            row += 1
        return row + 1, height, inner

    def _put(self, row, col, text, attr=0):
        height, width = self.screen.getmaxyx()
        if 0 <= row < height:
            try:
                self.screen.addnstr(row, col, text, max(0, width - col - 1), attr)
            except self.curses.error:
                pass

    def _footer(self, text):
        height, _ = self.screen.getmaxyx()
        self._put(height - 1, 1, text, self._pair(1))

    # -- interface ---------------------------------------------------------

    def intro(self, title, body):
        self._frame(title, body)
        self._footer("Enter to continue, q to quit")
        self.screen.refresh()
        while True:
            key = self.screen.getch()
            if key in (ord("q"), ord("Q")):
                raise KeyboardInterrupt
            if key in (self.curses.KEY_ENTER, 10, 13, ord(" ")):
                return

    def info(self, title, body):
        self.intro(title, body)

    def error(self, message):
        self.intro("Problem", [message])

    def choose(self, title, body, options):
        index = 0
        while True:
            row, height, _ = self._frame(title, body)
            for i, (label, desc) in enumerate(options):
                if row + i >= height - 2:
                    break
                mark = ">" if i == index else " "
                attr = self._pair(2) if i == index else 0
                text = "%s %s" % (mark, label)
                if desc:
                    text += "   %s" % desc
                self._put(row + i, 2, text.ljust(max(10, self.screen.getmaxyx()[1] - 4)), attr)
            self._footer("Up/Down to move, Enter to select, q to quit")
            self.screen.refresh()
            key = self.screen.getch()
            if key in (self.curses.KEY_DOWN, ord("j")):
                index = (index + 1) % len(options)
            elif key in (self.curses.KEY_UP, ord("k")):
                index = (index - 1) % len(options)
            elif key in (self.curses.KEY_ENTER, 10, 13):
                return index
            elif key in (ord("q"), ord("Q")):
                raise KeyboardInterrupt
            elif ord("1") <= key <= ord("9") and key - ord("1") < len(options):
                return key - ord("1")

    def text(self, title, label, default="", validator=None, help_text=""):
        buf = list(default)
        message = ""
        self.curses.curs_set(1)
        try:
            while True:
                body = [help_text] if help_text else []
                if message:
                    body.append(message)
                row, _, _ = self._frame(title, body)
                self._put(row, 2, "%s: %s" % (label, "".join(buf)))
                self._footer("Enter to accept, Ctrl-U to clear, q at an empty field to quit")
                self.screen.move(row, min(len(label) + 4 + len(buf),
                                          self.screen.getmaxyx()[1] - 2))
                self.screen.refresh()
                key = self.screen.getch()
                if key in (self.curses.KEY_ENTER, 10, 13):
                    value = "".join(buf).strip()
                    if validator is None or validator(value):
                        return value
                    message = "That value is not valid."
                elif key in (self.curses.KEY_BACKSPACE, 127, 8):
                    if buf:
                        buf.pop()
                elif key == 21:                       # Ctrl-U
                    buf = []
                elif key in (ord("q"), ord("Q")) and not buf:
                    raise KeyboardInterrupt
                elif 32 <= key <= 126:
                    buf.append(chr(key))
        finally:
            self.curses.curs_set(0)

    def confirm(self, title, body, default=True):
        options = [("Yes", ""), ("No", "")]
        return self.choose(title, body, options if default else options[::-1]) == (
            0 if default else 1)


# ==========================================================================
# The wizard itself (user interface agnostic)
# ==========================================================================

class Cancelled(Exception):
    pass


def run_wizard(ui, args):
    ui.intro("Welcome", [
        "This sets up one box of a DYODE data diode.",
        "",
        "Each box needs the same config.yaml. The wizard reads this box's MAC "
        "address itself; the other box's address has to come from the other box, "
        "so set up one, copy its config.yaml over, and import it here.",
        "",
        "Nothing is written until you confirm at the end.",
    ])

    variant = args.variant or ["v1", "v2"][ui.choose(
        "Which DYODE", ["Which version of the hardware is this?"],
        [("DYODE v1 (full)", "Ethernet + optical link: files, Modbus, screen"),
         ("DYODE v2 (light)", "optocoupler serial link: Modbus only")])]

    side = args.side or ["in", "out"][ui.choose(
        "Which side", ["Which side of the diode is this box?"],
        [("Input (sending)", "connected to the sensitive network, sends data out"),
         ("Output (receiving)", "connected to the less trusted network, serves data")])]

    model = _load_or_new(ui, args, variant)

    if variant == "v1":
        _ask_ethernet(ui, model, side, args)
    else:
        _ask_serial(ui, model, args)

    _edit_modules(ui, model, variant)

    workdir = core.target_dir(variant, side, args.repo)
    plan = core.Plan(model, side, workdir)
    plan.unit_path = os.path.join(workdir, core.unit_name(side))
    plan.write_unit = ui.confirm("Start at boot", [
        "Generate a systemd service file for this side?",
        "It restarts DYODE if it crashes and sends its logs to the journal.",
        "The wizard writes it into the DYODE folder; installing it into "
        "/etc/systemd/system needs root and is shown as a command at the end.",
    ])
    return plan


def _load_or_new(ui, args, variant):
    if args.import_config:
        return _import_file(ui, args.import_config, variant)
    choice = ui.choose("Starting point", [
        "Is this the first box you are setting up?"], [
        ("Start a new configuration", "this is the first box"),
        ("Import config.yaml from the other box", "recommended for the second box")])
    if choice == 0:
        model = core.ConfigModel(variant)
        model.name = ui.text("Name", "A name for this diode", model.name,
                             lambda t: bool(t.strip()))
        return model
    while True:
        path = ui.text("Import", "Path to the other box's config.yaml", "",
                       help_text="For example /media/usb/config.yaml. Its modules "
                                 "and the other box's details are kept.")
        try:
            return _import_file(ui, path, variant)
        except (OSError, ValueError) as err:
            ui.error("Could not import that file: %s" % err)


def _import_file(ui, path, variant):
    with open(path, encoding="utf-8") as fh:
        model = core.ConfigModel.from_yaml_text(fh.read(), variant)
    ui.info("Imported", ["Read %s" % path,
                         "%d module(s): %s" % (len(model.modules),
                                               ", ".join(m.name for m in model.modules))])
    return model


def _ask_ethernet(ui, model, side, args):
    ifaces = core.list_interfaces(args.sys_root, include_virtual=args.all_interfaces)
    if not ifaces:
        ifaces = core.list_interfaces(args.sys_root, include_virtual=True)
    if not ifaces:
        raise Cancelled("No network interfaces found.")
    options = [(i.label(), "") for i in ifaces] + [("Enter a name by hand", "")]
    index = ui.choose("Diode interface", [
        "Which interface on THIS box faces the diode (the optical converter)?",
        "Its MAC address goes into the configuration automatically."],
        options)
    if index == len(ifaces):
        name = ui.text("Interface", "Interface name", "", lambda t: bool(t.strip()))
        mac = ui.text("MAC address", "Its MAC address", "", core.valid_mac)
    else:
        name, mac = ifaces[index].name, ifaces[index].mac
        if not core.valid_mac(mac or ""):
            mac = ui.text("MAC address", "MAC address of %s" % name, "", core.valid_mac,
                          help_text="This interface did not report a usable address.")

    default_ip = model.in_ip if side == "in" else model.out_ip
    ip = ui.text("Address", "Diode-side IP address for this box", default_ip,
                 core.valid_ipv4,
                 help_text="A private address on the point-to-point link. "
                           "The default pair is 10.0.1.1 and 10.0.1.2.")
    model.set_local(side, name, mac.lower(), ip)
    ui.info("This box", ["Side: %s" % core.side_word(side),
                         "Interface: %s" % name, "MAC: %s" % mac.lower(), "IP: %s" % ip])

    other = core.peer_side(side)
    if model.peer_complete(side):
        peer_mac = model.out_mac if other == "out" else model.in_mac
        peer_ip = model.out_ip if other == "out" else model.in_ip
        ui.info("The other box", ["Already known from the imported file:",
                                  "MAC: %s" % peer_mac, "IP: %s" % peer_ip])
        return
    peer_ip = ui.text("The other box", "IP address of the %s box" % (
        "output" if other == "out" else "input"),
        model.out_ip if other == "out" else model.in_ip, core.valid_ipv4)
    peer_mac = ui.text("The other box", "Its MAC address (leave empty to fill in later)",
                       "", lambda t: t == "" or core.valid_mac(t),
                       help_text="Needed for the static ARP entry, since nothing can "
                                 "answer ARP through a one-way link. If you do not "
                                 "know it yet, set up the other box and import its "
                                 "config.yaml here afterwards.")
    peer_if = ui.text("The other box", "Its diode-facing interface name",
                      model.out_if if other == "out" else model.in_if,
                      lambda t: bool(t.strip()))
    model.set_peer(side, peer_if, peer_mac.lower(), peer_ip)


def _ask_serial(ui, model, args):
    devices = core.serial_devices(args.dev_root)
    options = [(d, "") for d in devices] + [("Enter a device path by hand", "")]
    index = ui.choose("Serial link", [
        "Which serial device is wired to the optocoupler?",
        "On a Raspberry Pi this is normally /dev/serial0, the alias for the GPIO "
        "UART. Enable it with raspi-config: login shell No, hardware port Yes."],
        options)
    if index == len(devices):
        model.serial_device = ui.text("Serial link", "Device path", "/dev/serial0",
                                      core.valid_abs_path)
    else:
        model.serial_device = devices[index]
    rates = ["9600", "19200", "38400", "57600", "115200"]
    model.serial_baud = int(rates[ui.choose(
        "Serial link", ["Baud rate. It must match on both boxes.",
                        "Higher rates need a better optocoupler and shorter wiring."],
        [(r, "default" if r == "57600" else "") for r in rates])])


def _edit_modules(ui, model, variant):
    types = ([("modbus", "Mirror a PLC's registers and coils"),
              ("folder", "Send files dropped in a folder"),
              ("screen", "Share screenshots over HTTP")] if variant == "v1"
             else [("modbus", "Mirror a PLC's registers and coils")])
    while True:
        listing = ["%d. %s (%s, port %s)" % (i + 1, m.name, m.type, m.props.get("port"))
                   for i, m in enumerate(model.modules)] or ["No modules defined yet."]
        options = [("Add a module", "")]
        if model.modules:
            options.append(("Remove a module", ""))
        options.append(("Done", "continue to the summary"))
        choice = ui.choose("Modules", ["What should this diode transfer?"] + listing,
                           options)
        if options[choice][0] == "Add a module":
            index = ui.choose("Module type", ["What kind of transfer?"],
                              [(t, d) for t, d in types])
            _add_module(ui, model, types[index][0])
        elif options[choice][0] == "Remove a module":
            index = ui.choose("Remove", ["Which module?"],
                              [(m.name, m.type) for m in model.modules])
            model.remove_module(index)
        else:
            if not model.modules:
                ui.error("At least one module is needed: without one there is "
                         "nothing for DYODE to transfer.")
                continue
            return


def _add_module(ui, model, mtype):
    names = {m.name for m in model.modules}
    name = ui.text("New %s module" % mtype, "A name for this module",
                   "%s %d" % (mtype.capitalize(), len(model.modules) + 1),
                   lambda t: bool(t.strip()) and t.strip() not in names)
    props = {"type": mtype, "port": model.next_port(mtype)}
    if mtype == "modbus":
        props["ip"] = ui.text(name, "PLC address (as seen from the INPUT box)",
                              "192.168.1.10", core.valid_ipv4)
        props["port_out"] = int(ui.text(
            name, "Port for the mirrored Modbus server on the output box", "502",
            core.valid_port, help_text="502 is standard but needs root; 1502 does not."))
        registers = ui.text(name, "Holding register ranges", "0-100", core.valid_ranges,
                            help_text="Comma separated, end excluded: 0-100 means "
                                      "addresses 0 to 99. Leave empty for none.")
        coils = ui.text(name, "Coil ranges", "0-10", core.valid_ranges,
                        help_text="Same format. Leave empty for none.")
        if not registers and not coils:
            ui.error("A Modbus module needs at least one register or coil range.")
            return
        props["registers"] = core.split_ranges(registers)
        props["coils"] = core.split_ranges(coils)
    elif mtype == "folder":
        props["in"] = ui.text(name, "Folder to send from (on the input box)",
                              "/home/pi/in", core.valid_abs_path)
        props["out"] = ui.text(name, "Folder to receive into (on the output box)",
                               "/home/pi/out", core.valid_abs_path)
    else:
        props["in"] = ui.text(name, "Folder where screenshots appear (input box)",
                              "/home/pi/screenz", core.valid_abs_path)
        props["out"] = ui.text(name, "Folder on the output box", "/home/pi/screenz",
                               core.valid_abs_path)
        props["http_port"] = int(ui.text(name, "Web server port on the output box",
                                         "8080", core.valid_port))
    model.add_module(name.strip(), props)


def review_and_apply(ui, plan, dry_run=False):
    model = plan.model
    error = model.validate()
    if error:
        ui.error("The generated configuration is not valid: %s" % error)
        return False
    body = ["This is what will be written:", ""] + plan.describe() + [""]
    for warning in model.warnings(plan.side):
        body.append("! " + warning)
    body += ["", "--- config.yaml ---"] + model.to_yaml_text().splitlines()
    if not ui.confirm("Review", body):
        ui.info("Nothing written", ["No files were changed."])
        return False
    if dry_run:
        ui.info("Dry run", ["Nothing written (--dry-run)."] + plan.describe())
        return True
    created = plan.apply()
    body = ["Written:"] + ["  " + p for p in created]
    if plan.backups:
        body += ["Existing files were backed up:"] + ["  " + p for p in plan.backups]
    ui.info("Done", body)
    steps = core.next_steps(plan)
    ui.info("Next steps", sum([["%d. %s" % (i + 1, s.splitlines()[0])]
                               + s.splitlines()[1:] + [""]
                               for i, s in enumerate(steps)], []))
    return True


# ==========================================================================
# Entry point
# ==========================================================================

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="DYODE installer and configuration wizard")
    p.add_argument("--plain", action="store_true",
                   help="plain text instead of the curses interface")
    p.add_argument("--repo", default=core.REPO_ROOT,
                   help="path to the DYODE checkout (default: this folder)")
    p.add_argument("--variant", choices=["v1", "v2"], help="skip the version question")
    p.add_argument("--side", choices=["in", "out"], help="skip the side question")
    p.add_argument("--import-config", metavar="FILE",
                   help="import the other box's config.yaml straight away")
    p.add_argument("--dry-run", action="store_true", help="show, but write nothing")
    p.add_argument("--all-interfaces", action="store_true",
                   help="also list virtual interfaces (lo, docker, ...)")
    p.add_argument("--sys-root", default="/sys/class/net", help=argparse.SUPPRESS)
    p.add_argument("--dev-root", default="/dev", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    use_curses = not args.plain and sys.stdin.isatty() and sys.stdout.isatty()
    if use_curses:
        try:
            import curses
        except ImportError:
            use_curses = False
    try:
        if use_curses:
            def body(screen):
                ui = CursesUi(screen)
                return review_and_apply(ui, run_wizard(ui, args), args.dry_run)
            ok = curses.wrapper(body)
        else:
            ui = PlainUi()
            ok = review_and_apply(ui, run_wizard(ui, args), args.dry_run)
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was written.")
        return 1
    except Cancelled as err:
        print("\n%s" % err)
        return 1
    except EOFError:
        print("\nInput ended unexpectedly. Nothing was written.")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
