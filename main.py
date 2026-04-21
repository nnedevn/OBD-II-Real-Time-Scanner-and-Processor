"""
OBD II + IBM Granite LLM Scanner — Main Entry Point
=====================================================
Wires together all components:

  OBDReader  ──→  DataBuffer  ──→  AnomalyDetector  ──→  LLMInterface
                                                    ──→  DTC handler
                                                    ──→  Periodic summary
                                                    ──→  Terminal dashboard
                         └──→  DashboardServer (WebSocket → browser UI)

Usage:
  python main.py                       # Full mode: terminal + browser dashboard
  python main.py --no-web              # Terminal only (no browser dashboard)
  python main.py --no-terminal         # Browser dashboard only (no Rich TUI)
  python main.py --port 8080           # Change dashboard port (default 8080)
  python main.py --ask "Is boost normal?"  # Single Q&A then exit

Browser dashboard:
  http://localhost:8080
  Kiosk mode: chromium-browser --kiosk --app=http://localhost:8080

Requirements:
  pip install obd requests rich fastapi uvicorn
  ollama pull granite4:3b

Bluetooth setup (Linux):
  sudo rfkill unblock bluetooth
  bluetoothctl
    scan on
    pair <ADAPTER_MAC>
    trust <ADAPTER_MAC>
    connect <ADAPTER_MAC>
  sudo rfcomm bind /dev/rfcomm0 <ADAPTER_MAC>
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config
from obd_reader import OBDReader, OBDSample
from data_buffer import DataBuffer
from anomaly_detector import AnomalyDetector, AnomalyEvent
from llm_interface import LLMInterface
from dashboard_server import DashboardServer
from brake_monitor import BrakeMonitor, BrakeEvent, BrakeTrend
from database import Database
from vehicle_profile import VEHICLE_INFO

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if config.VERBOSE else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            Path(config.LOG_DIR) / f"scanner_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
            encoding="utf-8",
        ) if Path(config.LOG_DIR).exists() else logging.NullHandler(),
    ],
)
logger = logging.getLogger(__name__)
console = Console()


# ── CSV Logger ────────────────────────────────────────────────────────────────

class CSVLogger:
    """Appends every OBD sample to a timestamped CSV file."""

    def __init__(self):
        self._file = None
        self._writer = None
        self._headers_written = False

    def open(self):
        if not config.LOG_RAW_DATA:
            return
        log_dir = Path(config.LOG_DIR)
        log_dir.mkdir(exist_ok=True)
        fname = log_dir / f"obd_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self._file = open(fname, "w", newline="", encoding="utf-8")
        logger.info(f"Logging raw data to {fname}")

    def log(self, sample: OBDSample):
        if not config.LOG_RAW_DATA or self._file is None:
            return
        row = {"timestamp": sample.timestamp, "datetime": sample.datetime_str}
        row.update(sample.values)
        if sample.dtcs:
            row["dtcs"] = "|".join(sample.dtcs)
        if not self._headers_written:
            self._writer = csv.DictWriter(
                self._file, fieldnames=list(row.keys()), extrasaction="ignore"
            )
            self._writer.writeheader()
            self._headers_written = True
        self._writer.writerow(row)
        self._file.flush()

    def close(self):
        if self._file:
            self._file.close()


# ── LLM Analysis Logger ───────────────────────────────────────────────────────

class LLMLogger:
    """
    Appends every completed Granite analysis to a per-session JSONL file
    so the full history of LLM output is available offline for later
    review, fine-tuning data extraction, or cross-session trend analysis.

    One record per line. Schema:
      {
        "timestamp":   <float unix>,
        "datetime":    "<ISO-8601>",
        "type":        "anomaly" | "dtc" | "brake" | "summary",
        "model":       "<config.LLM_MODEL>",
        "trigger":     { ...type-specific metadata... },
        "context":     "<telemetry/trend snapshot sent to LLM>",
        "output":      "<full Granite response text, possibly empty>",
        "output_empty": <bool>    # true when the LLM returned nothing
      }

    JSONL is used over CSV so multi-line LLM output doesn't need escaping,
    and the file stays parseable even if a run crashes mid-write.
    """

    def __init__(self):
        self._file = None
        self._path: Optional[Path] = None

    def open(self):
        log_dir = Path(config.LOG_DIR)
        log_dir.mkdir(exist_ok=True)
        self._path = log_dir / f"llm_analyses_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        self._file = open(self._path, "a", encoding="utf-8")
        logger.info(f"Logging LLM analyses to {self._path}")

    def log(
        self,
        analysis_type: str,
        trigger: dict[str, Any],
        context: str,
        output: Optional[str],
    ):
        if self._file is None:
            return
        now = time.time()
        record = {
            "timestamp": now,
            "datetime": datetime.fromtimestamp(now).isoformat(),
            "type": analysis_type,
            "model": config.LLM_MODEL,
            "trigger": trigger,
            "context": context,
            "output": output or "",
            "output_empty": not bool(output),
        }
        try:
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._file.flush()
        except Exception as e:
            logger.error(f"LLMLogger failed to write {analysis_type} record: {e}")

    def close(self):
        if self._file:
            self._file.close()
            self._file = None


# ── Dashboard ─────────────────────────────────────────────────────────────────

def build_dashboard(
    buffer: DataBuffer,
    detector: AnomalyDetector,
    llm_output: str,
    status: str,
) -> Layout:
    """Build a Rich terminal dashboard layout."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="telemetry", ratio=2),
        Layout(name="llm", ratio=3),
    )

    # Header
    layout["header"].update(Panel(
        Text("🚗  OBD II + IBM Granite LLM Scanner", style="bold cyan", justify="center"),
        style="cyan",
    ))

    # Live telemetry table
    latest = buffer.latest()
    tbl = Table(show_header=True, header_style="bold magenta", expand=True)
    tbl.add_column("PID", style="dim")
    tbl.add_column("Value", justify="right")
    tbl.add_column("Unit")
    tbl.add_column("Status")

    if latest:
        breach_info = detector.breach_summary()
        for pid, val in latest.values.items():
            unit = latest.units.get(pid, "")
            val_str = f"{val:.1f}" if isinstance(val, float) else str(val)
            b = breach_info.get(pid)
            if b and b["severity"] == "critical":
                status_icon = "🚨"
                style = "red"
            elif b and b["severity"] == "warn":
                status_icon = "⚠️ "
                style = "yellow"
            else:
                status_icon = "✅"
                style = "green"
            tbl.add_row(pid, val_str, unit, status_icon, style=style)

        if latest.dtcs:
            tbl.add_row("DTCs", ", ".join(latest.dtcs), "", "🚨", style="red bold")

    layout["telemetry"].update(Panel(tbl, title="Live Telemetry", border_style="blue"))

    # LLM output panel
    llm_text = Text(llm_output or "Waiting for LLM analysis...", overflow="fold")
    layout["llm"].update(Panel(llm_text, title="Granite Analysis", border_style="green"))

    # Footer
    buf_len = len(buffer)
    layout["footer"].update(Panel(
        f"Buffer: {buf_len}/{buffer.capacity} samples | "
        f"Model: {config.LLM_MODEL} | "
        f"Status: {status} | "
        f"Time: {datetime.now().strftime('%H:%M:%S')}",
        style="dim",
    ))

    return layout


