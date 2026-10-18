# Fighting 68 — native Python driver

A stand-alone driver for the VTER *Fighting 68* Hall-effect keyboard (USB
`0c45:8030`). It replaces the WebHID configurator at
<https://vter.driveall.cn/#/> for the parts that matter on a Hall-effect board:
**per-key sensitivity and rapid trigger, SOCD, and calibration**.

Everything here was recovered from that web app with REA
(<https://github.com/morluto/rea>) plus static reading of its bundle, and then
verified against a real Fighting 68 plugged into the machine.

## Run it

```console
> uv sync
> uv run python -m fighting68 list          # is the board there?
> uv run python -m fighting68 info          # board-wide Hall-effect settings
> uv run python -m fighting68 rt            # per-key sensitivity overrides
> uv run python -m fighting68 socd          # SOCD rules
> uv run python -m fighting68 calibrate     # press every key, get the measured travel
```

Take a backup before changing anything — `dump` / `restore` round-trip every
region byte for byte:

```console
> uv run python -m fighting68 dump -o my-board.json
> uv run python -m fighting68 restore my-board.json
```

### Qt interface

```console
> uv run python -m fighting68 gui
```

The same four areas as the CLI, in one window. **Sensitivity** draws the board
itself — the 65 keys at their real widths — and clicking one fills the form with
its actuation point, press/release sensitivity, switch type and rapid-trigger
flags. **SOCD** adds and removes rules, **Settings** is the board-wide block and
**Calibration** runs for 20 s and fills a per-key table as the reports arrive.
**Backup…** / **Restore…** in the header do what `dump` / `restore` do. Device
work runs off the GUI thread and each edit opens the board on its own, so nothing
blocks the window and nothing is written unless you press an Apply button.

### Hall-effect sensitivity and rapid trigger

Each key has an 8-byte rapid-trigger record. Travel values are in mm.

```console
> uv run python -m fighting68 rt A --trigger 1.2 --down 0.5 --up 0.08 --flags 3
> uv run python -m fighting68 rt W --trigger 0.8 --switch jade
> uv run python -m fighting68 rt A --clear
```

| field | meaning |
| --- | --- |
| `trigger` | actuation point in mm |
| `down` | how far you must press again before the key re-fires (mm) |
| `up` | how far you must lift before it releases (mm) |
| `flags` | `1` rapid trigger over the whole travel, `2` bottom optimization, `4` "Rampage Mode" |
| `switch` | switch type the key is calibrated against: `stock`, `aurora`, `jade`, `king` |

`info` carries the board-wide settings: top/bottom dead zones, report rate
(1/2/3 = 1/4/8 kHz), the stability filter, the rest timer and adaptive dynamic
calibration.

### SOCD

A rule resolves two keys pressed at once. The driver stores the same rule on
both keys, and so does this tool.

```console
> uv run python -m fighting68 socd --key1 A --key2 D --mode last
> uv run python -m fighting68 socd --key1 W --key2 S --mode key1
> uv run python -m fighting68 socd --key1 W --key2 S --clear
```

| mode | behaviour |
| --- | --- |
| `last` | the most recently pressed key wins |
| `key1` | the first key always wins |
| `key2` | the second key always wins |
| `cancel` | neither key fires |

### Calibration

`calibrate` puts the board into its calibration mode and summarises the live
reports it sends back. Press each key fully while it runs — that is what the
firmware measures. `travel` is the deepest movement it saw, `of` is the full
travel of the switch.

## The wire protocol

Transport is the vendor HID collection on usage page `0xFF68`, 64-byte reports,
report id `0`. Requests start with `0xAA`, replies with `0x55`; everything else
is identical in both directions:

```
0xAA <cmd> <len> <offset lo> <offset hi> 0 0 0 <payload at byte 8>
```

`len` is the payload length in this packet and `offset` the byte offset inside
the region the `cmd` names. Transfers of more than 56 payload bytes go in 56-byte
chunks, each sent only after the previous one is acknowledged by a reply echoing
the same `cmd`.

| region | read | write | size | contents |
| --- | --- | --- | --- | --- |
| device | `0x10` | – | 24 | identity (VID/PID at bytes 4–7) |
| settings | `0x11` | `0x21` | 32 | dead zones, report rate, stability, rest time, adaptive calibration |
| keys | `0x12` | `0x22` | 512 | 128 × 4-byte key records |
| led | `0x13` | `0x23` | 16 | lighting (no RGB on this board) |
| colors | `0x14` | `0x24` | 512 | per-key lighting |
| macros | `0x15` | `0x25` | 4008 | macro store |
| fn | `0x16` | `0x26` | 512 | the Fn layer's key records |
| rt | `0x17` | `0x27` | 1024 | 128 × 8-byte rapid-trigger records |
| dks | `0x18` | `0x28` | 1024 | dynamic keystroke records |

Short commands put their arguments at byte 2 instead of byte 8:

| packet | meaning |
| --- | --- |
| `AA 0F FF 00 00 …` | factory reset everything (`0x05` resets the key map) |
| `AA 64 00 00 00 …` | enter calibration mode |
| `AA 65 00 00 00 …` | leave calibration mode |
| `AA 00 00 00 00 …` | trailing "transfer finished" packet |

While calibrating the board streams `0xFB` reports: `[2]` is the key-map slot,
`[3]` its calibration state, `[10:12]` the measured travel and `[12:14]` the
switch's full travel, both in 0.1 mm.

### Records

A key record is 4 bytes (`getUpdateKeyData` in the bundle):

| byte 0 | record |
| --- | --- |
| `00` | empty |
| `01` | mouse: `kind`, `code` |
| `02` | key: usage in byte 2, or a special code in byte 1 |
| `03` | media/consumer usage, 16-bit little-endian in bytes 1–2 |
| `06` | macro slot |
| `08` | dynamic keystroke slot |
| `09` | mod-tap: tap, hold, time/10 |
| `0A` | toggle |
| `0B` | SOCD: mode, key1 usage, key2 usage |
| `0C` | RS: key1 usage, key2 usage |

A rapid-trigger record is 8 bytes (`getSenRt`): switch type, flags, then
trigger point, press sensitivity and release sensitivity as 16-bit
little-endian values in 0.01 mm.

## Evidence

`rea analyze-javascript-application` on the downloaded bundle reported 7
relevant files, 5 parsed JavaScript files and 1.15 M visited AST nodes, with no
Electron windows, no preload entrypoints, no context-bridge APIs and no native
add-on bindings — i.e. a plain WebHID page, all device logic in
`static/js/app.94fa93896fc63cff684f.js`. The device table in that bundle maps
`Fighting68` to `productId` 32816 (`0x8030`) under `vendorId` 3141 (`0x0C45`)
with `usagePage` 65384 (`0xFF68`), and its transport object `Ra` holds every
packet builder quoted above.

`analysis/` keeps the extracted key catalog and the two one-off scripts that
turn it into `fighting68/layout.py`, so the cap labels and HID usage table stay
traceable to the bundle.

## Not covered

Macros, the DKS/MT/TGL/RS key types (the record formats are documented and
decoded, but there is no writer), profiles, per-key lighting, firmware update
and the music-reactive lighting that talks to `127.0.0.1:8080`. `dump` and
`restore` cover those regions as opaque bytes.

The Qt interface is `PySide6-Essentials`; it needs a desktop session, while the
CLI works anywhere the board shows up in `hid.enumerate()`.

Tested on one Fighting 68 (`0c45:8030`, firmware block `0x0166`) on Windows. The
`0c45:8051` axial variant is listed against the same layout and packet builders
in the bundle but was not available to test. The protocol was derived from one
bundle revision; a firmware update could change it.
