# Lessons Learned: The 1.8 V SD Card

A debugging post-mortem from bringing up an SD card over SPI on an
nRF54L15-DK. The bug took most of a day. The fix was a board setting that
takes ten seconds and one multimeter probe.

Written to be read cold by someone who was not there.

---

## TL;DR

An SD card refused to work over SPI. The investigation produced elaborate
theories about silicon errata, counterfeit hardware, and broken card firmware.

**The actual causes were two configuration mistakes:**

1. The development kit ships with its supply set to **1.8 V**. SD cards
   require **2.7-3.6 V**.
2. The SPI bus was assigned to pins that are physically wired to the
   on-board debugger's serial ports.

Neither is exotic. Both are documented in the vendor's own hardware guide.

The interesting part is not the bug. It is **why a competent investigation
walked past it for hours**, and what made the wrong evidence so convincing.

---

## What actually happened

| Symptom | Interpretation at the time | Reality |
|---|---|---|
| Card never initialises | Wiring or driver problem | Pins shared with debugger UART |
| Card reports 2 GB instead of 16 GB | Card is counterfeit | Undervolted card fabricating registers |
| Capacity register fails its own checksum | Card firmware is defective | Card could not read its own flash at 1.8 V |
| Reads return other sectors' data | Card has broken SPI mode | Wrong capacity implied wrong addressing |
| A second card fails differently | Confirms card-specific faults | Both cards undervolted, each failing its own way |
| **Loopback test passes** | **The bus is proven good** | **Tested MCU-to-MCU at 1.8 V; card never involved** |

Every anomaly had one cause. Each was investigated as if it were separate.

---

## Root cause 1: supply voltage

The kit's regulator defaults to 1.8 V, changed only through a vendor GUI
utility. The hardware guide states it plainly:

> "BUCK supplies VDDM input with a default voltage of 1.8 V... To change the
> VDDM voltage, use the Board Configurator GUI."

That document had already been downloaded and searched — for pin maps. The
power section was never opened.

**Why 1.8 V produced such convincing wrong answers**, rather than obvious
failure:

A memory card is a small computer. Its **digital logic** (protocol engine,
command decode, checksum unit) works well below spec. Its **analog** circuits
(charge pumps, flash sense amplifiers, voltage references) do not.

The result is the worst possible failure mode: a device perfectly capable of
*talking to you*, but incapable of correctly *reading its own contents*. It
answered every command crisply, and everything it said was false.

Critically, **undervoltage biases; it does not randomise.** Randomness needs a
noise-dominated process. Undervoltage shifts switching thresholds and
propagation delays — same silicon, same voltage, same inputs, same wrong
answer every single time. This is why undervolted RAM fails the same bit
repeatedly rather than producing noise.

So the bad data was perfectly **reproducible**, which read as "trustworthy."

### The two registers failed in two different ways

Worth dwelling on, because it explains why the false readings were so
convincing — and because the failures were not the same kind:

| Register | Value at 1.8 V | Internal CRC7 | What it actually was |
|---|---|---|---|
| Identity (CID) | mfr `0x89`, name `"NCard"` | **valid** | a coherent **fallback identity** |
| Capacity (CSD) | ~2 GB | **invalid** | genuinely **corrupted bits** |

The identity register's checksum being *valid* means those bytes were a
complete, intact register — just not this card's. `0x89` / `"NCard"` turns out
to be a documented signature: it is not a registered manufacturer ID (tools
list it as "Unknown"), and it is reported in card surveys and initialisation
logs specifically in cases of card failure or corruption. It is what a card
presents when it cannot deliver its real identity — precisely what an
undervolted controller unable to read its own configuration flash would do.

The capacity register, by contrast, failed its own checksum. That was not a
substituted default; it was misread bits that happened to decode into a
plausible number.

Two different failure modes, in two registers, from one cause. Investigating
either in isolation led away from the real answer.

---

## Root cause 2: pins shared with the debugger

The SPI peripheral's default pins were the debugger's UART flow-control
lines, routed to the interface MCU through analog switches. Two devices drove
the same nets.

This is in the hardware guide too, in a pin-mapping table listing exactly
which GPIOs belong to the virtual COM ports.

**Takeaway:** a pin being unclaimed in software does not mean it is free.
Devicetree describes the chip; it does not describe what the board wired to
it.

---

## The five reasoning failures

### 1. Self-consistency was mistaken for correctness

The strongest wrong evidence: the card reported a bogus capacity, *and*
returned a partition table that ended at exactly that bogus capacity.

