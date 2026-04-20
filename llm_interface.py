"""
LLM Interface — IBM Granite via Ollama
========================================
All communication with the local Granite model goes through this module.

It provides three high-level entry points:
  - analyze_telemetry()   — periodic health summary from a data window
  - analyze_anomaly()     — immediate alert explanation for a threshold breach
  - analyze_dtc()         — full diagnostic + repair guide for a fault code

Each method builds a structured prompt, calls Ollama, and streams the
response back via a callback (or returns it as a string).

Granite system prompt strategy
--------------------------------
Granite 3.x performs best with a clear system role + structured user prompt.
We use a single persistent system prompt that establishes the assistant as
an expert automotive diagnostic AI, and vary only the user prompt per call.
"""

import json
import logging
import time
from typing import Callable, Generator, Optional

import requests

import config
from vehicle_profile import get_llm_system_prompt_extension

logger = logging.getLogger(__name__)

# ── System Prompt ─────────────────────────────────────────────────────────────
# Injected once per conversation. Establishes tone, domain, and output format.
# The vehicle profile extension appends make/model/engine context + known weak points.

SYSTEM_PROMPT = """You are an expert automotive diagnostic AI assistant embedded in a real-time OBD-II scanner. Your role is to interpret live engine telemetry and fault codes from a vehicle's ECU.

You have deep expertise in:
- OBD-II PIDs and what their values indicate about engine health
- DTC (Diagnostic Trouble Code) interpretation across all OBD-II compliant vehicles
- Mechanical and electrical fault diagnosis
- Repair procedures, torque specs, and part identification
- Cost estimation for DIY vs professional repair

Your response style:
- Be concise and direct — the driver may be watching this in real time
- Use plain language, but include technical terms where they add precision
- When describing faults: state severity (informational / warning / critical), explain the root cause, and give actionable next steps
- For repair guides: provide step-by-step instructions, list required parts with common part numbers where possible, estimate difficulty (beginner/intermediate/advanced), and flag any safety precautions
- Never speculate beyond what the data supports — if more diagnosis is needed, say so clearly
""" + get_llm_system_prompt_extension()


# ── Prompt Templates ──────────────────────────────────────────────────────────

def _build_telemetry_prompt(telemetry_text: str, stats: dict) -> str:
    stats_lines = "\n".join(
        f"  {pid}: min={s['min']}, max={s['max']}, avg={s['avg']}"
        for pid, s in stats.items()
        if pid in ("RPM", "COOLANT_TEMP", "ENGINE_LOAD", "SPEED", "MAF",
                   "SHORT_FUEL_TRIM_1", "LONG_FUEL_TRIM_1")
    )
    return f"""Analyze the following OBD-II telemetry window and provide a brief engine health summary.

{telemetry_text}

Rolling stats (full buffer):
{stats_lines or '  (not yet available)'}

Respond with:
1. Overall engine status (one of: ✅ Normal | ⚠️ Attention needed | 🚨 Action required)
2. Key observations (2–4 bullet points on notable readings)
3. Any recommended actions (if none, say "No action needed")

Keep the summary under 150 words."""


def _build_anomaly_prompt(
    pid_name: str,
    value: float,
    unit: str,
    severity: str,
    snapshot: str,
    threshold_warn: Optional[float],
    threshold_critical: Optional[float],
) -> str:
    return f"""🚨 ANOMALY DETECTED

Parameter:  {pid_name}
Value:      {value} {unit}
Severity:   {severity.upper()}
Warn threshold:     {threshold_warn} {unit}
Critical threshold: {threshold_critical} {unit}

Current vehicle snapshot:
{snapshot}

Please:
1. Explain what this reading means and why it's concerning
2. List the most likely root causes (ordered by probability)
3. Describe the immediate risk to the vehicle if driving continues
4. State what the driver should do RIGHT NOW
5. Outline the diagnostic/repair steps

Be direct — this is a live alert."""


def _build_dtc_prompt(dtc_code: str, snapshot: str) -> str:
    return f"""A new Diagnostic Trouble Code has been detected:

DTC Code: {dtc_code}

Current vehicle telemetry at time of detection:
{snapshot}

Provide a complete diagnostic report:

## Code Definition
What this code means in plain language.

## Symptoms
What the driver may notice (if anything).

## Root Causes
List the most common causes ranked by likelihood, including:
- Sensor failures
- Wiring/connector issues
- Mechanical failures
- Software/calibration issues

## Diagnostic Steps
Step-by-step procedure to pinpoint the exact cause. Include:
- Which components to inspect first
- Any live data PIDs to watch
- Expected values vs what was observed

## Repair Procedure
Step-by-step repair instructions for the most likely cause. Include:
- Required tools
- Parts needed (with common OEM and aftermarket part numbers if known)
- Estimated repair difficulty: Beginner / Intermediate / Advanced
- Estimated time
- Any safety precautions (disconnect battery, depressurize fuel system, etc.)

## Cost Estimate
- DIY parts cost range
- Professional labor estimate (hours + typical shop rate)

## Can I drive?
Clear statement on whether it's safe to continue driving and for how far/long."""


