# Status

## Current state (2026-08-27, later session)

**Still debugging `disk_access_init()` reliability on spi22/P1 wiring.**
Major findings from an extensive on-hardware debugging session, in order:

1. **Nordic Errata No. 100** ("GPIO: Pins with analog functionality might be
   shorted together during power-on reset") names P1.06/P1.07/P1.10/P1.11 --
   our exact SCK/MOSI/MISO/CS pins -- as prone to being shorted together or
   to ground around reset. This is a real, Nordic-documented silicon issue,
   not a wiring mistake.
2. Tried moving to P0.00-P0.03 (not covered by the errata, freed by
   disabling `&uart30`) using spi22 with a custom pinctrl override. This
   **compiled fine but never worked electrically** -- confirmed via Nordic's
   own "Essential pin planning guidelines for the nRF54L Series" blog post:
   the nRF54L15 has hard power-domain routing restrictions. spi22 belongs to
   the "PERI" domain, hard-wired to P1 only; P0 belongs to the separate "LP"
   domain. The devicetree/pinctrl tooling doesn't validate this at build
   time, so the mistake only showed up as 100% deterministic hardware
   failure.
3. Switched to **spi30** (the LP-domain peripheral, confirmed to share its
   register address with `uart30` -- same physical hardware block, two
   modes). Found via Nordic's own test overlay
   (`nrf/tests/drivers/spi/spim_instances/`) that only specific pins within
   P0 carry spi30's signals (SCK=P0.00, MISO=P0.03, MOSI=P0.04 -- not
   P0.01/P0.02 as first guessed). Got this wired and building correctly, but
   on real hardware it was **also 100% deterministic failure** -- a raw
   hex-dump probe (bypassing the SD stack entirely) showed the MISO receive
   buffer reading all-`0x00` or all-`0xFF` depending on the reset, never
   real varying data, identically whether the SD card was physically
   inserted or removed. Points to a receive-path issue on this specific
   peripheral/pin combination not diagnosable further without an
   oscilloscope.
4. **Reverted to spi22/P1** (the errata-affected but at-least-sometimes-
   working pins) given spi30/P0 was consistently broken rather than
   intermittently broken.
5. **Critical methodology finding**: testing via `jlink.reset()` (a
   debugger-triggered soft reset) vs. a genuine USB power cycle are **not
   equivalent** on this pin set. Nordic's own workaround for Errata No. 100
   is literally "do not assert nRESET during power-on reset" -- and in
   practice, 28 consecutive `jlink.reset()` attempts on spi22/P1 produced
   **zero** instances of valid CMD8/SDHC detection, while genuine power
   cycles (USB unplug/replug) produced real `Found SDHC with CMD8 support`
   log lines on 2 out of 7 attempts -- something never once seen across all
   the soft-reset testing. Soft resets are not a valid stand-in for testing
   this specific erratum's behavior.
6. Even with proper power-cycle testing, full `disk_access_init()` success
   is still not reliable (0/7 fully clean inits so far, though 2/7 got
   genuinely further than any soft-reset attempt before still ultimately
   failing later in the ACMD41 negotiation or a subsequent step). Actively
   continuing to debug this -- next step is capturing a *complete* log of a
   power-cycle attempt that starts well, to see exactly where it still
   breaks down.

## Earlier state (2026-08-27, first session)

**SD card is wired up (CLK/CMD/D0/D3 -> P1.11/P1.06/P1.07/P1.10, 3.3V/GND
from the DK) but `disk_access_init()` is not yet succeeding reliably.**
Debugged with `CONFIG_SD_LOG_LEVEL_DBG`/`CONFIG_SDHC_LOG_LEVEL_DBG` (both
now in `firmware/prj.conf`, commented as temporary) and repeated resets
captured over RTT channel 0:

- 0/8 reset attempts succeeded in one test run.
- Failures show **varying underlying error codes across resets** (`-134`
  EILSEQ, `-5` EIO, `-22` EINVAL) at different stages (sometimes CMD0/CMD8
  themselves fail, sometimes it gets past both and into the CMD55/CMD41
  voltage-negotiation retry loop before failing) -- inconsistent failure
  point across identical resets points to a marginal electrical issue, not
  a firmware logic bug (the logic clearly works correctly when it gets a
  clean response, e.g. attempt 1 above progressed normally into ACMD41).
