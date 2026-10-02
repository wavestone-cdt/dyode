# DYODE Python 3 port

This branch (`python3-port`) replaces the original Python 2 code with a
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

DYODE_v1_full, on both boxes:

```bash
sudo apt install python3-venv udpcast iproute2
cd "DYODE_v1_full"
python3 -m venv venv && venv/bin/pip install -r requirements.txt
# then either run ../dyode_setup.py, or:
cp config.example.yaml config.yaml    # edit, then copy the same file to both boxes
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
- **Serial (v2):** one message per line, `<crc32 hex> <compact JSON>\n`.
- **File manifest (v1):** a JSON file listing each file's path, size and
  SHA-256, in sending order. If a batch is interrupted, the receiver
  resynchronizes on the next manifest instead of rejecting everything that
  follows.

## Tests

```bash
python3 -m unittest discover -s tests
```

This runs 67 tests in about 10 seconds, with no network or hardware
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
| `packaging/logrotate/dyode-transfer` | install as `/etc/logrotate.d/dyode-transfer` |
| `packaging/systemd/dyode-log-archive.{service,timer}` | weekly archiving timer |
