from __future__ import annotations

import os
import json
from typing import Dict

# pythoncom is required when using COM on Windows (DWSIM automation)
import pythoncom

# pythonnet: clr allows importing .NET assemblies and DWSIM DLLs
import clr
from System.IO import Directory

# Local DWSIM installation folder containing the required DLLs
DWSIM_PATH = r"C:\DWSIM\\"

# DWSIM bootstrap (load .NET assemblies + COM init)

# Initialize COM for this Python process (required for DWSIM automation on Windows)
pythoncom.CoInitialize()

# Ensure the current working directory is DWSIM folder so DLL references resolve cleanly
Directory.SetCurrentDirectory(DWSIM_PATH)

# Load the core DWSIM assemblies needed to create/solve flowsheets
clr.AddReference(DWSIM_PATH + "CapeOpen.dll")
clr.AddReference(DWSIM_PATH + "DWSIM.Automation.dll")
clr.AddReference(DWSIM_PATH + "DWSIM.Interfaces.dll")
clr.AddReference(DWSIM_PATH + "DWSIM.GlobalSettings.dll")
clr.AddReference(DWSIM_PATH + "DWSIM.SharedClasses.dll")
clr.AddReference(DWSIM_PATH + "DWSIM.Thermodynamics.dll")

# ThermoC is optional and not always present; load if available
try:
    clr.AddReference(DWSIM_PATH + "DWSIM.Thermodynamics.ThermoC.dll")
except Exception:
    pass

# Import DWSIM classes/enums
from DWSIM.Interfaces.Enums.GraphicObjects import ObjectType
from DWSIM.Thermodynamics import PropertyPackages
from DWSIM.Automation import Automation3
from DWSIM.GlobalSettings import Settings

# Unit conversion helpers
def C_to_K(T_C: float) -> float:
    return float(T_C) + 273.15


def bar_to_Pa(P_bar: float) -> float:
    return float(P_bar) * 1e5

# Property package factory

def create_property_package(pp_name: str):
    name = (pp_name or "").strip().lower()

    # SteamTables: good for pure water
    if name in ("steamtables", "steam tables", "steam_tables"):
        return PropertyPackages.SteamTablesPropertyPackage()

    # Peng-Robinson: cubic EOS, good generic choice for many systems
    if name in ("pengrobinson", "peng-robinson", "pr"):
        return PropertyPackages.PengRobinsonPropertyPackage()

    # CoolProp: depends on your DWSIM build and installed CoolProp support
    if name in ("coolprop",):
        return PropertyPackages.CoolPropPropertyPackage()

    raise NotImplementedError(f"Property package '{pp_name}' is not implemented.")

# Composition helpers

def _normalize(fracs: Dict[str, float]) -> Dict[str, float]:
    tot = sum(float(v) for v in fracs.values())
    if tot <= 0:
        raise ValueError("Composition total must be > 0.")
    return {str(k): float(v) / tot for k, v in fracs.items()}


def _resolve_available_compound_key(sim, requested_name: str) -> str:
    req = (requested_name or "").strip()
    if not req:
        raise ValueError("Empty compound name.")

    # Exact match
    if req in sim.AvailableCompounds:
        return req

    # Case-insensitive match
    keys = list(sim.AvailableCompounds.Keys)
    lower_map = {str(k).strip().lower(): str(k) for k in keys}
    hit = lower_map.get(req.lower())
    if hit:
        return hit

    raise KeyError(
        f"DWSIM compound '{requested_name}' not found in AvailableCompounds. "
        f"Examples: {keys[:30]}"
    )

def _get_mw_kg_kmol(av_comp) -> float:
    for attr in ("Molar_Weight", "MolarWeight", "MolecularWeight", "MW"):
        if hasattr(av_comp, attr):
            return float(getattr(av_comp, attr))
    raise AttributeError("Molecular weight attribute not found on DWSIM compound object.")

def _massfrac_to_molefrac(
    massfrac: Dict[str, float], mw_kg_kmol: Dict[str, float]
) -> Dict[str, float]:
    n = {c: float(massfrac[c]) / float(mw_kg_kmol[c]) for c in massfrac}
    denom = sum(n.values())
    if denom <= 0:
        raise ValueError("Invalid composition: sum(w_i/MW_i) <= 0.")
    return {c: n[c] / denom for c in n}

