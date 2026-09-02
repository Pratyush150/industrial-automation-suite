# Field notes

Things that cost us time on real plant floors, written down so they cost you
less. Nothing here is theory: every section is a failure mode with a symptom,
a cause and a fix.

---

## 1. Your float reads as garbage: Modbus word order

**Symptom.** You read two registers, decode a float32, and get `-1.6e-23` or
`3.4e38`. Or worse, you get `63.4` when the tank is at 63.4% — and then next
week you get `1.2e-41` on a different device from the same vendor.

**Cause.** The Modbus specification defines a register as 16 bits, big-endian
on the wire. It says *nothing* about which register holds the most significant
half of a 32-bit value. So every vendor picked. There are four combinations
and all four exist in the field:

| Layout | Word order | Byte order | `1.0` as float32 |
|---|---|---|---|
| ABCD | first register = MSW | normal | `[0x3F80, 0x0000]` |
| CDAB | first register = LSW | normal | `[0x0000, 0x3F80]` |
| BADC | first register = MSW | swapped | `[0x803F, 0x0000]` |
| DCBA | first register = LSW | swapped | `[0x0000, 0x803F]` |

ABCD and CDAB are both very common. BADC turns up behind protocol gateways
and on devices that `memcpy` a little-endian float straight into their
register image.

**The two-minute diagnostic.** Do not guess. Read the registers while the
process value is something you can independently see — a tank you can look at,
a temperature next to a handheld meter, a motor you just started — then decode
the same bytes all four ways and pick the plausible one:

```bash
python3 examples/01_decode_a_register_dump.py
```

```
word order  byte order          as float32     as uint32  verdict
----------------------------------------------------------------------------
big         big               -1.59501e-23    2577023613
big         little            -6.34817e-23    2593750338
little      big                       63.4    1115527578  <-- this one
little      little             1.61671e+37    2101516953
```

**Three refinements.**

1. If *none* of the float32 columns is plausible, it is probably a scaled
   integer. Look at the `uint32` column: `634` with a scale of `0.1`, or
   `6340` with `0.01`, is an extremely common map. Vendors avoid floats
   because integer registers are cheaper on small controllers.
2. Test with a value that is asymmetric. `0.0` decodes to `0.0` in all four
   orders and tells you nothing. Neither does an integer that happens to be
   palindromic in hex.
3. Once you know, write it in the tag database and never rediscover it. That
   is what the `word_order` and `byte_order` fields in
   `config/tags_bottling_line.yaml` are for.

**A related off-by-one.** Vendor documentation usually uses the 4xxxx
convention: holding register **40001** is protocol address **0**. Some
documents say "register 40001", some say "register 1", some say "address 0",
and a few say "40001" but mean address 40001. If every value in your map is
shifted by exactly one register — the temperature reads as the pressure —
this is why.

---

## 2. Serial RTU: inter-frame gaps and why the cable is not the problem

Modbus RTU has no start or end delimiter. A frame ends when the line has been
silent for **3.5 character times**. At 8N1 that is 11 bits per character:

| Baud | Character time | 3.5-character gap |
|---|---|---|
| 9600 | 1.15 ms | **4.01 ms** |
| 19200 | 0.57 ms | **2.01 ms** |
| 38400 and above | — | **1.75 ms** (fixed by the spec) |

Above 19200 the standard pins the gap at 1.75 ms rather than chasing
microseconds, because the timing is not achievable on a general-purpose OS
anyway.

**The USB-serial latency trap.** An FTDI adapter's default latency timer is
**16 ms**. The driver buffers incoming bytes for up to 16 ms before handing
them to userspace. At 9600 baud the required inter-frame gap is 4 ms, so
frames get smeared together and arrive as one blob. You get CRC errors that
look exactly like electrical noise, and you will spend an afternoon replacing
cable and adding ferrites.

Fix it first, before touching hardware:

```bash
# Linux, FTDI
cat /sys/bus/usb-serial/devices/ttyUSB0/latency_timer   # usually 16
echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB0/latency_timer
```

On Windows it is in Device Manager → Port Settings → Advanced → Latency Timer.
Set it to 1 ms. CH340 and CP210x adapters have similar buffering with fewer
knobs; if you cannot control it, use a real UART or a Modbus/TCP gateway.

