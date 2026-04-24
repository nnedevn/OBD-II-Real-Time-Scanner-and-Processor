"""
SAE J2012 DTC Dictionary — Static Fallback
==========================================
When the LLM is unavailable (Ollama down, network issue, circuit breaker open),
the diagnostic chain should still produce something useful for the driver.

This module ships a hand-curated dictionary of standardised OBD-II fault codes
so that even with zero LLM the scanner can resolve a code to its name and
plain-language meaning.

Scope
-----
- Full breadth of P0xxx / P2xxx codes commonly encountered in modern vehicles.
- A handful of Volvo P1xxx entries relevant to the target C30 T5 platform.
- Category fallback: an unknown code like P0287 still resolves to its
  SAE category (fuel/air metering, ignition, emissions, etc.) via the
  numeric prefix, so the driver gets *something* meaningful even for
  codes not explicitly listed.

This is intentionally not exhaustive — the LLM remains the right tool for
detailed diagnosis. The goal here is graceful degradation, not replacement.
"""

from typing import Optional, TypedDict


class DTCInfo(TypedDict):
    code: str
    name: str
    category: str
    hint: str
    source: str   # "j2012_known" | "category_fallback" | "unknown"


# ── SAE J2012 Category Map ────────────────────────────────────────────────────
# Based on the standardised ranges. Used as a fallback when we don't have
# a specific entry for a code — at minimum we can name the subsystem.

CATEGORY_BY_PREFIX: dict[str, str] = {
    # Powertrain (P) — the vast majority of codes the driver sees
    "P00": "Fuel and Air Metering / Auxiliary Emission Controls",
    "P01": "Fuel and Air Metering",
    "P02": "Fuel and Air Metering (Injector Circuit)",
    "P03": "Ignition System or Misfire",
    "P04": "Auxiliary Emission Controls (EGR / EVAP / Catalyst / O2 heaters)",
    "P05": "Vehicle Speed / Idle / Auxiliary Inputs",
    "P06": "Computer Output Circuit (ECU-internal)",
    "P07": "Transmission",
    "P08": "Transmission",
    "P09": "Transmission",
    "P0A": "Hybrid Propulsion",
    "P0B": "Hybrid Propulsion",
    "P10": "Manufacturer-specific — Fuel / Air Metering",
    "P11": "Manufacturer-specific — Fuel / Air Metering",
    "P12": "Manufacturer-specific — Injector Circuit",
    "P13": "Manufacturer-specific — Ignition / Misfire",
    "P14": "Manufacturer-specific — Auxiliary Emission",
    "P15": "Manufacturer-specific — VSS / Idle / Aux Inputs",
    "P16": "Manufacturer-specific — Computer Output",
    "P17": "Manufacturer-specific — Transmission",
    "P20": "Auxiliary Emission (NOx, particulate, DEF systems)",
    "P21": "Fuel and Air Metering (extended range)",
    "P22": "Fuel and Air Metering (extended range)",
    "P23": "Ignition / Glow plug",
    "P24": "Auxiliary Emission (DPF / SCR)",
    "P25": "Auxiliary Inputs (extended)",
    "P26": "Computer Output Circuit (extended)",
    "B":   "Body (airbag, climate, lighting, etc.)",
    "C":   "Chassis (ABS, stability, steering, etc.)",
    "U":   "Network / Communication (CAN bus, module comms)",
}


# ── Curated J2012 DTC Entries ─────────────────────────────────────────────────
# Conservative: only codes with well-established, unambiguous SAE definitions.
# Each entry: code → (name, hint).
# The hint is one sentence of actionable starting-point guidance.

