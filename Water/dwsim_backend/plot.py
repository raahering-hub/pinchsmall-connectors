from __future__ import annotations

import os
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator

from dwsim_water_backend import DWSIMWaterBackend

DWSIM_PATH = r"C:\DWSIM\\"


# -------------------------
# Tsat(P) solver from Psat(T) = P (bisection)
# -------------------------
def find_Tsat_for_P(
    bk: DWSIMWaterBackend,
    P_target: float,
    T_low: float = 273.5,
    T_high: float = 646.5,
    tol_P: float = 50.0,
    max_iter: int = 70,
) -> float | None:
    def Psat(T: float) -> float:
        return float(bk.sat_from_T(float(T), q=0.0)["P_Pa"])

    try:
        f_low = Psat(T_low) - P_target
        f_high = Psat(T_high) - P_target
    except Exception:
        return None

    if f_low * f_high > 0:
        return None

    a, b = T_low, T_high
    fa = f_low

    for _ in range(max_iter):
        m = 0.5 * (a + b)
        try:
            fm = Psat(m) - P_target
        except Exception:
            return None

        if abs(fm) <= tol_P:
            return m

        if fa * fm < 0:
            b = m
        else:
            a = m
            fa = fm

    return 0.5 * (a + b)


def main():
    bk = DWSIMWaterBackend(DWSIM_PATH)

    # -------------------------
    # Plot limits
    # -------------------------
    T_min_plot, T_max_plot = 250.0, 700.0
    s_min_plot, s_max_plot = 0.0, 9.0

    # All curves same thickness (academic look)
    LW = 1.0

    # -------------------------
    # Saturation dome (via Tsat(P) + TP with Tsat±dT)
    # -------------------------
    P_min = 1.0e4
    P_max = 2.2e7
    P_dome = np.geomspace(P_min, P_max, 280)

    dT_side = 0.5  # for evaluating properties around saturation
    dome_T, dome_sL, dome_sV = [], [], []

    for P in P_dome:
        Tsat = find_Tsat_for_P(bk, float(P))
        if Tsat is None:
            continue

        T1 = max(273.5, Tsat - dT_side)
        T2 = min(700.0, Tsat + dT_side)

        try:
            liq = bk.props_TP(float(T1), float(P))
            vap = bk.props_TP(float(T2), float(P))
        except Exception:
            continue

        dome_T.append(Tsat)
        dome_sL.append(liq["s_kJ_kgK"])
        dome_sV.append(vap["s_kJ_kgK"])

    dome_T = np.array(dome_T)
    sL = np.array(dome_sL)
    sV = np.array(dome_sV)

    if len(dome_T) < 40:
        print("Not enough dome points generated.")
        return

    idx = np.argsort(dome_T)
    dome_T, sL, sV = dome_T[idx], sL[idx], sV[idx]

    # -------------------------
    # Figure
    # -------------------------
    fig, ax = plt.subplots(figsize=(12, 7))

    # -------------------------
    # Isobars (P constant): plot BOTH sides explicitly
    # -------------------------
    # Keep away from saturation by this margin:
    dT_avoid = 2.0  # K

    isobars_MPa = [0.01, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10]

    # plot isobars first (so dome can be overlaid cleanly)
    for P_MPa in isobars_MPa:
        P = float(P_MPa) * 1e6  # Pa
        Tsat = find_Tsat_for_P(bk, P)
        if Tsat is None:
            continue

        # --- (a) Subcooled branch: T <= Tsat - dT_avoid
        T_liq_hi = max(276.0, Tsat - dT_avoid)
        if T_liq_hi > 276.0:
            T_liq = np.linspace(275.0, T_liq_hi, 90)
            s_liq, T_liq_ok = [], []
            for T in T_liq:
                try:
                    pt = bk.props_TP(float(T), float(P))
                    s_liq.append(pt["s_kJ_kgK"])
                    T_liq_ok.append(T)
                except Exception:
                    continue
            if len(s_liq) > 15:
                ax.plot(s_liq, T_liq_ok, color="black", linewidth=LW, linestyle="--")

        # --- (b) Two-phase segment inside dome (horizontal at Tsat)
        j = int(np.argmin(np.abs(dome_T - Tsat)))
        ax.plot([sL[j], sV[j]], [dome_T[j], dome_T[j]],
                color="black", linewidth=LW, linestyle="--")

        # --- (c) Superheated branch: T >= Tsat + dT_avoid
        T_vap_lo = min(646.0, Tsat + dT_avoid)
        if T_vap_lo < 700.0:
            T_vap = np.linspace(T_vap_lo, 700.0, 110)
            s_vap, T_vap_ok = [], []
            for T in T_vap:
                try:
                    pt = bk.props_TP(float(T), float(P))
                    s_vap.append(pt["s_kJ_kgK"])
                    T_vap_ok.append(T)
                except Exception:
                    continue
            if len(s_vap) > 15:
                ax.plot(s_vap, T_vap_ok, color="black", linewidth=LW, linestyle="--")

                # Label only some pressures (kPa) to avoid clutter
                if P_MPa in (0.1, 1, 10):
                    P_kPa = P / 1000.0
                    ax.text(s_vap[-1], T_vap_ok[-1], f"{P_kPa:.0f} kPa",
                            fontsize=9, color="black")

    # -------------------------
    # Saturation dome (same thickness)
    # -------------------------
    ax.plot(sL, dome_T, color="black", linewidth=LW, linestyle="-", label="Saturation dome")
    ax.plot(sV, dome_T, color="black", linewidth=LW, linestyle="-")

    # -------------------------
    # Quality lines x (inside dome) — dotted
    # -------------------------
    x_list = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    for x in x_list:
        s_x = sL + float(x) * (sV - sL)
        ax.plot(s_x, dome_T, color="black", linewidth=LW, linestyle=":")

        jj = int(0.30 * len(dome_T))
        ax.text(float(s_x[jj]), float(dome_T[jj]), f"x={x:g}",
                fontsize=9, rotation=25, color="black")

    # -------------------------
    # Dense academic ticks & grid
    # -------------------------
    ax.set_xlim(s_min_plot, s_max_plot)
    ax.set_ylim(T_min_plot, T_max_plot)

    # Major ticks (legible)
    ax.xaxis.set_major_locator(MultipleLocator(0.5))
    ax.yaxis.set_major_locator(MultipleLocator(25.0))

    # Minor ticks (your request: s in 0.05 steps)
    ax.xaxis.set_minor_locator(MultipleLocator(0.05))
    ax.yaxis.set_minor_locator(MultipleLocator(5.0))

    ax.grid(True, which="major", linewidth=0.8, linestyle="-", color="0.75")
    ax.grid(True, which="minor", linewidth=0.4, linestyle=":", color="0.88")

    ax.set_xlabel("entropy, s [kJ/(kg·K)]")
    ax.set_ylabel("temperature, T [K]")
    ax.set_title("Water/Steam (SteamTables): T–s diagram (B/W)")

    ax.legend(loc="upper right", frameon=True)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "water_Ts_diagram_bw_dense.png")
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    print("[OK] Saved:", out_path)

    plt.show()


if __name__ == "__main__":
    main()