**Turnaround delay.** Some slaves need a few milliseconds after the last byte
of a request before they will listen. Hammering them back-to-back produces
intermittent timeouts that *move around when you change the poll order* —
which is a very effective way to convince yourself the wiring is bad.
`ModbusRtuDriver` inserts one inter-frame gap between transactions by default;
raise `turnaround_delay` if a particular slave needs more.

**Other RTU realities.**

* One master. Two masters on one RS-485 segment produces collisions that look
  like random CRC failures.
* Termination is 120 Ω at each *end* of the bus, not at each device. Three
  terminators loads the line until nothing works above 19200.
* Parity has to match on every device on the segment. 8E1 and 8N1 are both
  11 bits on the wire, so a mismatch does not change the timing — it just
  fails the parity check on every frame.
* A slave that is powered off does not answer, and a slave with the wrong unit
  ID also does not answer. Those are indistinguishable from the master's side,
  so check the unit ID before you check the fuse.

---

## 3. Why polling every tag every 100 ms kills a PLC

It is tempting to poll everything as fast as possible "so the data is fresh".
Here is what it actually costs.

**On the PLC.** A small controller services its communications stack between
logic scans. Every Modbus request costs it time it is not spending on control.
Push enough requests and you extend the logic scan time, which is the number
the machine's actual behaviour depends on. The symptoms are familiar and
rarely attributed to polling: the HMI goes sluggish, a motion sequence gets
jittery, a watchdog trips at 3 a.m.

**On the link.** Each request is a round trip. Thirty tags polled individually
at 10 Hz is 300 round trips per second. On a 20 ms round trip — normal for
Modbus/TCP through a plant switch — that is 6 seconds of work per second. The
loop cannot keep up, and the effect is not graceful: requests queue, timeouts
fire, and the data gets *older* the harder you poll.

**Three fixes, in order of impact.**

1. **Poll groups.** Not every tag deserves the same rate. Conveyor speed at
   500 ms, temperatures at 2 s, totalisers at 10 s. This is free and it is
   usually a 5-10x reduction in request count.

2. **Register coalescing.** Thirty tags scattered over forty registers do not
   need thirty requests. Merge them into contiguous blocks:

   ```
   group fast        21 tags -> 2 request(s)
       line1/discrete[0..2] 3 regs, 3 tags  (padding 0 regs)
       line1/holding[0..43] 44 regs, 18 tags  (padding 23 regs)
   ```

   Reading 23 registers of padding is far cheaper than 16 extra round trips.
   The rules the merge must respect: never across devices, never across
   register areas, never over 125 registers (2000 bits for coils), and only
   bridge a gap when the padding is cheaper than another round trip.

   ```bash
   tools/factorylink dump-tags --plan
   ```

3. **Staggering.** If four groups have periods that all divide 10 s, they come
   due together at t=0, t=10, t=20. The PLC sees a burst then silence. Offset
   each group's phase and the same traffic is spread evenly. This is a common
   cause of "the HMI freezes every ten seconds".

**Rule of thumb.** Set the poll period to a fraction of the time constant of
the thing you are measuring. A tank level with a 20-minute time constant does
not need 100 ms sampling; you are storing noise and paying for it twice, once
on the wire and once on disk.

---

## 4. Network segmentation on a plant floor

**The default assumption is wrong.** A control network is not an office
network with different cables. Treat it as one and you eventually take a line
down with a broadcast storm or an antivirus scan.

**What matters in practice.**

* **Separate the control network.** Controllers, drives and I/O on their own
  VLAN or their own physical switches. The acquisition box sits between that
  network and the business network, and it is the *only* thing that does. Two
  network interfaces, no routing between them.
* **Direction of initiation.** Data flows out. If a business-network host can
  open a connection to a controller, the segmentation is decorative. This is
  also the practical argument for MQTT Sparkplug over polling from a
  historian upstairs: the edge node establishes an outbound connection to the
  broker, so no inbound rule is needed.
* **Multicast and broadcast.** EtherNet/IP I/O is multicast, PROFINET is
  layer-2 with strict cycle times. A managed switch without IGMP snooping
  floods multicast to every port, including the port your acquisition box is
  on. It will also flood it to the drive that has a 1 ms cycle time.
* **Do not put a DHCP server on the control network.** A controller that
  changes IP address is a controller you have lost.