_CODES: dict[str, tuple[str, str]] = {
    # P00xx — Fuel and air metering
    "P0010": ("Camshaft Position Actuator Circuit / Open (Bank 1)",
              "Check CVVT/VVT solenoid wiring, then the solenoid itself."),
    "P0011": ("Camshaft Position - Timing Over-Advanced (Bank 1)",
              "Usually CVVT solenoid stuck, oil pressure low, or timing belt out a tooth."),
    "P0014": ("Exhaust Cam Position - Timing Over-Advanced (Bank 1)",
              "Exhaust CVVT solenoid or oil control valve; check oil level and condition first."),
    "P0016": ("Crankshaft/Camshaft Position Correlation (Bank 1 Sensor A)",
              "Timing chain/belt stretched or jumped a tooth; check cam-crank sync urgently."),
    "P0030": ("HO2S Heater Control Circuit (Bank 1 Sensor 1)",
              "Upstream O2 sensor heater circuit; most often the sensor itself has failed."),
    "P0031": ("HO2S Heater Control Circuit Low (Bank 1 Sensor 1)",
              "Upstream O2 sensor heater draw low; replace sensor or check fuse."),
    "P0036": ("HO2S Heater Control Circuit (Bank 1 Sensor 2)",
              "Downstream O2 sensor heater circuit; usually the sensor."),

    # P01xx — Fuel/air metering (MAF, O2, temp sensors)
    "P0101": ("Mass Air Flow Circuit Range/Performance",
              "MAF reading implausible for engine load; clean or replace MAF, check intake boot for leaks."),
    "P0102": ("Mass Air Flow Circuit Low Input",
              "MAF signal too low; inspect wiring and connector, replace sensor if needed."),
    "P0103": ("Mass Air Flow Circuit High Input",
              "MAF signal too high; unmetered air downstream of sensor or a bad sensor."),
    "P0106": ("MAP/Barometric Pressure Circuit Range/Performance",
              "MAP sensor reading implausible; check vacuum hoses first, then the sensor."),
    "P0107": ("MAP/Barometric Pressure Circuit Low",
              "MAP sensor signal low; open wire or failed sensor."),
    "P0108": ("MAP/Barometric Pressure Circuit High",
              "MAP sensor signal high; lost vacuum reference or short-to-power."),
    "P0111": ("Intake Air Temperature Circuit Range/Performance",
              "IAT sensor reading doesn't match conditions; often part of a MAF assembly on Volvos."),
    "P0112": ("Intake Air Temperature Circuit Low",
              "IAT signal stuck low; wiring short to ground or sensor failure."),
    "P0113": ("Intake Air Temperature Circuit High",
              "IAT signal stuck high; open circuit or sensor failure."),
    "P0116": ("Coolant Temperature Circuit Range/Performance",
              "ECT sensor value implausible; often a stuck-open thermostat masquerading as a sensor fault."),
    "P0117": ("Coolant Temperature Circuit Low",
              "ECT reading stuck low; short to ground or sensor failure."),
    "P0118": ("Coolant Temperature Circuit High",
              "ECT reading stuck high; open circuit or sensor failure."),
    "P0121": ("Throttle/Pedal Position Sensor Range/Performance (A)",
              "TPS signal doesn't match engine demand; clean throttle body, check sensor."),
    "P0122": ("Throttle/Pedal Position Sensor Low (A)",
              "TPS signal low; wiring issue or failed sensor."),
    "P0123": ("Throttle/Pedal Position Sensor High (A)",
              "TPS signal high; often a shorted sensor or damaged throttle body."),
    "P0128": ("Coolant Thermostat Below Regulating Temperature",
              "Engine not reaching operating temperature; replace thermostat."),
    "P0130": ("O2 Sensor Circuit (Bank 1 Sensor 1)",
              "Upstream O2 sensor circuit fault; usually the sensor is aged out."),
    "P0131": ("O2 Sensor Circuit Low Voltage (Bank 1 Sensor 1)",
              "Upstream O2 reading lean-pegged; vacuum leak or fuel supply issue."),
    "P0132": ("O2 Sensor Circuit High Voltage (Bank 1 Sensor 1)",
              "Upstream O2 reading rich-pegged; leaking injector, bad MAF, or fuel pressure high."),
    "P0133": ("O2 Sensor Circuit Slow Response (Bank 1 Sensor 1)",
              "Upstream O2 sensor lazy; replace the sensor."),
    "P0134": ("O2 Sensor Circuit No Activity Detected (Bank 1 Sensor 1)",
              "Upstream O2 not switching; sensor dead or no exhaust heat reaching it."),
    "P0135": ("O2 Sensor Heater Circuit (Bank 1 Sensor 1)",
              "Upstream O2 heater failed; replace the sensor."),
    "P0137": ("O2 Sensor Circuit Low (Bank 1 Sensor 2)",
              "Downstream O2 stuck low; usually the sensor itself."),
    "P0138": ("O2 Sensor Circuit High (Bank 1 Sensor 2)",
              "Downstream O2 stuck high; sensor or exhaust leak."),
    "P0141": ("O2 Sensor Heater Circuit (Bank 1 Sensor 2)",
              "Downstream O2 heater failed; replace the sensor."),
    "P0171": ("System Too Lean (Bank 1)",
              "Fuel trims too positive: vacuum leak, weak fuel pump, dirty MAF, or failed PCV — order by likelihood."),
    "P0172": ("System Too Rich (Bank 1)",
              "Fuel trims too negative: leaking injector, high fuel pressure, or bad MAF."),
    "P0174": ("System Too Lean (Bank 2)",
              "Bank 2 lean; same causes as P0171 but isolated to one bank (V-engines mostly)."),
    "P0175": ("System Too Rich (Bank 2)",
              "Bank 2 rich; same causes as P0172 isolated to one bank."),

    # P02xx — Injector and fuel circuit
    "P0201": ("Injector Circuit Malfunction — Cylinder 1",
              "Injector open/short or wiring issue on cylinder 1."),
    "P0202": ("Injector Circuit Malfunction — Cylinder 2",
              "Injector open/short or wiring issue on cylinder 2."),
    "P0203": ("Injector Circuit Malfunction — Cylinder 3",
              "Injector open/short or wiring issue on cylinder 3."),
    "P0204": ("Injector Circuit Malfunction — Cylinder 4",
              "Injector open/short or wiring issue on cylinder 4."),
    "P0205": ("Injector Circuit Malfunction — Cylinder 5",
              "Injector open/short or wiring issue on cylinder 5."),
    "P0221": ("Throttle/Pedal Position Sensor Range/Performance (B)",
              "Second TPS channel implausible; electronic throttle body often the cause."),
    "P0234": ("Turbocharger Overboost Condition",
              "Boost exceeded target; stuck wastegate, bad boost sensor, or a tune/leak issue."),
    "P0299": ("Turbocharger Underboost",
              "Boost below target; boost leak, cracked inlet pipe, or failing turbo."),

    # P03xx — Ignition / misfire
    "P0300": ("Random/Multiple Cylinder Misfire Detected",
              "Multiple cylinders misfiring; ignition coils, plugs, fuel pressure, or vacuum leak."),
    "P0301": ("Cylinder 1 Misfire Detected",
              "Cylinder 1 misfire; swap coil/plug with another cylinder to isolate."),
    "P0302": ("Cylinder 2 Misfire Detected",
              "Cylinder 2 misfire; swap coil/plug with another cylinder to isolate."),
    "P0303": ("Cylinder 3 Misfire Detected",
              "Cylinder 3 misfire; swap coil/plug with another cylinder to isolate."),
    "P0304": ("Cylinder 4 Misfire Detected",
              "Cylinder 4 misfire; swap coil/plug with another cylinder to isolate."),
    "P0305": ("Cylinder 5 Misfire Detected",
              "Cylinder 5 misfire; swap coil/plug with another cylinder to isolate."),
    "P0313": ("Misfire Detected with Low Fuel",
              "Misfire correlated with low fuel level; refuel before further diagnosis."),
    "P0327": ("Knock Sensor 1 Circuit Low Input (Bank 1)",
              "Knock sensor signal low; wiring or sensor failure."),
    "P0328": ("Knock Sensor 1 Circuit High Input (Bank 1)",
              "Knock sensor signal high; wiring short or sensor failure."),
    "P0335": ("Crankshaft Position Sensor A Circuit",
              "CKP sensor fault; car may die or not start. Test sensor output."),
    "P0336": ("Crankshaft Position Sensor A Range/Performance",
              "CKP signal implausible; sensor or reluctor-wheel damage."),
    "P0340": ("Camshaft Position Sensor A Circuit (Bank 1)",
              "CMP sensor fault; engine may run poorly or stall."),
    "P0341": ("Camshaft Position Sensor A Range/Performance (Bank 1)",
              "CMP signal erratic; sensor, wiring, or timing component issue."),

    # P04xx — Emissions
    "P0401": ("Exhaust Gas Recirculation Flow Insufficient",
              "EGR valve clogged with carbon or stuck closed; clean or replace."),
    "P0402": ("Exhaust Gas Recirculation Flow Excessive",
              "EGR stuck open; causes rough idle and stalling."),
    "P0411": ("Secondary Air Injection System Incorrect Flow",
              "Common cold-start fault; check SAI pump, hoses, and check valves."),
    "P0420": ("Catalyst System Efficiency Below Threshold (Bank 1)",
              "Catalytic converter failing or downstream O2 aged out; replace downstream O2 first."),
    "P0430": ("Catalyst System Efficiency Below Threshold (Bank 2)",
              "Bank 2 catalyst; same diagnosis flow as P0420."),
    "P0440": ("Evaporative Emission System Malfunction",
              "EVAP leak; start with the fuel cap, then check purge valve."),
    "P0441": ("Evaporative Emission System Incorrect Purge Flow",
              "EVAP purge valve stuck; often stuck open (idles badly) or closed (check engine light only)."),
    "P0442": ("Evaporative Emission System Leak (small)",
              "Small EVAP leak (~0.040\"); fuel cap, hose, or charcoal canister."),
    "P0446": ("Evaporative Emission System Vent Control Circuit",
              "EVAP vent valve stuck or wiring issue."),
    "P0455": ("Evaporative Emission System Leak (large)",
              "Large EVAP leak; almost always the fuel cap not sealing."),
    "P0456": ("Evaporative Emission System Very Small Leak",
              "Very small EVAP leak; smoke test needed."),
    "P0457": ("Evaporative Emission System Leak — Fuel Cap",
              "Fuel cap loose or damaged; tighten / replace."),
    "P0480": ("Cooling Fan 1 Control Circuit",
              "Cooling fan relay or motor fault; engine may overheat in traffic."),

    # P05xx — VSS / idle / auxiliary inputs
    "P0500": ("Vehicle Speed Sensor A",
              "VSS signal missing; affects speedo, ABS, and TCM operation."),
    "P0501": ("Vehicle Speed Sensor A Range/Performance",
              "VSS implausible; wheel-speed sensor or harness on modern cars."),
    "P0505": ("Idle Control System Malfunction",
              "Unstable idle; on e-throttle cars, usually carbon in throttle body."),
    "P0506": ("Idle Control System RPM Lower Than Expected",
              "Idle lower than target; vacuum leak or a dragging accessory."),
    "P0507": ("Idle Control System RPM Higher Than Expected",
              "Idle higher than target; vacuum leak, throttle body carbon, or stuck PCV."),
    "P0562": ("System Voltage Low",
              "Battery or charging system voltage low; test alternator."),
    "P0563": ("System Voltage High",
              "Over-voltage; usually a failed voltage regulator / alternator."),
    "P0571": ("Cruise Control/Brake Switch A Circuit",
              "Brake switch signal inconsistent; replace brake light switch."),

    # P06xx — ECU-internal
    "P0601": ("Internal Control Module Memory Checksum Error",
              "ECU memory corruption; may need reflash or replacement."),
    "P0606": ("ECM/PCM Processor",
              "Internal ECU fault; rare — usually warrants replacement."),
    "P0622": ("Generator Field F Terminal Circuit",
              "Alternator field wire fault; check harness before replacing alternator."),

    # P07xx — Transmission
    "P0700": ("Transmission Control System Malfunction",
              "Generic 'TCM has a code' pointer; scan the TCM for the actual fault."),
    "P0715": ("Input/Turbine Speed Sensor Circuit",
              "Input shaft speed sensor fault; common on automatics."),
    "P0720": ("Output Speed Sensor Circuit",
              "Output shaft speed sensor fault."),
    "P0731": ("Gear 1 Incorrect Ratio",
              "Transmission slipping in 1st; solenoid, fluid level, or internal wear."),
    "P0740": ("Torque Converter Clutch Circuit",
              "TCC solenoid fault; fluid or internal failure."),

    # P2xxx — Extended
    "P2096": ("Post-Catalyst Fuel Trim System Too Lean (Bank 1)",
              "Lean trim downstream; often small exhaust leak ahead of rear O2."),
    "P2187": ("System Too Lean at Idle (Bank 1)",
              "Lean only at idle; vacuum leak or PCV (Volvo: flame trap failure is classic)."),
    "P2188": ("System Too Rich at Idle (Bank 1)",
              "Rich only at idle; stuck-open purge valve or leaking injector."),
    "P2195": ("O2 Sensor Signal Biased/Stuck Lean (Bank 1 Sensor 1)",
              "Upstream O2 stuck lean; sensor aged or unmetered air."),
    "P2196": ("O2 Sensor Signal Biased/Stuck Rich (Bank 1 Sensor 1)",
              "Upstream O2 stuck rich; sensor aged or fuel system issue."),

    # Volvo P1xxx — manufacturer-specific, C30/P1-platform relevant
    "P1121": ("Throttle Position Sensor Inconsistent with MAF",
              "Volvo: electronic throttle carbon buildup is the usual cause."),
    "P1171": ("Fuel System Lean During Acceleration (Bank 1)",
              "Volvo: check fuel pressure, MAF, and vacuum/boost leaks."),
    "P1324": ("Crank Case Ventilation System",
              "Volvo white-block specific: PCV / flame trap system failing — very common on this engine."),
}