# ── Main Application ──────────────────────────────────────────────────────────

class OBDScanner:
    def __init__(self, show_terminal: bool = True, show_web: bool = True, web_port: int = 8080):
        self.reader = OBDReader()
        self.buffer = DataBuffer()
        self.detector = AnomalyDetector()
        self.llm = LLMInterface()
        self.csv_logger = CSVLogger()
        self.llm_logger = LLMLogger()
        self.db = Database()
        self.show_terminal = show_terminal
        self.show_web = show_web
        self.web_port = web_port

        # Brake efficiency monitor — wired to LLM for decline alerts
        # Instantiated before DashboardServer so it can be passed in
        self.brake_monitor = BrakeMonitor()
        self.brake_monitor.on_event(self._on_brake_event)
        self.brake_monitor.on_alert(self._on_brake_alert)

        # Browser dashboard server (optional)
        self.dash_server = (
            DashboardServer(self.buffer, self.llm, self.brake_monitor)
            if show_web else None
        )

        self._llm_output = ""
        self._status = "Initializing..."
        self._running = False

    def _on_sample(self, sample: OBDSample):
        """Called on every OBD poll tick."""
        # 1. Push to buffer
        self.buffer.push(sample)

        # 2. Log to CSV
        self.csv_logger.log(sample)

        # 3. Push to browser dashboard
        if self.dash_server:
            self.dash_server.push_sample(sample)

        # 4. Check thresholds
        anomalies = self.detector.check(sample)
        for event in anomalies:
            asyncio.get_event_loop().call_soon_threadsafe(
                self._schedule_anomaly_alert, event, sample
            )

        # 5. Feed brake monitor state machine
        self.brake_monitor.push_sample(sample)

        # 6. Periodic LLM summary (handled in async loop, not here)

    def _on_dtc(self, new_dtcs: list[str], cleared_dtcs: list[str]):
        """Called when DTC list changes."""
        for code in new_dtcs:
            self.db.log_dtc(code, state="new")
            asyncio.get_event_loop().call_soon_threadsafe(
                self._schedule_dtc_analysis, code
            )
        for code in cleared_dtcs:
            self.db.log_dtc(code, state="cleared")
            console.print(f"[green]✅ DTC cleared: {code}[/green]")
        # Push DTC update to browser
        if self.dash_server:
            self.dash_server.push_dtc_change(new_dtcs, cleared_dtcs)

    def _schedule_anomaly_alert(self, event: AnomalyEvent, sample: OBDSample):
        asyncio.ensure_future(self._run_anomaly_alert(event, sample))

    def _schedule_dtc_analysis(self, code: str):
        asyncio.ensure_future(self._run_dtc_analysis(code))

    async def _run_anomaly_alert(self, event: AnomalyEvent, sample: OBDSample):
        snapshot = self.buffer.format_latest_for_llm()
        output_parts = []
        alert_title = f"{event.pid_name}={event.value}{event.unit} [{event.severity.upper()}]"

        # Index the event itself (separate from the LLM analysis row below).
        self.db.log_anomaly(
            pid_name=event.pid_name,
            value=event.value,
            unit=event.unit,
            severity=event.severity,
            threshold_warn=event.threshold_warn,
            threshold_critical=event.threshold_critical,
            consecutive_count=event.consecutive_count,
        )

        # Notify browser dashboard immediately
        if self.dash_server:
            self.dash_server.push_llm_event("anomaly", f"🚨 {alert_title}")

        def on_token(token: str):
            output_parts.append(token)
            self._llm_output = f"🚨 ANOMALY: {alert_title}\n\n" + "".join(output_parts)
            if self.dash_server:
                self.dash_server.push_llm_token(token)

        if not self.show_terminal:
            console.print(f"\n[bold red]🚨 ANOMALY: {alert_title}[/bold red]")

        result = self.llm.analyze_anomaly(
            pid_name=event.pid_name,
            value=event.value,
            unit=event.unit,
            severity=event.severity,
            snapshot=snapshot,
            threshold_warn=event.threshold_warn,
            threshold_critical=event.threshold_critical,
            stream_callback=on_token if config.LLM_STREAM else None,
        )
        anomaly_trigger = {
            "pid_name": event.pid_name,
            "value": event.value,
            "unit": event.unit,
            "severity": event.severity,
            "threshold_warn": event.threshold_warn,
            "threshold_critical": event.threshold_critical,
            "consecutive_count": event.consecutive_count,
        }
        self.llm_logger.log(
            analysis_type="anomaly",
            trigger=anomaly_trigger,
            context=snapshot,
            output=result,
        )
        self.db.log_llm_analysis(
            analysis_type="anomaly",
            trigger=anomaly_trigger,
            context=snapshot,
            output=result,
        )
        if self.dash_server:
            self.dash_server.push_llm_done(result or "")
        if result and not self.show_terminal:
            console.print(Panel(result, title="Granite Analysis", border_style="red"))

    async def _run_dtc_analysis(self, code: str):
        snapshot = self.buffer.format_latest_for_llm()
        output_parts = []

        if self.dash_server:
            self.dash_server.push_llm_event("dtc", f"🔍 DTC {code} — Analysing…")

        def on_token(token: str):
            output_parts.append(token)
            self._llm_output = f"🔍 DTC ANALYSIS: {code}\n\n" + "".join(output_parts)
            if self.dash_server:
                self.dash_server.push_llm_token(token)

        if not self.show_terminal:
            console.print(f"\n[bold red]🔍 Analysing DTC: {code}[/bold red]")

        result = self.llm.analyze_dtc(
            dtc_code=code,
            snapshot=snapshot,
            stream_callback=on_token if config.LLM_STREAM else None,
        )
        dtc_trigger = {"dtc_code": code}
        self.llm_logger.log(
            analysis_type="dtc",
            trigger=dtc_trigger,
            context=snapshot,
            output=result,
        )
        self.db.log_llm_analysis(
            analysis_type="dtc",
            trigger=dtc_trigger,
            context=snapshot,
            output=result,
        )
        if self.dash_server:
            self.dash_server.push_llm_done(result or "")
        if result and not self.show_terminal:
            console.print(Panel(result, title=f"DTC {code} Analysis", border_style="red"))

    # ── Brake monitor callbacks ────────────────────────────────────────────────

    def _on_brake_event(self, event: BrakeEvent):
        """
        Called (from the OBD poll thread) each time a qualifying braking event
        completes. Pushes a lightweight stats update to the browser dashboard.
        This is intentionally *not* LLM-triggered on every event — only on trend
        alerts (see _on_brake_alert).
        """
        logger.debug(f"Brake event logged: {event.summary()}")
        self.db.log_brake_event(
            timestamp=event.timestamp,
            datetime_str=event.datetime_str,
            entry_speed_kmh=event.entry_speed_kmh,
            exit_speed_kmh=event.exit_speed_kmh,
            duration_s=event.duration_s,
            peak_decel_g=event.peak_decel_g,
            avg_decel_g=event.avg_decel_g,
            estimated_distance_m=event.estimated_distance_m,
            switch_confirmed=event.switch_confirmed,
        )
        if self.dash_server:
            stats = self.brake_monitor.dashboard_stats()
            asyncio.get_event_loop().call_soon_threadsafe(
                self.dash_server.push_brake_stats, stats
            )

    def _on_brake_alert(self, alert_text: str, recent: BrakeTrend, baseline: BrakeTrend):
        """
        Called when the brake monitor detects a sustained efficiency decline
        (recent window >= EFFICIENCY_DROP_THRESHOLD below baseline).
        Triggers a full LLM brake health analysis.
        """
        console.print(f"\n[bold yellow]⚠️  BRAKE EFFICIENCY ALERT[/bold yellow]")
        console.print(f"[yellow]{alert_text}[/yellow]")
        asyncio.get_event_loop().call_soon_threadsafe(
            self._schedule_brake_analysis
        )

    def _schedule_brake_analysis(self):
        asyncio.ensure_future(self._run_brake_analysis())

    async def _run_brake_analysis(self):
        """Run LLM brake health analysis and stream result to dashboard."""
        trend_text = self.brake_monitor.format_for_llm()
        output_parts = []

        if self.dash_server:
            self.dash_server.push_llm_event("brake", "🛑 Brake Health Analysis")

        def on_token(token: str):
            output_parts.append(token)
            self._llm_output = "🛑 BRAKE HEALTH ANALYSIS\n\n" + "".join(output_parts)
            if self.dash_server:
                self.dash_server.push_llm_token(token)

        if not self.show_terminal:
            console.print("\n[bold yellow]🛑 Running brake health analysis...[/bold yellow]")

        result = self.llm.analyze_brake_health(
            trend_text=trend_text,
            stream_callback=on_token if config.LLM_STREAM else None,
        )
        brake_stats = self.brake_monitor.dashboard_stats()
        brake_trigger = {
            "total_events": brake_stats.get("total_events"),
            "recent_avg_g": brake_stats.get("recent_avg_g"),
            "medium_avg_g": brake_stats.get("medium_avg_g"),
            "declining": brake_stats.get("declining"),
            "decline_pct": brake_stats.get("decline_pct"),
            "switch_confirmed": brake_stats.get("switch_confirmed"),
        }
        self.llm_logger.log(
            analysis_type="brake",
            trigger=brake_trigger,
            context=trend_text,
            output=result,
        )
        self.db.log_llm_analysis(
            analysis_type="brake",
            trigger=brake_trigger,
            context=trend_text,
            output=result,
        )
        if self.dash_server:
            self.dash_server.push_llm_done(result or "")
        if result and not self.show_terminal:
            console.print(Panel(result, title="🛑 Brake Health Analysis", border_style="yellow"))

    async def _periodic_summary_loop(self):
        """Sends telemetry to LLM on a fixed interval for health summaries."""
        if config.LLM_PERIODIC_SUMMARY_INTERVAL <= 0:
            return
        await asyncio.sleep(config.LLM_PERIODIC_SUMMARY_INTERVAL)  # Initial delay
        while self._running:
            output_parts = []
            if self.dash_server:
                self.dash_server.push_llm_event("summary", "📊 Periodic Health Summary")

            def on_token(token: str):
                output_parts.append(token)
                self._llm_output = "📊 HEALTH SUMMARY\n\n" + "".join(output_parts)
                if self.dash_server:
                    self.dash_server.push_llm_token(token)

            telemetry = self.buffer.format_for_llm()
            stats = self.buffer.stats_summary()
            result = self.llm.analyze_telemetry(
                telemetry_text=telemetry,
                stats=stats,
                stream_callback=on_token if config.LLM_STREAM else None,
            )
            summary_trigger = {
                "interval_s": config.LLM_PERIODIC_SUMMARY_INTERVAL,
                "buffer_samples": len(self.buffer),
                "stats": stats,
            }
            self.llm_logger.log(
                analysis_type="summary",
                trigger=summary_trigger,
                context=telemetry,
                output=result,
            )
            self.db.log_llm_analysis(
                analysis_type="summary",
                trigger=summary_trigger,
                context=telemetry,
                output=result,
            )
            if self.dash_server:
                self.dash_server.push_llm_done(result or "")
            if result and not self.show_terminal:
                console.print(Panel(result, title="📊 Periodic Health Summary", border_style="green"))

            await asyncio.sleep(config.LLM_PERIODIC_SUMMARY_INTERVAL)

    async def run(self):
        """Main async run loop."""
        # Setup
        os.makedirs(config.LOG_DIR, exist_ok=True)
        self.csv_logger.open()
        self.llm_logger.open()
        self.db.open()
        vehicle_str = (
            f"{VEHICLE_INFO['year']} {VEHICLE_INFO['make']} {VEHICLE_INFO['model']} "
            f"{VEHICLE_INFO['trim']} ({VEHICLE_INFO['engine']})"
        )
        self.db.start_session(
            hardware_profile=getattr(config, "HARDWARE_PROFILE", None),
            llm_model=config.LLM_MODEL,
            vehicle=vehicle_str,
            pids_monitored=list(config.MONITORED_PIDS),
        )

        # Start browser dashboard server
        if self.dash_server:
            await self.dash_server.start(port=self.web_port)
            console.print(f"[cyan]🌐 Dashboard: http://localhost:{self.web_port}[/cyan]")
            console.print(
                f"[dim]   Kiosk: chromium-browser --kiosk --app=http://localhost:{self.web_port}[/dim]"
            )

        # Check LLM availability
        if not self.llm.is_available:
            console.print(
                f"[yellow]⚠️  Ollama model '{config.LLM_MODEL}' not available. "
                f"Run: ollama pull {config.LLM_MODEL}[/yellow]"
            )

        # Connect OBD
        self._status = "Connecting to OBD adapter..."
        console.print(f"[cyan]{self._status}[/cyan]")

        if not self.reader.connect():
            console.print("[red]❌ Could not connect to OBD adapter.[/red]")
            console.print("Troubleshooting:")
            console.print("  1. Ensure the adapter is plugged into the OBD-II port (under dash)")
            console.print("  2. Turn ignition to ON position (engine doesn't need to run)")
            console.print("  3. Pair the Bluetooth adapter: bluetoothctl → pair <MAC>")
            console.print("  4. Bind to serial: sudo rfcomm bind /dev/rfcomm0 <MAC>")
            console.print("  5. Update OBD_PORT in config.py")
            return

        self._status = f"Connected | Protocol: {self.reader._connection.protocol_name()}"
        console.print(f"[green]✅ {self._status}[/green]")

        # Tell brake monitor whether the brake switch Mode 22 PID responded
        switch_available = any(
            cmd.name == "VOLVO_BRAKE_SWITCH"
            for cmd in self.reader._supported_mode22
        )
        self.brake_monitor.notify_switch_available(switch_available)

        # Register callbacks
        self.reader.subscribe(self._on_sample)
        self.reader.subscribe_dtc(self._on_dtc)

        # Start streaming
        await self.reader.start_stream()
        self._running = True

        # Start periodic summary task
        summary_task = asyncio.create_task(self._periodic_summary_loop())

        try:
            if self.show_terminal and config.SHOW_LIVE_DASHBOARD:
                await self._run_with_dashboard()
            else:
                await self._run_headless()
        except KeyboardInterrupt:
            console.print("\n[yellow]Shutting down...[/yellow]")
        finally:
            self._running = False
            summary_task.cancel()
            await self.reader.stop()
            if self.dash_server:
                await self.dash_server.stop()
            self.csv_logger.close()
            self.llm_logger.close()
            self.db.end_session()
            self.db.close()
            console.print("[green]Goodbye.[/green]")

    async def _run_with_dashboard(self):
        """Run with Rich live dashboard."""
        with Live(
            build_dashboard(self.buffer, self.detector, self._llm_output, self._status),
            refresh_per_second=2,
            console=console,
        ) as live:
            while self._running:
                live.update(
                    build_dashboard(
                        self.buffer, self.detector, self._llm_output, self._status
                    )
                )
                await asyncio.sleep(0.5)

    async def _run_headless(self):
        """Run without dashboard — just log output."""
        self._status = "Streaming (headless mode)"
        console.print("[green]Streaming OBD data. Press Ctrl+C to stop.[/green]")
        while self._running:
            latest = self.buffer.latest()
            if latest:
                console.print(f"[dim]{latest.summary_text()}[/dim]")
            await asyncio.sleep(config.POLL_INTERVAL_SECONDS)

    async def ask(self, question: str):
        """One-shot Q&A mode — answer a question about the vehicle and exit."""
        if not self.reader.connect():
            console.print("[red]Could not connect to OBD adapter.[/red]")
            return

        self.reader.subscribe(self.buffer.push)
        await self.reader.start_stream()

        # Collect a few seconds of data first
        console.print("[cyan]Collecting telemetry...[/cyan]")
        await asyncio.sleep(5)
        await self.reader.stop()

        snapshot = self.buffer.format_latest_for_llm()
        stats = self.buffer.stats_summary()

        console.print(f"\n[bold]Q: {question}[/bold]\n")
        console.print("[green]Granite:[/green] ", end="")

        # Route brake-related questions to the specialist brake health analysis
        brake_keywords = {"brake", "braking", "bleed", "bleeding", "pad", "rotor",
                          "caliper", "stopping", "deceleration"}
        q_words = set(question.lower().split())
        if q_words & brake_keywords:
            trend_text = self.brake_monitor.format_for_llm()
            self.llm.analyze_brake_health(
                trend_text=trend_text,
                stream_callback=lambda t: console.print(t, end=""),
            )
        else:
            self.llm.answer_question(
                question=question,
                snapshot=snapshot,
                stats=stats,
                stream_callback=lambda t: console.print(t, end=""),
            )
        console.print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OBD II + IBM Granite real-time vehicle diagnostics"
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="Disable browser dashboard (terminal only)",
    )
    parser.add_argument(
        "--no-terminal",
        action="store_true",
        help="Disable Rich terminal UI (browser dashboard only)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Browser dashboard port (default: 8080)",
    )
    parser.add_argument(
        "--ask",
        metavar="QUESTION",
        help='Ask a single question about the vehicle and exit. '
             'Example: --ask "Is my engine running hot?"',
    )
    args = parser.parse_args()

    scanner = OBDScanner(
        show_terminal=not args.no_terminal,
        show_web=not args.no_web,
        web_port=args.port,
    )

    if args.ask:
        asyncio.run(scanner.ask(args.ask))
    else:
        asyncio.run(scanner.run())


if __name__ == "__main__":
    main()