def _build_brake_health_prompt(trend_text: str) -> str:
    return f"""Analyze the following braking performance data collected from the vehicle's OBD-II speed sensor and brake light switch. This data is being used as a proxy to assess brake system health — specifically to detect whether brake fade or a need for brake fluid bleeding may be developing over time.

{trend_text}

Provide a brake system health assessment covering:

## Braking Performance Summary
Interpret the deceleration figures. For context:
- Panic stop (dry): ~1.0 g
- Normal hard braking: 0.6–0.8 g
- Moderate braking: 0.3–0.5 g
- Light braking: 0.1–0.25 g
State whether the recorded values are consistent with a healthy brake system.

## Trend Analysis
Comment on whether the trend across the recorded windows (recent / medium / long) suggests degradation. A 10%+ drop in peak deceleration over comparable entry speeds is a meaningful indicator.

## Bleeding Diagnosis
Based on the trend data, assess the likelihood that brake fluid bleeding is indicated:
- **Bleeding likely needed** — if there is a consistent, sustained decline in peak g that cannot be attributed to entry speed variation
- **Monitor closely** — if there is a moderate decline or inconsistent pattern
- **System appears healthy** — if deceleration is stable or within normal variation

Note: This is a behavioral estimate only. Low g values alone may also indicate worn pads, rotor glazing, or driver technique. Brake fluid condition cannot be confirmed without a moisture/boiling-point test.

## Recommended Next Steps
Give 2–4 concrete actions the driver should take, ordered by urgency. If bleeding is indicated, note that bleeding the 2012 Volvo C30 T5 requires a pressure bleeder or a helper, starting at the caliper furthest from the master cylinder (right rear), and that Volvo specifies DOT 4+ fluid (e.g. Pentosin Super DOT4 or ATE SL.6).

Keep the response under 250 words."""


def _build_natural_language_prompt(question: str, snapshot: str, stats: dict) -> str:
    stats_lines = "\n".join(
        f"  {pid}: min={s['min']}, max={s['max']}, avg={s['avg']}"
        for pid, s in stats.items()
    )
    return f"""The driver is asking a question about their vehicle. Answer it using the live telemetry data below.

Driver's question: "{question}"

Current telemetry snapshot:
{snapshot}

Session statistics:
{stats_lines or '  (not yet available)'}

Answer conversationally and accurately. If the data doesn't contain enough information to answer definitively, say so and suggest what additional checks would help."""


# ── Ollama Client ─────────────────────────────────────────────────────────────