* **Managed switches, port mirroring.** When something is intermittent, the
  only real diagnostic is a packet capture. If you cannot mirror a port, you
  are guessing.
* **Time.** If timestamps come from several machines, they need NTP from the
  same source. Comparing a trend from one system with an event log from
  another when the clocks differ by 40 seconds produces confident, wrong
  conclusions. This is why every timestamp in `factorylink` comes from one
  injected clock.

**Latency, not bandwidth.** Plant data is small — kilobytes per second — and
almost every problem is latency or jitter, not throughput. A gigabit link with
a 200 ms latency spike every minute is worse than a steady 10 Mbit link. Test
for jitter, not for speed.

---

## 5. Alarm rationalisation: why nobody reads the alarm list

**The arithmetic.** Put a bare `value > 18.0` on a motor current with 0.3 A of
noise, sitting near its limit, and poll it at 2 Hz:

```
configuration                                alarms raised  per hour
--------------------------------------------------------------------
no deadband, no delay                                 1789      1789
deadband 1.0 A, no delay                                 3         3
deadband 1.0 A, 5 s on-delay                             3         3
deadband 1.0 A, 5 s on / 10 s off                        1         1
```

(`python3 examples/03_alarm_chatter.py` — one hour of simulated data.)

One tag. 1,789 events an hour. EEMUA 191 puts the manageable steady-state
load at roughly **6 alarms per hour per operator**, and calls more than **10
alarms in 10 minutes** a flood. A single un-deadbanded analogue alarm exceeds
the flood threshold by itself, permanently.

**What actually happens next** is not that operators complain. They stop
reading the list. The alarm banner becomes wallpaper, and the one alarm that
mattered scrolls off the top while somebody acknowledges forty that did not.
Every major process incident review of the last thirty years contains a
version of this sentence.

**The four mechanisms that fix it.**

1. **Deadband / hysteresis.** Raise at the limit, clear at limit minus a
   deadband. Size the deadband from the *noise*, not from the limit: two to
   three times the signal's RMS noise is a reasonable start.
2. **On-delay.** The condition has to hold continuously before it is
   annunciated. Kills transients and single bad samples outright.
3. **Off-delay.** The condition has to be gone continuously before it clears.
   Stops the flicker when a signal hovers.
4. **Shelving.** A known-bad instrument gets silenced for a *bounded* time, on
   the record, with an expiry. Unbounded suppression is a disabled alarm that
   nobody remembers disabling — which is how alarms end up disabled for years.

**Rationalisation is the boring part that matters.** For every alarm, answer:

* What does the operator *do* about it? If the answer is "nothing" or "watch
  it", it is not an alarm. It is a trend, or a diagnostic-severity event.
* How long do they have? That determines the priority, not how bad it feels.
* Is it a duplicate? A high-high on a tag that already has a high alarm adds
  a second event for the same problem. Give them different consequences or
  merge them.
* Does it fire during a normal start-up or shutdown? If so it needs state-based
  suppression, or it will fire on every start for the life of the plant.

**Measure it.** `AlarmEngine.summary()` reports the 10-minute alarm rate and a
flood flag. If the rate is above ten in ten minutes on a steady process, the
configuration is wrong. Fix the configuration, not the operators.

---

## 6. Quality flags: "the tank is empty" versus "I did not read the tank"

A monitoring system that cannot tell these apart will eventually raise a
low-level alarm because a switch rebooted, and eventually fail to raise one
because a sensor froze.

Three rules that this package enforces:

* A failed read produces a `Quality.BAD` reading with `value=None`. It never
  produces `0.0`.
* The alarm engine **holds** its state on bad quality. A comms failure must
  never look like the process recovering.
* The historian does not archive bad-quality samples. A gap in a trend is
  honest; a flat line at zero is a lie you will believe six months later.

**The nastiest sensor failure** is the one that does not look like a failure: a
transmitter that freezes at its last value. The number is plausible, the
quality is good, and nothing alarms. The simulator has this as an injectable
fault (`--fault sensor_stuck@600`) precisely because it is the one worth
building a detector for. The detector is a rate-of-change alarm on the
*absence* of change, or a comparison against a redundant measurement.

---

## 7. Historian compression: how much fidelity are you actually buying?

Thirty tags at 2 Hz is 5.2 million rows a day. Most of those rows say the same
thing as the row before them.

