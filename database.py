"""
SQLite Historical Event Store
==============================
Persists structured events across sessions for trend analysis, predictive
maintenance, and future API / dashboard queries. Raw per-sample OBD
telemetry keeps living in the per-session CSV files — only events that
are interesting to query cross-session land here.

Design principles
-----------------
1. Dual-write, flat files are authoritative.
   The existing CSV (samples), JSONL (LLM analyses), and brake_events.json
   writers all continue to run exactly as before. The database is a
   secondary index. If the DB file gets corrupted or deleted, it can be
   rebuilt from the flat files without any data loss.

2. Failures must never crash the scanner.
   Every write method catches and logs its own exceptions. A missing DB
   or a schema error will degrade gracefully to "event not indexed" —
   never to a crash in the hot path.

3. Low write pressure.
   At ~1 Hz sample polling, actual events (anomalies, DTCs, brake events,
   LLM analyses) fire at minute-to-hour scale, not sample scale. So a
   single shared connection with autocommit + WAL is plenty.

Schema
------
sessions         — one row per scanner run
anomaly_events   — one row per threshold breach fired by AnomalyDetector
dtc_events       — one row per DTC appearance OR clearing
llm_analyses     — one row per completed Granite analysis (mirrors JSONL)
brake_events     — one row per qualifying braking event

Read helpers (for future dashboard / API consumption)
-----------------------------------------------------
recent_anomalies / anomalies_by_pid
recent_llm_analyses
recent_brake_events / brake_stats_since
session_summary
"""

