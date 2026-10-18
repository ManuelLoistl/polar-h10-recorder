import csv
import json

import pytest

from polar_recorder.recorder import (
    ACC_RATE_HZ,
    ECG_RATE_HZ,
    ECG_RAIL_UV,
    POLAR_EPOCH_OFFSET_NS,
    QualityTracker,
    Session,
    default_output_dir,
    list_sessions,
    quality_from_csv,
    read_ecg_window,
    read_markers,
    requalify,
    sample_sensor_times,
)

DEVICE = {"name": "Polar H10 TEST", "address": "AA", "firmware": "3.1.1"}
SOFTWARE = {"recorder": "0.1.0"}
HOST = 1_758_000_000_000_000_000
PERIOD = 1e9 / 130


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def make_session(tmp_path, clock, label="rest"):
    return Session(tmp_path, label, DEVICE, 87, SOFTWARE, now_ns=clock)


def test_sample_sensor_times_last_sample_is_frame_timestamp():
    ts = 1_000_000_000_000
    times = sample_sensor_times(ts, 3, 130)
    assert times[-1] == ts
    assert times[0] == ts - round(2 * 1e9 / 130)
    assert times == sorted(times)


def test_session_folder_and_files(tmp_path):
    s = make_session(tmp_path, Clock(HOST), label="Rest Test!")
    assert s.dir.parent == tmp_path
    assert s.dir.name.endswith("_rest-test")
    for name in ["ecg", "acc", "hr", "rr", "markers", "gaps"]:
        assert (s.dir / f"{name}.csv").exists()
    meta = json.loads((s.dir / "metadata.json").read_text())
    assert meta["label"] == "Rest Test!"
    assert meta["battery_percent_at_start"] == 87
    assert meta["streams"]["ecg"]["sample_rate_hz"] == ECG_RATE_HZ
    assert meta["streams"]["acc"]["sample_rate_hz"] == ACC_RATE_HZ
    assert meta["end_time_utc"] is None


def test_ecg_rows_have_host_and_sensor_time(tmp_path):
    s = make_session(tmp_path, Clock(HOST))
    sensor_last = 500_000_000_000  # raw sensor clock ns since 2000
    s.add_ecg(sensor_last + POLAR_EPOCH_OFFSET_NS, [10, -20, 30])
    s.flush()
    r = rows(s.dir / "ecg.csv")
    assert list(r[0].keys()) == ["host_time_utc_ns", "sensor_time_ns", "ecg_uv"]
    assert [x["ecg_uv"] for x in r] == ["10", "-20", "30"]
    assert int(r[2]["sensor_time_ns"]) == sensor_last
    # offset measured on first frame: host time of last sample == host clock
    assert int(r[2]["host_time_utc_ns"]) == HOST
    assert int(r[1]["host_time_utc_ns"]) == HOST - round(PERIOD)
    assert s.counts["ecg"] == 3


def test_acc_rows(tmp_path):
    s = make_session(tmp_path, Clock(HOST))
    s.add_acc(POLAR_EPOCH_OFFSET_NS + 10**12, [(1, 2, 3), (4, 5, 6)])
    s.flush()
    r = rows(s.dir / "acc.csv")
    assert list(r[0].keys()) == [
        "host_time_utc_ns", "sensor_time_ns", "acc_x_mg", "acc_y_mg", "acc_z_mg"]
    assert [x["acc_z_mg"] for x in r] == ["3", "6"]
    assert s.counts["acc"] == 2


def test_hr_and_rr_rows(tmp_path):
    s = make_session(tmp_path, Clock(HOST))
    s.add_hr(62, [1000.0, 1024 / 1024 * 1000], 1)
    s.add_hr(63, [], None)
    s.flush()
    hr = rows(s.dir / "hr.csv")
    assert [x["hr_bpm"] for x in hr] == ["62", "63"]
    assert [x["sensor_contact"] for x in hr] == ["1", ""]
    rr = rows(s.dir / "rr.csv")
    assert [x["rr_ms"] for x in rr] == ["1000.000", "1000.000"]
    assert s.counts["hr"] == 2 and s.counts["rr"] == 2