# DWSIM flowsheet setup helpers

def _add_pp_and_compounds(sim, dwsim_components: list[str], pp_name: str):
    # Select compounds
    for comp_name in dwsim_components:
        key = _resolve_available_compound_key(sim, comp_name)
        comp = sim.AvailableCompounds[key]
        sim.SelectedCompounds.Add(comp.Name, comp)

    # Add and select property package
    pp = create_property_package(pp_name)
    sim.AddPropertyPackage(pp)
    sim.SelectedPropertyPackage = pp
    return pp


def apply_massfrac_as_molefrac(sim, stream, massfrac_by_dwsim_key: Dict[str, float]) -> Dict[str, float]:

    # Normalize mass fractions first
    w = _normalize(massfrac_by_dwsim_key)

    # Retrieve MW for each compound from DWSIM
    mw: Dict[str, float] = {}
    for cname in w.keys():
        av = sim.AvailableCompounds[cname]
        mw[cname] = _get_mw_kg_kmol(av)

    # Convert to mole fractions and normalize
    x = _massfrac_to_molefrac(w, mw)
    x = _normalize(x)

    # Apply on phase 0 (Overall phase in your DWSIM build)
    ph0 = stream.Phases[0]
    for cname, xi in x.items():
        ph0.Compounds[cname].MoleFraction = float(xi)

    return x

# Extract the key thermodynamic properties from a DWSIM material stream.
def get_stream_props(stream) -> dict:
    T = float(stream.GetTemperature())      # K
    P = float(stream.GetPressure())         # Pa
    h = float(stream.GetMassEnthalpy())     # kJ/kg
    s = float(stream.GetMassEntropy())      # kJ/(kg.K)
    m = float(stream.GetMassFlow())         # kg/s

    # Vapor fraction (molar basis) for the "Vapor" phase
    vf_obj = stream.GetProp("phaseFraction", "Vapor", None, "", "mole")
    try:
        x_v = float(vf_obj[0])  # some DWSIM builds return arrays
    except TypeError:
        x_v = float(vf_obj)     # others return scalar

    # Convert vapor fraction into a simple phase label
    tol = 1e-6
    if x_v <= tol:
        phase = "L"
    elif x_v >= 1.0 - tol:
        phase = "V"
    else:
        phase = "L+V"

    return {
        "temperature_K": T,
        "pressure_Pa": P,
        "massflow_kg_s": m,
        "enthalpy_kJ_kg": h,
        "entropy_kJ_kgK": s,
        "vapor_fraction": x_v,
        "phase": phase,
    }

# Public API (ONE YAML -> ONE connector)

