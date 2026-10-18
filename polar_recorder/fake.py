"""A simulated Polar H10 for developing the page without hardware (python -m polar_recorder --fake).

Same interface as ble.PolarLink. Emits a synthetic 60 bpm ECG at 130 Hz in frames of 73
samples (one every 73/130 s), a resting accelerometer at 50 Hz in frames of 36 samples
(one every 36/50 s), both like a real H10, and HR/RR once a second. Drops the link once,
20 s after Start, for 3 s, so the gap path is exercised. Never touches Bluetooth.
"""

from __future__ import annotations

import asyncio
import math
import time

from .recorder import ACC_RATE_HZ, ECG_RATE_HZ

DROP_AFTER_S = 20.0
DROP_FOR_S = 3.0
ECG_FRAME = 73  # samples per ECG frame, as a real H10 sends
ACC_FRAME = 36  # samples per ACC frame, as a real H10 sends
DEVICE = {"name": "Polar H10 FAKE01", "address": "FA:KE:00:00:00:01"}


def ecg_uv(t: float) -> int:
    """One beat per second: P, Q, R, S, T as Gaussian bumps, in microvolts."""
    p = t % 1.0
    v = 80 * math.exp(-((p - 0.10) / 0.025) ** 2)
    v -= 150 * math.exp(-((p - 0.215) / 0.006) ** 2)
    v += 1200 * math.exp(-((p - 0.23) / 0.008) ** 2)
    v -= 300 * math.exp(-((p - 0.245) / 0.007) ** 2)
    v += 220 * math.exp(-((p - 0.42) / 0.04) ** 2)
    return int(round(v))


class FakePolarLink:
    def __init__(self, on_ecg, on_acc, on_hr, on_state):
        self._on_ecg, self._on_acc, self._on_hr, self._on_state = on_ecg, on_acc, on_hr, on_state
        self.state = "disconnected"
        self.info: dict | None = None
        self.auto_reconnect = False
        self.reconnect_attempt = 0
        self._task: asyncio.Task | None = None

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self._on_state(state, detail)

    async def scan(self, timeout: float = 6.0) -> list[dict]:
        await asyncio.sleep(1.0)
        return [dict(DEVICE)]

    async def connect(self, address: str) -> dict:
        self._set_state("connecting", address)
        await asyncio.sleep(0.5)
        self.info = {"name": DEVICE["name"], "address": address, "model": "H10", "serial": "FAKE01",
                     "firmware": "5.0.0", "hardware": "fake", "battery_percent": 80, "ecg_supported": True}
        self._set_state("connected", self.info["name"])
        return self.info

    async def refresh_battery(self) -> int:
        return 80

    async def start_streams(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._stream())
        self._set_state("streaming")

    async def stop_streams(self) -> None:
        await self._cancel()
        self._set_state("connected")

    async def disconnect(self) -> None:
        await self._cancel()
        self.info = None
        self._set_state("disconnected")

    async def _cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _stream(self) -> None:
        base_ns = time.time_ns()
        base = time.time()
        # Next frame number per stream (frame k is due at base + k * period, so no drift).
        periods = {"ecg": ECG_FRAME / ECG_RATE_HZ, "acc": ACC_FRAME / ACC_RATE_HZ, "hr": 1.0}
        nxt = {"ecg": 1, "acc": 1, "hr": 1}
        dropped = False
        while True:
            name = min(nxt, key=lambda n: nxt[n] * periods[n])
            await asyncio.sleep(max(0.0, base + nxt[name] * periods[name] - time.time()))
            if self.auto_reconnect and not dropped and time.time() - base >= DROP_AFTER_S:
                dropped = True
                self.reconnect_attempt = 1
                self._set_state("reconnecting", "attempt 1")
                await asyncio.sleep(DROP_FOR_S)
                self.reconnect_attempt = 0
                self._set_state("streaming", "reconnected after 1 attempt(s)")
                # Frames due during the dropout never arrive; numbering follows the clock.
                now = time.time() - base
                for n in nxt:
                    nxt[n] = max(nxt[n], math.ceil(now / periods[n]))
                continue
            k = nxt[name]
            nxt[name] += 1
            if name == "ecg":
                last = k * ECG_FRAME - 1
                ts = base_ns + round(last * 1e9 / ECG_RATE_HZ)
                self._on_ecg(ts, [ecg_uv((last - (ECG_FRAME - 1 - i)) / ECG_RATE_HZ) for i in range(ECG_FRAME)])
            elif name == "acc":
                last = k * ACC_FRAME - 1
                ts = base_ns + round(last * 1e9 / ACC_RATE_HZ)
                self._on_acc(ts, [(int(20 * math.sin(last / ACC_RATE_HZ)), 0, 1000)] * ACC_FRAME)
            else:
                self._on_hr(60, [1000.0], 1)