class GraniteClient:
    """
    Thin wrapper around the Ollama REST API for IBM Granite.

    Supports both streaming and non-streaming responses.
    Falls back gracefully if Ollama is not running.
    """

    def __init__(self):
        self.base_url = config.OLLAMA_BASE_URL
        self.model = config.LLM_MODEL
        self._available = None  # Cached availability check

    def is_available(self) -> bool:
        """Check if Ollama is running and the model is loaded."""
        if self._available is not None:
            return self._available
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", [])]
                self._available = any(self.model in m for m in models)
                if not self._available:
                    logger.warning(
                        f"Model '{self.model}' not found in Ollama. "
                        f"Run: ollama pull {self.model}"
                    )
                return self._available
        except requests.exceptions.ConnectionError:
            logger.error(
                "Cannot reach Ollama. Is it running? Start with: ollama serve"
            )
        self._available = False
        return False

    def chat(
        self,
        user_prompt: str,
        stream_callback: Optional[Callable[[str], None]] = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> str:
        """
        Send a prompt to Granite and return the full response.

        If stream_callback is provided, tokens are passed to it as they arrive
        (and the full text is still returned at the end).
        """
        self._available = None  # Reset cache on each call to re-check

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {
                "temperature": config.LLM_TEMPERATURE,
                "num_predict": config.LLM_MAX_TOKENS,
            },
            "stream": stream_callback is not None,
        }

        try:
            if stream_callback is not None:
                return self._stream_chat(payload, stream_callback)
            else:
                return self._blocking_chat(payload)
        except requests.exceptions.ConnectionError:
            msg = "⚠️  LLM offline — Ollama is not reachable."
            logger.error(msg)
            return msg
        except Exception as e:
            msg = f"⚠️  LLM error: {e}"
            logger.error(msg)
            return msg

    def _blocking_chat(self, payload: dict) -> str:
        payload["stream"] = False
        r = requests.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=120,
        )
        r.raise_for_status()
        return r.json()["message"]["content"]

    def _stream_chat(
        self, payload: dict, callback: Callable[[str], None]
    ) -> str:
        payload["stream"] = True
        full_text = []
        with requests.post(
            f"{self.base_url}/api/chat",
            json=payload,
            stream=True,
            timeout=120,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line:
                    chunk = json.loads(line)
                    token = chunk.get("message", {}).get("content", "")
                    if token:
                        full_text.append(token)
                        callback(token)
                    if chunk.get("done"):
                        break
        return "".join(full_text)


# ── High-Level Interface ──────────────────────────────────────────────────────

class LLMInterface:
    """
    High-level interface used by the main orchestration loop.

    Wraps GraniteClient with domain-specific methods and rate limiting
    to prevent hammering the LLM with every sensor tick.
    """

    def __init__(self):
        self._client = GraniteClient()
        self._last_summary_time: float = 0
        self._last_anomaly_times: dict[str, float] = {}
        self._anomaly_cooldown = 30.0  # Seconds between repeated anomaly alerts

    def analyze_telemetry(
        self,
        telemetry_text: str,
        stats: dict,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Periodic health summary. Called every LLM_PERIODIC_SUMMARY_INTERVAL seconds."""
        now = time.time()
        if now - self._last_summary_time < config.LLM_PERIODIC_SUMMARY_INTERVAL:
            return ""  # Not time yet
        self._last_summary_time = now

        prompt = _build_telemetry_prompt(telemetry_text, stats)
        logger.debug("Sending telemetry to LLM for analysis...")
        return self._client.chat(prompt, stream_callback=stream_callback)

    def analyze_anomaly(
        self,
        pid_name: str,
        value: float,
        unit: str,
        severity: str,
        snapshot: str,
        threshold_warn: Optional[float] = None,
        threshold_critical: Optional[float] = None,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Triggered immediately when a threshold is breached. Has per-PID cooldown."""
        now = time.time()
        last = self._last_anomaly_times.get(pid_name, 0)
        if now - last < self._anomaly_cooldown:
            return ""  # Suppress repeated alerts for same PID

        self._last_anomaly_times[pid_name] = now
        prompt = _build_anomaly_prompt(
            pid_name, value, unit, severity, snapshot,
            threshold_warn, threshold_critical
        )
        logger.warning(f"Anomaly: {pid_name}={value}{unit} ({severity}). Querying LLM...")
        return self._client.chat(prompt, stream_callback=stream_callback)

    def analyze_dtc(
        self,
        dtc_code: str,
        snapshot: str,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Full diagnostic + repair guide for a newly detected DTC."""
        prompt = _build_dtc_prompt(dtc_code, snapshot)
        logger.warning(f"New DTC detected: {dtc_code}. Querying LLM for diagnosis...")
        return self._client.chat(prompt, stream_callback=stream_callback)

    def answer_question(
        self,
        question: str,
        snapshot: str,
        stats: dict,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Answer a natural language question about the vehicle using live data."""
        prompt = _build_natural_language_prompt(question, snapshot, stats)
        return self._client.chat(prompt, stream_callback=stream_callback)

    def analyze_brake_health(
        self,
        trend_text: str,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        On-demand brake system health analysis.

        Call this with the output of BrakeMonitor.format_for_llm() after the
        user explicitly requests a brake check, or automatically once the
        monitor has accumulated enough events (e.g. >= 10) and a declining
        trend has been flagged.

        Not rate-limited here — callers are responsible for deciding when to
        trigger this (it is relatively expensive at 250 tokens).
        """
        if not trend_text or "No braking events" in trend_text:
            return (
                "Not enough braking event data has been collected yet to perform "
                "a brake health analysis. Drive the vehicle normally for a while "
                "so the system can record sufficient braking events, then ask again."
            )
        prompt = _build_brake_health_prompt(trend_text)
        logger.info("Requesting brake health analysis from LLM...")
        return self._client.chat(prompt, stream_callback=stream_callback)

    @property
    def is_available(self) -> bool:
        return self._client.is_available()
