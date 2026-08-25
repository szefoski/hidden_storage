# Hidden SD Storage via nRF54L15-DK (SPI passthrough over J-Link RTT)

## Context

Goal: an SD card wired to the nRF54L15-DK over SPI, driven entirely by firmware on
the DK. A host PC never talks to the SD card directly and never sees a USB mass
storage device — instead, the host uses J-Link RTT (over the existing debug
connection) to send raw sector read/write commands to the firmware, which relays
them to the SD card over SPI. There is no USB device stack in the firmware at all,
so the OS has nothing to enumerate. This is a fresh, empty project directory — no
existing code to build on.

## Architecture

```
Host PC (Python, pylink/RTT)  <-- J-Link debug USB -->  nRF54L15-DK firmware  <-- SPI -->  microSD card
       sdrtt.py CLI                                     (Zephyr disk_access API)         (raw sectors, no FS)
```

- No filesystem is mounted anywhere in the firmware — sector I/O only, via Zephyr's
  `disk_access_read()` / `disk_access_write()` against the `sdmmc-disk` block device.
  This reuses Zephyr's existing SD-over-SPI driver (`sdhc-spi-slot` compatible +
  `sdmmc-disk` child in devicetree) instead of writing a custom SD/SPI protocol stack.
- No `CONFIG_USB_DEVICE_STACK`, no MSC sample class — the OS never sees a drive.
- RTT is used with a dedicated up/down buffer pair (buffer index 1) so the default
  buffer 0 stays free for normal Zephyr logging/console, kept separate from the
  binary command channel.

## Known hardware risk (flag to user, do not "fix" preemptively)

Nordic DevZone has reports of SD-over-SPI init failures on nRF54L15 (CMD8 timeout /
"Legacy card detected", CMD58 "No OCR" → `disk_access_init()` returns -134). This is
environment/wiring/card-dependent. Plan accounts for it by using Zephyr's stock
`sdmmc_spi` driver defaults (400 kHz init clock, standard init sequence) and by
surfacing `disk_access_init()` failures clearly over RTT rather than masking them.
If it occurs on real hardware, next steps are pull-up/wiring checks or a slower
init clock — not something to speculatively code around now.

## Firmware (nRF Connect SDK / Zephyr app)

Directory: `firmware/`

- `firmware/CMakeLists.txt`, `firmware/prj.conf` — standard NCS app skeleton.
- `firmware/boards/nrf54l15dk_nrf54l15_cpuapp.overlay` — devicetree overlay:
  - Use `&spi22` (labeled `nordic_expansion_spi` on this board's DK expansion
    connector) as the SPI bus, since `&spi00` is already claimed by onboard
    external flash. Note: spi22 conflicts with UART30 pin defaults on this board —
    the overlay must reconfigure/disable UART30 pinctrl for those pins.
  - Add an `sdhc-spi-slot` compatible child node with an `sdmmc-disk` node,
    `cs-gpios`, and a `pinctrl-0` group for spi22 pins matching the user's actual
    wiring to the SD breakout module (SCK/MOSI/MISO/CS) — placeholder pins in the
    overlay, called out as **needs verification against actual wiring** since I
    cannot see the physical connections.
- `firmware/prj.conf` key options:
  - `CONFIG_DISK_DRIVERS=y`, `CONFIG_DISK_DRIVER_SDMMC=y`, `CONFIG_SPI=y`
  - `CONFIG_USE_SEGGER_RTT=y` for direct `SEGGER_RTT_*` buffer API access
  - USB device stack left disabled (default) — explicitly documented as the
    reason no MSC device ever appears.
- `firmware/src/main.c` — init disk, init RTT buffer 1, run command loop.
- `firmware/src/rtt_proto.c/.h` — binary frame protocol:
  - Frame: `[opcode:1][sector:4 LE][count:2][payload...][crc16:2]`
  - Opcodes: `0x00 INFO` (returns sector size + sector count), `0x01 READ`,
    `0x02 WRITE`; responses use `0x80 | opcode` with a status byte + payload.
  - Reused: CRC16-CCITT (small table-based implementation, mirrored in Python).
- `firmware/src/sd_block.c/.h` — thin wrapper around `disk_access_read/write/ioctl`
  for the `"SD"` disk name, translating protocol requests into disk_access calls.

## Host tool (Python)

Directory: `host/`

- `host/requirements.txt` — `pylink-square`
- `host/sdrtt.py` — CLI using pylink to attach to the nRF54L15 target (cpuapp
  core), open RTT, and speak the same frame protocol as firmware:
  - `sdrtt.py info` — print sector size/count
  - `sdrtt.py read <sector> <count> -o out.bin`
  - `sdrtt.py write <sector> <count> -i in.bin`
  - `sdrtt.py dump -o image.bin` / `sdrtt.py restore -i image.bin` — whole-card
    convenience wrappers around read/write
- `host/protocol.py` — frame encode/decode + CRC16, kept separate from `sdrtt.py`
  so it can be unit-tested without hardware.

## Verification

- `west build -b nrf54l15dk/nrf54l15/cpuapp firmware/` — confirms the firmware
  compiles against the real board target and devicetree overlay.
- `python -m pytest host/` (or a small `host/test_protocol.py`) — round-trips
  frame encode/decode and CRC16 without needing hardware.
- Documented manual hardware test (for the user to run, since no physical board/SD
  card is attached in this environment): flash via `west flash`, run
  `sdrtt.py info` then `sdrtt.py write`/`read` against a test sector, and confirm
  the OS's disk/device list shows no new drive while this is happening.

## Not building in this pass

- No encryption/steganography layer — the current requirement is "OS doesn't see
  a mass-storage device," not defeating forensic analysis of the SD card's raw
  bytes. Can be added later as a payload-encryption step in the protocol if needed.
