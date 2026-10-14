#!/usr/bin/env python3
"""
modem_bbs.py

Answers incoming calls on an attached Hayes-compatible modem and lets the
caller view/send messages over an attached LoRa mesh radio — Meshtastic
or MeshCore, whichever is configured in config.yaml (radio.backend).

Requires:  pip install pyserial pyyaml, plus meshtastic or meshcore
           depending on which backend you're using (see README.md)

Run manually first to test, then install as a systemd service (see
meshbbs.service / README.md).
"""

import concurrent.futures
import logging
import textwrap
import time

import serial

from config import CONFIG
from radio_bridge import RadioBridge

MESH_SEND_TIMEOUT = CONFIG["radio"]["send_timeout"]  # seconds to wait for
                         # mesh.send() before giving up — LoRa sends can be
                         # genuinely slow, but should never be allowed to
                         # freeze the whole modem session if the radio's
                         # unresponsive
_mesh_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

MODEM_PORT = CONFIG["modem"]["port"]
MODEM_BAUD = CONFIG["modem"]["idle_baud"]    # local Pi<->modem link speed while idle/in command mode
CONNECT_BAUD = CONFIG["modem"]["connect_baud"]  # local Pi<->modem link speed once actually
                               # CONNECTed — the modem's default "&B0 variable DTE
                               # rate" behavior switches ITS OWN local serial rate
                               # to match the negotiated line speed while online,
                               # then reverts to the command-mode rate on hangup.
                               # We switch pyserial's rate to track that same
                               # change (see Modem._set_baud), rather than locking
                               # the rate with &B1 and depending on RTS/CTS
                               # hardware flow control to bridge the mismatch —
                               # that requires the USB-serial adapter to support
                               # RTS/CTS reliably, which Prolific-chipset adapters
                               # in particular are known to handle inconsistently
                               # on Linux.
LOG_FILE = CONFIG["logging"]["log_file"]
TERM_WIDTH = CONFIG["bbs"]["term_width"]  # wrap all outgoing text to this column width
BANNER_TITLE = CONFIG["bbs"]["banner_title"]
BANNER_SUBTITLE = CONFIG["bbs"]["banner_subtitle"]

# Which mesh radio backend to use: "meshtastic" or "meshcore".
# Set the matching device port in config.yaml under radio.port.
RADIO_BACKEND = CONFIG["radio"]["backend"]


def make_radio_bridge() -> RadioBridge:
    if RADIO_BACKEND == "meshtastic":
        from meshtastic_bridge import MeshtasticBridge
        return MeshtasticBridge()
    elif RADIO_BACKEND == "meshcore":
        from meshcore_bridge import MeshCoreBridge
        return MeshCoreBridge()
    else:
        raise ValueError(f"Unknown RADIO_BACKEND: {RADIO_BACKEND}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),  # also print to the terminal, so a foreground
                                   # run shows exactly where startup stalls
    ],
)

# Sent to the modem on startup and after every hangup. See config.yaml's
# modem.init_strings for the actual sequence and per-command explanations.
INIT_STRINGS = CONFIG["modem"]["init_strings"]


