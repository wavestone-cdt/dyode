# DYODE Python 3 port

This fork replaces the original Python 2 code with a
Python 3 rewrite. It targets **Python 3.11 or newer**: Raspberry Pi OS
Bookworm ships 3.11, and the code is written to keep working under Python
3.14's new multiprocessing default. The folder layout, the entry points
(`dyode_in.py` / `dyode_out.py`) and the `config.yaml` format are unchanged,
so existing configurations keep working.

## Upgrade both boxes together

The data sent through the diode changed from `pickle` to checksummed JSON,
so **an old input box cannot talk to a new output box, or vice versa**.
Update both sides at the same time.

## Guided setup (`dyode_setup.py`)

Instead of editing `config.yaml` by hand, run the wizard from the repository
root on each box:

```bash
python3 dyode_setup.py             # curses interface
python3 dyode_setup.py --plain     # plain text (over a pipe, or no curses)
```

It asks which DYODE version this is, which side of the diode the box is,
and which interface faces the diode; it then reads that interface's MAC
address from the system itself, so it never has to be typed. It also walks
through the modules (Modbus ranges, folders, screen sharing), can generate
a systemd unit, and validates the result with the runtime's own config
loader before anything is written. An existing `config.yaml` is backed up
with a timestamp, and nothing at all is written until the review screen is
confirmed (`--dry-run` stops before writing).

**Setting up two boxes.** A script cannot read the *other* box's MAC
address, and the input box needs it for the static ARP entry. So:

1. Run the wizard on the first box and define the modules there.
2. Copy the resulting `config.yaml` to the second box (a USB stick is fine).
3. Run the wizard on the second box and choose **Import config.yaml from the
   other box** (or pass `--import-config /path/to/config.yaml`). It keeps the
   modules and the first box's details, and adds its own interface and MAC.
4. Copy that completed file back to the first box, so both boxes match.

If you already know the other box's MAC, type it in step 1 and both files
are complete immediately.

Useful flags: `--variant v1|v2` and `--side in|out` skip those questions,
`--all-interfaces` also lists virtual ones (`lo`, `docker0`), and `--repo`
points at a checkout elsewhere.

## Installing

`install.sh` at the top of the repository installs everything a box needs,
either from the internet or entirely offline from files shipped in the repo:

```bash
sudo ./install.sh                  # asks: offline or online, v1 or v2, side
sudo ./install.sh --offline        # only the bundled files; no network at all
sudo ./install.sh --online         # apt and PyPI, as usual
sudo ./install.sh --offline --variant v2 --side out --yes   # unattended
```

It installs `udpcast` (v1 only) and Python virtualenv support from the OS,
builds a virtualenv **on this machine** in the variant's folder, installs the
Python packages into it, checks they import, and then starts the setup
wizard (`--no-wizard` to skip it). Run `./install.sh --help` for all options.

**Do not copy `venv/` between machines or revisions.** A virtualenv records
the absolute path it was created at and the exact interpreter that made it.
It happens to keep working when the path and Python never change, and breaks
confusingly when either does. `install.sh` detects a venv carried over from
another folder (Python 3.11+ records where it was made) and rebuilds it.

### Offline installs

The bundle lives in `packaging/`:

| Path | Contents |
|---|---|
| `packaging/wheels/` | every Python package, for Python 3.11–3.14 on x86_64 and aarch64 |
| `packaging/debs/<os>-<release>/<arch>/` | `udpcast` and venv support for each supported system (below) |

Each folder carries a `SHA256SUMS`. `install.sh --offline` checks the whole
bundle **before changing anything**, so a copy damaged on its way across the
air gap stops the install instead of leaving it half done.

The bundled `.deb` files are installed through a private, local-only apt
repository built from those files at install time. apt resolves the
dependencies itself, installs only what the host is actually missing, never
downgrades a package the host already has newer, and cannot reach the
network. pip runs with `--no-index`, so it cannot either.

Supported systems, each bundled with `udpcast` and `python3-venv` plus their
dependencies:

