"""
meshcore_bridge.py

MeshCore backend for the BBS, implementing the same RadioBridge interface
as meshtastic_bridge.py (Meshtastic). MeshCore's Python library is asyncio-based
and event-driven, so this runs its own event loop in a background thread
and exposes plain blocking methods to the rest of the (synchronous) BBS
code.

For a companion board that runs full MeshCore firmware onboard and
exposes it over USB — just set radio.port in config.yaml to the board's
device.

Requires:  pip install meshcore --break-system-packages
Docs:      https://github.com/meshcore-dev/meshcore_py

NOTE: MeshCore's Python API is younger and moves faster than Meshtastic's.
Method/event names below (send_msg, send_chan_msg, CONTACT_MSG_RECV,
CHANNEL_MSG_RECV) are current as of writing but worth checking against
`pip show meshcore` / the project's README if something doesn't fire.
"""

import asyncio
import threading

from meshcore import MeshCore, EventType

from config import CONFIG
from radio_bridge import RadioBridge, MessageLog

DEFAULT_DB_PATH = CONFIG["radio"]["db_path"]
DEFAULT_PORT = CONFIG["radio"]["port"]
DEFAULT_BAUD = CONFIG["radio"]["baud"]
DEFAULT_CHANNEL_IDX = CONFIG["radio"]["channel_idx"]  # broadcast channel used when no destination given
CONNECT_TIMEOUT = CONFIG["radio"]["send_timeout"]


class MeshCoreBridge(RadioBridge):
    def __init__(self, port=DEFAULT_PORT, baud=DEFAULT_BAUD,
                 db_path=DEFAULT_DB_PATH, channel_idx=DEFAULT_CHANNEL_IDX):
        self.log = MessageLog(db_path)
        self.channel_idx = channel_idx
        self._mc = None
        self._contacts_by_prefix = {}
        self._port, self._baud = port, baud

        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._connect_error = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=15)
        if self._connect_error:
            raise self._connect_error
        if self._mc is None:
            raise RuntimeError("Timed out connecting to MeshCore radio")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        except Exception as e:  # surfaced to __init__ via self._connect_error
            self._connect_error = e
        finally:
            self._ready.set()
        self._loop.run_forever()

    async def _connect(self):
        self._mc = await MeshCore.create_serial(self._port, self._baud)

        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)
        self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)

        result = await self._mc.commands.get_contacts()
        if result.type != EventType.ERROR:
            for prefix, contact in result.payload.items():
                self._contacts_by_prefix[prefix] = contact

    async def _on_contact_msg(self, event):
        payload = event.payload or {}
        prefix = payload.get("pubkey_prefix", "unknown")
        contact = self._contacts_by_prefix.get(prefix)
        sender = contact.get("adv_name", prefix) if contact else prefix
        self.log.add(sender, "in", payload.get("text", ""))

    async def _on_channel_msg(self, event):
        payload = event.payload or {}
        idx = payload.get("channel_idx", "?")
        self.log.add(f"channel {idx}", "in", payload.get("text", ""))

    def get_recent(self, limit=10):
        return self.log.recent(limit)

    def send(self, text, destination=None):
        """destination: a contact name or pubkey prefix to DM, or None to
        broadcast on the configured channel."""
        future = asyncio.run_coroutine_threadsafe(
            self._send_async(text, destination), self._loop
        )
        future.result(timeout=15)
        self.log.add("me", "out", text)

    async def _send_async(self, text, destination):
        if destination:
            contact = self._contacts_by_prefix.get(destination) or next(
                (c for c in self._contacts_by_prefix.values()
                 if c.get("adv_name") == destination),
                None,
            )
            if contact is None:
                raise ValueError(f"Unknown contact: {destination}")
            await self._mc.commands.send_msg(contact, text)
        else:
            await self._mc.commands.send_chan_msg(self.channel_idx, text)

    def close(self):
        async def _disconnect():
            await self._mc.disconnect()

        asyncio.run_coroutine_threadsafe(_disconnect(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
