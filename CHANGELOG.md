# Changelog

All notable changes to this fork of DYODE. Versions follow
[semantic versioning](https://semver.org/); the version is in `VERSION`.

## [1.2.0] - 2026-10-12

### Added

- **Log streaming** (`type: logs`, DYODE v1). The input box collects syslog
  over UDP and TCP (LF and RFC 6587 octet-counted framing) and follows log
  files through rotation. The output box writes them to logrotate-friendly
  files (`prefixed`, `raw` or `json`) and/or forwards them to a SIEM over
  UDP or TCP. Batches are numbered, compressed (about 12× on syslog text)
  and sent twice, spaced in time. Lost batches are reported by sequence
  range (`logs_gap`), never silently missing. Also included: a bandwidth
  cap, a heartbeat with stale-link detection, a source allow-list, and
  control-character escaping. See `DYODE_v1_full/LOG_STREAMING.md`.
- The setup wizard can add `logs` modules. `dyode_logs.py --stats`
  totals log lines sent, delivered, lost and dropped.

### Notes

- DYODE v2 ignores `logs` modules with a warning, so a shared config file
  still loads there.

## [1.0.0] - 2026-10-07

First release of the Python 3 port of
[wavestone-cdt/dyode](https://github.com/wavestone-cdt/dyode).

### Supported systems

| System | CPU | Python |
|---|---|---|
| Ubuntu 24.04 LTS | amd64, arm64 | 3.12 |
| Ubuntu 26.04 LTS | amd64, arm64 | 3.14 |
| Raspberry Pi OS 12 "bookworm", 64-bit | arm64 | 3.11 |
| Raspberry Pi OS 13 "trixie", 64-bit | arm64 | 3.13 |

Ubuntu 22.04 is not supported: its `python3` is 3.10, older than DYODE
requires. 32-bit Raspberry Pi OS is not covered by the offline bundle.

### Installation

- `install.sh` installs a box from scratch: OS packages (`udpcast`, Python
  venv support), a virtualenv built on the machine itself, and DYODE's Python
  packages, then starts the setup wizard. Choose `--online` (apt and PyPI) or
  `--offline`, or let it ask.
- **Offline installs** need no network at all. The repository ships every
  Python package as wheels (Python 3.11–3.14, x86_64 and aarch64) and the OS
  packages for each supported system. The bundle is checked against
  `SHA256SUMS` before anything changes; OS packages go through a private
  local apt repository, so apt installs only what is missing and never
  downgrades.
- The bundle is rebuilt on GitHub by the *Offline install bundle* workflow,
  or by hand with `tools/build_wheelhouse.sh` and `tools/build_debs.sh`.
- A `venv/` copied in from another folder or revision is detected and
  rebuilt rather than trusted.

### Setup

- Guided setup wizard (`dyode_setup.py`), curses or plain text: picks the
  diode interface, reads its MAC address, configures modules, and writes
  `config.yaml` and a systemd unit.
- The generated systemd unit waits for the diode interface and its address,
  restarts with it after a link flap, and never gives up retrying.

### File transfer

- Received files are identified by content, not arrival order. Previously a
  single lost transfer shifted every later file by one and discarded the
  rest of the batch.
- The receiver no longer stops listening while it verifies a file, and the
  sender pauses between files (`file_gap`), since a diode offers no
  handshake.
- Sent files are kept in `.dyode_sent/` for `keep_sent_hours` instead of
  being deleted: a diode cannot confirm delivery.
- Incoming files are staged outside the output folder, so NFS, SFTP or
  anything else serving it never sees a partial file.
- The static ARP entry is set before starting and re-asserted continuously;
  without it nothing reaches the output side while both ends report success.
- Forward error correction is configurable per module (`fec`, or `none` for
  full line rate on a clean link).
- An idle `udp-receiver` timeout is recognised from timing and bytes
  received rather than its wording, so it is no longer logged as an error.

### Logging

- `/var/log/dyode-transfer/dyode.log` (human-readable) and `transfer.jsonl`
  (one JSON event per line for monitoring tools), with `file_missing` events
  naming every file that never arrived.
- Rotation by logrotate, weekly archives by a systemd timer.

### Python 3 port

- Runs on Python 3.11+, pymodbus 3.11–3.12 on asyncio, `inotify_simple`.
- JSON replaces pickle on every network path, removing remote code
  execution on the output box; framing is length-prefixed and CRC-checked.
- File names are passed to udpcast as argument lists, never through a shell.
- Datastores are sized from the configured register ranges, so addresses
  such as 400–449 are served where SCADA expects them.

### Verified

Beyond the unit tests, this release was installed offline with the real
bundle on Ubuntu 24.04, transferred a file through real udpcast using
DYODE's own command lines, and round-tripped Modbus values through real
pymodbus 3.12.1.
