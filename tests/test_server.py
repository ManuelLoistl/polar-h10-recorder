import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from polar_recorder import server as server_mod
from polar_recorder.recorder import ECG_RAIL_UV, POLAR_EPOCH_OFFSET_NS
from polar_recorder.server import create_app


class FakeLink:
    instance = None

    def __init__(self, on_ecg, on_acc, on_hr, on_state):
        FakeLink.instance = self
        self.on_ecg, self.on_acc, self.on_hr, self.on_state = on_ecg, on_acc, on_hr, on_state
        self.state = "disconnected"
        self.info = None
        self.auto_reconnect = False
        self.reconnect_attempt = 0
        self.streams_started = 0
        self.streams_stopped = 0

    async def scan(self, timeout=6.0):
        return [{"name": "Polar H10 FAKE", "address": "AA:BB"}]

    async def connect(self, address):
        self.info = {"name": "Polar H10 FAKE", "address": address, "firmware": "3.1.1",
                     "battery_percent": 80, "ecg_supported": True}
        self.state = "connected"
        return self.info

    async def start_streams(self):
        self.streams_started += 1
        self.state = "streaming"

    async def stop_streams(self):
        self.streams_stopped += 1
        self.state = "connected"

    async def disconnect(self):
        self.state = "disconnected"

    async def refresh_battery(self):
        return 80


@pytest.fixture
def client(tmp_path):
    (tmp_path / "polar_recorder" / "static").mkdir(parents=True)
    (tmp_path / "polar_recorder" / "static" / "index.html").write_text("<h1>ok</h1>")
    app = create_app(tmp_path, link_factory=FakeLink)
    with TestClient(app) as c:
        yield c


def test_index_and_status(client):
    assert client.get("/").status_code == 200
    st = client.get("/api/status").json()
    assert st["connection"] == "disconnected" and st["recording"] is False


def test_start_requires_connection(client):
    assert client.post("/api/start", json={"label": "x"}).status_code == 409


def test_marker_requires_recording(client):
    assert client.post("/api/marker", json={"note": "x"}).status_code == 409


def test_full_flow(client, tmp_path):
    assert client.post("/api/scan").json()["devices"][0]["address"] == "AA:BB"
    info = client.post("/api/connect", json={"address": "AA:BB"}).json()
    assert info["firmware"] == "3.1.1"
    r = client.post("/api/start", json={"label": "  Test  "})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    link = FakeLink.instance
    assert link.streams_started == 1 and link.auto_reconnect is True
    link.on_ecg(POLAR_EPOCH_OFFSET_NS + 10**12, [1, 2, 3])
    link.on_hr(60, [1000.0], 1)
    assert client.post("/api/marker", json={"note": "beat"}).status_code == 200
    st = client.get("/api/status").json()
    assert st["recording"] is True and st["ecg_samples"] == 3 and st["hr_bpm"] == 60
    qa = client.post("/api/stop").json()
    assert qa["signals"]["ecg"]["sample_count"] == 3 and qa["marker_count"] == 1
    assert link.streams_stopped == 1 and link.auto_reconnect is False
    sessions = client.get("/api/sessions").json()
    assert sessions[0]["session_id"] == sid
    assert sessions[0]["label"] == "Test"
    assert sessions[0]["markers"][0]["note"] == "beat"
    assert sessions[0]["markers"][0]["offset_s"] >= 0
    assert sessions[0]["ecg_bytes"] > 0
    ecg = client.get(f"/api/sessions/{sid}/ecg", params={"start_s": -1, "seconds": 5}).json()
    assert len(ecg["samples"]) == 3
    assert Path(tmp_path / "recordings" / sid / "ecg.csv").exists()
    assert client.post("/api/stop").status_code == 409


def test_settings_output_dir(client, tmp_path):
    assert client.get("/api/settings").json()["output_dir"] == str(tmp_path / "recordings")
    new = tmp_path / "elsewhere"
    r = client.post("/api/settings", json={"output_dir": str(new)})
    assert r.status_code == 200 and new.exists()
    assert json.loads((tmp_path / "config.json").read_text())["output_dir"] == str(new)


def test_disconnect_while_recording_is_logged(client):
    client.post("/api/connect", json={"address": "AA:BB"})
    client.post("/api/start", json={"label": ""})
    link = FakeLink.instance
    link.on_ecg(POLAR_EPOCH_OFFSET_NS + 10**12, [1])
    link.on_state("reconnecting", "attempt 1")
    link.on_state("reconnecting", "attempt 2")
    link.on_state("streaming", "reconnected")
    link.on_ecg(POLAR_EPOCH_OFFSET_NS + 2 * 10**12, [2])
    link.on_acc(POLAR_EPOCH_OFFSET_NS + 2 * 10**12, [(0, 0, 1000)])
    link.on_hr(60, [1000.0], 1)
    assert client.get("/api/status").json()["gaps"] == 1
    qa = client.post("/api/stop").json()
    assert qa["signals"]["ecg"]["gap_count"] == 1


def test_reconnect_failure_stops_session(client):
    client.post("/api/connect", json={"address": "AA:BB"})
    client.post("/api/start", json={"label": ""})
    link = FakeLink.instance
    link.on_state("reconnecting", "attempt 1")
    link.on_state("reconnect_failed", "gave up")
    st = client.get("/api/status").json()
    assert st["recording"] is False and "Reconnect failed" in st["error"]
    assert len(client.get("/api/sessions").json()) == 1