| System | CPU | Python | Bundle folder |
|---|---|---|---|
| Ubuntu 24.04 LTS | amd64, arm64 | 3.12 | `ubuntu-24.04/` |
| Ubuntu 26.04 LTS | amd64, arm64 | 3.14 | `ubuntu-26.04/` |
| Raspberry Pi OS 12 "bookworm", 64-bit | arm64 | 3.11 | `debian-12/` |
| Raspberry Pi OS 13 "trixie", 64-bit | arm64 | 3.13 | `debian-13/` |

64-bit Raspberry Pi OS takes Python and udpcast unchanged from Debian's arm64
archive and identifies itself as Debian in `/etc/os-release`, so the
`debian-*` folders serve it (and plain Debian 12/13 on arm64). The wheels
cover Python 3.11–3.14, which spans all four.

**Not supported:** Ubuntu 22.04, whose `python3` is 3.10 and whose only 3.11
is a release candidate (`3.11.0~rc1`); and 32-bit Raspberry Pi OS (armhf),
which uses a separate archive. On any other system, install `udpcast` (v1)
and `python3-venv` by hand and run `install.sh --offline --skip-os-packages`;
the Python side still installs offline if its Python is 3.11–3.14 on x86_64
or aarch64.

### Refreshing the bundle

The bundle is built on a machine **with** internet access:

```bash
tools/build_wheelhouse.sh          # packaging/wheels/, then verifies it
tools/build_debs.sh                # packaging/debs/, needs Docker or Podman
tools/build_wheelhouse.sh --verify # check an existing wheelhouse
```

`build_wheelhouse.sh` finishes by proving that every `requirements.txt` can be
installed for every Python version and CPU type from the bundled files alone,
using pip with the network switched off. `build_debs.sh` resolves each
release's packages inside a clean `ubuntu:<release>` container, so apt — not
a hand-written list — decides the dependencies.

The **Offline install bundle** GitHub Actions workflow
(`.github/workflows/offline-bundle.yml`) runs both on GitHub's machines,
arm64 on native ARM runners, verifies the result and commits it back to the
branch. It runs automatically when a `requirements.txt` or a build script
changes, and can be started by hand from the repository's Actions tab.
**Rebuild the bundle whenever a requirement changes**: the test suite fails
if the wheelhouse no longer covers every requirement on every target.

### By hand

The steps `install.sh` performs, for a system it does not cover:

```bash
sudo apt install python3-venv udpcast iproute2
cd DYODE_v1_full
python3 -m venv venv && venv/bin/pip install -r requirements.txt
# offline: venv/bin/pip install --no-index --find-links ../packaging/wheels -r requirements.txt
```

DYODE_v2_light: the same, in `DYODE_v2_light/in` on the input Pi and in
`DYODE_v2_light/out` on the output Pi (no udpcast needed). Enable the GPIO
UART with `sudo raspi-config` → Interface Options → Serial Port: login shell
**No**, hardware port **Yes**.

pymodbus is pinned to `>=3.11,<3.13`, because 3.13 removed the datastore
calls this code uses. All pymodbus calls are in the "pymodbus shim" section
at the top of `modbus.py`, so moving to 3.13+ or 4.x later means editing
only that section.

## Running at boot

The original used `/etc/rc.local`. A systemd unit restarts DYODE if it
crashes and puts its logs in the journal (`journalctl -u dyode-in -f`).
Example for the v1 input box, saved as `/etc/systemd/system/dyode-in.service`:

```ini
[Unit]
Description=DYODE input side
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/home/pi/dyode/DYODE_v1_full
ExecStart=/home/pi/dyode/DYODE_v1_full/venv/bin/python dyode_in.py
Restart=always
# root is needed for the static ARP entry on the input side,
# and for serving Modbus on port 502 on the output side.
User=root

[Install]
WantedBy=multi-user.target
```

Then run `sudo systemctl enable --now dyode-in`. The output side is the
same with `dyode_out.py`.

## New optional configuration keys

Everything below is optional; defaults match the original behaviour.

