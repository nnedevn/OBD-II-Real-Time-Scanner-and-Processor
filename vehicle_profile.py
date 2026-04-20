"""
Vehicle Profile — 2012 Volvo C30 T5
======================================
Engine:       B5254T2 — 2.5L inline-5 turbocharged, ~227 hp / 236 lb-ft
Transmission: M66 6-speed manual
Platform:     Volvo P1 (shared with S40 II, V50, C70 II)
ECU:          Bosch ME9.0 (engine), AW55 variant not present (manual)
Fuel system:  Multi-point port injection (not direct)
Forced induction: Single Mitsubishi TD04HL-15T turbocharger
Valve train:  CVVT (Continuously Variable Valve Timing) on intake cam only
O2 sensors:   Single bank (inline-5), upstream + downstream

Boost note
----------
The standard OBD-II INTAKE_PRESSURE PID (Mode 01, PID 0x0B) returns absolute
manifold pressure in kPa.  To derive gauge boost pressure:
    boost_psi  = (INTAKE_PRESSURE_kPa - BAROMETRIC_PRESSURE_kPa) * 0.145038
    boost_bar  = (INTAKE_PRESSURE_kPa - BAROMETRIC_PRESSURE_kPa) / 100.0
Stock peak boost on B5254T2 is approx. 7–9 PSI (148–162 kPa absolute at sea level).

Mode 22 PIDs
------------
Volvo stores many engine-specific parameters behind SAE Mode 22 (enhanced data
by DID, manufacturer-defined).  The PID addresses below were sourced from the
Volvospeed / SwedeSpeed community and ME9.0 ECU documentation.
THEY SHOULD BE VERIFIED against Volvo VIDA or an OBDII scanner with Volvo
extended coverage (e.g. Autel MaxiSys, Launch X431) before relying on values.
The framework is in place; addresses marked ??? need confirmation.
"""

from __future__ import annotations

import logging
from typing import Optional
import obd
from obd import OBDCommand
from obd.protocols import ECU
from obd.utils import bytes_to_int

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# STANDARD OBD-II MODE 01 PIDS
# Filtered for the B5254T2/P1 platform:
#   - Single bank only (no _B2 variants)
#   - No hybrid PIDs
#   - Relevant to a turbocharged port-injected manual-transmission vehicle
# ══════════════════════════════════════════════════════════════════════════════