- Also lowered `spi-max-frequency` from 24MHz to 4MHz in the overlay
  (`firmware/boards/nrf54l15dk_nrf54l15_cpuapp.overlay`). Turned out to be
  moot for the actual failure: Zephyr's SD stack always negotiates its own
  fixed 400kHz clock for the CMD0-ACMD41 identification phase regardless of
  `spi-max-frequency` (confirmed in `zephyr/subsys/sd/sd.c`,
  `SDMMC_CLOCK_400KHZ`) -- that setting only affects post-init data-transfer
  speed. Still a reasonable conservative choice for breadboard wiring;
  raise it again once the connection is solid and reliable.
- **Pull-up-resistor hypothesis (ruled out).** Identified the exact board in
  use -- Waveshare 3947 (Micro SD Storage Board) -- and pulled its actual
  schematic
  ([PDF](https://files.waveshare.com/upload/8/83/Micro-SD-Storage-Board-Schematic.pdf)).
  It already has onboard 10K pull-ups (R1-R7) to 3.3V on every relevant
  line (`D0`/`D1`/`D2`/`D3`/`CMD`/`CLK`, plus the separate `CD` pin) --
  missing pull-ups was not the problem. (Earlier guess was wrong: "bare
  pins, no visible components" was an inaccurate visual read of the board;
  the passives are there, just small SMD parts.)
- **Retry-the-whole-init experiment.** Added a 5-attempt retry loop around
  `sd_block_init()` in `firmware/src/main.c` (200ms between attempts),
  reasoning that since some individual resets got further than others
  (see above), retrying might succeed where a single attempt didn't. It
  didn't help -- 0/6 resets succeeded even with 5 retries each within the
  same boot, and more tellingly, **`CMD0` itself** (the simplest possible
  SD command, needs only CLK toggling + CS asserted) started failing more
  often than before, not less. That regression points away from
  firmware/timing entirely and back to a physical connection problem --
  something more fundamental than protocol-level flakiness.
- **Current focus: re-verify the physical wiring itself.** Double-checking
  wire-by-wire that CLK/CMD/D0/D3/3.3V/GND actually land on the intended
  physical DK header pins (easy to miscount positions on a 2xN header),
  and that every connection is firm (breadboard/jumper connections are a
  common source of exactly this kind of intermittent behavior). README now
  documents the exact Waveshare 3947 pin-to-DK-pin mapping being used.

## Previous state (2026-08-25)

Firmware **builds, flashes, and runs on the real nRF54L15-DK.** RTT link to
the host is confirmed working end-to-end: `sdrtt.py info` connects, sends a
framed request, and gets back a correctly-CRC'd response. It reports
`STATUS_ERROR` because no SD card is wired up yet — that's the expected,
correct behavior (`disk_access_init()` fails cleanly, firmware surfaces the
error instead of hanging).

Also confirmed the actual point of the project: with the board flashed and
RTT traffic flowing, `lsusb` shows only the J-Link debug probe
(`1366:1069 SEGGER J-Link`) — no mass-storage or other device appears. This
was checked with no SD card present, so it's not full confirmation (the
concern would be if enumerating an SD card via SPI at runtime somehow
triggered a new device — it can't, since there's no USB device stack in this
firmware at all, but worth a final sanity check once the card is wired up
too).

**Blocked on the SD card reader/breakout module delivery** for the SPI/SD
electrical bring-up (wiring, pin numbers in the overlay, and the actual
`disk_access_init()` success path).

Host-side protocol code (`host/protocol.py`) is unit-tested and passing
without hardware (`python3 host/test_protocol.py`, 9 tests). `host/sdrtt.py`
(the RTT CLI) is confirmed working against real hardware for `info`, `write`,
and `read` -- all three round-trip correctly over RTT and report the expected
clean `ERROR` status (no SD card wired up yet, so `disk_access_*` calls fail,
but the framing/CRC/chunking/error-propagation all work). `dump`/`restore`
are written but not yet exercised on hardware.

**`host/fake_firmware.py`** is a hardware-free double for the firmware's
frame-handling logic (`FakeFirmware`, mirroring `rtt_proto.c` + `main.c`'s
dispatch, as a second independent implementation, not generated from the C).
It exposes the same `rtt_write`/`rtt_read` interface `sdrtt.py` calls on a
real `pylink.JLink`, so it can stand in for the board entirely.
`host/test_sdrtt.py` (8 tests, all passing) exercises `info`/`write`/`read`/
`raw-read`/`dump`/`restore` through it, including multi-chunk transfers
(>32 sectors, forcing multiple RTT requests) and a regression test for the
exact READ-response hang bug found on real hardware. Caveat documented in
`fake_firmware.py`'s docstring: it's hand-maintained, not derived from the C,
so it won't automatically catch a future firmware regression unless the
double is updated to match -- it's real coverage for the host-side chunking/
header/error-propagation logic, not a substitute for hardware testing.

`write` now embeds a small length header (`host/protocol.py`:
`build_header`/`parse_header`, host-side only, not part of the wire protocol)
so `read` auto-detects how many bytes/sectors to fetch instead of requiring
an explicit count. `dump`/`restore` stay header-free for byte-exact whole-card
images; `raw-read` is the explicit-count escape hatch for sectors not written
via `write`.

**Real bug found and fixed while first exercising `read` on hardware:** the
firmware's READ response omitted the payload entirely on error, but the host
always expects a response sized to the sector count *it* requested --
mismatched lengths made `read` hang until timeout instead of failing cleanly.
Fixed in `firmware/src/main.c` by zero-filling and sending the full-size
payload on error too (same pattern `INFO`'s response already used). This is
the kind of bug that's easy to miss without hardware in the loop -- `write`
and `info` happened to not hit it, since their response sizes were already
status-independent.

**Windows portability pass:** `host/*.py` were audited against the pattern
used in the sibling `nRF54L15-storage` project (a related on-chip-RRAM
storage tool of the user's) -- adopted `pathlib.Path` for all file I/O and
added a clearer connection-failure error message pointing at the J-Link
Software Pack download. Deliberately did *not* adopt that sibling project's
"avoid pylink, shell out to JLinkExe directly" approach: that works for their
tool because it only needs flash `loadfile`/`savebin`, which JLinkExe's CLI
scripting handles fine, but our RTT-based bidirectional binary protocol has
no clean CLI-scriptable equivalent (would mean hand-rolling the SEGGER RTT
ring-buffer protocol via raw memory pokes). `pylink-square` already ships
official Windows wheels, so it isn't actually the portability gap here.
README now has a Windows/PowerShell command block alongside the Linux/macOS
one -- **not tested on real Windows**, direct translation only.

**Sector-size safety check + byte/MB offset addressing:** `sdrtt.py` now
queries the card's real sector size via `INFO` before any
`read`/`write`/`raw-read`/`dump`/`restore` and refuses to proceed if it ever
disagrees with the hardcoded 512-byte `SECTOR_SIZE` constant, rather than
silently doing the wrong sector-boundary math. Chose validate-and-fail-loudly
over threading a dynamic sector size through firmware buffers/RTT
framing/host chunking -- real SD/SDHC/SDXC cards are universally 512-byte
block-addressed by spec, so full adaptation isn't worth the added surface
for a case that essentially never happens. Also: `sector` on
`read`/`write`/`raw-read` now accepts a size-suffixed offset (`2M`, `512K`,
`1G`) as an alternative to a raw sector number
(`protocol.parse_sector_or_offset`), converted before anything else runs;
a non-sector-aligned offset is rejected with the nearest valid sector
numbers rather than silently rounded. 29 tests passing (up from 19).

## Discoveries / gotchas found along the way

- **Kconfig for disk access is `CONFIG_DISK_ACCESS`, not `CONFIG_DISK_DRIVERS`.**
  `DISK_DRIVERS` only builds the low-level per-backend drivers; the
  `disk_access_read/write/ioctl/init()` API firmware actually calls
  (`firmware/src/sd_block.c`) lives behind `CONFIG_DISK_ACCESS`, which selects
  `DISK_DRIVERS` itself. Confirmed by reading
  `~/ncs/v3.4.0/zephyr/subsys/disk/Kconfig`.
- **SD-over-SPI needs `CONFIG_SDMMC_STACK=y`** to pull in the SD command stack
  (it selects `CONFIG_SDHC`). Once that's on, `CONFIG_DISK_DRIVER_SDMMC` and
  `CONFIG_SPI_SDHC` default to `y` on their own, gated by whether the
  devicetree overlay actually has `zephyr,sdmmc-disk` / `zephyr,sdhc-spi-slot`
  nodes present.
- **`&spi22` (the DK's `nordic_expansion_spi` label) conflicts with UART30's
  default pin assignment** on the nRF54L15-DK — the devicetree overlay
  disables `&uart30` to free those pins.
- Nordic DevZone has open reports of SD-over-SPI init failures specifically on
  nRF54L15 (CMD8 timeout → "Legacy card detected", CMD58 "No OCR" →
  `disk_access_init()` returns -134). Environment/wiring/card-dependent —
  something to watch for once real hardware is in the loop, not something
  fixed preemptively in code.
- Installed via **nRF Util** (`nrfutil sdk-manager`), Nordic's current
  official CLI installer — see [README.md](README.md) for exact commands.
  Needed to free ~5GB (pip cache) first on this machine before the SDK +
  toolchain download would fit.
- The J-Link device name for pylink's `jlink.connect()` is **`NRF54L15_M33`**,
  not `NRF54L15_XXAA` as originally guessed — found by probing with
  `JLinkExe` directly. `host/sdrtt.py`'s default is updated to match.
- `pip install` into system Python is blocked by Debian/Ubuntu's PEP 668
  guard; use a venv for the host tool (`host/.venv`, see README).

## How to build / use

See [README.md](README.md) — toolchain setup, build command, flashing, and
host tool usage are all there and confirmed working through the build step.

## Next steps (SD card reader is in hand now)

1. **Wiring overlay corrected (2026-08-27)**: the overlay previously had
   placeholder SPI pins that turned out to collide with onboard buttons/LEDs
   (P1.08/P1.09/P1.10 = Button2/Button1/LED1). Replaced with Nordic's own
   verified default pin assignment for this exact node (`nordic_expansion_spi`
   in `nrf54l_05_10_15_cpuapp_common.dtsi`) instead of guessing -- see the
   wiring table in [README.md](README.md#wiring-the-sd-card). Rebuilt and
   reflashed with the corrected overlay; RTT link still confirmed working
   (`sdrtt.py info` still cleanly reports `ERROR` with no card wired up,
   same as before -- no regression).
2. Wire the SD breakout module's SCK/MISO/MOSI/CS/3V3/GND to
   P1.11/P1.07/P1.06/P1.10/VDDIO/GND per that table.
3. Run `host/.venv/bin/python host/sdrtt.py info` — RTT link is already
   proven working; this run is specifically to test SD-over-SPI init. If
   `disk_access_init()` fails (see CMD8/CMD58 note above), check
   wiring/pull-ups before touching firmware logic.
4. Once `info` reports sane sector size/count, test `read`/`write`/`dump`
   round-trips against a throwaway card (or spare sectors) before trusting it
   with real data.
5. Re-check `lsusb` with the card wired up and RTT traffic flowing, to
   confirm nothing new appears now that real SPI/SD activity is happening
   (expected: still just the J-Link probe, per the reasoning above).

## Planned next improvement: encrypt data at rest (not started)

Once raw read/write against a real card is confirmed working end-to-end (the
steps above), the next improvement is encrypting data at rest using the
nRF54L15's on-chip security hardware — **CRACEN** (hardware AES/SHA/ECC/TRNG,
exposed via Zephyr's PSA Crypto API) and **KMU** (hardware key storage that
can hold an AES key CRACEN can use for encrypt/decrypt without firmware ever
being able to read the key back out).

Full design — cipher choice (AES-256-CTR, per-sector deterministic IV), key
provisioning (generated on-device from CRACEN's TRNG into a KMU protected
slot, no export path — key loss means data loss, by design), the exact PSA
API calls involved, and a step-by-step implementation/verification plan — is
written up in [ENCRYPTION_PLAN.md](ENCRYPTION_PLAN.md). Deliberately not
started yet: layering hardware-backed crypto onto a data path that hasn't
seen a real SD card would make it hard to tell which layer broke if
something failed. Pick this up after the raw-storage path above is solid.

## Planned next improvement: store a git repository on the hidden storage (not started)

Also queued for after the raw-storage path is confirmed: a host-side wrapper
(`host/git_hidden.py`, no firmware changes) that uses `git bundle` --
git's own format for offline/removable-media transport -- around the
existing `write`/`read` commands. `push <sector>` snapshots refs into a
bundle and writes it (full-state replace, not incremental); `fetch`/`clone
<sector>` reads the bundle back and lets git unpack it natively, since git
already treats a bundle file as a valid fetch/clone source. Full design in
[GIT_STORAGE_PLAN.md](GIT_STORAGE_PLAN.md). Chosen over a full custom
`git-remote-helper` protocol implementation -- our storage is a dumb blob
store with no live git process on the other end, so push would be a
whole-bundle replace either way; the extra protocol work would only buy UX
polish (`git push card` instead of `git_hidden.py push <sector>`), not real
incremental transfer.

## RESOLVED (2026-08-27, evening) -- SD card works end to end

`sdrtt.py info` reports the card correctly and a write/read round-trip is
byte-for-byte identical, verified against a **stock, unpatched** NCS v3.4.0.
No Zephyr SDK modifications are required.

    sector_size=512 sector_count=61765632 capacity=30159.0 MiB

Cross-checked against ground truth read from the same card in a laptop: CID
manufacturer 0x1d (ADATA), OEM "AD", 61765632 sectors. The DK reads exactly
the same values, and sector 0 decodes as a valid FAT32 MBR (type 0x0c, start
2048, 61763584 sectors, which sums exactly to the card's capacity).

### Two root causes, both real

**1. Supply voltage was 1.8 V (the decisive one).** The nRF54L15 DK ships with
its buck regulator at 1.8 V; the SD spec requires 2.7-3.6 V. Found by Daniel
measuring VDD_IO to GND with a multimeter. Fixed via nRF Connect for Desktop
-> Board Configurator -> VDD = 3.3 V (persists in the DK).

This single setting explains every earlier symptom, including the ones that
looked impossible:

- cards that answered short commands but failed all data transfers
- a CSD that arrived with a valid transport CRC16 but failed its own internal
  CRC7, decoding to a nonsense 2 GB capacity and the wrong addressing mode
- one card whose reported identity differed from what a laptop read
- another card that never left the ACMD41 idle state (R1=0x01) through 1000
  retries -- it physically could not complete initialisation at 1.8 V
- **the loopback test passing**, which was the single most misleading piece of
  evidence: it clocked nRF->nRF at 1.8 V, worked perfectly, and never involved
  the card at all

**2. The SPI pins collided with the on-board debugger.** Table 9 of the DK
Hardware User Guide maps the two J-Link virtual COM ports to P0.00-P0.03 and
P1.04-P1.07, routed through analog switches U4/U5. spi22's stock default pins
are P1.06/P1.07 -- the UART1 RTS/CTS lines. Moved to SCK=P1.12, MOSI=P1.08,
MISO=P1.11, CS=P1.14.

### Other fixes made along the way

- `overrun-character` had been set to 0x00 (copied from a Nordic SPI loopback
  test). SD requires MOSI held high while reading, and Zephyr's `sdhc_spi_rx()`
  passes a NULL TX buffer, so every read byte is the overrun character. Now
  left at the nRF default of 0xff.
- `host/sdrtt.py` never drained the RTT up-buffer on connect, so a response
  frame left over from a previous invocation was consumed as the current one.
  Because `transact()` only validated the opcode, a stale READ response
  verified fine and silently returned another sector's data. Added
  `drain_channel()`.
- Added `sd_spi_prime_bus()` in `main.c`: Zephyr's `sdhc_spi_init_card()` sets
  `SPI_CS_ACTIVE_HIGH` to hold CS inactive for the SD spec's 74 power-up
  clocks, but that flag is ignored by the generic SPI context and the nRF SPIM
  driver, so those clocks go out with CS asserted (zephyrproject-rtos/zephyr
  issue #29946). Kept because it is spec-correct, though it was not the cause.

### Notes for future work

- Only spi20/21/22/30 can drive an SD card. spi00 runs from the 128 MHz PLL
  with a 4..126 prescaler, giving a floor near 1 MHz; SD identification needs
  400 kHz, so spi00 fails with -EINVAL. Confirmed by Nordic's own DevZone
  guidance to use SPIM20/21/22/30 for SD cards.
- Peripheral-to-port routing is fixed in silicon: spi00->P2, spi20/21/22->P1,
  spi30->P0. A mismatched pin/peripheral combination compiles without error
  and cannot work.
- Zephyr's `sdmmc_spi_read_cxd()` returns 0 even when the underlying request
  fails, so a failed CID/CSD read is silently treated as success and garbage is
  decoded as a valid register. Worth being aware of when debugging.

### Correction: the Goodram card was never faulty

Re-tested at 3.3 V, the Goodram card works exactly as well as the ADATA one:

    sector_size=512 sector_count=30617600 capacity=14950.0 MiB
    Card MID: 0xc9, OID: `M
    Card block count 30617600, block size 512

and a write/read round-trip is byte-for-byte identical. Those values match what
Linux reads from the same card (manfid 0x0000c9, oemid 0x4d60, 30617600
sectors) precisely.

Everything previously attributed to that card was an artefact of running it at
1.8 V: the "NCard" product string, manufacturer ID 0x89, the 3904512-block
capacity, and the CSD that failed its own CRC7. Under-volting made the card
fabricate self-consistent but entirely false register contents, which is why
the wrong values looked so convincing -- they even decoded into a coherent
capacity and a matching partition table.

Conclusions drawn during that period -- that the card was counterfeit, or that
its SPI mode was broken -- were wrong. Both cards are genuine and both work.
The lesson worth keeping: a reading can be perfectly self-consistent and still
be entirely wrong if the measurement conditions are out of spec. Confirm supply
voltage before interpreting any register content.

## Throughput tuning (2026-08-28)

Measured on a 4 MiB round-trip, verified byte-identical each time:

| Setting | Write | Read |
|---|---|---|
| 32-sector chunks, 4 MHz SPI, 1ms/5ms polls | 51.1 s (82 KB/s) | 53.4 s (79 KB/s) |
| 64-sector chunks, 8 MHz SPI, 100us/0.2ms polls | 42.7 s (98 KB/s) | 45.3 s (93 KB/s) |

About 20% faster. Changes: `SD_BLOCK_MAX_XFER_SECTORS` 32 -> 64 (with
`MAX_XFER_SECTORS` in host/protocol.py kept in step), poll sleeps cut from
1 ms (firmware) and 5 ms (host) to 100 us and 0.2 ms, and `spi-max-frequency`
raised from 4 MHz to spi22's 8 MHz ceiling. RAM went from 102 KB to 184 KB of
256 KB, since five buffers scale with the chunk size.

**The remaining limit is the debug probe, not this project.** RTT works by
having the probe read target RAM over SWD, so the SWD clock caps throughput.
The DK's on-board J-Link is fixed at 2 MHz: requesting 4/8/12/16 MHz is
accepted silently, still reports 2000 kHz, and changes the measured time not
at all (4 MiB took 42.74 s either way). At roughly 50 SWD clocks per 32-bit
word, 2 MHz gives about 160 KB/s theoretical, which brackets the ~98 KB/s
observed.

Doubling the chunk size only bought ~20% rather than ~50%, which confirms the
cost is per-byte (link bandwidth) rather than per-round-trip (latency): a
16 KB chunk took 200 ms, a 32 KB chunk takes 334 ms, so only ~66 ms of fixed
overhead per exchange was eliminated.

Going meaningfully faster needs a standalone J-Link probe that honours a
higher SWD clock -- `SWD_SPEED_KHZ` in host/sdrtt.py already requests it. The
SD bus itself is not the constraint: at 8 MHz it can sustain roughly 1 MB/s.

### RAM cleanup (2026-08-28)

RAM dropped from 184 KB to 145 KB of 256 KB (70% -> 55%), flash from 57.8 KB
to 55.0 KB, with no throughput change and a 5 MiB round-trip still verified
byte-identical.

Three changes:

- **The READ path no longer stages sector data twice.** `main.c` used to read
  into a private `data[]` buffer and then memcpy the whole payload into
  `rsp_buf`. `sd_block_read()` now writes straight into the response frame's
  payload area via a new `send_response_in_place()`, removing a
  `SD_BLOCK_MAX_XFER_SECTORS * SD_SECTOR_SIZE` buffer (32 KB) and a 32 KB copy
  per read. The WRITE path never had this detour -- it already handed
  `&req_buf[7]` straight to `sd_block_write()`.
- `CONFIG_SD_LOG_LEVEL_DBG` back to `INF`. DBG logs every SD command; it was
  what exposed the corrupt CSD during bring-up, but it is high volume and
  costs time on every transfer.
- `CONFIG_LOG_BUFFER_SIZE` 8192 -> 2048, which was only large to cope with
  DBG-level volume.

Also removed `CONFIG_LOG_BACKEND_RTT_BUFFER_SIZE=8192`: **it is not a real
Kconfig symbol**. It never appeared in the generated `.config` and had no
effect -- the RTT log channel is sized by `CONFIG_SEGGER_RTT_BUFFER_SIZE_UP`,
still at its 1024 default. A setting that silently does nothing is worse than
no setting, since it invites the assumption that a knob has been tried.

Remaining RAM is dominated by the four unavoidable 32 KB buffers that scale
with the chunk size: the two RTT ring buffers plus `req_buf` and `rsp_buf`.

### Core "invisible to the OS" claim verified (2026-08-28)

PLAN.md's headline requirement -- the host OS never sees a mass-storage
device -- was finally tested against real hardware, and holds. With the DK
actively reading the 16 GB SD card over RTT, the host's block-device list
(`lsblk`, loop devices filtered out) is byte-for-byte identical before and
during the transfer: no `mmcblk*`, no new `sd*`, and `journalctl -k` logs zero
mmc/scsi/usb-storage events. The only removable device present is the DK's own
10.7 MB J-Link firmware volume, which is there regardless.

So the card is fully accessible to the firmware and the RTT host tool while
being entirely absent from the operating system's storage stack, which is the
whole purpose of the design.

## Encryption added (2026-08-28) -- firmware-side CRACEN, AES-256-CTR

Optional payload encryption is now implemented and verified on hardware. It
closes the gap PLAN.md called out: "hidden" previously meant the OS did not
enumerate the card, but the raw sectors were plaintext.

Design (see the encryption section in README.md for usage):
- Runs entirely in the firmware on the nRF54L15's CRACEN hardware crypto
  engine via the PSA Crypto API (`firmware/src/crypto.c`). The host does no
  crypto; the card holds only ciphertext.
- Passphrase-based, derived on-device: the host sends the passphrase over the
  local RTT link, the DK runs PBKDF2-HMAC-SHA256 (20000 iterations, ~2.3s) to
  derive a 256-bit key, held in a volatile PSA key slot for the session only.
- AES-256-CTR keyed per absolute sector number (IV = sector in the high 64
  bits), so ciphertext is exactly 512 bytes/sector and every sector is
  independently accessible. Confidentiality, not integrity -- matches the
  passive-read threat.
- Opt-in and per command: a `RTT_PROTO_FLAG_ENC` (0x40) bit in the opcode byte
  selects encryption on a READ/WRITE. `write`/`read` set it under `--encrypt`;
  `raw-read`/`dump`/`restore` never do (so raw reads expose the ciphertext).
  Without `--encrypt`, behaviour is byte-for-byte unchanged.
- New opcode `RTT_PROTO_OP_UNLOCK` (0x03) sets the session key.

On-hardware verification (Goodram + ADATA both fine):
- Encrypted write then read with the correct passphrase: byte-identical.
- Read with a wrong passphrase: fails cleanly (no valid header), no plaintext.
- Raw read of the encrypted sectors: no `HSD1` magic, no plaintext, ~7.5
  bits/byte entropy over 512 bytes -- the card genuinely holds ciphertext.
- 5 MiB encrypted round-trip: byte-identical, ~92 KB/s -- within noise of the
  ~93 KB/s plaintext rate, so CRACEN adds no measurable overhead (still
  SWD-bound).

Cost: flash 55.0 KB -> 68.8 KB (CRACEN/PSA), RAM essentially unchanged
(145.3 -> 145.9 KB). Builds and runs on a stock, unpatched NCS v3.4.0.

Known limitation: the PBKDF2 salt is a fixed build-time constant
(`firmware/src/crypto.c`) -- adequate for a single-user store, upgradable to a
random per-card salt in a reserved sector later without a wire-protocol change.
