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
IDLE_TIMEOUT = CONFIG["bbs"]["idle_timeout"]
MAX_BAD_ENTRIES = CONFIG["bbs"]["max_bad_entries"]

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


class CarrierLost(Exception):
    """The caller's connection is gone (carrier dropped / NO CARRIER)."""


class IdleTimeout(Exception):
    """The caller stopped typing for longer than bbs.idle_timeout."""


class Modem:
    def __init__(self, port=MODEM_PORT, baud=MODEM_BAUD):
        self.idle_baud = baud
        self.ser = serial.Serial(port, baud, timeout=1)
        self._cd_usable = False   # set per-call; see wait_for_connect
        self._rx_tail = ""        # last few chars received, to spot "NO CARRIER"
        self._send_init()

    def _cd_asserted(self):
        try:
            return bool(self.ser.cd)
        except (OSError, serial.SerialException, NotImplementedError, AttributeError):
            return False

    def _check_carrier(self):
        """Raise CarrierLost if the modem's carrier-detect line has dropped.
        Only trusted if CD read as asserted right after CONNECT (some
        USB-serial adapters don't pass modem status lines through)."""
        if self._cd_usable and not self._cd_asserted():
            time.sleep(0.3)  # debounce a momentary glitch
            if not self._cd_asserted():
                raise CarrierLost("carrier detect line dropped")

    def _check_idle(self, since):
        if IDLE_TIMEOUT and time.monotonic() - since > IDLE_TIMEOUT:
            raise IdleTimeout(f"no input for {IDLE_TIMEOUT}s")

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
            logging.info("Modem: %r", line)
            if line.startswith("CONNECT"):
                self._set_baud(CONNECT_BAUD)
                # Let the link settle, then throw away anything that piled
                # up in the input buffer during the rate switch / carrier
                # establishment (line-noise glitches, trailing result-code
                # bytes) so it isn't mistaken for the caller typing.
                time.sleep(1.0)
                self.ser.reset_input_buffer()
                self._rx_tail = ""
                self._cd_usable = self._cd_asserted()
                logging.info("Carrier-detect line usable for hangup detection: %s",
                             self._cd_usable)
                return line
            if line in ("NO CARRIER", "ERROR", "BUSY"):
                return None

    def hangup(self, carrier_lost=False):
        self._cd_usable = False
        if carrier_lost:
            # Caller already dropped, so the modem is back in command mode;
            # skip the +++ escape and just let it finish its NO CARRIER.
            time.sleep(1)
        else:
            # Hayes escape: silence, "+++" with NO trailing CR (that's why
            # this can't go through _write(), which appends one), then
            # silence again. Guard time is S12 (default ~1s).
            time.sleep(1.2)
            self.ser.write(b"+++")
            time.sleep(1.2)
            self._write("ATH0")  # on-hook
            time.sleep(0.5)
            # Backstop: with &D2 set, dropping DTR also forces a hangup and
            # a return to command mode, independent of the escape sequence.
            self._pulse_dtr()
        self._set_baud(self.idle_baud)  # modem's own rate has now reverted
                                         # too, back to command-mode speed
        self.ser.reset_input_buffer()
        self._send_init()

    def _pulse_dtr(self):
        try:
            self.ser.dtr = False
            time.sleep(0.7)
            self.ser.dtr = True
            time.sleep(0.5)
        except (OSError, serial.SerialException) as e:
            logging.warning("Could not toggle DTR (%s); relying on +++/ATH0", e)

    def send_text(self, text):
        # Never write session text into a modem that's already hung up —
        # it would be taken as AT commands and answered with ERROR/OK,
        # which we'd then read back as "keystrokes".
        self._check_carrier()
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
        self._rx_tail = (self._rx_tail + ch)[-32:]
        if "NO CARRIER" in self._rx_tail:
            raise CarrierLost("modem reported NO CARRIER")
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
        since = time.monotonic()
        while True:
            self._check_idle(since)
            b = self.ser.read(1)
            if not b:
                self._check_carrier()
                continue
            ch = self._decode_caller_byte(b)
            # Only a visible character counts as a menu keystroke. Ignore
            # control characters (CR/LF left over from a previous Enter,
            # NULs, noise) and spaces rather than treating them as choices.
            if not ch.isprintable() or ch.isspace():
                continue
            # A human at 300 baud sends one key at a time. If more bytes are
            # already queued right behind this one, it's a burst of modem
            # output or line noise, not typing: run it through the decoder
            # (which spots "NO CARRIER") and discard it, rather than
            # replying to each byte — replies into a hung-up modem just
            # produce more junk coming back.
            time.sleep(0.2)
            if self.ser.in_waiting >= 2:
                for _ in range(64):
                    if not self.ser.in_waiting:
                        break
                    extra = self.ser.read(1)
                    if extra:
                        self._decode_caller_byte(extra)
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
        since = time.monotonic()
        while True:
            b = self.ser.read(1)
            if not b:
                self._check_carrier()
                self._check_idle(since)
                continue
            since = time.monotonic()
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
    bad_entries = 0
    while True:
        modem.send_wrapped("")
        modem.send_wrapped("[V]iew  [S]end  [Q]uit")
        choice = modem.read_char_from_caller("> ").lower()
        if choice == "v":
            bad_entries = 0
            show_messages(modem, mesh)
        elif choice == "s":
            bad_entries = 0
            send_message(modem, mesh)
        elif choice == "q":
            modem.send_wrapped("Goodbye!")
            return
        else:
            bad_entries += 1
            logging.info("Unrecognized menu choice: %r (%d in a row)", choice, bad_entries)
            if MAX_BAD_ENTRIES and bad_entries >= MAX_BAD_ENTRIES:
                modem.send_wrapped("Too many invalid entries. Goodbye!")
                return
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
            logging.info("Call connected: %s (serial rate now %s)",
                         result, modem.ser.baudrate)
            carrier_lost = False
            try:
                run_session(modem, mesh)
            except CarrierLost as e:
                carrier_lost = True
                logging.info("Call ended: %s", e)
            except IdleTimeout as e:
                logging.info("Call ended: idle timeout (%s)", e)
                try:
                    modem.send_wrapped("Idle timeout. Goodbye!")
                except CarrierLost:
                    carrier_lost = True
            except Exception:
                logging.exception("Session error")
            finally:
                modem.hangup(carrier_lost=carrier_lost)
    except KeyboardInterrupt:
        pass
    finally:
        mesh.close()


if __name__ == "__main__":
    main()