| Key | Where | Default | Purpose |
|---|---|---|---|
| `dyode_in.interface` | top level | `eth0` | NIC facing the diode on the input box |
| `dyode_out.interface` | top level | `eth1` | NIC facing the diode on the output box |
| `serial.device` | top level (v2) | `/dev/serial0` | serial device (original hardcoded `/dev/ttyAMA0`) |
| `serial.baudrate` | top level (v2) | `57600` | must match on both sides |
| `plc_port`, `unit` | modbus module | `502`, `1` | PLC's TCP port and Modbus unit id |
| `interval` | modbus module | `1.0` | seconds between polls |
| `stale_after` | modbus module | `10` | warn on the output side after this many seconds without updates |
| `bind_out` | modbus module | `0.0.0.0` | address the output Modbus server listens on |
| `settle` | folder module | `2` | seconds a file must be unchanged before it is sent |
| `file_gap` | folder module | `0.5` | seconds between files, so the receiver can restart `udp-receiver` |
| `keep_sent_hours` | folder module | `24` | how long sent files are kept under `.dyode_sent`; `0` deletes them |
| `staging` | folder module | `<out>.incoming` | where transfers land before verification; must be outside `out` |
| `batch_timeout` | folder module | `300` | seconds before an incomplete batch is closed and its missing files named |
| `autostart` | folder module | `5` | `udp-sender` hello retransmissions before data starts |
| `start_timeout` | folder module | `300` | `udp-receiver` start timeout in seconds; `0` waits indefinitely |
| `dyode_in.arp_interval` | top level | `60` | seconds between re-asserting the static ARP entry |
| `bitrate` | folder module | 8 ÷ folder modules | udpcast Mbit/s for this module |
| `fec` | folder module | `8x16/64` | udpcast FEC ratio, or `none` to disable — see *Throughput tuning* below |
| `logging.dir` | top level | `/var/log/dyode-transfer` | log directory; empty string for stderr only |
| `logging.human` | top level | `dyode.log` | human-readable log file name |
| `logging.json` | top level | `transfer.jsonl` | JSON event log file name |
| `logging.per_file_events` | top level | `true` | `false` keeps batch summaries only |
| `http_port`, `http_bind` | screen module | `8080`, `0.0.0.0` | screen-sharing web server |
| `max_fps` | screen module | `10` | frame rate cap |

Module `type` is now case-insensitive (`Modbus` works), and a typo in the
config stops start-up with a clear message instead of silently doing nothing.

## Running as a systemd service (changed)

Symptom this fixes: transfers worked when `dyode_in.py` and `dyode_out.py`
were started by hand, but as a systemd service the output side logged
timeout errors and no file arrived. Four separate causes, all of which only
show up unattended.

**The static ARP entry was set once, at start-up.** Nothing can answer ARP
through a one-way link, so without that entry the kernel cannot resolve the
destination MAC and drops every outbound packet *locally* — `udp-sender`
still exits 0 and the input side reports a clean send, while the output side
sees nothing at all. The entry is also lost whenever the diode NIC goes down
and comes back, and at boot the service can start before that NIC has its
hand-configured address, in which case the entry was never established.
Either way DYODE looked healthy and moved nothing.

Now: `dyode_in.py` refuses to start if it cannot set the entry (so systemd's
`Restart=always` retries until the interface is ready) and an `arp-keeper`
process re-asserts it every `arp_interval` seconds, logging only transitions.

**`--nokbd` was missing.** Both tools "read start signal from keyboard, and
display a message telling the user to press any key to start" unless told
otherwise, and under systemd stdin is `/dev/null`. Both invocations now pass
`--nokbd`, stdin is explicitly `/dev/null`, and the unit sets
`StandardInput=null`.

**`--autostart` was misread as a receiver count.** `udp-sender(1)` defines it
as "starts transmission after *n* retransmissions of hello packet" — so the
old `--autostart 1` gave the output box almost no time to have
`udp-receiver` listening. Default is now 5, configurable per module.

**An idle timeout was treated as a failure.** `udp-receiver` aborts at start
if it sees no sender, which on a quiet diode is its normal state. That was
logged as an error *and* followed by a one-second sleep — a window with
nothing listening, in which a transfer is missed outright. `start_timeout`
is now explicit (default 300) and `0` waits indefinitely.

The first attempt at telling idle apart from failure looked for the word
"timeout" in udp-receiver's output. **The real udpcast never says it.** On
an idle start timeout udpcast 20120424 prints only:

```
udp-receiver 20120424
Receiver Error
```