STANDARD_PIDS = [
    # ── Engine basics ─────────────────────────────────────────────────────────
    "RPM",                      # Engine RPM                         (rpm)
    "ENGINE_LOAD",              # Calculated engine load             (%)
    "ABSOLUTE_LOAD",            # Absolute engine load (includes boost-driven load) (%)
    "RUN_TIME",                 # Engine run time since start        (s)

    # ── Temperatures ──────────────────────────────────────────────────────────
    "COOLANT_TEMP",             # Engine coolant temperature         (°C)
    "INTAKE_TEMP",              # Intake air temperature (pre-turbo) (°C)
    "AMBIENT_AIR_TEMP",         # Ambient air temperature            (°C)
    "OIL_TEMP",                 # Engine oil temperature (Mode 01 PID 0x5C)
                                # NOTE: C30 T5 *may* not expose this via Mode 01;
                                # see Mode 22 custom PID below as fallback.

    # ── Pressure & air ────────────────────────────────────────────────────────
    "INTAKE_PRESSURE",          # Absolute manifold air pressure     (kPa)
                                # *** KEY PID for turbo boost monitoring ***
                                # boost_psi = (INTAKE_PRESSURE - BAROMETRIC) * 0.145038
    "BAROMETRIC_PRESSURE",      # Ambient barometric pressure        (kPa)
    "MAF",                      # Mass air flow rate                 (g/s)
    "MAX_MAF",                  # Max MAF value recorded             (g/s)

    # ── Fuel system ───────────────────────────────────────────────────────────
    "FUEL_STATUS",              # Fuel system loop status (open/closed loop)
    "SHORT_FUEL_TRIM_1",        # Short-term fuel trim bank 1        (%)
    "LONG_FUEL_TRIM_1",         # Long-term fuel trim bank 1         (%)
    "FUEL_PRESSURE",            # Fuel rail pressure (gauge)         (kPa)
    "FUEL_RAIL_PRESSURE_VAC",   # Fuel rail pressure vacuum ref.     (kPa)
                                # B5254T2 uses port injection, this is
                                # the relevant fuel pressure PID
    "FUEL_LEVEL",               # Fuel tank level                    (%)
    "FUEL_INJECT_TIMING",       # Fuel injection timing              (°)
    "FUEL_RATE",                # Engine fuel rate                   (L/h)
    "COMMANDED_EQUIV_RATIO",    # Lambda / equivalence ratio         (ratio)
    "ETHANOL_PERCENT",          # Ethanol content in fuel            (%)

    # ── Throttle & driver demand ───────────────────────────────────────────────
    "THROTTLE_POS",             # Throttle position (sensor 1)       (%)
    "RELATIVE_THROTTLE_POS",    # Relative throttle position         (%)
    "ABSOLUTE_THROTTLE_POS_B",  # Throttle position sensor 2        (%)
    "COMMANDED_THROTTLE_ACTUATOR",  # Commanded throttle             (%)
    "ACCELERATOR_POS_D",        # Accelerator pedal position D       (%)
    "ACCELERATOR_POS_E",        # Accelerator pedal position E       (%)

    # ── Ignition ──────────────────────────────────────────────────────────────
    "TIMING_ADVANCE",           # Spark ignition timing advance      (°)

    # ── O2 sensors (single bank — inline-5) ──────────────────────────────────
    "O2_B1S1",                  # Upstream O2 sensor voltage         (V)
    "O2_B1S2",                  # Downstream O2 sensor voltage       (V)
    "O2_S1_WR_VOLTAGE",         # Wideband O2 bank 1 sensor 1       (V)
    "O2_S1_WR_CURRENT",         # Wideband O2 bank 1 sensor 1       (mA)

    # ── Catalyst & emissions ──────────────────────────────────────────────────
    "CATALYST_TEMP_B1S1",       # Catalyst temperature upstream       (°C)
    "CATALYST_TEMP_B1S2",       # Catalyst temperature downstream     (°C)
    "EVAPORATIVE_PURGE",        # EVAP purge solenoid duty cycle     (%)
    "EVAP_VAPOR_PRESSURE",      # EVAP system vapor pressure         (Pa)
    "AIR_STATUS",               # Secondary air injection status

    # ── System & diagnostics ──────────────────────────────────────────────────
    "CONTROL_MODULE_VOLTAGE",   # ECU supply voltage                 (V)
    "DISTANCE_W_MIL",           # Distance travelled with MIL on     (km)
    "DISTANCE_SINCE_DTC_CLEAR", # Distance since codes cleared       (km)
    "WARMUPS_SINCE_DTC_CLEAR",  # Warm-up cycles since codes cleared
    "TIME_WITH_MIL",            # Time with MIL on                   (min)
    "TIME_SINCE_DTC_CLEARED",   # Time since DTCs cleared            (min)
    "OBD_COMPLIANCE",           # OBD standard compliance
]


# ══════════════════════════════════════════════════════════════════════════════
# DERIVED / COMPUTED CHANNELS
# Not real OBD PIDs — calculated in post-processing from raw PID values.
# The main loop adds these to each sample after polling.
# ══════════════════════════════════════════════════════════════════════════════