# ── Public API ────────────────────────────────────────────────────────────────

def lookup(code: str) -> DTCInfo:
    """
    Resolve a DTC code to its best available description.

    Strategy:
      1. Normalise to uppercase and strip whitespace.
      2. Look up exact match in the curated dictionary.
      3. If not found, derive a category fallback from the 3-char prefix.
      4. If nothing matches (e.g. malformed code), return an 'unknown' record.

    Always returns a DTCInfo dict — never None, never raises.
    """
    if not code:
        return DTCInfo(
            code="", name="Unknown code",
            category="Unknown",
            hint="Provide a valid OBD-II code (e.g. P0171, C1234, B1000).",
            source="unknown",
        )

    c = code.strip().upper()

    # Exact match
    if c in _CODES:
        name, hint = _CODES[c]
        category = _category_for(c)
        return DTCInfo(
            code=c, name=name, category=category, hint=hint,
            source="j2012_known",
        )

    # Category fallback — 3-char prefix (e.g. P01 from P0171), or single-letter
    # for B/C/U which have less granularity in our category map.
    category = _category_for(c)
    if category != "Unknown":
        return DTCInfo(
            code=c,
            name=f"{c} — no detailed entry available",
            category=category,
            hint=(
                f"This code falls under '{category}'. "
                "Without LLM analysis, consult a service manual or wait for "
                "Ollama to return online for a full diagnosis."
            ),
            source="category_fallback",
        )

    return DTCInfo(
        code=c, name=f"{c} — unrecognised",
        category="Unknown",
        hint="Code format not recognised. Expect Pxxxx, Bxxxx, Cxxxx, or Uxxxx.",
        source="unknown",
    )


def format_for_display(info: DTCInfo) -> str:
    """Render a DTCInfo as a short multi-line string for the dashboard / terminal."""
    return (
        f"DTC {info['code']} — {info['name']}\n"
        f"Category: {info['category']}\n"
        f"Hint: {info['hint']}\n"
        f"(Static J2012 fallback — LLM unavailable)"
    )


# ── Internals ────────────────────────────────────────────────────────────────

def _category_for(code: str) -> str:
    """Return the best-matching category for a code, or 'Unknown'."""
    # P/B/C/U codes have different prefix structures
    if code.startswith("P") and len(code) >= 3:
        return CATEGORY_BY_PREFIX.get(code[:3], "Unknown")
    if code[:1] in ("B", "C", "U"):
        return CATEGORY_BY_PREFIX.get(code[:1], "Unknown")
    return "Unknown"


def stats() -> dict:
    """How many codes are in the dictionary — useful for smoke tests."""
    return {
        "known_codes": len(_CODES),
        "categories": len(CATEGORY_BY_PREFIX),
    }