class Modem:
    def __init__(self, port=MODEM_PORT, baud=MODEM_BAUD):
        self.idle_baud = baud
        self.ser = serial.Serial(port, baud, timeout=1)
        self._send_init()

    def _set_baud(self, baud):
        """Switch pyserial's rate to match the modem's own local
        Pi<->modem rate. The modem is left at its default &B0 ("variable
        DTE rate"), which switches ITS rate to match the actual connect
        speed while online and reverts to the command-mode rate once back
        on-hook — this mirrors that same switch on our end so the two
        never get out of sync."""
        if self.ser.baudrate != baud:
            self.ser.baudrate = baud

    def _send_init(self):
        for cmd in INIT_STRINGS:
            self._write(cmd)
            time.sleep(0.5)
            self.ser.reset_input_buffer()

    def _write(self, s):
        self.ser.write((s + "\r").encode("ascii"))

    def _readline(self):
        try:
            raw = self.ser.readline()
        except serial.SerialException:
            return ""
        return raw.decode("ascii", errors="ignore").strip()

    def wait_for_connect(self):
        """Block until the modem reports CONNECT; return the result line,
        or None if the call failed/never came (ERROR/BUSY/NO CARRIER).
        On success, switches pyserial to CONNECT_BAUD before returning."""
        logging.info("Waiting for incoming call...")
        while True:
            line = self._readline()
            if not line:
                continue
            logging.info("Modem: %s", line)
            if line.startswith("CONNECT"):
                self._set_baud(CONNECT_BAUD)
                # Let the link settle, then throw away anything that piled
                # up in the input buffer during the rate switch / carrier
                # establishment (line-noise glitches, trailing result-code
                # bytes) so it isn't mistaken for the caller typing.
                time.sleep(1.0)
                self.ser.reset_input_buffer()
                return line
            if line in ("NO CARRIER", "ERROR", "BUSY"):
                return None

    def hangup(self):
        time.sleep(1)
        self._write("+++")   # escape sequence back to command mode
        time.sleep(1)
        self._write("ATH0")  # on-hook
        time.sleep(0.5)
        self._set_baud(self.idle_baud)  # modem's own rate has now reverted
                                         # too, back to command-mode speed
        self.ser.reset_input_buffer()
        self._send_init()

    def send_text(self, text):
        self.ser.write(text.encode("ascii", errors="ignore"))

    def _decode_caller_byte(self, b):
        """Decode one incoming byte from the caller, masking off the high
        bit before interpreting it as ASCII. The far end's terminal sends
        an extra bit set on top of standard 7-bit ASCII (observed: every
        byte we were failing to recognize, including CR/Enter itself,
        turned out to be the expected character with bit 7 set — likely
        an old-school parity bit or similar framing convention). Without
        this mask, .decode('ascii', errors='ignore') silently drops any
        byte >= 0x80, which is why some letters (and Enter) appeared to
        just vanish."""
        raw = b[0] & 0x7F
        ch = chr(raw)
        logging.debug("raw byte from caller: %r -> %r", b, ch)
        return ch

    def send_wrapped(self, text, width=TERM_WIDTH, indent=""):
        """Word-wrap our own output to `width` columns before sending, so
        it displays correctly on a 40-column (or any fixed-width) dumb
        terminal. `text` may contain embedded newlines for paragraph
        breaks; an empty string sends a blank line. Not used for echoing
        the caller's own keystrokes — see read_line_from_caller."""
        for line in text.split("\n"):
            if not line:
                self.send_text("\r\n")
                continue
            wrapped = textwrap.wrap(line, width=width, subsequent_indent=indent) or [""]
            for wline in wrapped:
                self.send_text(wline + "\r\n")

    def read_char_from_caller(self, prompt=""):
        """Read and echo a single keystroke — the classic BBS hotkey-menu
        convention (no Enter needed). Used for the main menu choice; also
        sidesteps any dependency on how the caller's terminal sends line
        endings, since there's no line terminator to wait for at all."""
        if prompt:
            self.send_text(prompt)
        while True:
            b = self.ser.read(1)
            if not b:
                continue
            ch = self._decode_caller_byte(b)
            # Only a visible character counts as a menu keystroke. Ignore
            # control characters (CR/LF left over from a previous Enter,
            # NULs, noise) and spaces rather than treating them as choices.
            if not ch.isprintable() or ch.isspace():
                continue
            self.send_text(ch + "\r\n")
            return ch

    def read_line_from_caller(self, prompt=""):
        """Read a CR/LF-terminated line typed by the caller, echoing and
        handling backspace as we go. Used where actual free-text entry is
        needed (e.g. composing a message) — Enter genuinely is required
        here since there's no other way to know the caller is done typing."""
        if prompt:
            self.send_text(prompt)
        buf = ""
        while True:
            b = self.ser.read(1)
            if not b:
                continue
            ch = self._decode_caller_byte(b)
            if ch in ("\r", "\n"):
                self.send_text("\r\n")
                return buf
            elif ch in ("\x08", "\x7f"):  # backspace / delete
                if buf:
                    buf = buf[:-1]
                    self.send_text("\x08 \x08")
            else:
                buf += ch
                self.send_text(ch)