def compute_derived(values: dict, units: dict) -> tuple[dict, dict]:
    """
    Calculate derived channels from raw PID values.
    Returns (extra_values, extra_units) to merge into the sample.
    """
    derived_values = {}
    derived_units = {}

    # ── Boost pressure ────────────────────────────────────────────────────────
    # Derive gauge boost from MAP and barometric pressure
    map_kpa = values.get("INTAKE_PRESSURE")
    baro_kpa = values.get("BAROMETRIC_PRESSURE")
    if map_kpa is not None and baro_kpa is not None:
        boost_kpa = float(map_kpa) - float(baro_kpa)
        derived_values["BOOST_KPA"] = round(boost_kpa, 1)
        derived_values["BOOST_PSI"] = round(boost_kpa * 0.145038, 2)
        derived_units["BOOST_KPA"] = "kPa"
        derived_units["BOOST_PSI"] = "psi"

    # ── Air-fuel ratio (from equivalence ratio) ────────────────────────────────
    lambda_val = values.get("COMMANDED_EQUIV_RATIO")
    if lambda_val is not None and float(lambda_val) > 0:
        # Stoichiometric AFR for gasoline = 14.7
        afr = 14.7 / float(lambda_val)
        derived_values["AFR"] = round(afr, 2)
        derived_units["AFR"] = ":1"

    # ── Manifold vacuum (at idle / off-boost) ─────────────────────────────────
    if map_kpa is not None and baro_kpa is not None:
        vacuum_inhg = (float(baro_kpa) - float(map_kpa)) * 0.2953
        if vacuum_inhg > 0:
            derived_values["VACUUM_INHG"] = round(vacuum_inhg, 1)
            derived_units["VACUUM_INHG"] = "inHg"

    return derived_values, derived_units


# ══════════════════════════════════════════════════════════════════════════════
# VOLVO MODE 22 CUSTOM PID COMMANDS
# Mode 22: Enhanced data by DID (SAE J1979 Service $22)
# These are Volvo/Bosch ME9.0 proprietary parameters for the B5254T2.
#
# PID address format: 22 XX YY  (Mode 22, Data ID high byte, low byte)
# Response format:    62 XX YY [data bytes]
#
# ⚠️  VERIFICATION STATUS marked on each command:
#   [VERIFIED]   — Confirmed on B5254T2 / ME9.0 by community sources
#   [LIKELY]     — Plausible based on Bosch ME9 documentation, unconfirmed for C30
#   [FRAMEWORK]  — Address uncertain; query will fail silently if wrong
#
# To verify an address:
#   1. Connect a Volvo VIDA/DiCE or Autel MaxiSys with Volvo coverage
#   2. Navigate to: Engine > Live Data > [parameter]
#   3. Note the DID hex address shown in the raw CAN data
#   4. Update the command bytes here
# ══════════════════════════════════════════════════════════════════════════════

def _decode_uint8_percent(messages) -> float:
    """Decode a single byte as 0–100% (value / 2.55)."""
    d = messages[0].data
    if len(d) < 4:
        return None
    return round(d[3] / 2.55, 1)

def _decode_int8_degrees(messages) -> float:
    """Decode a signed byte as degrees (e.g. ignition trim, -128 to +127°)."""
    d = messages[0].data
    if len(d) < 4:
        return None
    val = d[3] if d[3] < 128 else d[3] - 256
    return round(float(val) * 0.75, 2)  # 0.75°/bit scaling common on ME9

def _decode_uint16_kpa(messages) -> float:
    """Decode a 16-bit unsigned integer as pressure in kPa (scaling: /100)."""
    d = messages[0].data
    if len(d) < 5:
        return None
    raw = (d[3] << 8) | d[4]
    return round(raw / 100.0, 1)

def _decode_uint16_temp(messages) -> float:
    """Decode a 16-bit value as temperature in °C (scaling: /10 − 40)."""
    d = messages[0].data
    if len(d) < 5:
        return None
    raw = (d[3] << 8) | d[4]
    return round(raw / 10.0 - 40.0, 1)

def _decode_uint8_temp(messages) -> float:
    """Decode a single byte as temperature in °C (raw − 40 scaling)."""
    d = messages[0].data
    if len(d) < 4:
        return None
    return float(d[3]) - 40

def _decode_uint16_rpm(messages) -> float:
    """Decode a 16-bit value as RPM (scaling: /4)."""
    d = messages[0].data
    if len(d) < 5:
        return None
    raw = (d[3] << 8) | d[4]
    return round(raw / 4.0, 0)

def _decode_uint8_duty(messages) -> float:
    """Decode a byte as duty cycle percent (raw / 2.55)."""
    d = messages[0].data
    if len(d) < 4:
        return None
    return round(d[3] / 2.55, 1)

