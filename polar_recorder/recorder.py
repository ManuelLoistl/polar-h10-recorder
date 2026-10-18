"""Session on disk: CSV writers, per-sample timestamps, gaps, markers, metadata, QA."""

from __future__ import annotations

import csv
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 2000-01-01T00:00:00Z expressed in Unix nanoseconds. polar-python adds this to the
# raw sensor clock; we subtract it again to store the sensor clock unshifted.
POLAR_EPOCH_OFFSET_NS = 946_684_800_000_000_000

ECG_RATE_HZ = 130
ECG_RESOLUTION_BITS = 14
ACC_RATE_HZ = 50
ACC_RANGE_G = 4
FRAME_GAP_FACTOR = 1.5

COLUMNS = {
    "ecg": ["host_time_utc_ns", "sensor_time_ns", "ecg_uv"],
    "acc": ["host_time_utc_ns", "sensor_time_ns", "acc_x_mg", "acc_y_mg", "acc_z_mg"],
    "hr": ["host_time_utc_ns", "hr_bpm", "sensor_contact"],
    "rr": ["host_time_utc_ns", "rr_ms"],
    "markers": ["host_time_utc_ns", "note"],
    "gaps": ["signal", "start_host_time_utc_ns", "end_host_time_utc_ns", "duration_ms", "reason"],
}
SIGNALS = ("ecg", "acc", "hr")
EXPECTED_RATE = {"ecg": ECG_RATE_HZ, "acc": ACC_RATE_HZ, "hr": 1}

# Signal validity. The H10 reports +/-19364 uV at full scale (14 bit); a second whose
# samples sit mostly at that rail carries no ECG (loose strap, dry electrodes, off body).
ECG_RAIL_UV = 19364
RAIL_FRACTION = 0.98
SATURATED_SECOND_FRACTION = 0.5
RR_SILENCE_FLAG_S = 5