import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
      id                INTEGER PRIMARY KEY AUTOINCREMENT,
      started_at        REAL    NOT NULL,
      started_iso       TEXT    NOT NULL,
      ended_at          REAL,
      ended_iso         TEXT,
      hardware_profile  TEXT,
      llm_model         TEXT,
      vehicle           TEXT,
      pids_monitored    TEXT,      -- JSON array
      notes             TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS anomaly_events (
      id                 INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id         INTEGER,
      timestamp          REAL NOT NULL,
      datetime           TEXT NOT NULL,
      pid_name           TEXT NOT NULL,
      value              REAL NOT NULL,
      unit               TEXT,
      severity           TEXT NOT NULL,         -- 'warn' | 'critical'
      threshold_warn     REAL,
      threshold_critical REAL,
      consecutive_count  INTEGER,
      FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dtc_events (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id  INTEGER,
      timestamp   REAL NOT NULL,
      datetime    TEXT NOT NULL,
      code        TEXT NOT NULL,
      state       TEXT NOT NULL,                -- 'new' | 'cleared'
      FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_analyses (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id    INTEGER,
      timestamp     REAL NOT NULL,
      datetime      TEXT NOT NULL,
      type          TEXT NOT NULL,              -- 'anomaly' | 'dtc' | 'brake' | 'summary'
      model         TEXT,
      trigger_json  TEXT,                       -- JSON blob, schema varies by type
      context       TEXT,
      output        TEXT,
      output_empty  INTEGER NOT NULL DEFAULT 0,
      FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brake_events (
      id                    INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id            INTEGER,
      timestamp             REAL NOT NULL,
      datetime              TEXT NOT NULL,
      entry_speed_kmh       REAL,
      exit_speed_kmh        REAL,
      duration_s            REAL,
      peak_decel_g          REAL,
      avg_decel_g           REAL,
      estimated_distance_m  REAL,
      switch_confirmed      INTEGER,            -- 0/1
      FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """,
    # Indexes keyed to the queries we actually expect:
    #   "show me all anomalies for PID X in the last 30 days"
    #   "show me occurrences of DTC Pxxxx over time"
    #   "most recent N LLM analyses of type T"
    #   "brake events since date"
    "CREATE INDEX IF NOT EXISTS idx_anomaly_pid_ts ON anomaly_events(pid_name, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_dtc_code_ts   ON dtc_events(code, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_llm_type_ts   ON llm_analyses(type, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_brake_ts      ON brake_events(timestamp)",
]


# ── Database ──────────────────────────────────────────────────────────────────

class Database:
    """
    SQLite-backed event index.

    Single shared connection with check_same_thread=False — SQLite's internal
    lock serialises writes. Safe for our workload where the OBD poll thread
    and the asyncio main loop both occasionally write events.

    WAL journaling keeps readers (e.g. the dashboard or a future HTTP API)
    from blocking the writer.
    """

    DEFAULT_FILENAME = "scanner.db"

    def __init__(self, path: Optional[Path] = None):
        self.path: Path = Path(path) if path else Path(config.LOG_DIR) / self.DEFAULT_FILENAME
        self._conn: Optional[sqlite3.Connection] = None
        self._session_id: Optional[int] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def open(self) -> None:
        """Create the DB file if needed, apply schema, and prepare the connection."""
        try:
            self.path.parent.mkdir(exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.path),
                check_same_thread=False,
                isolation_level=None,  # autocommit — each execute is its own tx
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            for stmt in SCHEMA_STATEMENTS:
                self._conn.execute(stmt)
            logger.info(f"SQLite event store ready at {self.path}")
        except Exception as e:
            logger.error(f"Failed to open SQLite DB at {self.path}: {e}")
            self._conn = None

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception as e:
                logger.warning(f"Error closing DB: {e}")
            self._conn = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    @property
    def session_id(self) -> Optional[int]:
        return self._session_id

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def start_session(
        self,
        hardware_profile: Optional[str] = None,
        llm_model: Optional[str] = None,
        vehicle: Optional[str] = None,
        pids_monitored: Optional[list[str]] = None,
    ) -> Optional[int]:
        """
        Record the start of a scanner run. Returns the new session id, or
        None if the DB is unavailable (caller should not error out — events
        will simply not be indexed this session).
        """
        now = time.time()
        cur = self._exec(
            """
            INSERT INTO sessions
              (started_at, started_iso, hardware_profile, llm_model, vehicle, pids_monitored)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                datetime.fromtimestamp(now).isoformat(),
                hardware_profile,
                llm_model,
                vehicle,
                json.dumps(pids_monitored or []),
            ),
        )
        if cur is None:
            return None
        self._session_id = cur.lastrowid
        logger.info(f"DB session #{self._session_id} started")
        return self._session_id

    def end_session(self) -> None:
        """Stamp the current session as ended. Idempotent."""
        if self._session_id is None:
            return
        now = time.time()
        self._exec(
            "UPDATE sessions SET ended_at=?, ended_iso=? WHERE id=?",
            (now, datetime.fromtimestamp(now).isoformat(), self._session_id),
        )

    # ── Event writers ─────────────────────────────────────────────────────────

    def log_anomaly(
        self,
        pid_name: str,
        value: float,
        unit: Optional[str],
        severity: str,
        threshold_warn: Optional[float],
        threshold_critical: Optional[float],
        consecutive_count: int,
    ) -> None:
        now = time.time()
        self._exec(
            """
            INSERT INTO anomaly_events
              (session_id, timestamp, datetime, pid_name, value, unit,
               severity, threshold_warn, threshold_critical, consecutive_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._session_id,
                now,
                datetime.fromtimestamp(now).isoformat(),
                pid_name,
                float(value),
                unit,
                severity,
                threshold_warn,
                threshold_critical,
                consecutive_count,
            ),
        )

    def log_dtc(self, code: str, state: str) -> None:
        """state: 'new' | 'cleared'"""
        now = time.time()
        self._exec(
            """
            INSERT INTO dtc_events (session_id, timestamp, datetime, code, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                self._session_id,
                now,
                datetime.fromtimestamp(now).isoformat(),
                code,
                state,
            ),
        )

    def log_llm_analysis(
        self,
        analysis_type: str,
        trigger: dict[str, Any],
        context: str,
        output: Optional[str],
    ) -> None:
        now = time.time()
        self._exec(
            """
            INSERT INTO llm_analyses
              (session_id, timestamp, datetime, type, model,
               trigger_json, context, output, output_empty)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._session_id,
                now,
                datetime.fromtimestamp(now).isoformat(),
                analysis_type,
                config.LLM_MODEL,
                json.dumps(trigger, default=str),
                context,
                output or "",
                int(not bool(output)),
            ),
        )

    def log_brake_event(
        self,
        timestamp: float,
        datetime_str: str,
        entry_speed_kmh: float,
        exit_speed_kmh: float,
        duration_s: float,
        peak_decel_g: float,
        avg_decel_g: float,
        estimated_distance_m: float,
        switch_confirmed: bool,
    ) -> None:
        self._exec(
            """
            INSERT INTO brake_events
              (session_id, timestamp, datetime, entry_speed_kmh, exit_speed_kmh,
               duration_s, peak_decel_g, avg_decel_g, estimated_distance_m,
               switch_confirmed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._session_id,
                timestamp,
                datetime_str,
                entry_speed_kmh,
                exit_speed_kmh,
                duration_s,
                peak_decel_g,
                avg_decel_g,
                estimated_distance_m,
                int(bool(switch_confirmed)),
            ),
        )

    # ── Read helpers (for dashboard / API) ────────────────────────────────────

    def recent_anomalies(self, limit: int = 100) -> list[dict]:
        return self._query(
            "SELECT * FROM anomaly_events ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )

    def anomalies_by_pid(
        self, pid: str, since: Optional[float] = None, limit: int = 1000
    ) -> list[dict]:
        if since is None:
            return self._query(
                "SELECT * FROM anomaly_events WHERE pid_name=? "
                "ORDER BY timestamp DESC LIMIT ?",
                (pid, limit),
            )
        return self._query(
            "SELECT * FROM anomaly_events WHERE pid_name=? AND timestamp>=? "
            "ORDER BY timestamp DESC LIMIT ?",
            (pid, since, limit),
        )

    def recent_dtcs(self, limit: int = 50) -> list[dict]:
        return self._query(
            "SELECT * FROM dtc_events ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )

    def recent_llm_analyses(
        self, analysis_type: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        if analysis_type:
            rows = self._query(
                "SELECT * FROM llm_analyses WHERE type=? ORDER BY timestamp DESC LIMIT ?",
                (analysis_type, limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM llm_analyses ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        # Parse the trigger JSON blob for callers
        for row in rows:
            raw = row.get("trigger_json")
            if raw:
                try:
                    row["trigger"] = json.loads(raw)
                except Exception:
                    row["trigger"] = None
        return rows

    def recent_brake_events(self, limit: int = 50) -> list[dict]:
        return self._query(
            "SELECT * FROM brake_events ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )

    def brake_stats_since(self, since: float) -> dict:
        """Aggregate brake metrics over a time window. Useful for trend cards."""
        rows = self._query(
            "SELECT peak_decel_g, avg_decel_g, entry_speed_kmh "
            "FROM brake_events WHERE timestamp>=?",
            (since,),
        )
        if not rows:
            return {"count": 0}
        peaks = [r["peak_decel_g"] for r in rows if r["peak_decel_g"] is not None]
        return {
            "count": len(rows),
            "avg_peak_g": round(sum(peaks) / len(peaks), 3) if peaks else None,
            "min_peak_g": round(min(peaks), 3) if peaks else None,
            "max_peak_g": round(max(peaks), 3) if peaks else None,
        }

    def session_summary(self, session_id: Optional[int] = None) -> dict:
        """Return the session row plus per-table event counts."""
        sid = session_id if session_id is not None else self._session_id
        if sid is None or self._conn is None:
            return {}
        session_rows = self._query("SELECT * FROM sessions WHERE id=?", (sid,))
        if not session_rows:
            return {}
        counts: dict[str, int] = {}
        for table in ("anomaly_events", "dtc_events", "llm_analyses", "brake_events"):
            try:
                cur = self._conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (sid,)
                )
                counts[table] = cur.fetchone()[0]
            except Exception as e:
                logger.error(f"Count query on {table} failed: {e}")
                counts[table] = 0
        return {"session": session_rows[0], "counts": counts}

    # ── Internals ─────────────────────────────────────────────────────────────

    def _exec(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Cursor]:
        if self._conn is None:
            return None
        try:
            return self._conn.execute(sql, params)
        except Exception as e:
            verb = sql.strip().split(None, 1)[0]
            logger.error(f"DB {verb} failed: {e}")
            return None

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        if self._conn is None:
            return []
        try:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"DB query failed: {e}")
            return []
