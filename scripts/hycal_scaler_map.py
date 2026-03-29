#!/usr/bin/env python3
"""
HyCal FADC Scaler Map
=====================
Polls EPICS scaler channels (B_DET_HYCAL_FADC_<name>) for every HyCal
module and displays a colour-coded geo map.  Colour intensity reflects
the scaler readout value.

Usage
-----
    python scripts/hycal_scaler_map.py              # real EPICS (default)
    python scripts/hycal_scaler_map.py --sim         # simulation (random)
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tkinter as tk
from tkinter import ttk
from typing import Dict, List, Optional, Tuple

# Allow importing from calibration/
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "calibration"))
from scan_utils import Module, load_modules, DEFAULT_DB_PATH


# ============================================================================
#  CONSTANTS
# ============================================================================

SCALER_PREFIX = "B_DET_HYCAL_FADC_"
POLL_INTERVAL_MS = 10000   # 10 seconds

# Colour palette
class C:
    BG      = "#0d1117"
    PANEL   = "#161b22"
    BORDER  = "#30363d"
    TEXT    = "#c9d1d9"
    DIM     = "#8b949e"
    ACCENT  = "#58a6ff"
    GREEN   = "#3fb950"


# ============================================================================
#  EPICS INTERFACE
# ============================================================================

class RealScalerEPICS:
    """Read scaler PVs via pyepics."""

    def __init__(self, modules: List[Module]):
        import epics as _epics
        self._epics = _epics
        self._pvs: Dict[str, object] = {}
        for m in modules:
            if m.mod_type in ("PbWO4", "PbGlass"):
                pvname = SCALER_PREFIX + m.name
                self._pvs[m.name] = _epics.PV(pvname, connection_timeout=2.0)

    def get(self, name: str) -> Optional[float]:
        pv = self._pvs.get(name)
        if pv and pv.connected:
            return pv.get()
        return None


class SimulatedScalerEPICS:
    """Return random values for testing."""

    def __init__(self, modules: List[Module]):
        import random
        self._rng = random
        self._names = [m.name for m in modules
                       if m.mod_type in ("PbWO4", "PbGlass")]

    def get(self, name: str) -> Optional[float]:
        return self._rng.uniform(0, 1000)


# ============================================================================
#  GUI
# ============================================================================

def _val_to_color(val: float, vmin: float, vmax: float) -> str:
    """Map a value to a blue-green-yellow-red colour scale."""
    if vmax <= vmin:
        t = 0.0
    else:
        t = max(0.0, min(1.0, (val - vmin) / (vmax - vmin)))
    # 0=dark blue, 0.33=cyan, 0.66=yellow, 1=red
    if t < 0.33:
        s = t / 0.33
        r, g, b = 0, int(180 * s), int(80 + 175 * (1 - s))
    elif t < 0.66:
        s = (t - 0.33) / 0.33
        r, g, b = int(255 * s), int(180 + 75 * s), int(80 * (1 - s))
    else:
        s = (t - 0.66) / 0.34
        r, g, b = 255, int(255 * (1 - s)), 0
    return f"#{r:02x}{g:02x}{b:02x}"


class ScalerMapGUI:

    CANVAS_SIZE = 680
    CANVAS_PAD  = 8
    MOD_SHRINK  = 0.92

    def __init__(self, root: tk.Tk, modules: List[Module],
                 epics, simulation: bool):
        self.root = root
        self.all_modules = modules
        self.ep = epics
        self.simulation = simulation

        self._scalable = [m for m in modules
                          if m.mod_type in ("PbWO4", "PbGlass")]
        self._values: Dict[str, float] = {}
        self._cell_ids: Dict[str, int] = {}
        self._scale = 1.0
        self._ox = self._oy = 0.0
        self._x_min = self._y_max = 0.0
        self._polling = True

        self._build_ui()
        self._refresh()

    def _build_ui(self):
        self.root.title("HyCal Scaler Map" +
                        ("  [SIMULATION]" if self.simulation
                         else "  [REAL EPICS]"))
        self.root.configure(bg=C.BG)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=C.BG, foreground=C.TEXT,
                         fieldbackground=C.PANEL, bordercolor=C.BORDER)
        style.configure("TButton", background=C.PANEL, foreground=C.TEXT,
                         padding=4)
        style.map("TButton", background=[("active", C.BORDER)])

        # top bar
        top = tk.Frame(self.root, bg="#0d1520", height=32)
        top.pack(fill="x")
        tk.Label(top, text="  HYCAL SCALER MAP  ", bg="#0d1520", fg=C.GREEN,
                 font=("Consolas", 13, "bold")).pack(side="left", padx=8)
        mode_text = "SIMULATION" if self.simulation else "REAL EPICS"
        mode_fg = "#d29922" if self.simulation else C.GREEN
        tk.Label(top, text=mode_text, bg="#0d1520", fg=mode_fg,
                 font=("Consolas", 9, "bold")).pack(side="left", padx=4)
        self._lbl_actual_range = tk.Label(top, text="", bg="#0d1520",
                                          fg=C.DIM, font=("Consolas", 9))
        self._lbl_actual_range.pack(side="right", padx=8)

        # main
        main = tk.Frame(self.root, bg=C.BG)
        main.pack(fill="both", expand=True, padx=6, pady=4)

        # canvas
        self._canvas_frame = tk.LabelFrame(
            main, text=" Module Map ", bg=C.BG, fg=C.ACCENT,
            font=("Consolas", 9, "bold"))
        self._canvas_frame.pack(fill="both", expand=True)

        sz = self.CANVAS_SIZE
        self._canvas = tk.Canvas(self._canvas_frame, width=sz, height=sz,
                                  bg="#0a0e14", highlightthickness=0)
        self._canvas.pack(padx=4, pady=4)
        self._canvas.bind("<Button-1>", self._on_click)

        self._compute_mapping()
        self._draw_modules()

        # colour bar legend
        leg = tk.Frame(self._canvas_frame, bg=C.BG)
        leg.pack(fill="x", padx=4, pady=(0, 4))
        tk.Label(leg, text="Low", bg=C.BG, fg=C.DIM,
                 font=("Consolas", 8)).pack(side="left")
        bar = tk.Canvas(leg, width=200, height=12, bg=C.BG,
                        highlightthickness=0)
        bar.pack(side="left", padx=4)
        for i in range(200):
            c = _val_to_color(i, 0, 199)
            bar.create_line(i, 0, i, 12, fill=c)
        tk.Label(leg, text="High", bg=C.BG, fg=C.DIM,
                 font=("Consolas", 8)).pack(side="left")

        # bottom controls
        ctrl = tk.Frame(self.root, bg=C.BG)
        ctrl.pack(fill="x", padx=6, pady=(0, 6))

        self._btn_poll = ttk.Button(ctrl, text="Polling: ON",
                                     command=self._toggle_polling)
        self._btn_poll.pack(side="left", padx=2)
        ttk.Button(ctrl, text="Refresh Now",
                   command=self._refresh).pack(side="left", padx=2)

        # Editable colour range
        tk.Label(ctrl, text="  Min:", bg=C.BG, fg=C.TEXT,
                 font=("Consolas", 9)).pack(side="left")
        self._range_min_var = tk.DoubleVar(value=0)
        tk.Entry(ctrl, textvariable=self._range_min_var, width=6,
                 bg=C.PANEL, fg=C.TEXT, font=("Consolas", 9),
                 insertbackground=C.TEXT, borderwidth=1
                 ).pack(side="left", padx=2)
        tk.Label(ctrl, text="Max:", bg=C.BG, fg=C.TEXT,
                 font=("Consolas", 9)).pack(side="left")
        self._range_max_var = tk.DoubleVar(value=1000)
        tk.Entry(ctrl, textvariable=self._range_max_var, width=6,
                 bg=C.PANEL, fg=C.TEXT, font=("Consolas", 9),
                 insertbackground=C.TEXT, borderwidth=1
                 ).pack(side="left", padx=2)

        self._lbl_info = tk.Label(ctrl, text="", bg=C.BG, fg=C.TEXT,
                                   font=("Consolas", 9))
        self._lbl_info.pack(side="right", padx=4)

    def _compute_mapping(self):
        drawn = [m for m in self.all_modules if m.mod_type != "LMS"]
        if not drawn:
            return
        x_min = min(m.x - m.sx / 2 for m in drawn)
        x_max = max(m.x + m.sx / 2 for m in drawn)
        y_min = min(m.y - m.sy / 2 for m in drawn)
        y_max = max(m.y + m.sy / 2 for m in drawn)
        usable = self.CANVAS_SIZE - 2 * self.CANVAS_PAD
        self._scale = min(usable / (x_max - x_min),
                          usable / (y_max - y_min))
        draw_w = (x_max - x_min) * self._scale
        draw_h = (y_max - y_min) * self._scale
        self._ox = self.CANVAS_PAD + (usable - draw_w) / 2
        self._oy = self.CANVAS_PAD + (usable - draw_h) / 2
        self._x_min = x_min
        self._y_max = y_max

    def _mod_rect(self, m: Module) -> Tuple[float, float, float, float]:
        cx = self._ox + (m.x - self._x_min) * self._scale
        cy = self._oy + (self._y_max - m.y) * self._scale
        hw = m.sx * self._scale * self.MOD_SHRINK / 2
        hh = m.sy * self._scale * self.MOD_SHRINK / 2
        return cx - hw, cy - hh, cx + hw, cy + hh

    def _draw_modules(self):
        self._canvas.delete("all")
        self._cell_ids.clear()
        for m in self.all_modules:
            if m.mod_type == "LMS":
                continue
            x0, y0, x1, y1 = self._mod_rect(m)
            rid = self._canvas.create_rectangle(
                x0, y0, x1, y1, fill="#15181d", outline="", width=0,
                tags=(f"mod_{m.name}",))
            self._cell_ids[m.name] = rid

    def _on_click(self, event):
        items = self._canvas.find_closest(event.x, event.y)
        if not items:
            return
        tags = self._canvas.gettags(items[0])
        for tag in tags:
            if tag.startswith("mod_"):
                name = tag[4:]
                val = self._values.get(name)
                val_str = f"{val:.1f}" if val is not None else "N/A"
                self._lbl_info.configure(text=f"{name}: {val_str}")
                # highlight
                self._canvas.delete("highlight")
                m_mods = [m for m in self.all_modules if m.name == name]
                if m_mods:
                    x0, y0, x1, y1 = self._mod_rect(m_mods[0])
                    self._canvas.create_rectangle(
                        x0, y0, x1, y1, outline=C.ACCENT, width=2,
                        tags=("highlight",))
                break

    def _refresh(self):
        # Read all values
        for m in self._scalable:
            v = self.ep.get(m.name)
            if v is not None:
                self._values[m.name] = float(v)

        # Colour range from GUI entries
        vmin = self._range_min_var.get()
        vmax = self._range_max_var.get()
        # Show actual data range in top bar
        vals = [v for v in self._values.values()]
        if vals:
            self._lbl_actual_range.configure(
                text=f"Data: {min(vals):.0f} .. {max(vals):.0f}")
        else:
            self._lbl_actual_range.configure(text="")

        # Update colours
        for m in self._scalable:
            rid = self._cell_ids.get(m.name)
            if rid is None:
                continue
            v = self._values.get(m.name)
            if v is not None:
                color = _val_to_color(v, vmin, vmax)
            else:
                color = "#15181d"
            self._canvas.itemconfigure(rid, fill=color)

        # Schedule next poll
        if self._polling:
            self._poll_id = self.root.after(POLL_INTERVAL_MS, self._refresh)

    def _toggle_polling(self):
        self._polling = not self._polling
        if self._polling:
            self._btn_poll.configure(text="Polling: ON")
            self._refresh()
        else:
            self._btn_poll.configure(text="Polling: OFF")
            if hasattr(self, '_poll_id'):
                self.root.after_cancel(self._poll_id)


# ============================================================================
#  MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="HyCal FADC Scaler Map")
    parser.add_argument("--sim", action="store_true",
                        help="Simulation mode (random values, no EPICS)")
    parser.add_argument("--database", default=DEFAULT_DB_PATH,
                        help="Path to hycal_modules.json")
    args = parser.parse_args()

    modules = load_modules(args.database)
    print(f"Loaded {len(modules)} modules")

    simulation = args.sim
    if simulation:
        ep = SimulatedScalerEPICS(modules)
    else:
        ep = RealScalerEPICS(modules)

    root = tk.Tk()
    ScalerMapGUI(root, modules, ep, simulation)
    root.mainloop()


if __name__ == "__main__":
    main()
