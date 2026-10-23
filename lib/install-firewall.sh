# shellcheck shell=bash
# Firewall rules for DYODE's diode ports, sourced by install.sh.
#
# Linux accepts a packet for any of its own addresses on any interface.  So
# a host on the IT network could send to the output box's diode ports
# (dyode_out.ip) through its IT-facing NIC, and udp-receiver and the Modbus,
# log and screen listeners would take it as coming through the diode.
# Messages are signed since 1.5.0, but udp-receiver (udpcast) is not, and a
# flood still costs memory and CPU.  These rules drop any datagram for a
# diode port unless it arrives on the diode NIC from the input box.
#
# One nftables table, "inet dyode", generated from config.yaml by
# lib/dyode_firewall.py; nothing else on the box is touched.  Persisted as
# /etc/dyode/firewall.nft, loaded at boot by dyode-firewall.service (after
# nftables.service, whose stock config flushes the ruleset).  Every file is
# managed: a repeat run changes nothing, a hand-edited one is backed up.
# Never installed with --side in or on v2 (serial, no diode network); --no-firewall skips it, --firewall-only
# applies it on an installed box.

FW_ETC="${DYODE_ETC:-/etc}"                # override: tests
FW_FILE="$FW_ETC/dyode/firewall.nft"
FW_UNIT="$FW_ETC/systemd/system/dyode-firewall.service"
FW_SYSCTL="$FW_ETC/sysctl.d/92-dyode-firewall.conf"

firewall_wanted() {
  # v2 has no network path through the diode (a serial link): nothing to filter.
  [ "${NO_FIREWALL:-0}" -eq 0 ] && [ "${SIDE:-}" != in ] && [ "${VARIANT:-v1}" != v2 ]
}

describe_firewall() {
  if firewall_wanted; then
    say "  firewall:     $FW_FILE (diode ports only from the input box; --no-firewall skips)"
  fi
}

firewall_unit_content() {
  cat <<'EOF'
# Managed by DYODE install.sh (rewritten on each run; --no-firewall to skip).
[Unit]
Description=DYODE firewall rules for the diode ports
Documentation=file:/etc/dyode/firewall.nft
DefaultDependencies=no
After=nftables.service
Before=network-pre.target dyode-out.service dyode-in.service
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/dyode/firewall.nft
ExecStop=/usr/sbin/nft delete table inet dyode

[Install]
WantedBy=multi-user.target
EOF
}

# A config.yaml and a Python that can read it, from an installed tree.
find_config_and_python() {
  local t
  for t in "$ROOT/DYODE_v1_full"; do
    if [ -f "$t/config.yaml" ]; then
      FW_CFG="$t/config.yaml"
      if [ -x "$t/venv/bin/python" ]; then FW_PY="$t/venv/bin/python"; else FW_PY="${PY:-python3}"; fi
      return 0
    fi
  done
  return 1
}

apply_firewall() {                 # apply_firewall CONFIG PYTHON
  local cfg="$1" py="$2" new rules iface result
  firewall_wanted || return 0
  step "diode-port firewall"
  if [ -z "$cfg" ] || [ ! -f "$cfg" ]; then
    say "   no config.yaml yet: after the setup wizard, run: sudo ./install.sh --firewall-only"
    return 0
  fi
  rules="$(mktemp)"
  if ! "$py" "$ROOT/lib/dyode_firewall.py" "$cfg" > "$rules" 2>"$rules.err"; then
    warn "could not build firewall rules from ${cfg#"$ROOT"/}: $(cat "$rules.err")"
    rm -f "$rules" "$rules.err"
    return 0
  fi
  iface="$("$py" "$ROOT/lib/dyode_firewall.py" "$cfg" --interface 2>/dev/null)"
  rm -f "$rules.err"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "   would write $FW_FILE, load it with nft and enable dyode-firewall.service:"
    sed -n 's/^\t*\(.*udp dport.*\) counter drop.*/     \1 -> drop/p' "$rules"
    rm -f "$rules"
    return 0
  fi
  if [ "$(id -u)" -ne 0 ] && [ -z "${DYODE_ETC:-}" ]; then
    warn "not root: skipped the firewall rules. Rerun: sudo ./install.sh --firewall-only"
    rm -f "$rules"
    return 0
  fi
  if ! command -v nft >/dev/null 2>&1; then
    warn "nft not found: install nftables, then run: sudo ./install.sh --firewall-only"
    rm -f "$rules"
    return 0
  fi
  if ! nft -c -f "$rules" >/dev/null 2>&1; then
    warn "nft rejected the generated rules; nothing changed ($(nft -c -f "$rules" 2>&1 | head -1))"
    rm -f "$rules"
    return 0
  fi
  result="$(install_file "$FW_FILE" "$rules")"
  rm -f "$rules"
  case "$result" in
    unchanged) say "   $FW_FILE already up to date" ;;
    "")        say "   wrote $FW_FILE" ;;
    *)         say "   replaced $FW_FILE (previous copy: $result)" ;;
  esac
  new="$(mktemp)"
  firewall_unit_content > "$new"
  install_file "$FW_UNIT" "$new" >/dev/null
  printf '# Managed by DYODE install.sh: drop packets on the diode NIC whose source\n# would not be routed back through it.\nnet.ipv4.conf.%s.rp_filter = 1\n' "$iface" > "$new"
  install_file "$FW_SYSCTL" "$new" >/dev/null
  rm -f "$new"
  if nft -f "$FW_FILE"; then
    say "   loaded: $(sed -n 's/^# Diode ports \(.*\)$/\1/p' "$FW_FILE")"
  else
    warn "nft could not load $FW_FILE; it will be loaded at the next boot"
  fi
  sysctl -q -p "$FW_SYSCTL" >/dev/null 2>&1 || true
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable dyode-firewall.service >/dev/null 2>&1 ||
      warn "could not enable dyode-firewall.service; the rules will not survive a reboot"
  fi
}
