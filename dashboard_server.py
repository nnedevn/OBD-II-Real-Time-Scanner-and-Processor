"""
Dashboard WebSocket Server
===========================
FastAPI server that:
  - Serves dashboard.html on GET /
  - Pushes live OBD samples to all connected browsers via WebSocket /ws/data
  - Streams LLM tokens to all connected browsers via WebSocket /ws/llm
  - Accepts natural language questions from the browser via /ws/llm (bidirectional)

Integration:
    server = DashboardServer(buffer, llm_interface)
    server.push_sample(sample)       # called by OBD reader callback
    server.push_llm_token(token)     # called by LLM streaming callback
    server.push_llm_event(event)     # called for anomaly/DTC alerts
    await server.start()             # starts uvicorn in background
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import config
from obd_reader import OBDSample

logger = logging.getLogger(__name__)

DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"


# ── Connection manager ─────────────────────────────────────────────────────────

class ConnectionManager:
    """Tracks all active WebSocket connections and broadcasts to them."""

    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)
        logger.info(f"Dashboard client connected ({len(self._connections)} total)")

    def disconnect(self, ws: WebSocket):
        self._connections.remove(ws)
        logger.info(f"Dashboard client disconnected ({len(self._connections)} remaining)")

    async def broadcast(self, message: dict):
        """Send a JSON message to all connected clients."""
        if not self._connections:
            return
        text = json.dumps(message)
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._connections.remove(ws)

    @property
    def count(self) -> int:
        return len(self._connections)


# ── Dashboard Server ───────────────────────────────────────────────────────────

class DashboardServer:
    """
    Owns the FastAPI app and exposes push methods that the main loop calls.
    Also handles incoming questions from the browser and routes them to the LLM.
    """

    def __init__(self, buffer=None, llm_interface=None, brake_monitor=None):
        self._buffer = buffer
        self._llm = llm_interface
        self._brake_monitor = brake_monitor  # Optional BrakeMonitor for health queries

        self._data_manager = ConnectionManager()
        self._llm_manager = ConnectionManager()

        self._app = FastAPI(title="OBD II Scanner Dashboard")
        self._server: Optional[uvicorn.Server] = None

        self._setup_routes()

    def _setup_routes(self):
        app = self._app

        @app.get("/", response_class=HTMLResponse)
        async def serve_dashboard():
            if not DASHBOARD_HTML.exists():
                return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)
            return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))

        @app.get("/api/config")
        async def get_config():
            """Expose gauge config to the frontend (thresholds, vehicle info)."""
            from vehicle_profile import VEHICLE_INFO, THRESHOLDS_C30_T5
            return {
                "vehicle": VEHICLE_INFO,
                "thresholds": {
                    k: {kk: vv for kk, vv in v.items() if kk in ("warn", "critical")}
                    for k, v in THRESHOLDS_C30_T5.items()
                },
                "poll_interval": config.POLL_INTERVAL_SECONDS,
            }

        @app.websocket("/ws/data")
        async def data_socket(ws: WebSocket):
            """Push OBD samples to browser. Stays open until client disconnects."""
            await self._data_manager.connect(ws)

            # Send latest buffered data immediately on connect so the UI isn't blank
            if self._buffer:
                snapshot = self._buffer.latest()
                if snapshot:
                    await ws.send_text(json.dumps(self._sample_to_msg(snapshot)))

            try:
                # Keep alive — client can send pings
                while True:
                    await ws.receive_text()
            except WebSocketDisconnect:
                self._data_manager.disconnect(ws)

        @app.websocket("/ws/llm")
        async def llm_socket(ws: WebSocket):
            """
            Bidirectional LLM channel.
            Server pushes: tokens, events (anomaly/DTC alerts), status messages.
            Client sends:  natural language questions {"type":"question","text":"..."}
            """
            await self._llm_manager.connect(ws)
            try:
                while True:
                    raw = await ws.receive_text()
                    msg = json.loads(raw)
                    if msg.get("type") == "question":
                        asyncio.ensure_future(
                            self._handle_question(msg.get("text", ""), ws)
                        )
            except WebSocketDisconnect:
                self._llm_manager.disconnect(ws)

    # ── Push helpers called by main loop ──────────────────────────────────────

    def push_sample(self, sample: OBDSample):
        """Called synchronously from OBD reader callback — schedules async broadcast."""
        if self._data_manager.count == 0:
            return
        asyncio.get_event_loop().call_soon_threadsafe(
            asyncio.ensure_future,
            self._data_manager.broadcast(self._sample_to_msg(sample)),
        )

    def push_llm_token(self, token: str):
        """Push a streaming token to all LLM panel clients."""
        if self._llm_manager.count == 0:
            return
        asyncio.get_event_loop().call_soon_threadsafe(
            asyncio.ensure_future,
            self._llm_manager.broadcast({"type": "token", "text": token}),
        )

    def push_llm_event(self, event_type: str, title: str, body: str = ""):
        """Push an anomaly/DTC event header to the LLM panel."""
        asyncio.get_event_loop().call_soon_threadsafe(
            asyncio.ensure_future,
            self._llm_manager.broadcast({
                "type": "event",
                "event_type": event_type,   # "anomaly" | "dtc" | "summary"
                "title": title,
                "body": body,
            }),
        )

    def push_llm_done(self, full_text: str):
        """Signal to the browser that LLM streaming is complete."""
        asyncio.get_event_loop().call_soon_threadsafe(
            asyncio.ensure_future,
            self._llm_manager.broadcast({"type": "done", "full_text": full_text}),
        )

    def push_dtc_change(self, new_dtcs: list[str], cleared_dtcs: list[str]):
        """Broadcast DTC changes to the data panel."""
        asyncio.get_event_loop().call_soon_threadsafe(
            asyncio.ensure_future,
            self._data_manager.broadcast({
                "type": "dtc_update",
                "new": new_dtcs,
                "cleared": cleared_dtcs,
            }),
        )

    def push_brake_stats(self, stats: dict):
        """
        Broadcast brake efficiency stats to the data panel.
        Called after each qualifying braking event.
        stats is the dict returned by BrakeMonitor.dashboard_stats().
        """
        asyncio.get_event_loop().call_soon_threadsafe(
            asyncio.ensure_future,
            self._data_manager.broadcast({
                "type": "brake_stats",
                **stats,
            }),
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _sample_to_msg(sample: OBDSample) -> dict:
        return {
            "type": "sample",
            "ts": sample.timestamp,
            "values": sample.values,
            "units": sample.units,
            "dtcs": sample.dtcs,
        }

    async def _handle_question(self, question: str, ws: WebSocket):
        """Route a browser question to the LLM and stream the response back."""
        if not self._llm or not self._buffer:
            await ws.send_text(json.dumps({
                "type": "event",
                "event_type": "error",
                "title": "LLM not available",
            }))
            return

        await ws.send_text(json.dumps({
            "type": "event",
            "event_type": "question",
            "title": f"Q: {question}",
        }))

        tokens = []

        def on_token(t: str):
            tokens.append(t)
            asyncio.get_event_loop().call_soon_threadsafe(
                asyncio.ensure_future,
                ws.send_text(json.dumps({"type": "token", "text": t})),
            )

        # Route brake-related questions to the specialist brake health analysis
        brake_keywords = {"brake", "braking", "bleed", "bleeding", "pad",
                          "rotor", "caliper", "stopping", "deceleration"}
        q_words = set(question.lower().split())
        if q_words & brake_keywords and self._brake_monitor:
            trend_text = self._brake_monitor.format_for_llm()
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._llm.analyze_brake_health(trend_text, on_token),
            )
        else:
            snapshot = self._buffer.format_latest_for_llm()
            stats = self._buffer.stats_summary()
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._llm.answer_question(question, snapshot, stats, on_token),
            )
        await ws.send_text(json.dumps({"type": "done", "full_text": "".join(tokens)}))

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self, host: str = "0.0.0.0", port: int = 8080):
        """Start uvicorn in a background task."""
        cfg = uvicorn.Config(
            self._app,
            host=host,
            port=port,
            log_level="warning",
            loop="asyncio",
        )
        self._server = uvicorn.Server(cfg)
        asyncio.ensure_future(self._server.serve())
        logger.info(f"Dashboard available at http://localhost:{port}")
        logger.info(
            f"Kiosk mode: chromium-browser --kiosk http://localhost:{port}"
        )

    async def stop(self):
        if self._server:
            self._server.should_exit = True
