"""FastAPI app: glues PolarLink and Session, serves the single page."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from importlib.metadata import version as pkg_version
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import __version__
from .ble import NOT_FOUND_HINT, EcgUnsupported, NotFound, PolarLink
from .recorder import Session, default_output_dir, list_sessions, read_ecg_window

log = logging.getLogger(__name__)
MAX_SESSION_S = 3600   # a session is stopped automatically at one hour
PROJECT_DIR = Path(__file__).resolve().parent.parent


class ConnectBody(BaseModel):
    address: str


class StartBody(BaseModel):
    label: str = ""


class MarkerBody(BaseModel):
    note: str = ""


class SettingsBody(BaseModel):
    output_dir: str


class Recorder:
    """Application state: one link, at most one active session."""
    stopped_at_limit: str | None = None   # session id stopped by the one-hour limit, until the next start

    def __init__(self, project_dir: Path, link_factory):
        self.project_dir = project_dir
        self.config_path = project_dir / "config.json"
        self.output_dir = self._load_output_dir()
        self.session: Session | None = None
        self.hr_bpm: int | None = None
        self.contact: int | None = None
        self.last_error = ""
        self.link_detail = ""
        self.link = link_factory(self._on_ecg, self._on_acc, self._on_hr, self._on_state)

    # ------------------------------------------------------------------ config
    def _load_output_dir(self) -> Path:
        return default_output_dir(self.project_dir)

    def set_output_dir(self, path: str) -> None:
        p = Path(path).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        self.output_dir = p
        self.config_path.write_text(json.dumps({"output_dir": str(p)}, indent=2))

    # ------------------------------------------------------------------ link callbacks
    def _on_ecg(self, ts, samples):
        if self.session:
            self.session.add_ecg(ts, samples)

    def _on_acc(self, ts, samples):
        if self.session:
            self.session.add_acc(ts, samples)

    def _on_hr(self, bpm, rr, contact):
        self.hr_bpm = bpm
        self.contact = contact
        if self.session:
            self.session.add_hr(bpm, rr, contact)

    def _on_state(self, state, detail):
        self.link_detail = detail
        if self.session is None:
            return
        if state in ("reconnecting", "disconnected"):
            self.session.on_disconnect()
        elif state == "reconnect_failed":
            self.last_error = "Reconnect failed after 5 minutes; the session was stopped."
            self.stop_session()

    # ------------------------------------------------------------------ session
    def start_session(self, label: str) -> Session:
        if self.session:
            raise HTTPException(409, "Already recording")
        if self.link.state not in ("connected", "streaming"):
            raise HTTPException(409, "Connect to the H10 first")
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"Output directory not writable: {self.output_dir} ({e})")
        software = {
            "recorder": __version__,
            "python": platform.python_version(),
            "bleak": pkg_version("bleak"),
            "polar_python": pkg_version("polar-python"),
        }
        info = dict(self.link.info or {})
        self.session = Session(self.output_dir, label.strip(), info, info.get("battery_percent"), software)
        self.last_error = ""
        self.stopped_at_limit = None
        log.info("session started: %s", self.session.dir)
        return self.session

    def stop_session(self) -> dict:
        s, self.session = self.session, None
        if s is None:
            raise HTTPException(409, "Not recording")
        qa = s.stop()
        log.info("session stopped: %s qa=%s", s.dir, qa)
        return qa

    async def stop_if_over_limit(self) -> bool:
        s = self.session
        if s is None or (time.time_ns() - s.started_ns) / 1e9 < MAX_SESSION_S:
            return False
        self.link.auto_reconnect = False
        try:
            await self.link.stop_streams()
        except Exception as e:
            log.warning("stop_streams: %s", e)
        self.stop_session()
        self.stopped_at_limit = s.session_id
        log.info("session stopped at the %d s limit: %s", MAX_SESSION_S, s.dir)
        return True

    def status(self) -> dict:
        s = self.session
        return {
            "connection": self.link.state,
            "detail": self.link_detail,
            "device": self.link.info,
            "hr_bpm": self.hr_bpm,
            "contact": self.contact,
            "recording": s is not None,
            "session_id": s.session_id if s else None,
            "label": s.label if s else "",
            "elapsed_s": round((time.time_ns() - s.started_ns) / 1e9, 1) if s else 0,
            "ecg_samples": s.counts["ecg"] if s else 0,
            "acc_samples": s.counts["acc"] if s else 0,
            "hr_samples": s.counts["hr"] if s else 0,
            "rr_samples": s.counts["rr"] if s else 0,
            "markers": s.counts["markers"] if s else 0,
            "gaps": max(s.gap_count.values()) if s else 0,
            "ecg_saturated_s": s.quality.saturated_seconds() if s else 0,
            "ecg_saturated_now": s.quality.saturated_now(time.time_ns()) if s else False,
            "stopped_at_limit": self.stopped_at_limit,
            "reconnect_attempt": self.link.reconnect_attempt,
            "error": self.last_error,
            "output_dir": str(self.output_dir),
        }


def create_app(project_dir: Path = PROJECT_DIR, link_factory=PolarLink) -> FastAPI:
    rec = Recorder(project_dir, link_factory)
    static = project_dir / "polar_recorder" / "static" / "index.html"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def flusher():
            while True:
                await asyncio.sleep(1)
                if rec.session:
                    rec.session.flush()
                await rec.stop_if_over_limit()

        task = asyncio.create_task(flusher())
        yield
        task.cancel()
        if rec.session:
            rec.link.auto_reconnect = False
            rec.session.stop()
            rec.session = None
        await rec.link.disconnect()

    app = FastAPI(title="Polar H10 Recorder", lifespan=lifespan)
    app.state.recorder = rec

    def session_dir(session_id: str) -> Path:
        if "/" in session_id or ".." in session_id:
            raise HTTPException(404, "Unknown session")
        root = rec.output_dir.resolve()
        d = (rec.output_dir / session_id).resolve()
        if d.parent != root or not d.is_dir() or not (d / "metadata.json").exists():
            raise HTTPException(404, "Unknown session")
        return d

    @app.get("/")
    async def index():
        return FileResponse(static, headers={"Cache-Control": "no-cache"})

    @app.get("/api/status")
    async def status():
        return rec.status()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(rec.status())
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            pass

    @app.post("/api/scan")
    async def scan():
        rec.last_error = ""
        try:
            devices = await rec.link.scan()
        except Exception as e:
            rec.last_error = f"Scan failed: {e}"
            raise HTTPException(500, rec.last_error)
        if not devices:
            rec.last_error = NOT_FOUND_HINT
        return {"devices": devices}

    @app.post("/api/connect")
    async def connect(body: ConnectBody):
        rec.last_error = ""
        try:
            return await rec.link.connect(body.address)
        except NotFound as e:
            rec.last_error = str(e)
            raise HTTPException(404, str(e))
        except EcgUnsupported as e:
            rec.last_error = str(e)
            raise HTTPException(400, str(e))

    @app.post("/api/disconnect")
    async def disconnect():
        if rec.session:
            raise HTTPException(409, "Stop the recording first")
        await rec.link.disconnect()
        return {"ok": True}

    @app.post("/api/start")
    async def start(body: StartBody):
        session = rec.start_session(body.label)
        rec.link.auto_reconnect = True
        try:
            await rec.link.start_streams()
        except Exception as e:
            rec.link.auto_reconnect = False
            rec.session = None
            session.stop()
            rec.last_error = f"Could not start streams: {e}"
            raise HTTPException(500, rec.last_error)
        return {"session_id": session.session_id}

    @app.post("/api/stop")
    async def stop():
        if rec.session is None:
            raise HTTPException(409, "Not recording")
        rec.link.auto_reconnect = False
        try:
            await rec.link.stop_streams()
        except Exception as e:
            log.warning("stop_streams: %s", e)
        return rec.stop_session()

    @app.post("/api/marker")
    async def marker(body: MarkerBody):
        if rec.session is None:
            raise HTTPException(409, "Not recording")
        rec.session.add_marker(body.note.strip())
        return {"ok": True}

    @app.get("/api/sessions")
    async def sessions():
        return list_sessions(rec.output_dir)

    @app.get("/api/sessions/{session_id}/ecg")
    async def ecg(session_id: str, start_s: float = 0.0, seconds: float = 10.0):
        d = session_dir(session_id)
        if not (d / "ecg.csv").exists():
            raise HTTPException(404, "Unknown session")
        return {"samples": read_ecg_window(d, start_s, min(seconds, 60.0))}

    @app.post("/api/sessions/{session_id}/reveal")
    async def reveal(session_id: str):
        d = session_dir(session_id)
        if platform.system() != "Darwin":
            raise HTTPException(501, "Reveal is only available on macOS")
        subprocess.Popen(["open", "-R", str(d)])
        return {"ok": True}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        d = session_dir(session_id)
        if rec.session is not None and rec.session.session_id == session_id:
            raise HTTPException(409, "Stop the recording first")
        try:
            shutil.rmtree(d)
        except OSError as e:
            raise HTTPException(500, f"Could not remove the folder: {e}")
        log.info("session deleted: %s", d)
        return {"ok": True}

    @app.get("/api/settings")
    async def get_settings():
        return {"output_dir": str(rec.output_dir)}

    @app.post("/api/settings")
    async def set_settings(body: SettingsBody):
        if rec.session:
            raise HTTPException(409, "Stop the recording first")
        try:
            rec.set_output_dir(body.output_dir)
        except OSError as e:
            raise HTTPException(400, f"Cannot use {body.output_dir}: {e}")
        return {"output_dir": str(rec.output_dir)}

    return app
