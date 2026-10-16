# Caveats

Known limitations and hardware-specific problems that DYODE does not (yet)
handle for you. Each entry says how to recognise the problem, what causes it,
and what to do about it.

---

## Large folder transfers fail on some receive-side network cards

**Status:** open. Found in the field on 1.4.0, but not caused by it: the
folder-transfer code and the bundled udpcast (20120424) are identical in every
release since 1.0.0. No automatic fix ships yet. Use the workarounds below.

### Symptoms

- Files up to a few hundred MB arrive intact. Files of roughly 1 GB and up
  fail part-way, and so far 5 GB has always failed.
- The output box logs something like:

  ```
  ERROR udp-receiver failed after 39.0 s (a transfer started and then stopped): ... Missed blocks: 8,399,400,401,...,511
  ```

  The list is udpcast naming the packets it lost in one *slice* of the file.
  A long unbroken run up to the end of the slice (here `...511`) means the
  receiver briefly stopped reading. It does not look like random line noise.
- On the output box, `nstat -az UdpRcvbufErrors` jumps during the failed
  transfer.

### Cause

DYODE sends files with `udp-sender --async` and forward error correction
(FEC, default `8x16/64`). A diode has no return path, so lost packets cannot
be re-sent. If one FEC group loses more packets than the FEC can rebuild,
`udp-receiver` aborts the whole file. This is udpcast's documented behaviour.

Packets are lost on the **receiving host**, in the UDP socket buffer, before
DYODE reads them. The Linux default buffer is about 208 KB, which is only
about 2 ms of traffic at `bitrate: 900`. Any pause on the receive box longer
than that overflows the buffer. Larger files take longer to send, so they are
more likely to hit a pause, which is why failures look size-dependent. There
is no file-size limit in DYODE.

How often those pauses happen depends heavily on the **receive-side network
card and its driver**, because of how each driver batches interrupts, sizes
its receive ring, and charges packet memory against the socket buffer.
Field data, all with the same diode, the same DYODE config and
`bitrate: 900`:

| Send-side NIC | Receive-side NIC | Result |
|---|---|---|
| Broadcom BCM57414 (`bnxt_en`) at 1 Gb | Intel X710 (`i40e`) at 1 Gb | 10 GB files, no errors, default settings |
| Intel X520 (`ixgbe`) at 1 Gb | Intel X520 (`ixgbe`) at 1 Gb | Fails from about 1 GB up; `UdpRcvbufErrors` rises |

**Ruled out** on the failing pair:

- Disk speed: SSD, 2.6 GB/s sustained, far above 900 Mbit/s.
- SFP modules: swapped old Cisco 1 Gb optics for Intel-coded ones and locked
  the speed to 1 Gb, with no change. Optics problems would also show up as
  `rx_crc_errors` in `ethtool -S`, not as `UdpRcvbufErrors`.
- The diode: a Canary Communications GF-55SDN. It is rated 1000 Mbit/s one-way
  on 1000BASE-SX fiber, with no flow control. At `bitrate: 900` the traffic on
  the wire is about 920–930 Mbit/s, which is within its rating.
- DYODE version: see Status above.

### Diagnosing it

On the **receive** box, before and after a large test transfer:

```
nstat -az UdpRcvbufErrors                                   # socket buffer overflow
ethtool -S <iface> | grep -E 'rx_missed_errors|rx_no_buffer_count|crc'
ip -s link show <iface>                                     # RX dropped / missed
sudo ss -uapmn | grep -A1 udp-receiver                      # during a transfer: rb<N> = live buffer size
```

| What rises | Where packets are lost |
|---|---|
| `UdpRcvbufErrors` | Socket buffer: see workarounds 2–4 |
| `rx_missed_errors` / `rx_no_buffer_count` / RX `missed` | NIC receive ring: see workaround 3 |
| `rx_crc_errors` | Optics, cabling or dirty connectors |
| Nothing, but transfers still fail | Before the box, for example at the diode: lower `bitrate` |

A few TX drops on the receive-side interface are expected and harmless. That
box cannot send anything through the diode, so its ARP, IPv6 and DHCP
traffic is discarded.

### Workarounds

In order of preference:

1. **Use a receive-side NIC known to work.** Intel X710 has been proven in the
   field. Broadcom BCM57414 is being trialled as a replacement for the X520 at
   the time of writing.
2. **Raise the socket buffer** on the receive box. DYODE does not pass
   `--rcvbuf`, so udpcast uses the system default and this applies without any
   code change. It was verified in the lab: a 2 GB transfer at 900 Mbit/s went
   from failing every time to succeeding every time.

   ```
   sudo tee /etc/sysctl.d/90-dyode.conf <<'EOF'
   net.core.rmem_default = 8388608
   net.core.rmem_max = 16777216
   EOF
   sudo sysctl --system
   ```

   If errors continue, double both values (to 33554432 and 67108864) and
   check the live size with `ss` as shown above.
3. **Enlarge the NIC receive ring:** `sudo ethtool -G <iface> rx 4096` (the
   maximum is shown by `ethtool -g <iface>`).
4. **Tune interrupt batching (X520 / `ixgbe`):** `sudo ethtool -C <iface>
   rx-usecs N`. Here `1` means adaptive (the default), `0` means no batching,
   and `10` or higher means fixed microseconds. Try 50, then 20, then 0,
   testing after each. Settings from workarounds 3 and 4 are lost on reboot.
   To keep them, use a oneshot systemd unit bound to the interface:

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
5. **CPU side:** set the `performance` governor (`cpupower frequency-set -g
   performance`), and keep the DYODE service off the core that handles the
   NIC's interrupts (`grep <iface> /proc/interrupts`; set `CPUAffinity=` in a
   `systemctl edit` override).
6. **Lower `bitrate`** to about 800 for more headroom on a gigabit diode. Raise
   `fec` (for example `8x32/64`) at a cost in bandwidth.

On the affected X520 pair, workarounds 2 and 3 were applied, but they were not
confirmed to resolve the problem on their own before the NIC swap was chosen.

### Possible future fix (not implemented)

- `install.sh` writes the sysctl file above on output boxes, and optionally
  the NIC tuning unit.
- DYODE passes `--rcvbuf` to `udp-receiver` explicitly.
- When a transfer fails, DYODE reads `UdpRcvbufErrors` and the NIC drop
  counters and names the likely cause in the error message.
- The setup wizard warns when `bitrate` leaves little headroom on a gigabit
  diode.
