# Log streaming through DYODE (`type: logs`)

The `logs` module moves log lines one way, from the protected (OT) side to
the output (IT) side, the way a SOC needs them: continuously, in order, and
with every loss accounted for. It is DYODE v1 only, and uses nothing beyond
what DYODE already installs (the standard library and the existing CRC-framed
UDP transport).

```
 OT devices ──syslog UDP/TCP──┐
 files on the input box ──────┤  INPUT BOX ══ diode ══> OUTPUT BOX ──> files (logrotate-ready)
                              │  batch, number,                  └───> SIEM / syslog (UDP, TCP)
                              │  compress, send twice
```

## Use cases

- **OT syslog to the SOC.** Switches, firewalls, RTUs, PLC gateways and
  Linux hosts in the control network send syslog to the input box. The
  output box forwards it to the SIEM (Splunk, Elastic, QRadar, Graylog,
  Sentinel's syslog collector) or writes files a collector picks up. The SOC
  gets the logs and the OT network still has no inbound path.
- **Application log files.** HMI, historian or engineering-workstation logs
  written to a share or a local disk on the input box, followed like
  `tail -F` (`tail:`), including through logrotate's renames.
- **The input box's own health.** Today the input box's DYODE logs are only
  visible on the OT side. Tail `/var/log/dyode-transfer/dyode.log` and
  `transfer.jsonl` from the input box and the IT side can see sends, failures
  and `file_missing` events from both ends of the diode.
- **Off-site retention for audits.** IEC 62443 and NERC CIP style
  requirements to keep security event logs outside the zone that produced
  them. Files on the output side are written `0640`, and they rotate with
  logrotate like the rest of DYODE's logs.

Windows Event Log is not read directly. Run a forwarder (NXLog, Winlogbeat
with a syslog output, or Windows Event Forwarding to a Linux collector) that
sends syslog to the input box.

## Configuration

Add a module to `config.yaml` (the same file on both boxes). Minimal:

```yaml
  "OT logs":
    type: logs
    port: 9700                  # UDP port through the diode
    listen_udp: 514             # input box: receive syslog
    out: /var/log/dyode-ot      # output box: write received.log here
```

`dyode_setup.py` can also add one (module type *logs*).

| Key | Side | Default | Meaning |
|---|---|---|---|
| `listen_udp` | in | — | `514` or `10.0.0.1:514`. Syslog over UDP, one message per datagram. |
| `listen_tcp` | in | — | Syslog over TCP. LF-, NUL- and RFC 6587 octet-counted framing, detected per message. |
| `tail` | in | — | List of absolute paths or globs to follow. |
| `tail_from` | in | `end` | `end` skips what is already in a file at start-up; `start` sends it. Files that appear later are always read from the start. |
| `tail_state` | in | — | File to keep read offsets in, so a restart resumes instead of skipping. |
| `allow_from` | in | any | Networks allowed to send syslog, e.g. `[192.168.1.0/24]`. |
| `max_line` | in | 16384 | Longer lines are cut and marked `[dyode: truncated N bytes]`. |
| `max_tcp_clients` | in | 64 | Concurrent TCP syslog connections. |
| `buffer_lines` | in | 100000 | In-memory queue. When full, new lines are dropped and counted. |
| `batch_interval` | in | 0.5 | Seconds between sends; the latency floor. |
| `batch_bytes` | in | 48000 | Uncompressed size of one batch. |
| `copies` | in | 2 | Times each batch is sent, each copy `max(0.2, batch_interval)` s after the last. 1–4. |
| `max_kbps` | in | 2000 | Wire bandwidth cap for this module, copies and headers included. |
| `heartbeat` | in | 10 | An empty batch is sent after this many idle seconds. |
| `out` | out | — | Directory for received logs. |
| `out_file` | out | `received.log` | File name inside `out`. |
| `split_by_source` | out | false | One file per sending host (`<ip>.log`) instead. |
| `format` | out | `prefixed` | `prefixed` = `<UTC time> <source> <message>`, `raw` = message only, `json` = one object per line. |
| `forward` | out | — | `udp://host:514`, `tcp://host:601`, as a list. |
| `forward_wrap` | out | true | Give lines that are not syslog (tailed files) an RFC 5424 header with the original host and time. |
| `reorder_window` | out | 3.0 | Seconds to wait for a missing batch before declaring it lost. It is raised automatically if it is shorter than the last copy needs to arrive. |
| `stale_after` | out | 3 × heartbeat | Warn when nothing at all has arrived for this long. |

At least one source and one destination are required. Config errors stop
start-up with a message rather than running half-configured.

The listening ports below 1024 need root, which DYODE's service already has.
DYODE v2 (serial) ignores `logs` modules with a warning, so one config file
can still serve both variants.

## What happens to a line

1. **Input box.** Each source pushes `(time received, source, channel,
   message)` into a bounded queue. Invalid UTF-8 is kept byte-for-byte.
2. Every `batch_interval` the queue is cut into batches. Each batch gets a
   sequence number and the stream's random boot id, and it is
   zlib-compressed. Log text typically shrinks about 12×, so a 48 KB batch
   is 3–4 datagrams.
3. The batch goes out in the existing `DYO1` UDP framing (CRC32 per
   message). Its copies go out `0.5 s` apart by default. Spacing the copies
   in time matters: loss on a diode link tends to come in bursts, such as
   receive-buffer overruns, so a copy sent right behind the first would be
   lost with it.
4. **Output box.** Datagrams are reassembled and CRC-checked. Batches are
   checked for size, decompressed (with a bomb limit), de-duplicated, and
   put back in order.
5. A missing sequence number is waited for up to `reorder_window`. Then
   it is declared lost: a `WARNING` naming the range, and a `logs_gap` event.
   Delivery continues in order.
6. Lines go to the file and/or forwarders. For line-oriented outputs
   (`raw`, `prefixed`, TCP), control characters such as newlines and
   terminal escapes are written as `\xNN`. That way one record is always
   one line, and nothing in an OT log can drive a terminal or split a SIEM
   event. `json` keeps the message exactly as received.

### What the output side reports

In the journal and in `dyode.log`:

```
WARNING module 'OT logs': 2 log batch(es) lost in the diode (sequence 1041-1042)
WARNING module 'OT logs': input side dropped 7 line(s): its buffer was full
WARNING module 'OT logs': no log batches or heartbeats for 31s; the link or the input side may be down
INFO    module 'OT logs': log stream resumed
INFO    module 'OT logs': input side restarted (new stream)
```

In `transfer.jsonl`: `logs_gap`, `logs_dropped_at_source`, `logs_stale`,
`logs_resumed` and `logs_sender_restart`, plus a `logs_stats` record per
minute from each side. `dyode_logs.py --stats` totals lines sent, lines
delivered, batches lost and lines dropped.

The alert to build is **`logs_gap`**: it is the diode telling you which
log data the SOC will never see. Lines in a lost batch are gone. A diode
has no way to ask for them again.

## Measured behaviour

All numbers come from the test harness, on a loopback link with a relay
dropping datagrams at random. They were measured on the x86 build machine;
a Raspberry Pi will encode more slowly, so its throughput is still to be
measured.

Loss, 3000 lines over 145 batches, 5 runs each:

| Datagram loss | copies 1 | copies 2 (default) | copies 3 |
|---|---|---|---|
| 1% | 98.6% delivered | 100% | 100% |
| 3% | 93.0% | 99.3% | 100% |
| 10% | 84.2% | 97.2% | 100% |

In every run, every batch was either delivered or reported lost. Lines
always arrived in order, with no duplicates.

- **Compression:** 400 typical syslog lines, 51 KB → 4.2 KB (12×), 3 datagrams.
- **Throughput:** encoding runs at about 460,000 lines/s on one core. The
  real limit is `max_kbps`: the default 2000 kbit/s with 2 copies carries
  about **11,000 typical syslog lines/s**.
- **End to end with the real `dyode_in.py` / `dyode_out.py` processes:**
  5000/5000 UDP lines, 2000/2000 TCP lines and 300/300 file lines arrived,
  with stale, resume and restart detection confirmed by stopping and
  restarting the input service.

These tests do not use the optical link. Before production, run it on the
real hardware and watch `logs_gap`: zero gaps under your normal load is the
acceptance test.

## Limitations

- **Loss is reported, not recovered.** Copies make loss unlikely. They
  cannot make it impossible, because there is no return path. If a batch
  loses every copy, its lines are gone, and `logs_gap` says which ones.
- **Syslog over UDP is lossy before the diode.** A device sending faster
  than the input box reads loses packets in the kernel, and nothing can
  count those. Prefer `listen_tcp` for chatty or important sources.
- **Memory-only buffering.** A restart of the input service loses lines
  still in its queue (normally less than a second's worth). With
  `tail_state`, offsets are saved every 5 s *after reading*, so a crash can
  lose up to that much tailed text. The output side's TCP forward queue
  (200,000 lines) is also in memory.
- **No delivery acknowledgement to the SIEM.** TCP forwarding is plain
  syslog over TCP, not RELP. Lines in flight when the SIEM connection drops
  can be lost. UDP forwarding is fire-and-forget.
- **No TLS.** There is no syslog over TLS (6514) on either side yet. The
  forward leg runs in clear text on the IT network. To encrypt it now, wrap
  it with stunnel or send to a local rsyslog that relays over TLS.
- **Shared bandwidth.** The folder module's udpcast budget
  (`MAX_BITRATE_MBPS = 8`, split between folder modules) does not reserve
  room for logs, Modbus or screen traffic. On a link near that ceiling,
  lower a folder module's `bitrate` or this module's `max_kbps`. When
  `max_kbps` is reached, repeat copies are skipped first. Then new data
  waits, the queue fills, and further lines are dropped and counted.
- **Source identity.** `src` is the address the input box received from,
  so a relay hides the original sender. A syslog message's HOSTNAME field
  still carries it. For file lines, `src` is the input box's hostname.
- **Timestamps** are when the input box received the line, in UTC, from
  the input box's clock. Keep it on OT-side NTP, or times drift
  unnoticed.
- **No parsing or filtering.** Lines are carried as-is. `allow_from` is
  the only filter.
- **DYODE v2 (serial) does not support it.**

## Security notes

- **Input side (faces OT).** Only `allow_from` networks are accepted.
  TCP clients, line length and queue size are all capped, so a noisy or
  hostile device can fill the queue but cannot exhaust memory.
- **Output side (faces IT).** It never transmits into the diode. It accepts
  only datagrams that pass the CRC, are well-formed, are size-bounded and
  decompress within the bomb limit. With `split_by_source`, file names are
  reduced to `[A-Za-z0-9._-]`, so a forged source cannot write outside
  `out`.
- **Log content is untrusted.** Anything on the OT side can write a syslog
  line, so treat these logs in the SIEM as untrusted input: watch for forged
  events, not just parse them. The control-character escaping removes the
  terminal-escape and line-splitting tricks. It does not make the content
  true.

## DYODE today, and where it can go

### What the project does now (v1)

| Module | Transport | Delivery guarantee | Ceiling |
|---|---|---|---|
| `folder` | udpcast + FEC | SHA-256 per file; missing files reported (`file_missing`) | ~8 Mbit/s by default, shared by all folder modules |
| `modbus` | DYO1 UDP | latest values; stale warning | polling interval (1 s by default) |
| `screen` | DYO1 UDP | latest frame, lost frames skipped | `max_fps` 10, JPEG from a folder |
| `logs` | DYO1 UDP, compressed, repeated | in order; every loss reported | `max_kbps` (2 Mbit/s by default) |

DYODE v2 (serial, 57,600 baud ≈ 5.7 KB/s) carries Modbus only.

### Video: what exists and what it would take

The `screen` module is already a basic video path. JPEG frames dropped
into a folder are sent over UDP and served as a multi-viewer MJPEG stream
at `http://<output>:8080/`. Going further, in order of effort:

1. **Camera source for `screen`** (small change). Run `ffmpeg` on the
   input box to pull an RTSP or USB camera and write MJPEG to a pipe, split
   the pipe into frames, and reuse the existing sender. The output side is
   unchanged. Two constraints decide whether it works:
   - **ffmpeg is not installed today**, and it is not in the offline
     bundle. Adding it to `packaging/debs` brings a large dependency tree
     for each of the four supported systems.
   - **Loss compounds per frame.** A 720p JPEG is roughly 60–120 KB, about
     70 datagrams, and losing any one drops the whole frame. At 1% datagram
     loss about half of those frames would be dropped (0.99⁷⁰ ≈ 0.49).
     Small frames (640×360, high compression, ~15 datagrams) lose about 14%
     at the same rate. Video wants the next step.
2. **Forward error correction for DYO1 messages** (medium). XOR or
   Reed-Solomon parity datagrams per message in `dyode_common`. It would
   help `screen`, `logs` and `modbus` at once. It is the most valuable
   generic improvement, because nothing in the UDP path can repair a loss
   today.
3. **Recorded video through `folder`** (small, no new transport). `ffmpeg`
   writes HLS segments (`.ts`, a few seconds each) into a folder module's
   `in` directory. Every segment then crosses with udpcast FEC and a
   SHA-256 check, and any web server on the output side serves the
   playlist. Latency is 10–30 s rather than live, but each segment is
   verified, and a missing one is named by `file_missing`. For evidence or
   CCTV review this is the strongest option the project can offer with what
   it already has.
4. **Live H.264** (large). An RTP or MPEG-TS stream with SMPTE 2022-1 /
   RIST-style FEC, which works one way, unlike SRT, which needs a return
   path. This needs GStreamer or ffmpeg on both boxes, and would be its own
   module.

### Next steps for log streaming

- Syslog over TLS on input and output (`tls://`), using Python's `ssl`.
- Include/exclude filters (regex or severity) before the diode, to save
  bandwidth.
- A disk spool on the input side, so restarts and long overloads do not
  drop lines.
- Log streaming for DYODE v2: at 57,600 baud, compressed, roughly 100–200
  lines/s. Enough for security events from a small site.
- RELP forwarding on the output side, for acknowledged delivery to the
  SIEM.