def run_connector(catalog_yaml_path: str) -> dict:
    """
    Main entry point called by runner/Streamlit/QMD.

    It:
      1) Loads catalog.yaml
      2) Builds a minimal 1-stream DWSIM flowsheet
      3) Applies T/P/massflow and composition (MASSFRAC -> MOLEFRAC)
      4) Solves the flowsheet
      5) Saves .dwxmz and ONLY results/connector_simple.json
      6) Returns the same dict that was saved to JSON
    """
    import yaml

    print("[DEBUG] using integration.py =", __file__)

    # 1) Load YAML configuration for this connector case
    with open(catalog_yaml_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    if cfg.get("mode") != "connector_database":
        raise ValueError("Expected mode: connector_database")

    uid = str(cfg["uid"])

    # Model section (property package and expected state definition)
    model = cfg.get("model", {}) or {}
    pp_name = str(model.get("property_package", "PengRobinson")).strip()
    state_def = model.get("state_definition", ["T", "P"])

    # DWSIM section: selected components + mapping from "pinch names" to DWSIM names
    dws = cfg.get("dwsim", {}) or {}
    dwsim_components = list(dws.get("components", []))
    component_map = dict(dws.get("component_map", {}))

    if not dwsim_components or not component_map:
        raise ValueError("Missing dwsim.components or dwsim.component_map in catalog.yaml.")

    # 2) State and reference flow from YAML
    m_ref = float(cfg["reference_flow"]["massflow_kg_s"])

    st = cfg["state"]
    T_K = C_to_K(st["T_C"])
    P_Pa = bar_to_Pa(st["P_bar"])

    # 3) Composition from YAML (only MASSFRAC supported here)
    comp_block = cfg.get("compounds", {}) or {}
    basis = str(comp_block.get("basis", "MASSFRAC")).upper()
    if basis != "MASSFRAC":
        raise NotImplementedError("Only compounds.basis: MASSFRAC is supported.")

    # fracs_pinch keeps the original naming from the YAML (H2O, FAT, etc.)
    fracs_pinch = {k: float(v) for k, v in comp_block.items() if k != "basis"}

    # 4) Prepare results folder (case-local)
    case_dir = os.path.dirname(os.path.abspath(catalog_yaml_path))
    results_dir = os.path.join(case_dir, "results")
    os.makedirs(results_dir, exist_ok=True)

    # 5) Build a minimal DWSIM flowsheet (single stream)
    interf = Automation3()
    sim = interf.CreateFlowsheet()

    # Force solver mode to a deterministic mode (0 is common default)
    Settings.SolverMode = 0

    # Add compounds + property package to the flowsheet
    _add_pp_and_compounds(sim, dwsim_components, pp_name)

    # 6) Map composition keys (pinch -> DWSIM compound keys)

    # Convert the YAML composition keys into DWSIM compound keys
    massfrac_by_dwsim: Dict[str, float] = {}
    for pinch_name, frac in fracs_pinch.items():
        if pinch_name not in component_map:
            raise KeyError(
                f"Component '{pinch_name}' has no mapping in dwsim.component_map. "
                f"Available: {list(component_map.keys())}"
            )
        dwsim_name = str(component_map[pinch_name])
        dwsim_key = _resolve_available_compound_key(sim, dwsim_name)
        massfrac_by_dwsim[dwsim_key] = float(frac)

    # Ensure mapped fractions sum to 1
    massfrac_by_dwsim = _normalize(massfrac_by_dwsim)

    # 7) Create the stream and set T/P/massflow
    ms = sim.AddObject(ObjectType.MaterialStream, 50, 50, uid).GetAsObject()
    sim.AutoLayout()

    ms.SetTemperature(float(T_K))
    ms.SetPressure(float(P_Pa))
    ms.SetMassFlow(float(m_ref))

    # 8) Apply composition (MASSFRAC -> MOLEFRAC) to stream phase 0
    molefrac_applied = apply_massfrac_as_molefrac(sim, ms, massfrac_by_dwsim)
    print("[DEBUG] molefracs applied:", molefrac_applied)

    # 9) Solve flowsheet
    errors = interf.CalculateFlowsheet2(sim)
    if errors:
        print(f"[WARN] Solver issues for connector '{uid}':")
        for err in errors:
            print("  -", err)

    # 10) Extract key properties from the solved stream
    props = get_stream_props(ms)

    # 11) Save the solved flowsheet (.dwxmz) for debugging/reproducibility
    dwxmz_path = os.path.join(results_dir, f"{uid}.dwxmz")
    interf.SaveFlowsheet(sim, dwxmz_path, True)

    # 12) Build clean JSON (connector_simple)
    connector_simple = {
    "connector": cfg.get("domain") or cfg.get("engine"),
    "case": os.path.basename(os.path.dirname(catalog_yaml_path)),
    "model": {
        "property_package": pp_name,
    },
    "reference_flow": {
        "massflow_kg_s": m_ref
    },
    "state": {
        "T_K": props["temperature_K"],
        "P_Pa": props["pressure_Pa"]
    },
    "properties": {
        "h_kJ_kg": props["enthalpy_kJ_kg"],
        "s_kJ_kgK": props["entropy_kJ_kgK"],
        "vapor_fraction": props["vapor_fraction"],
        "phase": props["phase"]
    },
    "compounds": {
        "basis": "MASSFRAC",
        **fracs_pinch
    }
}
    # 13) Save connector_simple.json
    simple_path = os.path.join(results_dir, "connector_simple.json")
    with open(simple_path, "w", encoding="utf-8") as f:
        json.dump(connector_simple, f, indent=2, ensure_ascii=False)

    # Logging
    print(f"[OK] Connector '{uid}' saved:")
    print(f"     DWSIM : {dwxmz_path}")
    print(f"     JSON  : {simple_path}")

    # Return the same structure for upstream tools (runner/Streamlit/QMD)
    return connector_simple