def test_marker(tmp_path):
    clock = Clock(HOST)
    s = make_session(tmp_path, clock)
    clock.t += 5_000_000_000
    s.add_marker("palpitations, mild")
    r = rows(s.dir / "markers.csv")
    assert r == [{"host_time_utc_ns": str(clock.t), "note": "palpitations, mild"}]


def test_disconnect_gap_and_offset_remeasured(tmp_path):
    clock = Clock(HOST)
    s = make_session(tmp_path, clock)
    s.add_ecg(POLAR_EPOCH_OFFSET_NS + 10**12, [1])
    s.on_disconnect()
    # sensor clock restarted; host moved on 30 s
    clock.t = HOST + 30_000_000_000
    s.add_ecg(POLAR_EPOCH_OFFSET_NS + 5 * 10**9, [2])
    s.flush()
    gaps = rows(s.dir / "gaps.csv")
    assert len(gaps) == 1
    g = gaps[0]
    assert g["signal"] == "ecg" and g["reason"] == "disconnect"
    assert int(g["start_host_time_utc_ns"]) == HOST
    assert int(g["end_host_time_utc_ns"]) == clock.t
    assert g["duration_ms"] == "30000"
    ecg = rows(s.dir / "ecg.csv")
    assert int(ecg[1]["host_time_utc_ns"]) == clock.t
    meta = json.loads((s.dir / "metadata.json").read_text())
    assert len(meta["clock_offsets"]) == 2


def test_frame_gap_detected(tmp_path):
    s = make_session(tmp_path, Clock(HOST))
    f1 = POLAR_EPOCH_OFFSET_NS + 10**12
    s.add_ecg(f1, [0] * 73)
    f2 = f1 + round(73 * PERIOD)          # exactly one frame later: no gap
    s.add_ecg(f2, [0] * 73)
    f3 = f2 + round(2 * 73 * PERIOD)      # one frame missing
    s.add_ecg(f3, [0] * 73)
    s.flush()
    gaps = rows(s.dir / "gaps.csv")
    assert len(gaps) == 1
    assert gaps[0]["reason"] == "frame_gap" and gaps[0]["signal"] == "ecg"
    start = int(gaps[0]["start_host_time_utc_ns"])
    end = int(gaps[0]["end_host_time_utc_ns"])
    # from the last sample of frame 2 to the first sample of frame 3
    expected = round(2 * 73 * PERIOD) - round(72 * PERIOD)
    assert abs((end - start) - expected) <= 2


def test_stop_writes_qa(tmp_path):
    clock = Clock(HOST)
    s = make_session(tmp_path, clock)
    f = POLAR_EPOCH_OFFSET_NS + 10**12
    s.add_ecg(f, [0] * 130)
    s.add_acc(f, [(0, 0, 0)] * 50)
    s.add_hr(60, [1000.0], 1)
    s.add_marker("x")
    clock.t = HOST + 1_000_000_000
    qa = s.stop()
    assert qa["duration_s"] == 1.0
    assert qa["signals"]["ecg"]["sample_count"] == 130
    assert qa["signals"]["ecg"]["effective_rate_hz"] == 130.0
    assert qa["signals"]["acc"]["effective_rate_hz"] == 50.0
    assert qa["signals"]["hr"]["effective_rate_hz"] is None  # one packet, no span
    # last ECG sample coincides with the offset measurement (session start)
    assert qa["signals"]["ecg"]["last_sample_s"] == 0.0
    assert qa["signals"]["ecg"]["first_sample_s"] == pytest.approx(-129 / 130, abs=1e-3)
    assert qa["signals"]["ecg"]["gap_count"] == 0
    assert qa["marker_count"] == 1 and qa["rr_count"] == 1
    meta = json.loads((s.dir / "metadata.json").read_text())
    assert meta["end_time_utc"] is not None
    assert meta["qa"] == qa
    # after stop nothing more is written
    s.add_ecg(f, [1])
    assert s.counts["ecg"] == 130