Two stages, in order — this is the same arrangement a commercial process
historian uses:

1. **Exception deadband.** Drop a sample if it is within a deadband of the
   last stored value. One comparison, no memory. Perfect for a parked signal,
   poor for a ramp — a slow ramp becomes a staircase.
2. **Swinging door compression.** Ask whether a straight line from the last
   archived point still passes within tolerance of every point since. While it
   does, store nothing. A linear ramp compresses to two points regardless of
   length.

A useful starting ratio is **compression tolerance ≈ 2 × exception deadband**.

**The honest caveat.** Swinging door guarantees ±tolerance against the door's
own slope. Rebuilding the trend by joining archived points with straight lines
— which is what every trend viewer does — costs up to **2 × tolerance** in the
worst case. Choose the tolerance from the instrument's accuracy, then, and not
from a storage target:

```
signal             tolerance   stored    ratio   max error   bytes/day
----------------------------------------------------------------------
ramping level           0.05        8    0.001      0.0000       4,608
temperature             0.20       13    0.002      0.2705       7,488
noisy current           0.05     6245    0.867      0.0822   3,597,120
noisy current           1.00       73    0.010      1.7569      42,048
```

(`python3 examples/04_compression_tradeoff.py`.)

Read the third row: a tolerance smaller than the signal's noise buys you
nothing except 3.6 MB a day of archived noise. If a signal will not compress,
the tolerance is wrong, not the algorithm.

**Always configure a maximum interval.** A flat signal that stores nothing for
six hours is indistinguishable from a dead acquisition system. A heartbeat
point every few minutes costs almost nothing and proves the tag was being
read.

---

## 8. OEE: the three ways it gets computed wrong

```
Availability = run time / planned production time
Performance  = (ideal cycle time x total count) / run time
Quality      = good count / total count
OEE          = A x P x Q
```

1. **Planned downtime counted as an availability loss.** A scheduled
   changeover, planned maintenance or an unstaffed shift is not a loss you can
   act on. Subtract it from planned production time. Mixing it in makes OEE
   look terrible for reasons nobody can do anything about, and the number gets
   ignored.
2. **Good count used in the performance term.** Performance is measured on
   everything the machine made, including what it then threw away. Using good
   count in both performance and quality double-counts the scrap.
3. **Performance above 100% treated as good news.** It is not. It means the
   ideal cycle time is too slow, the run time is under-counted, or the counter
   double-counts. `compute_oee` clamps it *and* keeps `performance_raw` and a
   note, so the error is visible instead of hidden.

**OEE is a diagnosis, not a target.** A number on a board changes nothing. The
downtime Pareto is what turns "you have a 24% availability loss" into "half of
it is one chiller and one jam". That is the output somebody can act on:

```
reason                     events   minutes   share     cum
------------------------------------------------------------------
Chiller failure                 1      7.00   48.0%   48.0%
Motor overload trip             1      3.33   22.9%   70.9%
Air pressure loss               1      2.67   18.3%   89.1%
Conveyor jam                    1      1.58   10.9%  100.0%
```

---

## 9. Writing to a live process

Read `src/factorylink/safety.py` before you enable a single write. The short
version:

* **This is not a safety system.** No SIL rating, no redundancy, no proof
  test, no defined behaviour on loss of power or network. It runs on a
  general-purpose OS over a general-purpose network, both of which stall for
  seconds without warning. Safety functions belong in a safety PLC or a
  hardwired relay. Interlocks belong in the control PLC.
* **Read-only is the default**, and it should stay that way unless somebody
  has a specific, reviewed reason.
* **Allow-list, not a pattern.** An explicit list of tag names, plus the tag's
  own `writable` flag in the database. Two independent gates.
* **Clamp to the engineering range.** A units mistake then becomes a clipped
  setpoint instead of a full-scale command.
* **Rate limit per tag.** A stuck loop in calling code must not hammer a
  setpoint.
* **Confirmation tokens for critical tags.** Request a token bound to one tag
  and one value, then present it. Single use, expires.
* **Audit everything**, especially the refusals.

And the operational rule that matters more than any of the above: *the PLC
should reject a bad command on its own*. Range checks, interlocks and mode
conditions belong in ladder or structured text, where they run every scan
regardless of what the network is doing. Everything in this module is a second
line of defence, not the first.
