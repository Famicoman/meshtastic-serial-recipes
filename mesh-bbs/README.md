# MeshBBS — Dial-Up BBS for LoRa Mesh Radios

Turns a Raspberry Pi into a tiny dial-up BBS: callers connect over a real
phone line at up to 1200 baud, and can view or send messages that travel
over an attached LoRa mesh radio — either Meshtastic or MeshCore, your
choice.

## Files

- `modem_bbs.py` — main daemon: answers calls, runs the caller menu
- `config.py` — loads config.yaml, with built-in defaults
- `config.yaml` — all the tunable settings in one place
- `radio_bridge.py` — shared interface + SQLite message log
- `meshtastic_bridge.py` — Meshtastic backend
- `meshcore_bridge.py` — MeshCore backend
- `meshbbs.service` — systemd unit
- `99-meshbbs.rules` — udev rule for fixed `/dev/modem` /
  `/dev/meshradio` device names

## Hardware needed

- Raspberry Pi with two free USB ports (or one USB port + a UART header
  broken out, e.g. on the 40-pin GPIO header)
- An external Hayes-compatible modem that supports 1200 baud (Bell 212A /
  V.22), e.g. a USRobotics Courier or Sportster
- A companion LoRa mesh radio that runs full Meshtastic or MeshCore
  firmware onboard and talks the standard protocol over USB — e.g. Heltec
  V3, T-Beam, RAK, muzi Base Uno/Duo

## Connecting everything to the Pi

**The modem:**
1. Phone line → modem: plug a standard phone cord (RJ11) from your analog
   wall jack into the modem's "LINE" jack. (If the modem has both LINE
   and PHONE jacks, LINE is the one that goes to the wall — PHONE is only
   for daisy-chaining an actual telephone, not needed here.)
2. Modem → Pi: how you do this depends on the modem:
   - **USB modem**: plug it straight into a Pi USB port. It'll show up as
     something like `/dev/ttyACM0` or `/dev/ttyUSB0`.
   - **Serial (RS-232/DB9) modem**: you need a USB-to-serial (RS-232)
     adapter cable between the modem's DB9 port and a Pi USB port — the
     Pi has no native RS-232 port. Cheap FTDI or Prolific-chipset adapters
     work fine; avoid unbranded ones with no chipset info, they're a
     common source of flaky connections.
   - **Internal/PCI modem**: not usable here — you need something with an
     external port the Pi can reach.
3. Power the modem from its own wall adapter — don't try to run it off Pi
   USB power, older modems can draw more than a Pi port comfortably
   supplies.
4. **DIP switches (USR 5686 / 5686G family, and most classic USR
   external modems):** factory default is switches 3, 5, 8 DOWN and the
   rest UP — but you need **switch 5 UP** for this project. Switch 5
   controls whether auto-answer is possible at the hardware level at all;
   left at factory default (DOWN), it silently ignores the `ATS0=1`
   auto-answer command the script sends, and the modem will never pick
   up. Final positions: `1-UP 2-UP 3-DOWN 4-UP 5-UP 6-UP 7-UP 8-DOWN`.
   Also worth knowing: switch 2 (verbal vs numeric result codes) must
   stay UP — the script parses text like `CONNECT`/`NO CARRIER`, which
   only appear when result codes are in verbal form.

**The mesh radio:**
- Plug it into the Pi's other USB port with its USB-C/micro-USB cable.
  It'll enumerate as its own serial device, usually
  `/dev/ttyACM1`/`/dev/ttyUSB1` or similar (whatever's free after the
  modem claims a port).
- No line/antenna setup beyond making sure the antenna is actually
  attached before you power it on — transmitting without an antenna can
  damage the radio's power amp on some boards.

**Telling them apart reliably:**

Once both are plugged in:

```bash
ls -l /dev/serial/by-id/
```

This gives you stable, descriptive symlinks (containing the vendor/model)
rather than the `/dev/ttyUSB0` vs `/dev/ttyUSB1` numbering, which can
swap depending on plug-in order or after a reboot.

For a permanent fix, this project ships `99-meshbbs.rules`, a udev rule
that pins the modem and mesh radio to fixed names (`/dev/modem` and
`/dev/meshradio`) based on their USB vendor ID, so the numbering never
matters again:

```bash
sudo cp 99-meshbbs.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
# unplug/replug both devices (or reboot), then confirm:
ls -l /dev/modem /dev/meshradio
```

It assumes a Prolific-chipset RS-232 adapter for the modem and a Silicon
Labs CP210x for the mesh radio — check the comments in the rule file and
adjust if your hardware differs (run `lsusb` to get the exact vendor ID
if neither of those match). Once it's working, `/dev/modem` and
`/dev/meshradio` are already the defaults in `config.yaml`, so nothing
else needs changing.

