# Caveats

Known limitations and hardware-specific problems. Each entry says how to
recognise the problem, what causes it, and what to do about it.

---

## Large folder transfers fail part-way on some receiving boxes

**Status:** solved in the field. Since 1.4.2, `install.sh` applies the fix
automatically on receiving boxes. On a box installed with an earlier release,
run `sudo ./install.sh --tuning-only` (see [Fix](#fix)).

This was never a DYODE bug as such. The folder-transfer code and the bundled
udpcast (20120424) are identical in every release since 1.0.0. The cause is
two stock Linux defaults combined with an encrypted receive disk.

### Symptoms

- Files up to a few hundred MB arrive intact. Files from roughly 1 GB up fail
  part-way, every time.
- About 30–40 s into the transfer, the output box logs:

  ```
  ERROR udp-receiver failed after 39.0 s (a transfer started and then stopped): ... Missed blocks: 8,399,400,401,...,511
  ```

  The list is udpcast naming the packets it lost in one *slice* of the file.
  A long unbroken run up to the end of the slice means the receiver briefly
  stopped reading. Random line noise does not look like that.

  Since 1.4.2 the same message also says where the packets went. One
  version says *"N UDP packets were dropped on this box because a receive
  buffer was full"*: that is this problem. The other says *"no
  receive-buffer overflow on this box"*: then the packets were lost before
  reaching it (NIC, cabling or diode), and something else is wrong.
- On the output box, `nstat -az UdpRcvbufErrors` jumps during the failure.

### Cause

DYODE sends files with `udp-sender --async` and forward error correction
(FEC, default `8x16/64`). A diode has no return path, so a packet is never
re-sent. If one FEC group loses more packets than the FEC can rebuild,
`udp-receiver` aborts the whole file (udpcast's documented behaviour).
Anything that stops `udp-receiver` reading for a fraction of a second at a
high `bitrate` therefore loses the file.

What stopped it was **disk writeback**:

1. Linux holds written file data in memory and writes it out once it is
   **30 seconds old** (`vm.dirty_expire_centisecs = 3000`), or once the
   unwritten amount reaches 10% of RAM (`vm.dirty_background_ratio`). On a
   box with plenty of RAM (the affected pair had 256 GB) the 10% limit is
   about 25 GB and never comes into play. The 30-second rule is what
   triggers writeback.
2. So for the first ~30 s of a transfer nothing reaches the disk. Then the
   whole backlog, about 1 GB at `bitrate: 900`, is written in one burst. That
   is the "~1 GB threshold": it is really a time limit, which is why files
   that finish in under 30 s always worked.
3. On a **LUKS-encrypted** disk, that burst means encrypting ~1 GB at once.
   This competes for CPU with packet processing and holds up the process still
   appending to the file. `udp-receiver` stalled long enough to overflow its
   socket buffer. The Linux default buffer is ~208 KB, about 2 ms of traffic at
   900 Mbit/s, and udpcast uses the system default unless told otherwise.

Raw disk speed does not prevent it: the affected SSD sustained 2.6 GB/s
through LUKS with `dd`. The problem is one large burst, not throughput.

Field results, all through the same diode model (Canary Communications
GF-55SDN, 1000BASE-SX, 1000 Mbit/s one-way, no flow control) at
`bitrate: 900`:

| Pair | Receive disk | Result with stock settings |
|---|---|---|
| BCM57414 → X710 | not encrypted | 10 GB files, no errors |
| X520 → X520 | LUKS | fails from ~1 GB |
| X520 → BCM57414 (receive NIC swapped) | LUKS | still fails from ~1 GB |
| X520 → BCM57414, with the [fix](#fix) | LUKS | 1.3 GB and 5 GB intact (SHA-256 identical), no errors |

**Ruled out** along the way:

- Network cards: swapping the receive NIC changed nothing.
- SFPs: replacing old Cisco optics with Intel-coded ones, speed locked at
  1 Gb, changed nothing.
- The diode: traffic is ~920–930 Mbit/s on the wire, within its rating.
- Disk speed, RAM size, and the DYODE version.

### Fix

Two kernel settings on the **receiving** box. `install.sh` writes them to
`/etc/sysctl.d/90-dyode-receive.conf` and loads them:

```
# Write received data to disk continuously instead of in one burst.
vm.dirty_background_bytes = 67108864     # start writing at 64 MB
vm.dirty_bytes = 2147483648              # ceiling: 20% of RAM, at most 2 GB

# Room for short pauses: ~70 ms of traffic at 900 Mbit/s.
net.core.rmem_default = 8388608
net.core.rmem_max = 16777216
```

The values shown are for a box with 10 GB of RAM or more.

- **Writeback start** (`vm.dirty_background_bytes`) is the setting that fixed
  it. With writeback starting at 64 MB, the disk receives a steady ~110 MB/s
  stream that it easily keeps up with, and no burst ever happens.
- **The ceiling** (`vm.dirty_bytes`) is headroom for a disk that falls behind
  for a while. Linux starts pausing the writer gently from about halfway up,
  and hard at the ceiling, so the ceiling is how much unwritten data can
  build up before `udp-receiver` gets paused.
  - It scales with RAM like the kernel's own default (20%), capped at 2 GB
    and never below 128 MB. Large boxes get about 1 GB of headroom, and a
    small box (a Raspberry Pi) is never asked to hold more unwritten data than
    it can spare.
  - On boxes under about 1.25 GB of RAM, writeback starts at a quarter of the
    ceiling instead of 64 MB.
  - The ceiling is only reached if the disk is slower than the link. Then no
    ceiling is enough for a long transfer, and the fix is a lower `bitrate`.
    See [Sustained disk speed](#sustained-disk-speed).
- The `*_bytes` settings replace `vm.dirty_ratio` and
  `vm.dirty_background_ratio`, which then read 0. That is expected.
- **The larger socket buffer** did not fix it alone. It is kept as margin for
  other short pauses.

1.4.2 used a fixed 1 GB ceiling. 1.4.3 rewrites the file with the
RAM-scaled value. Run `sudo ./install.sh --tuning-only` to update an
existing box.

When it is installed:

- On every receiving box: v1, and v2 with `--side out`. It is never
  installed with `--side in`. It is harmless on a sending box.
- `--no-tuning` skips it. `--tuning-only` installs it on an existing box
  without reinstalling anything.
- A repeat run changes nothing. A hand-edited file is backed up before it is
  replaced.

To undo it:

```
sudo rm /etc/sysctl.d/90-dyode-receive.conf
sudo sysctl -w vm.dirty_background_ratio=10 vm.dirty_ratio=20 \
     net.core.rmem_default=212992 net.core.rmem_max=212992
```

Setting the `*_ratio` values switches the `*_bytes` ones off again.

The settings apply to the whole box, which suits a dedicated diode box. The
cost is that data is written to disk sooner, which on a diode box is also
safer.

### Sustained disk speed

The writeback settings stop *bursts* from stalling the receiver. They cannot
make a disk faster than the link. Many SSDs absorb writes into a fast cache
and slow down sharply once it is full, and encryption adds CPU work to every
block. A short `dd` test can therefore look fine while a 10 GB transfer
outruns the disk.

Measure the receive disk with more data than your largest file, bypassing
the cache:

```
cd <receive/staging folder>
dd if=/dev/zero of=./ddtest bs=1M count=20000 oflag=direct status=progress; rm ./ddtest
```

Watch the speed as it runs. Set `bitrate` so the link stays below the slowest
sustained figure. For example, a disk that settles at 90 MB/s (720 Mbit/s)
calls for `bitrate` of about 650. A full gigabit diode needs a disk that
sustains about 120 MB/s.

---

## Files larger than about 5 minutes' transfer time discarded on arrival

**Status:** fixed in 1.4.3. On earlier versions, use the workaround below.

On the receiving box, each batch has a `batch_timeout` (default 300 s). If no
file of the batch has arrived within that time, the batch is closed and its
missing files are named in the log. Up to 1.4.2, the clock only reset when a
**finished** file was stored. A single file that took longer than
`batch_timeout` to transfer therefore had its batch closed while it was still
arriving. When it then arrived, intact, it was discarded:

```
WARNING file big.bin never arrived (batch 1a2b3c4d, timeout)
WARNING batch 1a2b3c4d closed with 1 file(s) never received (timeout)
WARNING received a file with no manifest pending; discarded
```

How long a large file takes is easy to underestimate. With the default FEC
(`8x16/64`), the single-threaded encoder caps a gigabit link at about
350–400 Mbit/s on server hardware, whatever `bitrate` says. That makes a
10 GB file take roughly 4½–5½ minutes. The `batch_sent` event on the sending
box records each batch's `duration_s` and `mbps`.

Since 1.4.3 the clock also counts data still arriving: a batch only times out
after `batch_timeout` with nothing received at all.

**Workaround on earlier versions:** in the receiving box's folder module, set
`batch_timeout` longer than your longest single-file transfer, then restart
DYODE:

```yaml
    batch_timeout: 3600
```

If you applied these settings by hand before 1.4.2 (for example
`90-dyode.conf` and `91-dyode-writeback.conf`), they can stay. They hold the
same values. Delete them if you want only the managed file.

### Diagnosing it, or something like it

On the **receive** box, run this during a large test transfer, then press
Ctrl-C after it fails or finishes:

```
IF=<receive iface>
s() { awk '/^Udp:/{n++; if(n==2) print $6}' /proc/net/snmp; }
d() { t=0; while read -r a b r; do t=$((t+16#$b)); done < /proc/net/softnet_stat; echo $t; }
while sleep 1; do
  printf "%s rcvbuf_err=%s softnet_drop=%s nic_drop=%s/%s dirty=%s wb=%s\n" "$(date +%T)" \
    "$(s)" "$(d)" "$(cat /sys/class/net/$IF/statistics/rx_dropped)" \
    "$(cat /sys/class/net/$IF/statistics/rx_missed_errors)" \
    "$(awk '/^Dirty:/{print $2"k"}' /proc/meminfo)" "$(awk '/^Writeback:/{print $2"k"}' /proc/meminfo)"
done
```

| What you see | Meaning |
|---|---|
| `rcvbuf_err` jumps as `dirty` reaches ~1 GB and `wb` spikes | This problem: writeback bursts. Apply the fix. |
| `rcvbuf_err` jumps while `dirty` stays low | The receiver stalls for another reason (CPU). See [Other tuning](#other-tuning). |
| `softnet_drop` rises | The kernel's packet backlog is full: raise `net.core.netdev_max_backlog`. |
| `nic_drop` rises | The NIC's receive ring is full: `ethtool -G <iface> rx 4096`. |
| `rx_crc_errors` in `ethtool -S` rises | Optics, cabling or dirty connectors. |
| Nothing rises, but transfers fail | Loss before the box, for example at the diode: lower `bitrate`. |

To check whether a live transfer uses the larger buffer:
`sudo ss -uapmn | grep -A1 udp-receiver`. `rb8388608` means it does.

To take the disk out of the picture: stop DYODE on both boxes and run udpcast
by hand. Use `udp-receiver ... -f /dev/null` on the receive box, with the same
`udp-sender` options DYODE uses. If that succeeds where writing to a file
fails, the write path is the cause.

A few TX drops on the receive-side interface are normal. That box cannot send
anything through the diode, so its ARP, IPv6 and DHCP traffic is discarded.

### Other tuning

None of this was needed once writeback was fixed. It is listed for other
hardware:

- **NIC ring and interrupt batching:** `ethtool -G <iface> rx 4096`. Also
  `ethtool -C <iface> rx-usecs N`: on Intel X520 (`ixgbe`), `1` means adaptive
  (the default), `0` means no batching, and `10` or more means fixed
  microseconds. These settings are lost on reboot. To keep them, use a
  oneshot systemd unit bound to the interface:

  ```
  # /etc/systemd/system/dyode-nic-tune@.service
  [Unit]
  Description=DYODE NIC tuning for %i
  BindsTo=sys-subsystem-net-devices-%i.device
  After=sys-subsystem-net-devices-%i.device

  [Service]
  Type=oneshot
  RemainAfterExit=yes
  ExecStart=/usr/sbin/ethtool -G %i rx 4096
  ExecStart=/usr/sbin/ethtool -C %i rx-usecs 50

  [Install]
  WantedBy=sys-subsystem-net-devices-%i.device
  ```

  Enable it with `sudo systemctl enable --now dyode-nic-tune@<iface>.service`.
- **CPU:** use the `performance` governor (`cpupower frequency-set -g
  performance`). Keep the DYODE service off the core that handles the NIC's
  interrupts (`grep <iface> /proc/interrupts`; set `CPUAffinity=` with
  `systemctl edit`).
- **LUKS on an SSD:** `cryptsetup refresh --perf-no_read_workqueue
  --perf-no_write_workqueue --persistent <name>` (LUKS2) encrypts as data is
  written instead of queueing it. That gives lower and steadier delays. This
  changes the disk encryption setup, so only use it if needed.
- **Headroom:** `bitrate` around 800 on a gigabit diode, or a stronger `fec`
  (for example `8x32/64`), at a cost in bandwidth.

## Security (1.5.0)

1.5.0 signs every message through the diode and firewalls the diode ports.
What that does and does not cover:

- **Both v1 boxes need the same `dyode.key`.** DYODE stops at start-up
  without it. The setup wizard creates it on the first box and copies it on
  the second (from beside the imported `config.yaml`); `python3
  dyode_setup.py --new-key` creates one by hand. Anyone holding the key can
  forge messages, so keep it off file shares and out of e-mail.
- **Mixed versions do not work.** A 1.5.0 output box rejects a 1.4.x input
  box's unsigned messages, with *"message(s) rejected: not signed with this
  box's key"* in its log. Upgrade both together.
- **`auth.required: false`** runs unauthenticated with a warning at every
  start. Use it only to bring a pair up while the key is being copied.
- **DYODE v2 (serial) is not signed.** Its optocoupler link is physically
  one-way and point-to-point, so there is no network to spoof from.
- **udp-receiver itself is not authenticated.** A sender that gets past the
  firewall can still make it write a file into staging, but the file is
  discarded unless a signed manifest names its SHA-256.
- **Replay protection is in memory.** After the output service restarts, a
  recorded batch could be accepted once more. It can only bring back files
  that were genuinely sent before (possibly older versions of them).
- **The firewall covers the diode ports only.** The Modbus server
  (`port_out`) and the screen viewer face the IT network by design; restrict
  them with your own rules if needed. `--no-firewall` skips the rules;
  `sudo nft list table inet dyode` shows their drop counters. The service
  `dyode-firewall.service` reloads them at boot, after `nftables.service`
  (whose stock configuration flushes every table).
- **Interface binding is best effort.** The Modbus, log and screen
  listeners bind to the diode NIC with `SO_BINDTODEVICE`. Where that is not
  allowed they log a warning and rely on the firewall.
- **Files are sent from the descriptor DYODE opened**, through
  `/proc/self/fd/N`, rather than from a private copy as first proposed in the
  design review: a copy would double the disk space and time for multi-GB
  batches. The effect is the same: what is hashed is what is sent, and links
  are never followed.
- **The Modbus mirror is read-only.** A SCADA client that wrote to it now
  gets exception 01. Reads outside the configured ranges get exception 02;
  `unmapped: zero` restores the old zeros.
