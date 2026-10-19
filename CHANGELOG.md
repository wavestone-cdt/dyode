# Changelog

All notable changes to this fork of DYODE. Versions follow
[semantic versioning](https://semver.org/); the version is in `VERSION`.

## [1.4.3] - 2026-10-19

### Fixed

- **A file taking longer than `batch_timeout` (default 5 minutes) to transfer
  was discarded on arrival.** The receiving box closed the batch while the
  file was still arriving, because only finished files reset the clock. The
  intact file was then thrown away as "received a file with no manifest
  pending". With the default FEC capping a gigabit link at about 350–400
  Mbit/s, a 10 GB file takes about that long. Now any data still landing in
  the staging folder keeps open batches alive, so a batch only times out after
  `batch_timeout` with nothing received at all. Verified in the field: a 10 GB
  transfer that failed this way arrived intact. (On earlier versions: set
  `batch_timeout: 3600` in the receiving folder module.)

### Changed

- **The receive-side writeback ceiling scales with RAM.** `vm.dirty_bytes` in
  `/etc/sysctl.d/90-dyode-receive.conf` is now 20% of RAM (like the kernel's
  own default), at most 2 GB and at least 128 MB. In 1.4.2 it was a fixed
  1 GB: too tight on large boxes and too much for a small one such as a
  Raspberry Pi. Writeback still starts at 64 MB, or a quarter of the ceiling
  on boxes under ~1.25 GB of RAM. Run `sudo ./install.sh --tuning-only` to
  update an existing box.

### Documentation

- CAVEATS.md:
  - new entry for the batch-timeout problem
  - a section on sustained disk speed, which the writeback settings cannot
    fix: how to measure it and how to set `bitrate` from it
  - an explanation of what the writeback ceiling is for

## [1.4.2] - 2026-10-19

### Fixed

- **Large folder transfers failing part-way** (from about 1 GB, at high
  `bitrate`) on receiving boxes with plenty of RAM and an encrypted (LUKS)
  disk. The cause was disk writeback, not the network. Linux wrote ~30 s of
  received data at once, and that burst stalled `udp-receiver` long enough to
  overflow its socket buffer, so udpcast aborted the file. `install.sh` now
  writes `/etc/sysctl.d/90-dyode-receive.conf` on receiving boxes (v1, and v2
  `--side out`), with continuous writeback (`vm.dirty_background_bytes`) and a
  larger UDP receive buffer (`net.core.rmem_*`). Verified in the field: 1.3 GB
  and 5 GB transfers at 900 Mbit/s that had always failed now arrive with
  identical SHA-256. `--tuning-only` adds it to an existing box; `--no-tuning`
  skips it.

### Changed

- A stalled transfer's error now says where the packets went, from the
  kernel's UDP `RcvbufErrors` counter. "N UDP packets were dropped on this box
  because a receive buffer was full" means the receiver stalled (the case
  above). "No receive-buffer overflow on this box" means the packets were lost
  before reaching it (NIC, cabling, diode).

### Documentation

- CAVEATS.md rewritten with the confirmed cause. The 1.4.1 entry blamed the
  receive-side NIC; swapping it changed nothing. The entry now has the field
  results, the diagnostic procedure that found the cause, the fix and how to
  undo it, and other tuning for other hardware.

## [1.4.1] - 2026-10-16

### Documentation

- **New [CAVEATS.md](CAVEATS.md)** for known limitations. First entry: large
  folder transfers (from about 1 GB) can fail at high `bitrate` on some
  receive-side NICs (seen with the Intel X520, not with the X710). Packets
  overflow the receiving host's UDP socket buffer, and udpcast aborts the
  file. CAVEATS.md covers symptoms, how to diagnose it, hardware results,
  and workarounds (NIC choice, `net.core.rmem_*` sysctl, ring size, interrupt
  batching, bitrate). No code changes in this release.

## [1.4.0] - 2026-10-15

### Added

- **File-share setup.** After the setup wizard, `install.sh` asks whether to
  share a DYODE folder over NFS and/or SMB, on either box: received files on
  the output box (read-only by default), a drop folder on the input box
  (read-write). Unattended: `--share-nfs PATH=CLIENTS[:rw]`,
  `--share-smb PATH=USER[:rw]`, `--smb-password-file`, and `--shares-only` to
  change shares on an installed box without reinstalling.
- Shares live in one managed block in `/etc/exports` and
  `/etc/samba/smb.conf`; everything outside it is untouched. Re-running the
  same command changes nothing, so it suits Ansible. `smb.conf` is checked
  with `testparm` before it is installed, a rejected export is rolled back,
  and every file changed keeps a backup.
- Refused before anything is written: sharing DYODE's staging folder or a
  folder containing it, NFS to everyone, SMB guest access, SMB as root, and
  paths or names that could inject settings into either file.
- **Clients for the file-sharing services.** `--with ssh-client`,
  `nfs-client` and `smb-client` (or `clients` for all three) install only
  the client: `sftp`/`scp`/`ssh`, `mount -t nfs`, and `mount -t cifs` plus
  `smbclient`. For a box that connects out and should run no server.
- `smbclient` is now bundled, and `--with smb` installs it alongside Samba;
  `--with ssh` now names `openssh-client` explicitly. Each server therefore
  comes with its client, so its shares can be tested as `localhost`.

## [1.3.0] - 2026-10-15

### Added

- **File-sharing services in the offline bundle.** `openssh-server`,
  `openssh-sftp-server`, `nfs-kernel-server`, `nfs-common`, `samba` and
  `cifs-utils`, with their dependencies, for every supported system: Ubuntu
  24.04 and 26.04 (amd64, arm64) and 64-bit Raspberry Pi OS 12 and 13.
- `install.sh --with ssh,nfs,smb` (or `all` / `none`) installs them, online or
  offline. Interactive installs ask; unattended installs add none unless
  asked. Packages already present are left alone, and the installer lists
  each service it started and where it is configured.

### Notes

- The services start with their distribution defaults. SSH accepts password
  logins until you change `/etc/ssh/sshd_config`; NFS and Samba share
  nothing until configured.
- The offline bundle grows accordingly; Samba accounts for most of it.

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