def run_session(modem: Modem, mesh: RadioBridge):
    modem.send_wrapped("")
    modem.send_wrapped(BANNER_TITLE)
    modem.send_wrapped(BANNER_SUBTITLE)
    while True:
        modem.send_wrapped("")
        modem.send_wrapped("[V]iew  [S]end  [Q]uit")
        choice = modem.read_char_from_caller("> ").lower()
        if choice == "v":
            show_messages(modem, mesh)
        elif choice == "s":
            send_message(modem, mesh)
        elif choice == "q":
            modem.send_wrapped("Goodbye!")
            return
        else:
            logging.info("Unrecognized menu choice: %r", choice)
            modem.send_wrapped("Unrecognized choice.")


def show_messages(modem: Modem, mesh: RadioBridge, count=10):
    rows = mesh.get_recent(count)
    if not rows:
        modem.send_wrapped("")
        modem.send_wrapped("No messages yet.")
        return
    modem.send_wrapped("")
    modem.send_wrapped(f"-- last {len(rows)} messages --")
    for ts, sender, direction, text in rows:
        arrow = "<-" if direction == "in" else "->"
        line = f"{ts[11:16]} {arrow} {sender}: {text}"
        # indent continuation lines so wrapped message text is easy to
        # tell apart from the next entry's timestamp
        modem.send_wrapped(line, indent="  ")
        time.sleep(0.05)  # tiny pacing delay so we don't overrun a slow line


def send_message(modem: Modem, mesh: RadioBridge):
    text = modem.read_line_from_caller("Message to send (max 200 chars): ")
    text = text[:200]
    if not text:
        modem.send_wrapped("Empty message, cancelled.")
        return
    if not text.endswith(" "):
        text += " "
    modem.send_wrapped("Sending...")
    future = _mesh_executor.submit(mesh.send, text)
    try:
        future.result(timeout=MESH_SEND_TIMEOUT)
        modem.send_wrapped("Sent over mesh.")
    except concurrent.futures.TimeoutError:
        logging.error("mesh.send() did not return within %ss", MESH_SEND_TIMEOUT)
        modem.send_wrapped(
            f"No response from mesh radio after {MESH_SEND_TIMEOUT}s — "
            "it may still send in the background, but giving up on "
            "waiting so your session isn't stuck."
        )
    except Exception as e:
        logging.exception("Send failed")
        modem.send_wrapped(f"Failed to send: {e}")


def main():
    logging.info("BBS starting (radio backend: %s)", RADIO_BACKEND)
    try:
        logging.info("Connecting to mesh radio on %s ...", CONFIG["radio"]["port"])
        mesh = make_radio_bridge()
        logging.info("Mesh radio connected.")

        logging.info("Opening modem on %s at %s baud ...", MODEM_PORT, MODEM_BAUD)
        modem = Modem()
        logging.info("Modem initialized.")
    except Exception:
        logging.exception("Startup failed")
        raise
    logging.info("BBS ready.")
    try:
        while True:
            result = modem.wait_for_connect()
            if result is None:
                continue
            logging.info("Call connected: %s", result)
            try:
                run_session(modem, mesh)
            except Exception:
                logging.exception("Session error")
            finally:
                modem.hangup()
    except KeyboardInterrupt:
        pass
    finally:
        mesh.close()


if __name__ == "__main__":
    main()