The reasoning was: *"corruption could not produce two independent structures
that agree perfectly — therefore this is real data."*

That inference is invalid. When an entire subsystem operates out of spec,
everything downstream of the bad assumption stays internally consistent while
being uniformly wrong.

> **Self-consistency is not evidence of correctness. It only proves a single
> shared cause — which may be the fault itself.**

### 2. A checksum was trusted beyond its scope

The transport checksum passed on every transfer. This was read as "the data
is correct."

It is not what that checksum means. The corruption happened *inside* the
device, before it computed and transmitted the checksum. A transport checksum
protects the **wire**, and cannot detect corruption upstream of transmission.

The evidence was there and was misread:

| Check | Result | What it actually meant |
|---|---|---|
| Transport checksum (computed at send time) | passed | the link was clean |
| Register checksum (stored inside the data) | **failed** | the content was already corrupt |

A stored checksum failing while a transmit checksum passes is the precise
signature of "corrupted before transmission." Both were measured. The
conclusion drawn was the opposite of the correct one.

> **Know what each checksum covers. "A CRC passed" answers a narrow question.**

### 3. A passing test was over-generalised

A loopback test passed, and was treated as proof the bus was healthy. It was
the single most misleading result of the day.

The loopback ran MCU output to MCU input. Both ends were the same chip at
1.8 V, where 1.8 V logic works perfectly. **The card was not part of the
test.** It proved the two pins were connected — nothing more.

> **State precisely what a passing test excludes. A test that omits the
> suspect component cannot exonerate it.**

### 4. Stale data was analysed as live

Diagnostics were read from a device log buffer without draining it first, so
output from *previous* runs was analysed as the result of the current change.

At one point this made two physically different cards appear to return
byte-identical identity registers — an impossible result that triggered a long
false investigation. The reading was simply old.

The same mistake was then repeated minutes later with cached OS device
information, after being corrected on it.

> **An impossible result is far more often stale than profound. Make freshness
> structural: drain, then trigger, then capture, and anchor on a marker that
> proves which run the data belongs to.**

### 5. Hardware was blamed before configuration was exhausted

Three times the conclusion was "the hardware is faulty": a silicon erratum, a
counterfeit card, a defective card controller. All three were wrong.

The erratum was especially seductive because it listed *the exact pins in
use*. A plausible story that matches the symptoms suppresses further search —
which is precisely when it is most dangerous.

> **An erratum that names your pins is a hypothesis, not a verdict. It still
> has to outrank the boring explanations.**

---

## What actually broke the deadlock

Not analysis. **A multimeter.**

The engineer measured the supply rail directly and found 1.8 V. Every
"impossible" observation collapsed into one mundane cause within minutes.

He had also said, repeatedly and correctly, that the problem was in the
configuration — while the investigation kept looking outward at the hardware.

> **When someone's direct physical observation contradicts your inference,
> your inference is the weaker evidence.**

---

## Checklist: hardware bring-up

Before interpreting *any* data from an external device:

- [ ] **Measure the supply rail.** Not "it should be 3.3 V" — put a probe on
      it. Development kits ship with non-obvious defaults.
- [ ] **Read the board's hardware guide, all of it** — power and pin-mux
      sections, not just the part you think you need. Devicetree does not tell
      you what the PCB wired where.
- [ ] **Confirm no other on-board device shares your pins** — debugger UARTs,
      LEDs, buttons, on-board flash.
- [ ] **Check vendor forums for your exact part and peripheral early**, not
      after exhausting your own theories.
- [ ] **Verify a known-good reference** (the same card in a PC) and record its
      real values, so you can compare against ground truth instead of
      plausibility.

When reasoning about results:

- [ ] Ask *"could this reading be stale?"* before *"what does this reading
      mean?"*
- [ ] For each passing test, state what it does **not** cover.
- [ ] For each checksum, state its **scope**.
- [ ] Treat self-consistent-but-surprising data as suspect, not as proof.
- [ ] Prefer changing one measurable condition over building a theory that
      explains the anomaly.

---

## Discussion prompts

1. Where else do we trust a checksum, health check, or green test beyond what
   it actually covers?
2. Where in our current tooling could stale data be presented as live? What
   would make freshness provable rather than assumed?
3. Which of our "known-bad component" conclusions were ever independently
   confirmed — and which are just plausible stories that stopped the search?
4. When a colleague's direct observation conflicts with our model, what
   normally wins?

---

## The one-line version

**Verify your measurement conditions before you interpret your measurements —
otherwise you will build a perfectly rigorous investigation on top of numbers
that were never real.**