def test_status_has_contact_and_rr(client):
    assert client.get("/api/status").json()["contact"] is None
    client.post("/api/connect", json={"address": "AA:BB"})
    client.post("/api/start", json={"label": ""})
    FakeLink.instance.on_hr(62, [980.0, 1010.0], 1)
    st = client.get("/api/status").json()
    assert st["contact"] == 1 and st["rr_samples"] == 2
    client.post("/api/stop")


def record_one(client, label="x"):
    client.post("/api/connect", json={"address": "AA:BB"})
    sid = client.post("/api/start", json={"label": label}).json()["session_id"]
    FakeLink.instance.on_hr(60, [1000.0], 1)
    client.post("/api/marker", json={"note": "beat"})
    client.post("/api/stop")
    return sid


def test_reveal_refuses_bad_ids(client, monkeypatch):
    calls = []
    monkeypatch.setattr(server_mod.subprocess, "Popen", lambda args, **kw: calls.append(args))
    assert client.post("/api/sessions/%2e%2e/reveal").status_code == 404
    assert client.post("/api/sessions/nope/reveal").status_code == 404
    assert calls == []


def test_reveal_runs_open_on_macos(client, monkeypatch):
    sid = record_one(client)
    calls = []
    monkeypatch.setattr(server_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(server_mod.subprocess, "Popen", lambda args, **kw: calls.append(args))
    assert client.post(f"/api/sessions/{sid}/reveal").status_code == 200
    assert calls == [["open", "-R", str(client.app.state.recorder.output_dir / sid)]]


def test_reveal_is_501_elsewhere(client, monkeypatch):
    sid = record_one(client)
    calls = []
    monkeypatch.setattr(server_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(server_mod.subprocess, "Popen", lambda args, **kw: calls.append(args))
    r = client.post(f"/api/sessions/{sid}/reveal")
    assert r.status_code == 501 and "macOS" in r.json()["detail"]
    assert calls == []


def test_status_reports_saturation(client):
    st = client.get("/api/status").json()
    assert st["ecg_saturated_s"] == 0 and st["ecg_saturated_now"] is False
    client.post("/api/connect", json={"address": "AA:BB"})
    client.post("/api/start", json={"label": ""})
    link = FakeLink.instance
    base = POLAR_EPOCH_OFFSET_NS + 10**12
    link.on_ecg(base, [ECG_RAIL_UV] * 130)           # one second of rail, host time ~ now
    link.on_ecg(base + 2 * 10**9, [0] * 130)           # a clean second two seconds later
    st = client.get("/api/status").json()
    assert st["ecg_saturated_s"] >= 1
    assert isinstance(st["ecg_saturated_now"], bool)
    client.post("/api/stop")


def test_index_is_not_cached(client):
    assert client.get("/").headers["cache-control"] == "no-cache"


def test_delete_session_removes_folder(client):
    sid = record_one(client)
    folder = client.app.state.recorder.output_dir / sid
    assert folder.exists()
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert not folder.exists()
    assert sid not in [s["session_id"] for s in client.get("/api/sessions").json()]
    assert client.delete(f"/api/sessions/{sid}").status_code == 404


def test_delete_refuses_bad_ids_and_open_session(client):
    assert client.delete("/api/sessions/%2e%2e").status_code == 404
    assert client.delete("/api/sessions/nope").status_code == 404
    client.post("/api/connect", json={"address": "AA:BB"})
    sid = client.post("/api/start", json={"label": "live"}).json()["session_id"]
    r = client.delete(f"/api/sessions/{sid}")
    assert r.status_code == 409 and "Stop" in r.json()["detail"]
    client.post("/api/stop")
    assert (client.app.state.recorder.output_dir / sid).exists()


def test_delete_refuses_dot_id_even_when_output_dir_has_metadata(client):
    out = client.app.state.recorder.output_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "metadata.json").write_text("{}")
    assert client.delete("/api/sessions/%2e").status_code == 404
    assert client.post("/api/sessions/%2e/reveal").status_code == 404
    assert out.exists() and (out / "metadata.json").exists()


def test_session_stops_at_the_hour_limit(client):
    client.post("/api/connect", json={"address": "AA:BB"})
    sid = client.post("/api/start", json={"label": ""}).json()["session_id"]
    rec = client.app.state.recorder
    assert asyncio.run(rec.stop_if_over_limit()) is False
    rec.session.started_ns -= (server_mod.MAX_SESSION_S + 1) * 10**9
    assert asyncio.run(rec.stop_if_over_limit()) is True
    st = client.get("/api/status").json()
    assert st["recording"] is False and st["stopped_at_limit"] == sid
    assert FakeLink.instance.streams_stopped == 1 and FakeLink.instance.auto_reconnect is False
    assert len(client.get("/api/sessions").json()) == 1
    time.sleep(1.1)   # folder names carry seconds
    client.post("/api/start", json={"label": ""})
    assert client.get("/api/status").json()["stopped_at_limit"] is None
    client.post("/api/stop")


def test_status_carries_the_label(client):
    client.post("/api/connect", json={"address": "AA:BB"})
    assert client.get("/api/status").json()["label"] == ""
    client.post("/api/start", json={"label": "  morning rest "})
    assert client.get("/api/status").json()["label"] == "morning rest"
    client.post("/api/stop")