so the idle exit was still classified as a failure: an `ERROR` line in the
journal every `start_timeout` seconds, and the one-second deaf window right
behind it. Measured against a faithful stand-in, the gap before
`udp-receiver` was listening again was 1.018 s.

Classification now ignores the wording entirely and uses two facts:

| Received bytes | Ran for | Verdict | Logged |
|---|---|---|---|
| some | any | transfer started then stalled | `ERROR` |
| none | ≥ 80% of `start_timeout` | idle — normal | `INFO` once, then `DEBUG` |
| none | less | failed fast: interface, port, permissions | `ERROR` |
| none | any, with `start_timeout: 0` | should never have exited | `ERROR` |

Only the error verdicts back off for a second. After an idle timeout the
socket is reopened in about 15–20 ms. This also closes a hole in the
string-matching version, which would have swallowed a genuine fast failure
as "idle" if its message happened to contain the word "timeout".

If you want no periodic restarts at all, `start_timeout: 0` makes
`udp-receiver` wait indefinitely. You give up automatic recovery from a
wedged `udp-receiver`, which is rarely needed on a diode.

### The generated unit

`dyode_setup.py` now writes device dependencies for the diode NIC:

```
After=sys-subsystem-net-devices-eth0.device
BindsTo=sys-subsystem-net-devices-eth0.device
StartLimitIntervalSec=0
...
ExecStartPre=/bin/sh -c 'for i in $(seq 1 60); do ip -4 addr show dev eth0 ... '
StandardInput=null
...
[Install]
WantedBy=multi-user.target
WantedBy=sys-subsystem-net-devices-eth0.device
```

`network-online.target` only promises that *some* network is up; it says
nothing about a hand-configured address on the diode NIC, which is why the
`ExecStartPre` waits for it. `BindsTo=` stops the service when the NIC
disappears and the second `WantedBy=` starts it again when the NIC returns,
so a link flap does not leave a dead service. `StartLimitIntervalSec=0` stops
five quick restarts from disabling the unit permanently during a long
outage.

If you installed a unit generated before this change, regenerate it or add
those lines by hand — the code fixes alone do not repair a unit already in
`/etc/systemd/system`.

## Folder transfers: how a file is identified (changed)

**If you ran an earlier version of this port, upgrade the output box.** The
first version matched received files to manifest entries *by arrival order*:
the Nth transfer was assumed to be the Nth manifest entry. One lost transfer
shifted every later file by one, so each was checked against the previous
file's hash and the whole tail of the batch was discarded. Measured on a
20-file batch, a single dropped transfer cost 17 files:

```
file_stored    f00.bin    got=10000
file_stored    f01.bin    got=11994
file_stored    f02.bin    got=13988
file_rejected  f03.bin    got=17976   expected=15982     <- f03 never arrived
file_rejected  f04.bin    got=19970   expected=17976     <- everything shifts
```

Files are now matched **by content**: the receiver hashes the blob (which it
did anyway) and looks that hash up among the entries it is waiting for. A
lost transfer now costs exactly that one file. The wire format did not
change, so the input box needs no coordinated upgrade for this.

Consequences worth knowing:

- A damaged transfer can no longer be attributed to a file, so
  `file_rejected` carries `reason: no_matching_file` and a size rather than a
  path. The file that never arrived is named instead when its batch closes,
  as a `file_missing` event — which is more accurate, since the old message
  usually blamed the wrong file.
- Several recent batches stay open at once (four), so a lost manifest or two
  overlapping batches cannot cascade either. A re-sent path supersedes the
  older batch's outstanding entry, so nothing is reported missing twice.
- An incomplete batch is closed after `batch_timeout` and its missing files
  listed, rather than waiting for later manifests to push it out.

### Three related fixes

**The receiver no longer goes deaf between files.** It used to hash and move
each file before re-opening the socket. A diode has no flow control, so at
line rate the sender had already pushed hundreds of megabytes of the next
file into a closed socket. Verification now runs in a worker thread and
`udp-receiver` restarts immediately. The sender also pauses `file_gap`
seconds between files, because there is no handshake to wait on — raise it
if files still go missing.

