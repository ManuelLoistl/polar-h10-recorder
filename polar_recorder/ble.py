"""Polar H10 link: scan, connect, streams, device info, reconnect with backoff."""

from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient, BleakScanner
from polar_python import PolarDevice
from polar_python.constants import PmdMeasurementType, PolarCharacteristic
from polar_python.models import ACCData, ECGData

from .recorder import ACC_RANGE_G, ACC_RATE_HZ, ECG_RATE_HZ, ECG_RESOLUTION_BITS

log = logging.getLogger(__name__)

UUID_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
UUID_MODEL = "00002a24-0000-1000-8000-00805f9b34fb"
UUID_SERIAL = "00002a25-0000-1000-8000-00805f9b34fb"
UUID_FIRMWARE = "00002a26-0000-1000-8000-00805f9b34fb"
UUID_HARDWARE = "00002a27-0000-1000-8000-00805f9b34fb"

BACKOFF_S = [1, 2, 4, 8, 10]
RECONNECT_DEADLINE_S = 300
NOT_FOUND_HINT = (
    "No Polar H10 found. Make sure the strap is worn with moist electrodes, that it is not "
    "connected to Polar Flow, a watch or another app (a busy H10 stops advertising), and that "
    "this app has Bluetooth permission in System Settings > Privacy & Security > Bluetooth."
)


class NotFound(Exception):
    pass


class EcgUnsupported(Exception):
    pass


def parse_hr(data: bytes) -> tuple[int, list[float], int | None]:
    """Standard Heart Rate Measurement characteristic (0x2A37).

    Returns (bpm, RR intervals in ms, sensor contact 1/0 or None if unsupported).
    """
    flags = data[0]
    if flags & 0x01:
        bpm = int.from_bytes(data[1:3], "little")
        i = 3
    else:
        bpm = data[1]
        i = 2
    contact = None
    if flags & 0x04:
        contact = 1 if flags & 0x02 else 0
    if flags & 0x08:  # energy expended field present
        i += 2
    rr = []
    if flags & 0x10:
        while i + 1 < len(data):
            rr.append(int.from_bytes(data[i:i + 2], "little") / 1024.0 * 1000.0)
            i += 2
    return bpm, rr, contact


class _Device(PolarDevice):
    """PolarDevice whose BleakClient reports disconnects to us."""

    def __init__(self, address: str, disconnected_callback):
        super().__init__(address)
        self._client = BleakClient(address, disconnected_callback=disconnected_callback)

    @property
    def name(self) -> str | None:
        try:
            return self._client.name
        except Exception:
            return None

    async def read_str(self, uuid: str) -> str | None:
        try:
            raw = await self._client.read_gatt_char(uuid)
            return bytes(raw).decode(errors="replace").strip("\x00 ")
        except Exception as e:  # characteristic may be absent
            log.debug("read %s failed: %s", uuid, e)
            return None

    async def read_battery(self) -> int | None:
        try:
            return int((await self._client.read_gatt_char(UUID_BATTERY))[0])
        except Exception as e:
            log.debug("battery read failed: %s", e)
            return None

    async def subscribe_hr(self, handler) -> None:
        await self._client.start_notify(PolarCharacteristic.HEART_RATE.value, handler)

    async def unsubscribe_hr(self) -> None:
        await self._client.stop_notify(PolarCharacteristic.HEART_RATE.value)


