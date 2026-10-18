import time

from fastapi.testclient import TestClient

from polar_recorder.fake import FakePolarLink, ecg_uv
from polar_recorder.server import create_app


def test_ecg_waveform_has_an_r_peak_and_a_flat_baseline():
    assert ecg_uv(0.23) > 1000
    assert abs(ecg_uv(0.70)) < 20
    assert ecg_uv(1.23) == ecg_uv(0.23)


def test_fake_link_streams_into_a_session(tmp_path):
    (tmp_path / "polar_recorder" / "static").mkdir(parents=True)
    (tmp_path / "polar_recorder" / "static" / "index.html").write_text("<h1>ok</h1>")
    app = create_app(tmp_path, link_factory=FakePolarLink)
    with TestClient(app) as c:
        devices = c.post("/api/scan").json()["devices"]
        assert devices[0]["name"].startswith("Polar H10")
        info = c.post("/api/connect", json={"address": devices[0]["address"]}).json()
        assert info["ecg_supported"] is True and info["firmware"] == "5.0.0"
        assert c.post("/api/start", json={"label": "fake"}).status_code == 200
        time.sleep(1.5)
        st = c.get("/api/status").json()
        assert st["connection"] == "streaming"
        assert st["ecg_samples"] > 100 and st["acc_samples"] > 40
        assert st["hr_bpm"] == 60 and st["contact"] == 1
        qa = c.post("/api/stop").json()
        assert qa["signals"]["ecg"]["sample_count"] >= st["ecg_samples"]
        assert c.get("/api/status").json()["connection"] == "connected"
        c.post("/api/disconnect")
        assert c.get("/api/status").json()["connection"] == "disconnected"


def test_fake_stream_drops_once_and_keeps_sample_timing(monkeypatch):
    import asyncio

    from polar_recorder import fake

    monkeypatch.setattr(fake, "DROP_AFTER_S", 1.5)
    monkeypatch.setattr(fake, "DROP_FOR_S", 1.0)
    ecg, acc, states = [], [], []

    async def run():
        link = FakePolarLink(lambda ts, s: ecg.append((ts, s)), lambda ts, s: acc.append((ts, s)),
                             lambda *a: None, lambda st, d: states.append(st))
        link.auto_reconnect = True
        await link.start_streams()
        await asyncio.sleep(4.5)
        await link.stop_streams()

    asyncio.run(run())
    assert states.count("reconnecting") == 1
    assert states[:3] == ["streaming", "reconnecting", "streaming"]
    deltas = [(b[0] - a[0]) / 1e9 for a, b in zip(ecg, ecg[1:])]
    assert all(len(s) == 73 for _, s in ecg) and all(len(s) == 36 for _, s in acc)
    assert sum(d > 1.0 for d in deltas) == 1  # the one dropout
    assert all(abs(d - 73 / 130) < 1e-6 for d in deltas if d < 1.0)  # 73 samples at 130 Hz per frame
    adeltas = [(b[0] - a[0]) / 1e9 for a, b in zip(acc, acc[1:])]
    assert sum(d > 1.0 for d in adeltas) == 1
    assert all(abs(d - 36 / 50) < 1e-6 for d in adeltas if d < 1.0)  # 36 samples at 50 Hz per frame