**Sent files are retired, not deleted.** `udp-sender` exiting 0 means
"transmitted", not "delivered". Files now move to `.dyode_sent/` under the
watched folder and are pruned after `keep_sent_hours`. To re-send one, copy
it back into the watched folder. `scan_ready_files` skips `.dyode*`, so it is
not picked up again on its own.

**Staging moved out of the output folder.** `udp-receiver` writes the
incoming file for the whole duration of the transfer, and the old staging
directory was `<out>/.dyode_incoming`. The dot prefix hides it from `ls` but
not from NFS, SFTP, rsync, `find`, an indexer or an AV scanner — anything
serving the output folder could hand out a growing, unverified partial. At
900 Mbit/s a 10 GB file was exposed that way for about 90 seconds. Staging
now defaults to `<out>.incoming`, a sibling on the same filesystem so the
final `os.replace` stays atomic, and the config refuses a `staging` path
inside `out`.

## Transfer logging

Logs still go to stderr, so `journalctl -u dyode-in -f` is unchanged. On top
of that, two files are written to `/var/log/dyode-transfer`:

| File | Contents |
|---|---|
| `dyode.log` | human-readable, every module, both variants |
| `transfer.jsonl` | one JSON object per line, folder transfers only |

`transfer.jsonl` is newline-delimited JSON (not a JSON array, which cannot be
appended to or rotated). Loki, Splunk, Elastic, Vector and fluent-bit all
ingest it as-is.

```json
{"v":1,"ts":"2026-09-25T20:32:14.108Z","event":"batch_sent","side":"in","module":"transfer","batch":"9f3c1e2a","files":42,"files_failed":0,"bytes":1073741824,"duration_s":9.41,"mbps":912.4}
{"v":1,"ts":"2026-09-25T20:32:19.882Z","event":"file_stored","side":"out","module":"transfer","batch":"9f3c1e2a","path":"reports/q3.csv","bytes":81920}
{"v":1,"ts":"2026-09-25T20:32:20.041Z","event":"file_rejected","side":"out","module":"transfer","batch":"9f3c1e2a","path":"reports/q4.csv","reason":"checksum_mismatch","bytes":81900,"expected_bytes":81920}
{"v":1,"ts":"2026-09-25T20:32:24.550Z","event":"batch_received","side":"out","module":"transfer","batch":"9f3c1e2a","files":42,"files_stored":41,"files_rejected":1,"files_missing":0,"bytes":1073741824,"duration_s":10.3}
```

Events: `batch_sent`, `batch_failed`, `file_sent` (input side);
`manifest_received`, `file_stored`, `file_rejected`, `file_missing`,
`file_orphaned`, `batch_received` (output side). `batch_received` carries a
`reason` of `complete`, `timeout`, `superseded`, `evicted` or `shutdown`.

`logs` modules add `logs_gap`, `logs_dropped_at_source`, `logs_stale`,
`logs_resumed`, `logs_sender_restart` and a per-minute `logs_stats` from
each side; see [LOG_STREAMING.md](DYODE_v1_full/LOG_STREAMING.md).

The alert that matters most: **`file_missing`**. It names a file the
manifest promised that never arrived, which on a diode is the failure mode
you cannot otherwise detect.

- `v` is a schema version, so a later change does not silently break parsers.
- `ts` is ISO-8601 **UTC**. It sorts lexically and has no repeated hour when
  the clocks go back. The human log stays in local time.
- `batch` is the same id on both boxes, so sent-vs-received can be joined per
  batch if both sides' logs ever reach one collector.
- `mbps` on `batch_sent` gives the throughput trend directly.
- `per_file_events: false` drops the per-file records and keeps the batch
  summaries, for when thousands of small files make the volume unhelpful.

**The two sides count different things.** The input box knows what it sent,
the output box knows what arrived, and on a diode those numbers legitimately
differ. Neither box can see the other's, and output-side logs cannot come
back through the diode — collecting them needs an agent on the output
network.

**`batch_received` has no timeout.** If the last file of a batch is lost, the
summary does not appear until the next manifest arrives. Alert on a
`batch_sent` with no matching `batch_received` rather than waiting for one
that may be late.

### Rotation: logrotate, not Python