class QualityTracker:
    """Counts saturated ECG seconds, lost-contact packets and RR silence from session start."""

    def __init__(self, start_ns: int):
        self.start_ns = start_ns
        self._sec: dict[int, list[int]] = {}      # second -> [samples, samples at rail]
        self.contact_lost = 0
        self._rr_ns: list[int] = []               # RR arrival host times
        self._gaps: list[tuple[int, int]] = []    # logged link gaps, excluded from RR silence

    def add_ecg(self, host_ns: int, uv: int) -> None:
        b = self._sec.setdefault((host_ns - self.start_ns) // 1_000_000_000, [0, 0])
        b[0] += 1
        if abs(uv) >= RAIL_FRACTION * ECG_RAIL_UV:
            b[1] += 1

    def add_contact(self, contact: int | None) -> None:
        if contact == 0:
            self.contact_lost += 1

    def add_rr(self, host_ns: int) -> None:
        self._rr_ns.append(host_ns)

    def add_gap(self, start_ns: int, end_ns: int) -> None:
        self._gaps.append((start_ns, end_ns))

    def _silence_ns(self, a: int, b: int) -> int:
        """Length of [a, b] minus its overlap with the logged link gaps."""
        return (b - a) - sum(max(0, min(b, ge) - max(a, gs)) for gs, ge in self._gaps)

    @staticmethod
    def _saturated(bucket: list[int]) -> bool:
        return bucket[0] > 0 and bucket[1] / bucket[0] >= SATURATED_SECOND_FRACTION

    def saturated_seconds(self) -> int:
        return sum(1 for b in self._sec.values() if self._saturated(b))

    def saturated_now(self, now_ns: int) -> bool:
        """True if the previous whole second was saturated (False after an ECG dropout)."""
        current = (now_ns - self.start_ns) // 1_000_000_000
        b = self._sec.get(current - 1)
        return b is not None and self._saturated(b)

    def summary(self, end_ns: int) -> dict:
        if not self._rr_ns:
            silence_ns = self._silence_ns(self.start_ns, end_ns)
        else:
            edges = self._rr_ns + [end_ns]
            silence_ns = max(self._silence_ns(a, b) for a, b in zip(edges, edges[1:]))
        return {
            "ecg_rail_uv": ECG_RAIL_UV,
            "ecg_saturated_s": self.saturated_seconds(),
            "contact_lost_s": self.contact_lost,
            "rr_silence_s": round(silence_ns / 1e9, 1),
        }


def sample_sensor_times(frame_ts_ns: int, n: int, rate_hz: int) -> list[int]:
    """Sensor time of each sample in a frame whose timestamp marks the last sample."""
    return [frame_ts_ns - round((n - 1 - i) * 1_000_000_000 / rate_hz) for i in range(n)]


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


class Session:
    def __init__(self, output_dir: Path, label: str, device: dict, battery: int | None,
                 software: dict, now_ns=time.time_ns):
        self._now = now_ns
        self.started_ns = now_ns()
        local = datetime.fromtimestamp(self.started_ns / 1e9)
        self.session_id = local.strftime("%Y-%m-%d_%H-%M-%S")
        if _slug(label):
            self.session_id += "_" + _slug(label)
        self.label = label
        self.dir = Path(output_dir) / self.session_id
        self.dir.mkdir(parents=True, exist_ok=False)

        self._files = {}
        self._writers = {}
        for name, cols in COLUMNS.items():
            f = open(self.dir / f"{name}.csv", "a", newline="", encoding="utf-8")
            w = csv.writer(f)
            w.writerow(cols)
            self._files[name] = f
            self._writers[name] = w

        self.counts = {"ecg": 0, "acc": 0, "hr": 0, "rr": 0, "markers": 0}
        self.gap_count = {s: 0 for s in SIGNALS}
        self._gap_total_ns = {s: 0 for s in SIGNALS}
        self._offset_ns: int | None = None
        self._clock_offsets: list[dict] = []
        self._first_host = {s: None for s in SIGNALS}      # host ns of first sample
        self._last_host = {s: None for s in SIGNALS}       # host ns of last sample
        self._last_frame_ts = {s: None for s in SIGNALS}   # unix ns of last frame timestamp
        self._pending_gap: dict[str, int] = {}             # signal -> gap start host ns
        self.stopped = False
        self.quality = QualityTracker(self.started_ns)

        self.metadata = {
            "session_id": self.session_id,
            "label": label,
            "device": device,
            "battery_percent_at_start": battery,
            "streams": {
                "ecg": {"sample_rate_hz": ECG_RATE_HZ, "resolution_bits": ECG_RESOLUTION_BITS, "unit": "uV"},
                "acc": {"sample_rate_hz": ACC_RATE_HZ, "range_g": ACC_RANGE_G, "unit": "mg"},
            },
            "start_time_utc": _iso(self.started_ns),
            "end_time_utc": None,
            "clock_offsets": self._clock_offsets,
            "timestamp_derivation": (
                "sensor_time_ns is the H10 clock in ns since 2000-01-01T00:00:00Z as sent in the "
                "PMD frame; the frame timestamp marks the last sample and earlier samples are "
                "frame_ts - (n-1-i)*1e9/rate. host_time_utc_ns = sensor_time_ns + "
                "946684800000000000 + offset_ns, with offset_ns measured on the first frame after "
                "each connect (see clock_offsets). HR/RR rows carry the notification arrival time only."
            ),
            "software": software,
            "qa": None,
        }
        self._write_metadata()

    # ------------------------------------------------------------------ helpers
    def _write_metadata(self) -> None:
        tmp = self.dir / "metadata.json.tmp"
        tmp.write_text(json.dumps(self.metadata, indent=2))
        os.replace(tmp, self.dir / "metadata.json")

    def _ensure_offset(self, frame_unix_ns: int) -> int:
        if self._offset_ns is None:
            now = self._now()
            self._offset_ns = now - frame_unix_ns
            self._clock_offsets.append({"host_time_utc_ns": now, "offset_ns": self._offset_ns})
            self._write_metadata()
        return self._offset_ns

    def _log_gap(self, signal: str, start_ns: int, end_ns: int, reason: str) -> None:
        self._writers["gaps"].writerow([signal, start_ns, end_ns, round((end_ns - start_ns) / 1e6), reason])
        self.gap_count[signal] += 1
        self._gap_total_ns[signal] += end_ns - start_ns
        if signal == "hr":
            self.quality.add_gap(start_ns, end_ns)
        self.flush()

    def _close_pending_gap(self, signal: str, end_host_ns: int) -> None:
        start = self._pending_gap.pop(signal, None)
        if start is not None:
            self._log_gap(signal, start, end_host_ns, "disconnect")

    def _check_frame_gap(self, signal: str, frame_unix_ns: int, n: int, rate_hz: int,
                         first_sample_host_ns: int) -> None:
        prev = self._last_frame_ts[signal]
        if prev is not None:
            expected = n * 1_000_000_000 / rate_hz
            if frame_unix_ns - prev > FRAME_GAP_FACTOR * expected:
                self._log_gap(signal, self._last_host[signal], first_sample_host_ns, "frame_gap")
        self._last_frame_ts[signal] = frame_unix_ns

    # ------------------------------------------------------------------ inputs
    def add_ecg(self, frame_unix_ns: int, samples: list[int]) -> None:
        self._add_frame("ecg", frame_unix_ns, samples, ECG_RATE_HZ, lambda v: [v])

    def add_acc(self, frame_unix_ns: int, samples: list[tuple[int, int, int]]) -> None:
        self._add_frame("acc", frame_unix_ns, samples, ACC_RATE_HZ, lambda v: [v[0], v[1], v[2]])

    def _add_frame(self, signal, frame_unix_ns, samples, rate_hz, cells) -> None:
        if self.stopped or not samples:
            return
        n = len(samples)
        offset = self._ensure_offset(frame_unix_ns)
        sensor_times = sample_sensor_times(frame_unix_ns - POLAR_EPOCH_OFFSET_NS, n, rate_hz)
        host_times = [t + POLAR_EPOCH_OFFSET_NS + offset for t in sensor_times]
        self._close_pending_gap(signal, host_times[0])
        self._check_frame_gap(signal, frame_unix_ns, n, rate_hz, host_times[0])
        w = self._writers[signal]
        track = self.quality.add_ecg if signal == "ecg" else None
        for h, s, v in zip(host_times, sensor_times, samples):
            w.writerow([h, s, *cells(v)])
            if track:
                track(h, v)
        self.counts[signal] += n
        if self._first_host[signal] is None:
            self._first_host[signal] = host_times[0]
        self._last_host[signal] = host_times[-1]

    def add_hr(self, hr_bpm: int, rr_ms: list[float], contact: int | None) -> None:
        if self.stopped:
            return
        now = self._now()
        self._close_pending_gap("hr", now)
        self._writers["hr"].writerow([now, hr_bpm, "" if contact is None else contact])
        self.quality.add_contact(contact)
        for rr in rr_ms:
            self._writers["rr"].writerow([now, f"{rr:.3f}"])
            self.quality.add_rr(now)
        self.counts["hr"] += 1
        self.counts["rr"] += len(rr_ms)
        if self._first_host["hr"] is None:
            self._first_host["hr"] = now
        self._last_host["hr"] = now

    def add_marker(self, note: str) -> None:
        if self.stopped:
            return
        self._writers["markers"].writerow([self._now(), note])
        self.counts["markers"] += 1
        self.flush()

    def on_disconnect(self) -> None:
        """Bluetooth link dropped: open a gap per signal, re-measure the offset later."""
        if self.stopped:
            return
        now = self._now()
        for s in SIGNALS:
            if s not in self._pending_gap:
                self._pending_gap[s] = self._last_host[s] if self._last_host[s] is not None else now
        self._offset_ns = None
        self._last_frame_ts = {s: None for s in SIGNALS}
        self.flush()

    # ------------------------------------------------------------------ output
    def flush(self) -> None:
        for f in self._files.values():
            f.flush()

    def qa(self, end_ns: int) -> dict:
        """Effective rate is measured over the span from first to last sample, minus
        gaps, so stream start-up latency and the last undelivered frame do not
        depress it; those show up as first_sample_s / last_sample_s instead."""
        duration_s = (end_ns - self.started_ns) / 1e9
        signals = {}
        for s in SIGNALS:
            gap_s = self._gap_total_ns[s] / 1e9
            first, last, n = self._first_host[s], self._last_host[s], self.counts[s]
            rate = None
            if n >= 2 and last > first:
                live = (last - first) / 1e9 - gap_s
                if live > 0:
                    rate = round((n - 1) / live, 3)
            signals[s] = {
                "sample_count": n,
                "expected_rate_hz": EXPECTED_RATE[s],
                "effective_rate_hz": rate,
                "first_sample_s": None if first is None else round((first - self.started_ns) / 1e9, 3),
                "last_sample_s": None if last is None else round((last - self.started_ns) / 1e9, 3),
                "gap_count": self.gap_count[s],
                "gap_total_s": round(gap_s, 3),
            }
        return {
            "duration_s": round(duration_s, 3),
            "signals": signals,
            "marker_count": self.counts["markers"],
            "rr_count": self.counts["rr"],
            "quality": self.quality.summary(end_ns),
        }

    def stop(self) -> dict:
        if self.stopped:
            return self.metadata["qa"]
        end = self._now()
        for s in list(self._pending_gap):
            self._close_pending_gap(s, end)
        self.stopped = True
        summary = self.qa(end)
        self.metadata["end_time_utc"] = _iso(end)
        self.metadata["qa"] = summary
        self._write_metadata()
        for f in self._files.values():
            f.flush()
            os.fsync(f.fileno())
            f.close()
        return summary


def read_markers(session_dir: Path) -> list[dict]:
    """Markers as [{"offset_s": seconds since session start, "note": str}, ...]; [] if absent."""
    d = Path(session_dir)
    try:
        meta = json.loads((d / "metadata.json").read_text())
        t0 = datetime.fromisoformat(meta["start_time_utc"].replace("Z", "+00:00")).timestamp()
        with open(d / "markers.csv", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            return [{"offset_s": round(int(ns) / 1e9 - t0, 3), "note": note} for ns, note in reader]
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return []


def list_sessions(output_dir: Path) -> list[dict]:
    out = []
    root = Path(output_dir)
    if not root.exists():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        meta = d / "metadata.json"
        if d.is_dir() and meta.exists():
            try:
                item = json.loads(meta.read_text())
            except json.JSONDecodeError:
                continue
            item["markers"] = read_markers(d)
            ecg = d / "ecg.csv"
            item["ecg_bytes"] = ecg.stat().st_size if ecg.exists() else 0
            out.append(item)
    return out


def read_ecg_window(session_dir: Path, start_s: float, seconds: float) -> list[list[float]]:
    """ECG samples as [seconds since session start, µV] for the requested window."""
    meta = json.loads((Path(session_dir) / "metadata.json").read_text())
    t0 = datetime.fromisoformat(meta["start_time_utc"].replace("Z", "+00:00")).timestamp()
    lo, hi = t0 + start_s, t0 + start_s + seconds
    out = []
    with open(Path(session_dir) / "ecg.csv", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for host_ns, _sensor, uv in reader:
            t = int(host_ns) / 1e9
            if t < lo:
                continue
            if t > hi:
                break
            out.append([round(t - t0, 6), int(uv)])
    return out


def quality_from_csv(session_dir: Path) -> dict | None:
    """The quality block recomputed from a session's CSV files; None without an end time."""
    d = Path(session_dir)
    meta = json.loads((d / "metadata.json").read_text())
    if not meta.get("end_time_utc"):
        return None
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    to_ns = lambda iso: (datetime.fromisoformat(iso.replace("Z", "+00:00")) - epoch) // timedelta(microseconds=1) * 1000
    start_ns, end_ns = to_ns(meta["start_time_utc"]), to_ns(meta["end_time_utc"])
    q = QualityTracker(start_ns)
    with open(d / "ecg.csv", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for host_ns, _sensor, uv in reader:
            q.add_ecg(int(host_ns), int(uv))
    with open(d / "hr.csv", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for _host_ns, _bpm, contact in reader:
            q.add_contact(int(contact) if contact != "" else None)
    with open(d / "rr.csv", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for host_ns, _rr in reader:
            q.add_rr(int(host_ns))
    with open(d / "gaps.csv", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        for signal, gs, ge, _ms, _reason in reader:
            if signal == "hr":
                q.add_gap(int(gs), int(ge))
    return q.summary(end_ns)


def default_output_dir(project_dir: Path) -> Path:
    """The configured output folder (config.json) or <project>/recordings."""
    config = Path(project_dir) / "config.json"
    if config.exists():
        try:
            return Path(json.loads(config.read_text())["output_dir"])
        except (KeyError, json.JSONDecodeError, TypeError):
            pass
    return Path(project_dir) / "recordings"


BAD_METADATA = (OSError, ValueError, KeyError, AttributeError, UnicodeDecodeError)


def requalify(output_dir: Path) -> list[tuple[str, str]]:
    """Add the quality block to every finished session that lacks one. Returns (id, what)."""
    out = []
    root = Path(output_dir)
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        meta_path = d / "metadata.json"
        if not (d.is_dir() and meta_path.exists()):
            continue
        try:
            meta = json.loads(meta_path.read_text())
            qa = meta.get("qa")
            if not qa or not meta.get("end_time_utc"):
                out.append((d.name, "skipped"))
                continue
            if "quality" in qa:
                out.append((d.name, "kept"))
                continue
            quality = quality_from_csv(d)
        except BAD_METADATA:
            out.append((d.name, "skipped"))
            continue
        qa["quality"] = quality
        tmp = d / "metadata.json.tmp"
        tmp.write_text(json.dumps(meta, indent=2))
        os.replace(tmp, meta_path)
        out.append((d.name, "added"))
    return out