def _decode_int16_cvvt(messages) -> float:
    """Decode CVVT angle as signed 16-bit, scaling 1/10 degree."""
    d = messages[0].data
    if len(d) < 5:
        return None
    raw = (d[3] << 8) | d[4]
    if raw > 32767:
        raw -= 65536
    return round(raw / 10.0, 1)


# Custom OBD commands — each is a Mode 22 DID query
# Arguments: (name, description, command_bytes, num_frames, decoder, ecu, fast)

VOLVO_MODE22_COMMANDS: list[OBDCommand] = [

    # ── Turbo / Boost ─────────────────────────────────────────────────────────

    OBDCommand(
        "VOLVO_BOOST_ACTUAL",
        "Turbo boost pressure actual (Mode 22) [LIKELY — verify address]",
        b"2201F0",          # DID 0x01F0 — actual MAP from ME9 internal table
        5,
        _decode_uint16_kpa,
        ECU.ENGINE,
        True,
    ),

    OBDCommand(
        "VOLVO_BOOST_TARGET",
        "Turbo boost pressure target/requested (Mode 22) [LIKELY]",
        b"2201F1",          # DID 0x01F1 — ECU-commanded boost target
        5,
        _decode_uint16_kpa,
        ECU.ENGINE,
        True,
    ),

    OBDCommand(
        "VOLVO_WASTEGATE_DUTY",
        "Wastegate solenoid duty cycle % (Mode 22) [LIKELY]",
        b"2201F2",          # DID 0x01F2 — 0% = WG fully open (no boost), 100% = closed
        4,
        _decode_uint8_duty,
        ECU.ENGINE,
        True,
    ),

    # ── Temperatures ──────────────────────────────────────────────────────────

    OBDCommand(
        "VOLVO_OIL_TEMP",
        "Engine oil temperature °C (Mode 22) [LIKELY — C30 T5 may not expose via Mode 01]",
        b"220197",          # DID 0x0197 — oil temp sensor
        5,
        _decode_uint8_temp,
        ECU.ENGINE,
        True,
    ),

    OBDCommand(
        "VOLVO_CHARGE_AIR_TEMP",
        "Charge air temperature post-intercooler °C (Mode 22) [LIKELY]",
        b"220182",          # DID 0x0182 — post-intercooler IAT
        5,
        _decode_uint8_temp,
        ECU.ENGINE,
        True,
    ),

    # ── Ignition & knock ──────────────────────────────────────────────────────

    OBDCommand(
        "VOLVO_KNOCK_RETARD",
        "Knock-induced ignition retard degrees (Mode 22) [LIKELY]",
        b"2201A0",          # DID 0x01A0 — total knock correction
        4,
        _decode_int8_degrees,
        ECU.ENGINE,
        True,
    ),

    OBDCommand(
        "VOLVO_KNOCK_COUNT",
        "Knock event count (Mode 22) [FRAMEWORK — address uncertain]",
        b"2201A1",
        4,
        lambda m: bytes_to_int(m[0].data[3:4]) if len(m[0].data) >= 4 else None,
        ECU.ENGINE,
        True,
    ),

    # ── CVVT (Continuously Variable Valve Timing — intake cam only) ───────────

    OBDCommand(
        "VOLVO_CVVT_ACTUAL",
        "CVVT intake cam actual position degrees (Mode 22) [LIKELY]",
        b"220170",          # DID 0x0170 — intake CVVT measured angle
        5,
        _decode_int16_cvvt,
        ECU.ENGINE,
        True,
    ),

    OBDCommand(
        "VOLVO_CVVT_TARGET",
        "CVVT intake cam target position degrees (Mode 22) [LIKELY]",
        b"220171",          # DID 0x0171 — ECU-requested CVVT angle
        5,
        _decode_int16_cvvt,
        ECU.ENGINE,
        True,
    ),

    # ── Fuel injectors ────────────────────────────────────────────────────────

    OBDCommand(
        "VOLVO_INJ_PULSEWIDTH",
        "Fuel injector pulse width ms (Mode 22) [FRAMEWORK]",
        b"220155",          # DID 0x0155 — injector open time
        5,
        lambda m: round(((m[0].data[3] << 8) | m[0].data[4]) / 1000.0, 3)
                  if len(m[0].data) >= 5 else None,
        ECU.ENGINE,
        True,
    ),

    # ── Throttle body ─────────────────────────────────────────────────────────

    OBDCommand(
        "VOLVO_THROTTLE_TARGET",
        "Electronic throttle target position % (Mode 22) [LIKELY]",
        b"220160",          # DID 0x0160 — E-throttle requested angle
        4,
        _decode_uint8_percent,
        ECU.ENGINE,
        True,
    ),

    # ── Transmission (M66 manual) ─────────────────────────────────────────────
    # Manual gearbox has no TCU; only clutch switch and neutral switch
    # are available, via instrument cluster or body control module.
    # These are low-priority / informational only.

    OBDCommand(
        "VOLVO_CLUTCH_SWITCH",
        "Clutch pedal switch state 0=released 1=depressed (Mode 22) [FRAMEWORK]",
        b"2203A0",          # DID 0x03A0 — clutch switch (instrument cluster)
        4,
        lambda m: int(m[0].data[3]) if len(m[0].data) >= 4 else None,
        ECU.ALL,
        True,
    ),

    # ── Brake pedal switch ────────────────────────────────────────────────────
    # The brake light switch feeds into the ME9.0 ECU as a digital input used
    # to cancel cruise control and inform fuel cut-off logic. It is exposed
    # via Mode 22 as a single bit inside a switch-state DID.
    #
    # DID 0x184A is the Bosch ME9 "pedal switch states" byte on P1 Volvos.
    # Bit 0 of the response byte = brake pedal switch (1=pressed, 0=released).
    #
    # [LIKELY — verify with VIDA or a Bosch-capable scan tool]
    # If this returns null, brake_monitor.py automatically falls back to
    # inferring braking events from vehicle speed deceleration.

    OBDCommand(
        "VOLVO_BRAKE_SWITCH",
        "Brake pedal switch 0=released 1=depressed (Mode 22) [LIKELY]",
        b"22184A",          # DID 0x184A — ME9 pedal switch states byte
        4,
        lambda m: float(m[0].data[3] & 0x01) if len(m[0].data) >= 4 else None,
        ECU.ENGINE,
        True,
    ),

]