class PolarLink:
    """One H10 connection. States: disconnected, connecting, connected, streaming,
    reconnecting, reconnect_failed."""

    def __init__(self, on_ecg, on_acc, on_hr, on_state):
        self._on_ecg = on_ecg
        self._on_acc = on_acc
        self._on_hr = on_hr
        self._on_state = on_state
        self._dev: _Device | None = None
        self._address: str | None = None
        self._streaming = False
        self._reconnect_task: asyncio.Task | None = None
        self.state = "disconnected"
        self.info: dict | None = None
        self.auto_reconnect = False
        self.reconnect_attempt = 0

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        log.info("state=%s %s", state, detail)
        self._on_state(state, detail)

    # ------------------------------------------------------------------ scan
    async def scan(self, timeout: float = 6.0) -> list[dict]:
        found = await BleakScanner.discover(timeout=timeout)
        devices = [
            {"name": d.name, "address": d.address}
            for d in found
            if d.name and d.name.startswith("Polar H10")
        ]
        log.info("scan: %d BLE devices seen, Polar H10: %s", len(found), devices)
        return devices

    # ------------------------------------------------------------------ connect
    async def connect(self, address: str) -> dict:
        self._address = address
        self._set_state("connecting", address)
        dev = _Device(address, self._disconnected)
        try:
            await asyncio.wait_for(dev.connect(), timeout=30)
        except Exception as e:
            self._set_state("disconnected", str(e))
            detail = "connection timed out after 30 s" if isinstance(e, TimeoutError) else f"bleak: {str(e) or type(e).__name__}"
            raise NotFound(NOT_FOUND_HINT + f" ({detail})") from e
        self._dev = dev
        features = await dev.get_available_features()
        ecg_ok = PmdMeasurementType.ECG in features
        self.info = {
            "name": dev.name or "Polar H10",
            "address": address,
            "model": await dev.read_str(UUID_MODEL),
            "serial": await dev.read_str(UUID_SERIAL),
            "firmware": await dev.read_str(UUID_FIRMWARE),
            "hardware": await dev.read_str(UUID_HARDWARE),
            "battery_percent": await dev.read_battery(),
            "ecg_supported": ecg_ok,
        }
        self._set_state("connected", self.info["name"])
        if not ecg_ok:
            raise EcgUnsupported(
                "This H10 does not offer ECG streaming. Firmware 3.0.35 or newer is required; "
                f"update it in the Polar Flow app (reported firmware: {self.info['firmware']})."
            )
        return self.info

    async def start_streams(self) -> None:
        assert self._dev is not None
        await self._dev.subscribe_hr(self._hr_notification)
        await self._dev.start_ecg_stream(
            self._ecg_frame, sample_rate=ECG_RATE_HZ, resolution=ECG_RESOLUTION_BITS)
        await self._dev.start_acc_stream(
            self._acc_frame, sample_rate=ACC_RATE_HZ, resolution=16, range=ACC_RANGE_G)
        self._streaming = True
        self._set_state("streaming")

    async def stop_streams(self) -> None:
        self._streaming = False
        dev = self._dev
        if dev is None:
            return
        for coro in (dev.stop_ecg_stream(), dev.stop_acc_stream(), dev.unsubscribe_hr()):
            try:
                await asyncio.wait_for(coro, timeout=5)
            except Exception as e:
                log.warning("stop stream: %s", e)
        if self.state == "streaming":
            self._set_state("connected")

    async def disconnect(self) -> None:
        self.auto_reconnect = False
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        dev, self._dev = self._dev, None
        self._streaming = False
        if dev is not None:
            try:
                await dev.disconnect()
            except Exception as e:
                log.warning("disconnect: %s", e)
        self._set_state("disconnected")

    async def refresh_battery(self) -> int | None:
        if self._dev is None:
            return None
        b = await self._dev.read_battery()
        if self.info is not None and b is not None:
            self.info["battery_percent"] = b
        return b

    # ------------------------------------------------------------------ callbacks
    def _ecg_frame(self, frame: ECGData) -> None:
        self._on_ecg(frame.timestamp, frame.data)

    def _acc_frame(self, frame: ACCData) -> None:
        self._on_acc(frame.timestamp, frame.data)

    def _hr_notification(self, _char, data: bytearray) -> None:
        try:
            bpm, rr, contact = parse_hr(bytes(data))
        except IndexError:
            return
        self._on_hr(bpm, rr, contact)

    def _disconnected(self, _client) -> None:
        """Called by bleak on the event loop when the link drops."""
        if self._dev is None:
            return  # we initiated the disconnect
        log.warning("link dropped (auto_reconnect=%s)", self.auto_reconnect)
        self._dev = None
        was_streaming = self._streaming
        self._streaming = False
        if self.auto_reconnect and was_streaming and self._address:
            self._set_state("reconnecting", "attempt 1")
            self._reconnect_task = asyncio.get_event_loop().create_task(self._reconnect_loop())
        else:
            self._set_state("disconnected", "link dropped")

    async def _reconnect_loop(self) -> None:
        deadline = time.monotonic() + RECONNECT_DEADLINE_S
        attempt = 0
        while time.monotonic() < deadline and self.auto_reconnect:
            attempt += 1
            self.reconnect_attempt = attempt
            self._set_state("reconnecting", f"attempt {attempt}")
            dev = _Device(self._address, self._disconnected)
            try:
                await asyncio.wait_for(dev.connect(), timeout=20)
                self._dev = dev
                await self.start_streams()
                self.reconnect_attempt = 0
                self._set_state("streaming", f"reconnected after {attempt} attempt(s)")
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("reconnect attempt %d failed: %s", attempt, e)
                self._dev = None
                try:
                    await dev.disconnect()
                except Exception:
                    pass
            delay = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]
            await asyncio.sleep(delay)
        self.reconnect_attempt = 0
        self._set_state("reconnect_failed", f"gave up after {attempt} attempts")
