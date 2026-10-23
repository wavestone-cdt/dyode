# shellcheck shell=bash
# Kernel settings for a box that RECEIVES through the diode, sourced by
# install.sh.  Background: CAVEATS.md, "Large folder transfers fail part-way".
#
# udp-receiver must never stop reading for long: the diode cannot slow the
# sender down or re-send anything, so whatever arrives while it is paused is
# lost, and losing more than the forward error correction can rebuild aborts
# the file.  Two stock defaults made pauses long enough to do that:
#
#   - Linux keeps written data in memory until it is 30 s old and then writes
#     the backlog at once.  On a box with a lot of RAM nothing else triggers
#     writeback, so every ~30 s of a large transfer (about 1 GB) hit the disk
#     in one burst, and on an encrypted (LUKS) disk that burst stalled the
#     receiver.  Starting writeback early (64 MB) keeps it continuous and
#     small.  The ceiling (vm.dirty_bytes) is where Linux starts pausing the
#     writer -- gently from halfway up -- so it is headroom for a disk that
#     falls behind for a while.  It scales with RAM like the kernel's own
#     default (20%), capped at 2 GB: large boxes get ample headroom, small
#     ones (a Raspberry Pi) are not asked to hold more unwritten data than
#     they can spare.
#   - The default socket buffer (~208 KB) is about 2 ms of traffic at
#     900 Mbit/s.  udpcast does not size its own unless told to, so it uses
#     the system default; 8 MB gives ~70 ms of slack.
#
# One managed file, rewritten in full on each run (with a backup if it was
# changed by hand), then loaded with sysctl.  Harmless on a sending box, but
# only installed where a box can receive: v1 (one folder for both sides) and
# v2 --side out.

TUNE_ETC="${DYODE_ETC:-/etc}"           # override: tests
TUNE_FILE="$TUNE_ETC/sysctl.d/90-dyode-receive.conf"
TUNE_MEMINFO="${DYODE_MEMINFO:-/proc/meminfo}"   # override: tests

MIB=1048576
DIRTY_MAX=$((2048 * MIB))        # ceiling on big boxes
DIRTY_MIN=$((128 * MIB))         # floor on tiny ones
BACKGROUND_MAX=$((64 * MIB))     # start writing here

# Sets DIRTY_BYTES, BACKGROUND_BYTES and MEM_MIB from this box's RAM.
tuning_values() {
  local kb
  kb="$(awk '/^MemTotal:/{print $2; exit}' "$TUNE_MEMINFO" 2>/dev/null || true)"
  case "$kb" in ''|*[!0-9]*) kb=$((4096 * 1024)) ;; esac   # unknown: assume 4 GB
  MEM_MIB=$((kb / 1024))
  DIRTY_BYTES=$((kb * 1024 / 5))
  [ "$DIRTY_BYTES" -gt "$DIRTY_MAX" ] && DIRTY_BYTES=$DIRTY_MAX
  [ "$DIRTY_BYTES" -lt "$DIRTY_MIN" ] && DIRTY_BYTES=$DIRTY_MIN
  BACKGROUND_BYTES=$((DIRTY_BYTES / 4))
  [ "$BACKGROUND_BYTES" -gt "$BACKGROUND_MAX" ] && BACKGROUND_BYTES=$BACKGROUND_MAX
  return 0
}

tuning_wanted() {                # receives through the diode?
  [ "${NO_TUNING:-0}" -eq 0 ] && [ "${SIDE:-}" != in ]
}

tuning_content() {
  tuning_values
  cat <<EOF
# Managed by DYODE install.sh (rewritten on each run; --no-tuning to skip).
# Keeps udp-receiver from stalling during large transfers.  See CAVEATS.md.

# Write received data to disk continuously instead of in one burst every
# 30 s.  These replace vm.dirty_background_ratio / vm.dirty_ratio, which then
# read 0.  Ceiling: 20% of this box's ${MEM_MIB} MB of RAM, at most 2 GB.
vm.dirty_background_bytes = $BACKGROUND_BYTES
vm.dirty_bytes = $DIRTY_BYTES

# Room for short pauses: ~70 ms of traffic at 900 Mbit/s.
net.core.rmem_default = 8388608
net.core.rmem_max = 16777216
EOF
}

describe_tuning() {
  if tuning_wanted; then
    say "  tuning:       $TUNE_FILE (receive-side writeback and buffer; --no-tuning skips)"
  fi
}

apply_tuning() {
  tuning_wanted || return 0
  step "receive-side kernel settings"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "   would write $TUNE_FILE and load it with sysctl:"
    tuning_content | sed -n 's/^\([a-z].*\)/     \1/p'
    return 0
  fi
  if [ "$(id -u)" -ne 0 ] && [ -z "${DYODE_ETC:-}" ]; then
    warn "not root: skipped $TUNE_FILE; large transfers may fail (see CAVEATS.md). Rerun with sudo, or: sudo ./install.sh --tuning-only"
    return 0
  fi
  local new result
  new="$(mktemp)"
  tuning_content > "$new"
  result="$(install_file "$TUNE_FILE" "$new")"
  rm -f "$new"
  case "$result" in
    unchanged) say "   $TUNE_FILE already up to date" ;;
    "")        say "   wrote $TUNE_FILE" ;;
    *)         say "   replaced $TUNE_FILE (previous copy: $result)" ;;
  esac
  if command -v sysctl >/dev/null 2>&1; then
    if sysctl -q -p "$TUNE_FILE" >/dev/null; then
      tuning_values
      say "   loaded: writeback starts at $((BACKGROUND_BYTES / MIB)) MB, ceiling $((DIRTY_BYTES / MIB)) MB, receive buffer 8 MB"
    else
      warn "sysctl could not load $TUNE_FILE; it will apply at the next boot"
    fi
  else
    warn "sysctl not found; $TUNE_FILE will apply at the next boot"
  fi
}