def test_stop_while_disconnected_closes_gap(tmp_path):
    clock = Clock(HOST)
    s = make_session(tmp_path, clock)
    s.add_ecg(POLAR_EPOCH_OFFSET_NS + 10**12, [0])
    s.on_disconnect()
    clock.t = HOST + 10_000_000_000
    qa = s.stop()
    assert qa["signals"]["ecg"]["gap_count"] == 1
    assert qa["signals"]["ecg"]["gap_total_s"] == 10.0


def test_list_sessions_and_read_window(tmp_path):
    clock = Clock(HOST)
    s = make_session(tmp_path, clock)
    s.add_ecg(POLAR_EPOCH_OFFSET_NS + 10**12, list(range(130)))
    clock.t = HOST + 2_000_000_000
    s.stop()
    sessions = list_sessions(tmp_path)
    assert len(sessions) == 1 and sessions[0]["session_id"] == s.session_id
    window = read_ecg_window(s.dir, -1.0, 10.0)
    assert len(window) == 130
    assert window[-1][1] == 129
    # last sample coincides with session start; first is 129 periods earlier
    assert window[-1][0] == pytest.approx(0.0, abs=1e-6)
    assert window[0][0] == pytest.approx(-129 / 130, abs=1e-6)
    assert read_ecg_window(s.dir, 5.0, 10.0) == []


def test_read_markers_offsets_and_missing(tmp_path):
    t = [1_700_000_000_000_000_000]

    def now():
        t[0] += 500_000_000
        return t[0]

    s = Session(tmp_path, "m", {"name": "H10"}, 80, {}, now_ns=now)
    s.add_marker("first")
    s.add_marker("second note")
    s.stop()
    m = read_markers(s.dir)
    assert [x["note"] for x in m] == ["first", "second note"]
    assert [x["offset_s"] for x in m] == [0.5, 1.0]
    assert read_markers(tmp_path / "does-not-exist") == []


def test_list_sessions_carries_markers_and_ecg_size(tmp_path):
    s = Session(tmp_path, "z", {"name": "H10"}, 80, {})
    s.add_marker("x")
    s.stop()
    item = list_sessions(tmp_path)[0]
    assert item["markers"][0]["note"] == "x"
    assert item["ecg_bytes"] == (s.dir / "ecg.csv").stat().st_size


def _clock(start=1_700_000_000_000_000_000, step=0):
    t = [start]

    def now():
        t[0] += step
        return t[0]

    return now, t