# Lookup dict for easy access by name
VOLVO_COMMANDS_BY_NAME: dict[str, OBDCommand] = {
    cmd.name: cmd for cmd in VOLVO_MODE22_COMMANDS
}


# ══════════════════════════════════════════════════════════════════════════════
# ANOMALY THRESHOLDS — B5254T2 specific
# Based on Volvo factory service manual specs + community knowledge.
# ══════════════════════════════════════════════════════════════════════════════

THRESHOLDS_C30_T5 = {
    # Standard OBD PIDs
    "COOLANT_TEMP": {
        "warn": 107,        # Thermostat fully open at ~90°C; >107°C is above normal range
        "critical": 116,    # Approaching coolant boiling point; stop driving
    },
    "RPM": {
        "warn": 6000,       # Approaching B5254T2 redline (~6500)
        "critical": 6600,
    },
    "ENGINE_LOAD": {
        "warn": 90,
        "critical": 98,
    },
    "INTAKE_PRESSURE": {
        # Absolute MAP. At sea level, 101 kPa = atmospheric (no boost).
        # Stock T5 peak boost ~148–160 kPa absolute (~7–8.5 PSI).
        "warn": 168,        # ~9.7 PSI — slight over-boost
        "critical": 183,    # ~11.8 PSI — significant over-boost, risk of knock/damage
    },
    "INTAKE_TEMP": {
        "warn": 55,         # Intercooler heat soak territory
        "critical": 70,     # Severely elevated, power and knock risk
    },
    "SHORT_FUEL_TRIM_1": {
        "warn": 15,         # ±15% short trim — sensor or vacuum issue
        "critical": 25,
    },
    "LONG_FUEL_TRIM_1": {
        "warn": 12,         # ±12% long trim — persistent fueling problem
        "critical": 20,
    },
    "CONTROL_MODULE_VOLTAGE": {
        "warn": None,
        "critical": 11.5,   # Below 11.5V suggests charging system fault
        "low_warn": 11.8,   # Voltage low (check alternator / battery)
        "mode": "low",      # Alert when BELOW threshold, not above
    },
    "CATALYST_TEMP_B1S1": {
        "warn": 850,        # High cat temp — possible misfire or rich condition
        "critical": 950,
    },

    # Derived channels
    "BOOST_PSI": {
        "warn": 9.5,        # ~0.65 bar — slight over-boost
        "critical": 12.0,   # ~0.83 bar — significant over-boost
    },
    "AFR": {
        "warn": None,
        "critical": None,   # AFR monitored via fuel trims; no direct threshold here
    },

    # Volvo Mode 22 channels (active if Mode 22 queries succeed)
    "VOLVO_OIL_TEMP": {
        "warn": 130,        # Oil starts degrading rapidly above 135°C
        "critical": 145,
    },
    "VOLVO_CHARGE_AIR_TEMP": {
        "warn": 55,         # Charge air temp post-intercooler should stay <50°C ideally
        "critical": 70,
    },
    "VOLVO_KNOCK_RETARD": {
        "warn": -3.0,       # 3° retard — moderate knock activity
        "critical": -6.0,   # 6° retard — heavy knock, stop and investigate
        "mode": "low",      # Alert when value is BELOW (more negative) threshold
    },
    "VOLVO_BOOST_ACTUAL": {
        "warn": 168,        # Same as INTAKE_PRESSURE (kPa) but from Mode 22 if available
        "critical": 183,
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# VEHICLE METADATA
# Used by the LLM system prompt and for display purposes.
# ══════════════════════════════════════════════════════════════════════════════

VEHICLE_INFO = {
    "year": 2012,
    "make": "Volvo",
    "model": "C30",
    "trim": "T5",
    "engine": "B5254T2 2.5L I5 Turbo",
    "hp": 227,
    "torque_lbft": 236,
    "transmission": "M66 6-speed manual",
    "platform": "P1",
    "ecu": "Bosch ME9.0",
    "fuel_system": "Multi-point port injection",
    "boost_system": "Mitsubishi TD04HL-15T turbocharger",
    "valve_timing": "CVVT intake cam only",
    "redline_rpm": 6500,
    "thermostat_opens_c": 88,
    "stock_boost_psi_peak": 8.5,
    "known_weak_points": [
        "PCV oil trap / flame trap — common failure causing high oil consumption and smoke",
        "Throttle body carbon buildup on E-throttle — causes rough idle, stalling",
        "Timing belt — service interval 105k miles / 10 years (CRITICAL: interference engine)",
        "Turbo inlet pipe — prone to cracking, causes boost leaks and MAF errors",
        "Upper engine mount — often worn on high-mileage cars, causes vibration",
        "Coolant expansion tank cap — weak, causes pressurisation loss over time",
        "MAP sensor (TMAP) on intake manifold — fails silently, causes lean surging",
        "CVVT solenoid — can fail causing P0011/P0014 and rough idle",
    ],
}


# ══════════════════════════════════════════════════════════════════════════════
# LLM SYSTEM PROMPT EXTENSION
# Appended to the base system prompt in llm_interface.py when this profile loads.
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT_EXTENSION = f"""
Vehicle context:
  {VEHICLE_INFO['year']} {VEHICLE_INFO['make']} {VEHICLE_INFO['model']} {VEHICLE_INFO['trim']}
  Engine: {VEHICLE_INFO['engine']} | {VEHICLE_INFO['hp']} hp / {VEHICLE_INFO['torque_lbft']} lb-ft
  Transmission: {VEHICLE_INFO['transmission']}
  Platform: {VEHICLE_INFO['platform']} | ECU: {VEHICLE_INFO['ecu']}
  Stock peak boost: ~{VEHICLE_INFO['stock_boost_psi_peak']} PSI
  Engine redline: {VEHICLE_INFO['redline_rpm']} RPM
  ⚠ INTERFERENCE ENGINE — timing belt failure = catastrophic engine damage

Known weak points to consider when diagnosing:
""" + "\n".join(f"  • {w}" for w in VEHICLE_INFO["known_weak_points"])


def get_llm_system_prompt_extension() -> str:
    return SYSTEM_PROMPT_EXTENSION