Each module runs in its own process (see `supervise()`) and they all append to
these files. That rules out `TimedRotatingFileHandler`: whichever process
rotates first renames the file and the others keep writing to the unlinked
inode, losing lines silently. Instead the daemons use `WatchedFileHandler`,
which reopens when the inode changes, and `logrotate` does the rotation —
daily, `rotate 8`, plus `maxsize 100M` so a heavy day cannot fill `/var/log`
between runs. No `postrotate` signal is needed.

Rotated files are **not** compressed by logrotate. A weekly systemd timer
packs each week into `archive/dyode-logs-YYYY-Www.tar.gz` instead: consecutive
days of the same log are highly redundant, and gzip sees that redundancy
inside one tar stream. Archives are pruned after 26 weeks.

```bash
sudo cp packaging/logrotate/dyode-transfer /etc/logrotate.d/
sudo cp packaging/systemd/dyode-log-archive.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dyode-log-archive.timer
```

Edit `ExecStart` in `dyode-log-archive.service` if DYODE is not in
`/opt/dyode`. The archiver only ever touches rotated files, never the live
ones the daemons hold open, so it is safe to run at any time:

```bash
dyode_logs.py --archive --dry-run     # say what it would do
dyode_logs.py --stats                 # totals from transfer.jsonl
```

Files are created `0640 root adm`, so a monitoring agent needs to be in the
`adm` group to read them. If the log directory cannot be created or written,
the daemon logs a warning and carries on with stderr only rather than
refusing to start.

## Throughput tuning (folder transfers)

The original code shipped `MAX_BITRATE_MBPS = 8` with the comment *"empirical,
should be a bit less than 100 but isn't"*. That cap only ever applied to folder
modules — Modbus and screen modules use their own sockets and ignore it — and
it is now per-module via `bitrate`.

**On fast hardware the FEC encoder, not the link, is the ceiling.** udpcast's
forward error correction is single-threaded software Reed-Solomon. On
server-grade hardware (tested on Dell R440/R450 with SSD storage) the default
`8x16/64` ratio plateaus a 1 Gbit/s link at roughly 350–400 Mbit/s, with a
steady rate rather than the bursty profile packet loss produces. Setting
`fec: none` on the same hardware reaches line rate immediately.

Before reaching for sysctl and NIC tuning, check FEC first — on this class of
machine the kernel-side knobs are a rounding error by comparison. On Raspberry
Pi deployments the picture is different: a Pi 3B+ has its NIC behind USB 2.0
and tops out near 300 Mbit/s regardless, and SD-card write speed usually binds
before the network does.

**What you give up.** FEC is the only loss-recovery mechanism in the path.
There is no return channel, so the receiver cannot request a retransmit and the
sender never learns anything went wrong. With FEC off, one dropped packet means
the file fails its SHA-256 on arrival and is discarded: the output log records a
checksum mismatch, the input log records a clean send, and someone has to
notice and re-drop the file. That is a reasonable trade on a short, dedicated,
clean link and a bad one across marginal optics or a flaky media converter.

Recommended sequence:

1. Try a lighter ratio first (`fec: 8x8/128`). If it holds the throughput you
   need, you keep protection against isolated drops for free.
2. If you disable FEC, soak it — a few hundred GB over several hours — then
   check the receiver for losses and the output log for checksum mismatches:

   ```bash
   netstat -su | grep -Ei 'RcvbufErrors|InErrors|receive errors'
   ethtool -S eth0 | grep -Ei 'drop|miss|err|fifo'
   cat /proc/net/softnet_stat   # 2nd column: backlog drops
   ```

3. Leave headroom. With no flow control, set `bitrate` a little under line rate
   (≈900 on gigabit) — running flat against it turns transient microbursts into
   whole lost files.

The startup log line for each folder module now reports the settings in force,
so a running system can be checked without reading its config:

```
module 'transfer': watching /srv/in (port 9600, 900 Mbit/s, FEC off)
```

## Behaviour changes worth knowing

- **Register and coil ranges still exclude the end.** `0-100` means
  addresses 0 to 99, exactly as before; this is now documented.
- **The Modbus server's address space is sized from `config.yaml`**
  instead of a fixed 100. v1's sample range `400-450` is now served at
  addresses 400–449 (it used to land at 100–149).