## 1. Install dependencies

```bash
sudo apt update
sudo apt install python3-venv

python3 -m venv /root/meshbbs/venv
/root/meshbbs/venv/bin/pip install --upgrade pip
/root/meshbbs/venv/bin/pip install pyserial pyyaml

# and whichever mesh protocol you're using:
/root/meshbbs/venv/bin/pip install meshtastic     # for Meshtastic
/root/meshbbs/venv/bin/pip install meshcore       # for MeshCore
```

Using a venv keeps these packages isolated from the system Python rather
than installing into it directly (which recent Raspberry Pi OS/Debian
versions block by default unless you pass `--break-system-packages`).

## 2. Configure

All the tunable settings live in one place: `config.yaml`. It ships with
working defaults for this exact setup (`/dev/modem`, `/dev/meshradio`,
300-baud init sequence, etc.) — edit whatever needs to change for your
hardware:

- `modem.port`, `modem.idle_baud`, `modem.connect_baud` — the modem's
  serial connection (local Pi↔modem link speed, independent of the baud
  rate the modem negotiates with the caller over the phone line) and its
  AT `init_strings` sequence
- `radio.backend` — `meshtastic` or `meshcore`, plus that backend's
  `port`/`baud`/`db_path`/`send_timeout`
- `bbs.term_width` — column width all output is word-wrapped to (default
  40, for classic 40-column terminals); `bbs.banner_title` /
  `banner_subtitle` — what the caller sees on connect
- `bbs.idle_timeout` — seconds without a keystroke before the BBS hangs
  up on a silent caller (0 disables); `bbs.max_bad_entries` — consecutive
  unrecognized menu keys before it hangs up on line noise
- `logging.log_file`

Any key you leave out falls back to the built-in default in `config.py`,
so you only need to list what you're actually changing.

Create the working directory and copy the files there:

```bash
sudo mkdir -p /root/meshbbs
sudo cp radio_bridge.py meshtastic_bridge.py meshcore_bridge.py \
        modem_bbs.py config.py config.yaml /root/meshbbs/
sudo chown -R root:root /root/meshbbs
```

(If you haven't created the venv yet, do that now — see "Install
dependencies" above — since it lives in this same directory.)

## 3. Test manually before installing the service

```bash
cd /root/meshbbs
venv/bin/python3 modem_bbs.py
```

Dial in from another line/phone and confirm you see the menu. Check
`bbs.log` for what the modem is reporting if something's off — most
first-run issues are wrong AT init strings for your specific modem model
(check its manual, then adjust `config.yaml`'s `modem.init_strings`) or a
wrong `modem.port`.

## 4. Install as a systemd service

```bash
sudo cp meshbbs.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshbbs.service
sudo systemctl status meshbbs.service
journalctl -u meshbbs.service -f
```

## How it works

- `config.py` loads `config.yaml` into a single `CONFIG` dict, merged
  over built-in defaults, so every other module just does
  `from config import CONFIG` instead of hardcoding values.
- `modem_bbs.py` talks AT commands to the modem, auto-answers on the
  first ring (`ATS0=1`), and once it sees `CONNECT`, treats the serial
  port as a raw terminal: reads keystrokes byte-by-byte (with basic
  backspace handling and local echo) and writes menu text back.
- `radio_bridge.py` defines a small shared interface (`get_recent`,
  `send`, `close`) plus a SQLite-backed message log, so the BBS code
  doesn't need to know or care which mesh protocol is actually attached.
- `meshtastic_bridge.py` / `meshcore_bridge.py` each implement that interface
  for their respective protocol/library, and log every inbound message to
  `messages.db` — this is what lets a caller see message history from
  *before* they dialed in, since neither mesh protocol retains that on
  its own.
- **Hangup detection:** a caller whose terminal is powered off or whose
  line drops shouldn't leave the BBS typing into a dead connection. After
  each `CONNECT` it checks whether the modem's carrier-detect line is
  readable through the USB adapter (logged as "Carrier-detect line usable
  ..."); if so, a dropped carrier ends the session immediately. As a
  fallback that needs no hardware support, it also watches for the
  modem's `NO CARRIER` text, and the idle timeout and bad-entry limit
  above end sessions where the carrier never drops.
- The menu is intentionally plain ASCII with no cursor positioning or
  color — at 1200 baud you have roughly 150 bytes/second, so keeping
  screens short matters more than it would on a modern connection.

## Extending it

Ideas for next steps, not included here:
- Show/select destination contact or channel before sending
- Node list / signal strength display
- Simple username system so `direction`/`sender` in the log is more
  meaningful for multi-user setups
- xmodem/zmodem file transfer for firmware or images (there are Python
  libraries for both)
