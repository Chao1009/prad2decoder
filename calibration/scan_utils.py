"""
Shared types, constants, and helpers for HyCal calibration tools.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, List, Tuple


# ============================================================================
#  CONSTANTS
# ============================================================================

# Transporter coordinates when the beam hits HyCal centre (0, 0)
BEAM_CENTER_X: float = -126.75   # mm
BEAM_CENTER_Y: float = 10.11     # mm

# Transporter travel limits (symmetric about centre)
_LIMIT_RB_X = -582.65
_LIMIT_RB_Y = -672.50
PTRANS_X_MIN = _LIMIT_RB_X
PTRANS_X_MAX = 2 * BEAM_CENTER_X - _LIMIT_RB_X
PTRANS_Y_MIN = _LIMIT_RB_Y
PTRANS_Y_MAX = 2 * BEAM_CENTER_Y - _LIMIT_RB_Y

# Default database path (relative to this file → ../database/)
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "database", "hycal_modules.json")


# ============================================================================
#  COLOUR PALETTE (dark control-room theme)
# ============================================================================

class C:
    BG       = "#0d1117"
    PANEL    = "#161b22"
    BORDER   = "#30363d"
    TEXT     = "#c9d1d9"
    DIM      = "#8b949e"
    ACCENT   = "#58a6ff"
    GREEN    = "#3fb950"
    YELLOW   = "#d29922"
    RED      = "#f85149"
    ORANGE   = "#db6d28"
    # canvas module states
    MOD_TODO      = "#21262d"
    MOD_CURRENT   = "#d29922"
    MOD_DWELL     = "#3fb950"
    MOD_DONE      = "#1f6feb"
    MOD_ERROR     = "#f85149"
    MOD_SELECTED  = "#db6d28"
    MOD_INPATH    = "#3fb950"
    # display-only / inactive
    MOD_GLASS     = "#162230"
    MOD_PWO4_BG   = "#1a2a1a"
    MOD_LMS       = "#2d1f3d"
    MOD_EXCLUDED  = "#111418"
    MOD_SKIPPED   = "#15181d"
    PATH_LINE     = "#30506e"


# ============================================================================
#  MODULE DATA
# ============================================================================

@dataclass
class Module:
    name: str
    mod_type: str      # "PbWO4", "PbGlass", "LMS"
    x: float           # centre x in HyCal frame (mm)
    y: float           # centre y in HyCal frame (mm)
    sx: float          # module width  (mm)
    sy: float          # module height (mm)
    sector: str = ""   # "Center", "Top", "Right", "Bottom", "Left", "LMS"
    row: int = 0       # row within sector (1-indexed)
    col: int = 0       # col within sector (1-indexed)


def load_modules(json_path: str = DEFAULT_DB_PATH) -> List[Module]:
    """Load all modules from the HyCal module database JSON."""
    with open(json_path) as f:
        data = json.load(f)
    modules: List[Module] = []
    for entry in data:
        modules.append(Module(
            name=entry["n"], mod_type=entry["t"],
            x=entry["x"], y=entry["y"],
            sx=entry["sx"], sy=entry["sy"],
            sector=entry.get("sec", ""),
            row=entry.get("row", 0), col=entry.get("col", 0),
        ))
    return modules


# ============================================================================
#  COORDINATE TRANSFORMS
# ============================================================================

def module_to_ptrans(mx: float, my: float) -> Tuple[float, float]:
    """HyCal-frame module centre --> transporter set-point."""
    return (BEAM_CENTER_X + mx, BEAM_CENTER_Y - my)


def ptrans_to_module(px: float, py: float) -> Tuple[float, float]:
    """Transporter position --> beam position on HyCal (HyCal-frame)."""
    return (px - BEAM_CENTER_X, BEAM_CENTER_Y - py)


def ptrans_in_limits(x: float, y: float) -> bool:
    """Check if a transporter position is within travel limits."""
    return (PTRANS_X_MIN <= x <= PTRANS_X_MAX and
            PTRANS_Y_MIN <= y <= PTRANS_Y_MAX)


# ============================================================================
#  LG LAYER FILTER
# ============================================================================

def filter_scan_modules(all_modules: List[Module], lg_layers: int,
                        lg_sx: float = 38.15, lg_sy: float = 38.15
                        ) -> List[Module]:
    """All PbWO4 + PbGlass within lg_layers of PbWO4 bounding box."""
    pwo4 = [m for m in all_modules if m.mod_type == "PbWO4"]
    if not pwo4:
        return list(all_modules)
    min_x = min(m.x for m in pwo4)
    max_x = max(m.x for m in pwo4)
    min_y = min(m.y for m in pwo4)
    max_y = max(m.y for m in pwo4)
    scan = list(pwo4)
    if lg_layers > 0:
        mx = lg_layers * lg_sx
        my = lg_layers * lg_sy
        for m in all_modules:
            if m.mod_type == "PbGlass" and \
               min_x - mx <= m.x <= max_x + mx and \
               min_y - my <= m.y <= max_y + my:
                scan.append(m)
    return scan
