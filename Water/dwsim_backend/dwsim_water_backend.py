# dwsim_water_backend.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import pythoncom
import clr
from System.IO import Directory


@dataclass
class WaterPoint:
    T_K: float
    P_Pa: float
    h_kJ_kg: float
    s_kJ_kgK: float
    vapor_fraction: float
    flash_used: str
    q_requested: Optional[float]


class DWSIMWaterBackend:
    """
    DWSIM SteamTables backend for water.

    Notes (based on your observed behavior):
    - "TP" works reliably.
    - "TQ"/"PQ" may fail in some builds via MaterialStream.CalcEquilibrium.
    - "TVF"/"PVF" may run, but VF setting can be ignored depending on build.

    Therefore:
    - sat_from_T and sat_from_P attempt TQ/PQ first; if it fails, fallback to TVF/PVF.
    - We always return the REAL vapor fraction from GetProp after the flash.
    """

    def __init__(self, dwsim_path: str, stream_name: str = "WATER_BACKEND"):
        self.dwsim_path = dwsim_path
        self.stream_name = stream_name

        pythoncom.CoInitialize()
        Directory.SetCurrentDirectory(self.dwsim_path)

        # Assemblies
        clr.AddReference(self.dwsim_path + "CapeOpen.dll")
        clr.AddReference(self.dwsim_path + "DWSIM.Automation.dll")
        clr.AddReference(self.dwsim_path + "DWSIM.Interfaces.dll")
        clr.AddReference(self.dwsim_path + "DWSIM.GlobalSettings.dll")
        clr.AddReference(self.dwsim_path + "DWSIM.SharedClasses.dll")
        clr.AddReference(self.dwsim_path + "DWSIM.Thermodynamics.dll")
        try:
            clr.AddReference(self.dwsim_path + "DWSIM.Thermodynamics.ThermoC.dll")
        except Exception:
            pass

        # Import after loading references
        from DWSIM.Automation import Automation3
        from DWSIM.GlobalSettings import Settings
        from DWSIM.Interfaces.Enums.GraphicObjects import ObjectType
        from DWSIM.Thermodynamics import PropertyPackages

        self.interf = Automation3()
        self.sim = self.interf.CreateFlowsheet()
        Settings.SolverMode = 0

        # Add water compound
        water_key = self._find_water_key()
        water_comp = self.sim.AvailableCompounds[water_key]
        self.sim.SelectedCompounds.Add(water_comp.Name, water_comp)

        # SteamTables PP
        pp = PropertyPackages.SteamTablesPropertyPackage()
        self.sim.AddPropertyPackage(pp)
        self.sim.SelectedPropertyPackage = pp

        # One reusable stream
        self.ms = self.sim.AddObject(ObjectType.MaterialStream, 50, 50, self.stream_name).GetAsObject()
        self.sim.AutoLayout()

        # Composition = 100% water
        ph0 = self.ms.Phases[0]
        ph0.Compounds[water_comp.Name].MoleFraction = 1.0

        # Small flow
        self.ms.SetMassFlow(1.0)

    def _find_water_key(self) -> str:
        for k in ("Water", "H2O"):
            if k in self.sim.AvailableCompounds:
                return k
        keys = list(self.sim.AvailableCompounds.Keys)
        lower = {str(k).strip().lower(): str(k) for k in keys}
        for k in ("water", "h2o"):
            if k in lower:
                return lower[k]
        raise KeyError(f"Could not find Water/H2O in AvailableCompounds. Examples: {keys[:30]}")

    def _get_vapor_fraction(self) -> float:
        vf_obj = self.ms.GetProp("phaseFraction", "Vapor", None, "", "mole")
        try:
            return float(vf_obj[0])
        except TypeError:
            return float(vf_obj)

    def _set_vapor_fraction(self, vf: float) -> None:
        """
        Best-effort VF setter (some builds ignore it for saturation flashes).
        """
        vf = float(vf)
        try:
            self.ms.SetProp("phaseFraction", "Vapor", vf, "", "mole")
            return
        except Exception:
            pass

        for prop_id in ("phasefractionVapor", "phaseFractionVapor", "VaporFraction", "VF"):
            try:
                self.ms.SetPropertyValue(prop_id, vf)
                return
            except Exception:
                continue

        # If setter fails, we still allow running flashes that don't require it (TP).
        raise RuntimeError("Could not set vapor fraction (VF) on this DWSIM build.")

    def _extract(self, flash_used: str, q_requested: Optional[float]) -> WaterPoint:
        return WaterPoint(
            T_K=float(self.ms.GetTemperature()),
            P_Pa=float(self.ms.GetPressure()),
            h_kJ_kg=float(self.ms.GetMassEnthalpy()),
            s_kJ_kgK=float(self.ms.GetMassEntropy()),
            vapor_fraction=self._get_vapor_fraction(),
            flash_used=flash_used,
            q_requested=q_requested,
        )

    def _try_flash(self, flash_type: str) -> Tuple[bool, Optional[str]]:
        try:
            self.ms.CalcEquilibrium(flash_type, None)
            return True, None
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    # ---------------- Public API ----------------

    def props_TP(self, T_K: float, P_Pa: float) -> Dict[str, float]:
        self.ms.SetTemperature(float(T_K))
        self.ms.SetPressure(float(P_Pa))

        ok, err = self._try_flash("TP")
        if not ok:
            raise RuntimeError(f"TP flash failed: {err}")

        pt = self._extract("TP", None)
        return {
            "T_K": pt.T_K,
            "P_Pa": pt.P_Pa,
            "h_kJ_kg": pt.h_kJ_kg,
            "s_kJ_kgK": pt.s_kJ_kgK,
            "vapor_fraction": pt.vapor_fraction,
            "flash_used": pt.flash_used,
        }

    def sat_from_T(self, T_K: float, q: float) -> Dict[str, float]:
        """
        Saturation at given T. Best-effort:
        1) Try TQ (if supported)
        2) Else fallback to TVF (as in your earlier working run)
        """
        self.ms.SetTemperature(float(T_K))

        # Try TQ first
        try:
            self._set_vapor_fraction(q)  # use VF as "quality proxy"
        except Exception:
            # If setting fails, still try TQ (some builds might not need it)
            pass

        ok, err = self._try_flash("TQ")
        if ok:
            pt = self._extract("TQ", q)
            return {
                "T_K": pt.T_K,
                "P_Pa": pt.P_Pa,
                "h_kJ_kg": pt.h_kJ_kg,
                "s_kJ_kgK": pt.s_kJ_kgK,
                "vapor_fraction": pt.vapor_fraction,
                "flash_used": pt.flash_used,
                "q_requested": pt.q_requested,
            }

        # Fallback: TVF
        try:
            self._set_vapor_fraction(q)
        except Exception:
            pass

        ok2, err2 = self._try_flash("TVF")
        if not ok2:
            raise RuntimeError(f"TQ failed ({err}); TVF failed ({err2})")

        pt = self._extract("TVF", q)
        return {
            "T_K": pt.T_K,
            "P_Pa": pt.P_Pa,
            "h_kJ_kg": pt.h_kJ_kg,
            "s_kJ_kgK": pt.s_kJ_kgK,
            "vapor_fraction": pt.vapor_fraction,
            "flash_used": pt.flash_used,
            "q_requested": pt.q_requested,
        }

    def sat_from_P(self, P_Pa: float, q: float) -> Dict[str, float]:
        """
        Saturation at given P. Best-effort:
        1) Try PQ
        2) Else fallback to PVF
        """
        self.ms.SetPressure(float(P_Pa))

        try:
            self._set_vapor_fraction(q)
        except Exception:
            pass

        ok, err = self._try_flash("PQ")
        if ok:
            pt = self._extract("PQ", q)
            return {
                "T_K": pt.T_K,
                "P_Pa": pt.P_Pa,
                "h_kJ_kg": pt.h_kJ_kg,
                "s_kJ_kgK": pt.s_kJ_kgK,
                "vapor_fraction": pt.vapor_fraction,
                "flash_used": pt.flash_used,
                "q_requested": pt.q_requested,
            }

        # Fallback: PVF
        try:
            self._set_vapor_fraction(q)
        except Exception:
            pass

        ok2, err2 = self._try_flash("PVF")
        if not ok2:
            raise RuntimeError(f"PQ failed ({err}); PVF failed ({err2})")

        pt = self._extract("PVF", q)
        return {
            "T_K": pt.T_K,
            "P_Pa": pt.P_Pa,
            "h_kJ_kg": pt.h_kJ_kg,
            "s_kJ_kgK": pt.s_kJ_kgK,
            "vapor_fraction": pt.vapor_fraction,
            "flash_used": pt.flash_used,
            "q_requested": pt.q_requested,
        }


if __name__ == "__main__":
    DWSIM_PATH = r"C:\DWSIM\\"
    bk = DWSIMWaterBackend(DWSIM_PATH)

    print("=== Single-point saturation test at 100°C ===")
    try:
        a = bk.sat_from_T(373.15, q=0.0)
        b = bk.sat_from_T(373.15, q=1.0)
        print("q=0:", a)
        print("q=1:", b)
    except Exception as e:
        print("SAT test failed:", type(e).__name__, e)

    print("\n=== Single TP test ===")
    print(bk.props_TP(400.0, 2e5))