- **Files keep their subfolders on the output side.** Before, everything
  was flattened into one folder and same-named files overwrote each other.
- **Files already in the input folder at start-up are sent.** Before, they
  waited until some new file arrived.
- **Symlinks in the input folder are skipped.** A link dropped there could
  otherwise send any file on the input box through the diode.
- **Screen sharing supports several viewers at once**, and adds a still
  image at `/screen.jpg`.
- **DYODE v2 uses one process for all Modbus modules**, which share the
  single serial port. The original gave each module its own process
  opening the same port, which could not work with more than one module.
- **The output side logs a warning when updates stop.** The Modbus server
  keeps serving the last values it received, and clients cannot tell they
  are old.
- **Crashed modules are restarted** by a supervisor (this was a TODO in the
  original).

## Wire formats

- **UDP (v1 Modbus and screen):** each datagram carries a 16-byte header:
  magic `DYO1`, message id, chunk index and count, and a CRC32 of the whole
  message. Payloads are at most 1400 bytes, so datagrams fit a standard
  Ethernet frame. Messages with lost, reordered or corrupted chunks are
  rebuilt or dropped, never delivered corrupted.
- **Log batches (v1 `logs`):** inside the DYO1 framing, `DYLG`, a version
  byte, then zlib-compressed JSON carrying the stream's boot id, a sequence
  number, the sender's dropped-line count and the lines. Each batch is sent
  `copies` times, spaced in time; the receiver de-duplicates and reorders
  by sequence number and reports gaps.
- **Serial (v2):** one message per line, `<crc32 hex> <compact JSON>\n`.
- **File manifest (v1):** a JSON file listing each file's path, size and
  SHA-256, in sending order. If a batch is interrupted, the receiver
  resynchronizes on the next manifest instead of rejecting everything that
  follows.

## Tests

```bash
python3 -m unittest discover -s tests
```

This runs 179 tests in about 30 seconds, with no network or hardware
needed. The tests use small stand-in versions of pymodbus and pyserial
(`tests/fakes/`), so they exercise all of DYODE's own logic, including
end-to-end UDP, serial, HTTP and multiprocessing paths, plus the setup
wizard driven through a real pseudo-terminal, but **not the real
libraries**. Before relying on it, run a real check on your hardware:

1. On the output box, start `dyode_out.py --log-level DEBUG`.
2. On the input box, start `dyode_in.py --log-level DEBUG` with the PLC
   reachable.
3. From the output network, read the registers back, for example:
   `python3 -c "from pymodbus.client import ModbusTcpClient as C; c=C('OUTPUT_IP'); c.connect(); print(c.read_holding_registers(address=0, count=10).registers)"`
4. For v1, drop a file into each `in` folder and check that it appears in
   `out` with the same SHA-256 (`sha256sum`).

## Files

`dyode_common.py` and `modbus.py` are shared: the copies in
`DYODE_v2_light/in` and `out` must stay identical to the ones in
`DYODE_v1_full`, and `tests/test_layout.py` fails if they drift. Edit the
v1 copy, then copy it into the other two folders.

Repository root:

| Path | Purpose |
|---|---|
| `dyode_setup.py`, `dyode_setup_core.py` | guided setup wizard |
| `dyode_logs.py` | weekly log archiving (`--archive`) and a reader (`--stats`) |
| `DYODE_v1_full/logstream.py` | the `logs` module (log streaming); v1 only |
| `DYODE_v1_full/LOG_STREAMING.md` | log streaming: use cases, settings, limits, roadmap |
| `packaging/logrotate/dyode-transfer` | install as `/etc/logrotate.d/dyode-transfer` |
| `packaging/systemd/dyode-log-archive.{service,timer}` | weekly archiving timer |
| `install.sh` | installer: OS packages, virtualenv, Python packages; `--offline` or `--online` |
| `packaging/wheels/`, `packaging/debs/` | the offline install bundle |
| `tools/build_wheelhouse.sh`, `tools/build_debs.sh` | rebuild the bundle on a machine with internet access |
| `.github/workflows/offline-bundle.yml` | rebuilds the bundle on GitHub and commits it |