def test_quality_tracker_counts_saturated_seconds():
    t0 = 1_700_000_000_000_000_000
    q = QualityTracker(t0)
    sec = 1_000_000_000
    for i in range(130):                      # second 0: clean
        q.add_ecg(t0 + i * sec // 130, 300 if i % 2 else -300)
    for i in range(130):                      # second 1: all at the rail
        q.add_ecg(t0 + sec + i * sec // 130, ECG_RAIL_UV)
    for i in range(130):                      # second 2: 40 % at the rail -> not saturated
        q.add_ecg(t0 + 2 * sec + i * sec // 130, ECG_RAIL_UV if i < 52 else 0)
    assert q.saturated_seconds() == 1
    assert q.saturated_now(t0 + 2 * sec + 10) is True      # last completed second is 1
    assert q.saturated_now(t0 + 3 * sec + 10) is False     # last completed second is 2
    assert q.summary(t0 + 3 * sec)["ecg_saturated_s"] == 1
    gap = QualityTracker(t0)
    for i in range(130):                      # second 0 fully at the rail, then nothing
        gap.add_ecg(t0 + i * sec // 130, ECG_RAIL_UV)
    assert gap.saturated_now(t0 + sec + 10) is True     # previous second (0) was saturated
    assert gap.saturated_now(t0 + 6 * sec) is False     # seconds 1-5 had no ECG: no stale badge


def test_quality_tracker_contact_and_rr_silence():
    t0 = 1_700_000_000_000_000_000
    sec = 1_000_000_000
    q = QualityTracker(t0)
    for s in range(10):
        q.add_contact(1 if s < 8 else 0)
    for s in (2, 3, 4, 14):                   # longest silence is the 10 s gap (tail is only 3 s)
        q.add_rr(t0 + s * sec)
    out = q.summary(t0 + 17 * sec)
    assert out["contact_lost_s"] == 2
    assert out["rr_silence_s"] == 10.0
    assert out["ecg_rail_uv"] == ECG_RAIL_UV
    tail = QualityTracker(t0)
    for s in (2, 5):                          # gap is 3 s, the 10 s tail to the end wins
        tail.add_rr(t0 + s * sec)
    assert tail.summary(t0 + 15 * sec)["rr_silence_s"] == 10.0
    empty = QualityTracker(t0)
    assert empty.summary(t0 + 20 * sec)["rr_silence_s"] == 20.0
    assert empty.saturated_now(t0 + 5 * sec) is False


def test_rr_silence_excludes_logged_gaps():
    t0 = 1_700_000_000_000_000_000
    sec = 1_000_000_000
    q = QualityTracker(t0)
    for s in (2, 3, 4, 10):
        q.add_rr(t0 + s * sec)
    q.add_gap(t0 + 4 * sec + sec // 2, t0 + 9 * sec + sec // 2)
    assert q.summary(t0 + 12 * sec)["rr_silence_s"] == 2.0                 # 6 s interval minus 5 s excluded; the tail is 2 s


def test_rr_silence_without_gap_is_the_full_interval():
    t0 = 1_700_000_000_000_000_000
    sec = 1_000_000_000
    q = QualityTracker(t0)
    for s in (2, 3, 4, 10):
        q.add_rr(t0 + s * sec)
    assert q.summary(t0 + 12 * sec)["rr_silence_s"] == 6.0


def test_session_qa_has_quality_and_csv_path_matches(tmp_path):
    now, t = _clock(step=0)
    s = Session(tmp_path, "q", {"name": "H10"}, 80, {}, now_ns=now)
    sec = 1_000_000_000
    base = POLAR_EPOCH_OFFSET_NS + 10**12
    t[0] = s.started_ns                       # offset becomes 0-ish: host == frame time
    s._offset_ns = s.started_ns - base
    for k in range(2):                        # two seconds of ECG: second 0 clean, second 1 at the rail
        frame_ts = base + (k + 1) * sec - 1   # last sample 1 ns before the second boundary, so each frame fills one bucket
        samples = [0] * 130 if k == 0 else [ECG_RAIL_UV] * 130
        s.add_ecg(frame_ts, samples)
    t[0] = s.started_ns + 1 * sec
    s.add_hr(60, [1000.0], 1)
    s.add_hr(60, [900.0, 950.0], None)        # empty contact cell, two RR values on one timestamp
    t[0] = s.started_ns + 2 * sec
    s.add_hr(60, [], 0)
    t[0] = s.started_ns + 3 * sec
    s.on_disconnect()                         # link drops; the next HR packet closes the gap
    t[0] = s.started_ns + 6 * sec
    s.add_hr(60, [], 1)
    t[0] = s.started_ns + 9 * sec
    qa = s.stop()
    q = qa["quality"]
    assert q["ecg_saturated_s"] == 1
    assert q["contact_lost_s"] == 1
    assert q["rr_silence_s"] == 4.0           # last RR at 1 s, end at 9 s: 8 s minus the logged hr gap 2-6 s
    with open(s.dir / "gaps.csv", newline="") as f:
        rows = list(csv.reader(f))[1:]
    assert any(r[0] == "hr" and r[4] == "disconnect" for r in rows)
    assert quality_from_csv(s.dir) == q


def test_quality_from_csv_is_none_for_unstopped_session(tmp_path):
    now, _ = _clock()
    s = Session(tmp_path, "open", {"name": "H10"}, 80, {}, now_ns=now)
    assert quality_from_csv(s.dir) is None


def test_quality_tracker_pre_start_sample_counts_as_a_bucket():
    t0 = 1_700_000_000_000_000_000
    q = QualityTracker(t0)
    q.add_ecg(t0 - 1, ECG_RAIL_UV)            # bucket -1: partial pre-start frames count (at most one frame)
    assert q.saturated_seconds() == 1


def test_quality_tracker_saturation_threshold_is_half():
    t0 = 1_700_000_000_000_000_000
    sec = 1_000_000_000
    q = QualityTracker(t0)
    for i in range(130):                      # second 0: 65 of 130 at the rail -> saturated
        q.add_ecg(t0 + i * sec // 130, ECG_RAIL_UV if i < 65 else 0)
    for i in range(130):                      # second 1: 64 of 130 -> not saturated
        q.add_ecg(t0 + sec + i * sec // 130, ECG_RAIL_UV if i < 64 else 0)
    assert q.saturated_seconds() == 1
    assert q.saturated_now(t0 + sec + 10) is True
    assert q.saturated_now(t0 + 2 * sec + 10) is False


def test_requalify_adds_missing_quality_and_keeps_existing(tmp_path):
    now, t = _clock(step=1_000_000_000)
    s = Session(tmp_path, "old", {"name": "H10"}, 80, {}, now_ns=now)
    s.add_hr(60, [1000.0], 1)
    s.stop()
    meta_path = s.dir / "metadata.json"
    meta = json.loads(meta_path.read_text())
    expected = meta["qa"].pop("quality")
    meta_path.write_text(json.dumps(meta))
    s2 = Session(tmp_path, "new", {"name": "H10"}, 80, {}, now_ns=now)
    s2.stop()
    before = (s2.dir / "metadata.json").read_text()
    result = dict(requalify(tmp_path))
    assert result[s.session_id] == "added" and result[s2.session_id] == "kept"
    assert json.loads(meta_path.read_text())["qa"]["quality"] == expected
    assert (s2.dir / "metadata.json").read_text() == before


def test_requalify_skips_non_dict_metadata(tmp_path):
    bad = tmp_path / "2026-01-01_000000_bad"
    bad.mkdir()
    (bad / "metadata.json").write_text("[]")
    now, _ = _clock(step=1_000_000_000)
    s = Session(tmp_path, "ok", {"name": "H10"}, 80, {}, now_ns=now)
    s.stop()
    meta = json.loads((s.dir / "metadata.json").read_text())
    del meta["qa"]["quality"]
    (s.dir / "metadata.json").write_text(json.dumps(meta))
    result = dict(requalify(tmp_path))
    assert result[bad.name] == "skipped" and result[s.session_id] == "added"


def test_default_output_dir(tmp_path):
    assert default_output_dir(tmp_path) == tmp_path / "recordings"
    (tmp_path / "config.json").write_text(json.dumps({"output_dir": str(tmp_path / "elsewhere")}))
    assert default_output_dir(tmp_path) == tmp_path / "elsewhere"


def test_connect_timeout_message_names_the_timeout(monkeypatch):
    import asyncio
    from polar_recorder import ble

    async def never(self):
        raise TimeoutError

    monkeypatch.setattr(ble._Device, "connect", never)
    noop = lambda *a, **k: None
    link = ble.PolarLink(noop, noop, noop, noop)
    with pytest.raises(ble.NotFound) as exc:
        asyncio.run(link.connect("00:00:00:00:00:00"))
    assert "connection timed out after 30 s" in str(exc.value)
    assert "bleak: )" not in str(exc.value)
