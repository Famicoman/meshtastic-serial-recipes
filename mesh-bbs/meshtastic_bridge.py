"""
meshtastic_bridge.py

Meshtastic backend for the BBS. Implements the RadioBridge interface
(see radio_bridge.py) so modem_bbs.py can use it interchangeably with
meshcore_bridge.py.

For a companion board that runs full Meshtastic firmware onboard and
exposes the standard protocol over USB (Heltec, T-Beam, RAK, muzi Base
Uno/Duo, etc.) — just set radio.port in config.yaml to the board's device.

Requires:  pip install meshtastic --break-system-packages
Docs:      https://meshtastic.org/docs/software/python/cli/
"""

import meshtastic
import meshtastic.serial_interface
from pubsub import pub

from config import CONFIG
from radio_bridge import RadioBridge, MessageLog

DEFAULT_DB_PATH = CONFIG["radio"]["db_path"]
DEFAULT_PORT = CONFIG["radio"]["port"]


class MeshtasticBridge(RadioBridge):
    def __init__(self, port=DEFAULT_PORT, db_path=DEFAULT_DB_PATH):
        self.log = MessageLog(db_path)

        # Connecting also starts a background thread inside the meshtastic
        # library that reads from the radio and fires pubsub events.
        self.interface = meshtastic.serial_interface.SerialInterface(devPath=port)
        pub.subscribe(self._on_receive, "meshtastic.receive.text")

    def _on_receive(self, packet, interface):
        try:
            text = packet["decoded"]["text"]
        except (KeyError, TypeError):
            return
        sender = packet.get("fromId", "unknown")
        self.log.add(sender, "in", text)

    def get_recent(self, limit=10):
        return self.log.recent(limit)

    def send(self, text, destination=None):
        """destination: a Meshtastic node ID string, or None for broadcast."""
        if destination:
            self.interface.sendText(text, destinationId=destination)
        else:
            self.interface.sendText(text)
        self.log.add("me", "out", text)

    def close(self):
        self.interface.close()
