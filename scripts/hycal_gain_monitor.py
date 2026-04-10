#!/usr/bin/env python3
"""
HyCal Gain Monitor (PyQt6)
==========================
Visualises LMS-based gain factors across runs for all HyCal modules.
Reads text-based ``prad_{:06d}_LMS.dat`` files produced by the offline
gain analysis, displays a colour-coded HyCal geo map, LMS reference
channel stability charts, and a table of irregular (module, run) entries.

Usage
-----
    python scripts/hycal_gain_monitor.py
"""

from __future__ import annotations

import glob
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QLineEdit, QDoubleSpinBox,
    QFileDialog, QSplitter, QSizePolicy, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QToolTip,
)
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QSize
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont, QLinearGradient, QPalette,
)


# ===========================================================================
#  Paths & constants
# ===========================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
DB_DIR = SCRIPT_DIR / ".." / "database"
MODULES_JSON = DB_DIR / "hycal_modules.json"

LMS_NAMES = ["LMS1", "LMS2", "LMS3"]
LMS_REF_DEFAULT = 1          # index into LMS_NAMES -> "LMS2"
FILE_PATTERN = re.compile(r"prad_(\d{6})_LMS\.dat$")


# ===========================================================================
#  Module database (self-contained, mirrors calibration/scan_utils.py)
# ===========================================================================

class Module:
    __slots__ = ("name", "mod_type", "x", "y", "sx", "sy")

    def __init__(self, name, mod_type, x, y, sx, sy):
        self.name = name
        self.mod_type = mod_type
        self.x = x
        self.y = y
        self.sx = sx
        self.sy = sy


def load_modules(path: Path = MODULES_JSON) -> List[Module]:
    import json
    with open(path) as f:
        data = json.load(f)
    return [Module(e["n"], e["t"], e["x"], e["y"], e["sx"], e["sy"])
            for e in data]


# ===========================================================================
#  Colour palettes
# ===========================================================================

PALETTES = {
    "viridis": [
        (0.00, (68, 1, 84)), (0.25, (59, 82, 139)),
        (0.50, (33, 145, 140)), (0.75, (94, 201, 98)),
        (1.00, (253, 231, 37)),
    ],
    "inferno": [
        (0.00, (0, 0, 4)), (0.25, (120, 28, 109)),
        (0.50, (229, 89, 52)), (0.75, (253, 198, 39)),
        (1.00, (252, 255, 164)),
    ],
    "coolwarm": [
        (0.00, (59, 76, 192)), (0.25, (141, 176, 254)),
        (0.50, (221, 221, 221)), (0.75, (245, 148, 114)),
        (1.00, (180, 4, 38)),
    ],
    "hot": [
        (0.00, (11, 0, 0)), (0.33, (230, 0, 0)),
        (0.66, (255, 210, 0)), (1.00, (255, 255, 255)),
    ],
    "rainbow": [
        (0.00, (30, 58, 95)), (0.25, (59, 130, 246)),
        (0.50, (45, 212, 160)), (0.75, (234, 179, 8)),
        (1.00, (245, 101, 101)),
    ],
}
PALETTE_NAMES = list(PALETTES.keys())


def _lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def _cmap_qcolor(t: float, stops) -> QColor:
    t = max(0.0, min(1.0, t))
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t <= t1:
            s = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return QColor(_lerp(c0[0], c1[0], s),
                          _lerp(c0[1], c1[1], s),
                          _lerp(c0[2], c1[2], s))
    _, c = stops[-1]
    return QColor(*c)


# ===========================================================================
#  Data structures
# ===========================================================================

@dataclass
class LMSRecord:
    alpha_peak: float
    alpha_sigma: float
    alpha_chi2ndf: float
    lms_peak: float
    lms_sigma: float
    lms_chi2ndf: float


@dataclass
class ModuleRecord:
    lms_peak: float
    lms_sigma: float
    lms_chi2ndf: float
    gain_factors: Tuple[float, float, float]


@dataclass
class RunData:
    run_number: int
    lms: Dict[str, LMSRecord] = field(default_factory=dict)
    modules: Dict[str, ModuleRecord] = field(default_factory=dict)


@dataclass
class IrregularEntry:
    name: str
    mod_type: str
    run_number: int
    gain: float
    mean_gain: float
    std_dev: float
    deviation_sigma: float


# ===========================================================================
#  File parsing
# ===========================================================================

def parse_dat_file(filepath: str) -> Optional[RunData]:
    """Parse a single prad_NNNNNN_LMS.dat file."""
    basename = os.path.basename(filepath)
    m = FILE_PATTERN.search(basename)
    if not m:
        return None
    run_number = int(m.group(1))
    rd = RunData(run_number=run_number)

    try:
        with open(filepath) as f:
            lines = f.readlines()
    except OSError:
        return None

    if len(lines) < 3:
        return None

    # First 3 lines: LMS reference channels
    for i in range(3):
        parts = re.split(r'[,\s\t]+', lines[i].strip())
        if len(parts) < 7:
            continue
        try:
            name = parts[0].strip()
            rd.lms[name] = LMSRecord(
                alpha_peak=float(parts[1]),
                alpha_sigma=float(parts[2]),
                alpha_chi2ndf=float(parts[3]),
                lms_peak=float(parts[4]),
                lms_sigma=float(parts[5]),
                lms_chi2ndf=float(parts[6]),
            )
        except (ValueError, IndexError):
            continue

    # Remaining lines: module data
    for line in lines[3:]:
        parts = re.split(r'[,\s\t]+', line.strip())
        if len(parts) < 7:
            continue
        try:
            name = parts[0].strip()
            rd.modules[name] = ModuleRecord(
                lms_peak=float(parts[1]),
                lms_sigma=float(parts[2]),
                lms_chi2ndf=float(parts[3]),
                gain_factors=(float(parts[4]), float(parts[5]), float(parts[6])),
            )
        except (ValueError, IndexError):
            continue

    return rd


def load_all_runs(folder: str) -> List[RunData]:
    """Scan folder for prad_*_LMS.dat files, parse all, sort by run number."""
    pattern = os.path.join(folder, "prad_*_LMS.dat")
    files = sorted(glob.glob(pattern))
    runs: List[RunData] = []
    for f in files:
        rd = parse_dat_file(f)
        if rd is not None:
            runs.append(rd)
    runs.sort(key=lambda r: r.run_number)
    return runs


# ===========================================================================
#  Outlier detection
# ===========================================================================

def compute_irregular_entries(
    runs: List[RunData],
    ref_idx: int,
    mod_by_name: Dict[str, Module],
    sigma_threshold: float = 3.0,
    min_runs: int = 5,
) -> List[IrregularEntry]:
    """Find (module, run) pairs with outlier gain factors."""

    # Collect gain values per module across all runs
    # module_name -> [(run_number, gain)]
    all_gains: Dict[str, List[Tuple[int, float]]] = {}
    for rd in runs:
        for mname, mrec in rd.modules.items():
            gains = all_gains.setdefault(mname, [])
            gains.append((rd.run_number, mrec.gain_factors[ref_idx]))

    entries: List[IrregularEntry] = []
    for mname, gains_list in all_gains.items():
        if len(gains_list) < min_runs:
            continue
        values = [g for _, g in gains_list]
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std = math.sqrt(variance) if variance > 0 else 0.0
        if std == 0:
            continue

        mod = mod_by_name.get(mname)
        mod_type = mod.mod_type if mod else "?"

        for run_num, gain in gains_list:
            dev = abs(gain - mean) / std
            if dev > sigma_threshold:
                entries.append(IrregularEntry(
                    name=mname,
                    mod_type=mod_type,
                    run_number=run_num,
                    gain=gain,
                    mean_gain=mean,
                    std_dev=std,
                    deviation_sigma=dev,
                ))

    entries.sort(key=lambda e: (e.name, e.run_number))
    return entries


# ===========================================================================
#  HyCal Gain Map Widget
# ===========================================================================

class HyCalGainMapWidget(QWidget):
    """Colour-coded HyCal module map with zoom/pan and hover tooltips."""

    moduleHovered = pyqtSignal(str)
    paletteClicked = pyqtSignal()

    _SHRINK = 0.90
    _CLICK_THRESHOLD = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMinimumSize(400, 400)

        self._modules: List[Module] = []
        self._values: Dict[str, float] = {}
        self._vmin = 0.0
        self._vmax = 1.0
        self._log_scale = False
        self._palette_idx = 0
        self._hovered: Optional[str] = None
        self._rects: Dict[str, QRectF] = {}
        self._cb_rect: Optional[QRectF] = None
        self._layout_dirty = True

        # zoom / pan
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._drag_last: Optional[QPointF] = None
        self._drag_origin: Optional[QPointF] = None
        self._dragging = False

        # overlay reset button (top-right corner)
        self._reset_btn = QPushButton("Reset", self)
        self._reset_btn.setFixedSize(52, 24)
        self._reset_btn.setStyleSheet(
            "QPushButton{background:rgba(22,27,34,200);color:#8b949e;"
            "border:1px solid #30363d;font:bold 9px Consolas;border-radius:3px;}"
            "QPushButton:hover{background:rgba(33,38,45,220);color:#c9d1d9;}")
        self._reset_btn.clicked.connect(self.reset_view)

    # -- public API --

    def set_modules(self, modules: List[Module]):
        self._modules = [m for m in modules if m.mod_type != "LMS"]
        self._layout_dirty = True
        self.update()

    def set_gain_data(self, values: Dict[str, float],
                      vmin: float, vmax: float):
        self._values = values
        self._vmin = vmin
        self._vmax = vmax
        self.update()

    def set_log_scale(self, on: bool):
        self._log_scale = on
        self.update()

    def set_palette(self, idx: int):
        self._palette_idx = idx % len(PALETTES)
        self.update()

    def reset_view(self):
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._layout_dirty = True
        self.update()

    # -- layout --

    def _recompute_layout(self):
        self._rects.clear()
        if not self._modules:
            return
        w, h = self.width(), self.height()
        margin, top, bot = 12, 8, 50
        pw, ph = w - 2 * margin, h - top - bot

        x0 = min(m.x - m.sx / 2 for m in self._modules)
        x1 = max(m.x + m.sx / 2 for m in self._modules)
        y0 = min(m.y - m.sy / 2 for m in self._modules)
        y1 = max(m.y + m.sy / 2 for m in self._modules)

        base_scale = min(pw / (x1 - x0), ph / (y1 - y0))
        sc = base_scale * self._zoom
        dw, dh = (x1 - x0) * sc, (y1 - y0) * sc
        ox = margin + (pw - dw) / 2 + self._pan_x
        oy = top + (ph - dh) / 2 + self._pan_y
        shrink = self._SHRINK

        self._geo_x0 = x0
        self._geo_y1 = y1
        self._geo_sc = sc
        self._geo_ox = ox
        self._geo_oy = oy

        for m in self._modules:
            mw, mh = m.sx * sc * shrink, m.sy * sc * shrink
            cx = ox + (m.x - x0) * sc
            cy = oy + (y1 - m.y) * sc
            self._rects[m.name] = QRectF(cx - mw / 2, cy - mh / 2, mw, mh)
        self._layout_dirty = False

    def resizeEvent(self, event):
        self._layout_dirty = True
        # keep reset button in top-right corner
        self._reset_btn.move(self.width() - self._reset_btn.width() - 6, 6)
        super().resizeEvent(event)

    # -- painting --

    def paintEvent(self, event):
        if self._layout_dirty:
            self._recompute_layout()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#0a0e14"))

        if not self._rects:
            if not self._values:
                p.setPen(QColor("#555555"))
                p.setFont(QFont("Consolas", 12))
                p.drawText(QRectF(0, 0, w, h),
                           Qt.AlignmentFlag.AlignCenter, "No data loaded")
            p.end()
            return

        stops = list(PALETTES.values())[self._palette_idx]
        vmin, vmax = self._vmin, self._vmax
        log_scale = self._log_scale
        no_data = QColor("#1a1a2e")

        # precompute log bounds
        if log_scale:
            log_lo = math.log10(max(vmin, 1e-9))
            log_hi = math.log10(max(vmax, vmin * 10, 1e-8))

        for name, rect in self._rects.items():
            v = self._values.get(name)
            if v is not None:
                if log_scale:
                    lv = math.log10(max(v, 1e-9))
                    t = (lv - log_lo) / (log_hi - log_lo) if log_hi > log_lo else 0.5
                else:
                    t = ((v - vmin) / (vmax - vmin)) if vmax > vmin else 0.5
                p.fillRect(rect, _cmap_qcolor(t, stops))
            else:
                p.fillRect(rect, no_data)

        # hover highlight
        if self._hovered and self._hovered in self._rects:
            p.setPen(QPen(QColor("#58a6ff"), 2.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self._rects[self._hovered])

        # colour bar
        cb_w = min(300, w - 80)
        cb_h = 14
        cb_x = (w - cb_w) / 2
        cb_y = h - 40
        self._cb_rect = QRectF(cb_x, cb_y, cb_w, cb_h)

        grad = QLinearGradient(cb_x, 0, cb_x + cb_w, 0)
        for t, (r, g, b) in stops:
            grad.setColorAt(t, QColor(r, g, b))
        p.fillRect(self._cb_rect, QBrush(grad))
        p.setPen(QPen(QColor("#58a6ff"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(self._cb_rect)

        # range labels + palette name
        p.setPen(QColor("#8b949e"))
        p.setFont(QFont("Consolas", 9))
        p.drawText(QRectF(cb_x, cb_y + cb_h + 2, 80, 14),
                   Qt.AlignmentFlag.AlignLeft, f"{vmin:.4f}")
        p.drawText(QRectF(cb_x + cb_w - 80, cb_y + cb_h + 2, 80, 14),
                   Qt.AlignmentFlag.AlignRight, f"{vmax:.4f}")
        pname = PALETTE_NAMES[self._palette_idx]
        p.drawText(QRectF(cb_x, cb_y + cb_h + 2, cb_w, 14),
                   Qt.AlignmentFlag.AlignCenter, pname)
        p.end()

    # -- mouse events (zoom/pan from scan_geoview pattern) --

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton:
            self.reset_view()
            return
        if e.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self._drag_last = e.position()
            self._drag_origin = e.position()
            self._dragging = False

    def mouseReleaseEvent(self, e):
        if e.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            if self._dragging:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            elif e.button() == Qt.MouseButton.LeftButton:
                # click on colour bar -> cycle palette
                if self._cb_rect and self._cb_rect.contains(e.position()):
                    self.paletteClicked.emit()
            self._drag_last = None
            self._drag_origin = None
            self._dragging = False

    def mouseMoveEvent(self, e):
        # drag
        if self._drag_last is not None:
            pos = e.position()
            if not self._dragging:
                dx = pos.x() - self._drag_origin.x()
                dy = pos.y() - self._drag_origin.y()
                if dx * dx + dy * dy > self._CLICK_THRESHOLD ** 2:
                    self._dragging = True
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._dragging:
                self._pan_x += pos.x() - self._drag_last.x()
                self._pan_y += pos.y() - self._drag_last.y()
                self._drag_last = pos
                self._layout_dirty = True
                self.update()
            return

        # hover tooltip
        pos = e.position()
        # hand cursor over colour bar
        if self._cb_rect and self._cb_rect.contains(pos):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

        found = None
        for name in reversed(list(self._rects)):
            if self._rects[name].contains(pos):
                found = name
                break
        if found != self._hovered:
            self._hovered = found
            self.update()
            if found:
                v = self._values.get(found)
                tip = f"{found}: {v:.5f}" if v is not None else found
                QToolTip.showText(e.globalPosition().toPoint(), tip, self)
                self.moduleHovered.emit(found)
            else:
                QToolTip.hideText()

    def wheelEvent(self, e):
        factor = 1.15 if e.angleDelta().y() > 0 else 1.0 / 1.15
        new_zoom = max(0.5, min(self._zoom * factor, 20.0))
        if new_zoom == self._zoom:
            return
        pos = e.position()
        ratio = new_zoom / self._zoom
        self._pan_x = pos.x() + (self._pan_x - pos.x()) * ratio
        self._pan_y = pos.y() + (self._pan_y - pos.y()) * ratio
        self._zoom = new_zoom
        self._layout_dirty = True
        self.update()

    def sizeHint(self):
        return QSize(680, 680)


# ===========================================================================
#  LMS Line Chart Widget
# ===========================================================================

class LMSLineChartWidget(QWidget):
    """Line chart with error bars for LMS peak/alpha ratio vs run number."""

    PAD_L, PAD_R, PAD_T, PAD_B = 60, 16, 24, 32

    def __init__(self, parent=None):
        super().__init__(parent)
        self._run_numbers: List[int] = []
        self._ratios: List[float] = []
        self._errors: List[float] = []
        self._title: str = ""
        self.setMinimumHeight(100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_data(self, run_numbers: List[int], ratios: List[float],
                 errors: List[float], title: str):
        self._run_numbers = run_numbers
        self._ratios = ratios
        self._errors = errors
        self._title = title
        self.update()

    @staticmethod
    def _nice_ticks(lo: float, hi: float, max_ticks: int = 6):
        """Compute nice tick values for an axis range."""
        if hi <= lo:
            return [lo]
        raw = (hi - lo) / max(max_ticks - 1, 1)
        mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
        candidates = [1, 2, 2.5, 5, 10]
        step = mag
        for c in candidates:
            if c * mag >= raw:
                step = c * mag
                break
        start = math.ceil(lo / step) * step
        ticks = []
        v = start
        while v <= hi + step * 0.01:
            ticks.append(v)
            v += step
        return ticks

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#0a0e14"))

        # title
        p.setPen(QColor("#58a6ff"))
        p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        p.drawText(QRectF(self.PAD_L, 2, w - self.PAD_L - self.PAD_R, 20),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._title)

        runs = self._run_numbers
        ratios = self._ratios
        errors = self._errors
        if not runs or not ratios:
            p.setPen(QColor("#555555"))
            p.setFont(QFont("Consolas", 10))
            p.drawText(QRectF(0, 0, w, h),
                       Qt.AlignmentFlag.AlignCenter, "No data")
            p.end()
            return

        # plot area
        px = self.PAD_L
        py = self.PAD_T
        pw = w - self.PAD_L - self.PAD_R
        ph = h - self.PAD_T - self.PAD_B
        if pw < 20 or ph < 20:
            p.end()
            return

        # data ranges
        x_min, x_max = runs[0], runs[-1]
        if x_min == x_max:
            x_min -= 1
            x_max += 1

        y_vals = []
        for i, r in enumerate(ratios):
            y_vals.append(r)
            if i < len(errors):
                y_vals.append(r + errors[i])
                y_vals.append(r - errors[i])
        y_lo = min(y_vals)
        y_hi = max(y_vals)
        margin = (y_hi - y_lo) * 0.1 if y_hi > y_lo else 0.05
        y_lo -= margin
        y_hi += margin

        def to_sx(v):
            return px + (v - x_min) / (x_max - x_min) * pw

        def to_sy(v):
            return py + ph - (v - y_lo) / (y_hi - y_lo) * ph

        # grid + axes
        p.setPen(QPen(QColor("#21262d"), 1, Qt.PenStyle.DotLine))
        y_ticks = self._nice_ticks(y_lo, y_hi, 5)
        for yt in y_ticks:
            sy = to_sy(yt)
            p.drawLine(QPointF(px, sy), QPointF(px + pw, sy))

        # axes border
        p.setPen(QPen(QColor("#30363d"), 1))
        p.drawLine(QPointF(px, py), QPointF(px, py + ph))
        p.drawLine(QPointF(px, py + ph), QPointF(px + pw, py + ph))

        # y-axis labels
        p.setPen(QColor("#8b949e"))
        p.setFont(QFont("Consolas", 8))
        for yt in y_ticks:
            sy = to_sy(yt)
            p.drawText(QRectF(0, sy - 8, self.PAD_L - 4, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{yt:.3f}")

        # x-axis labels (skip some if too dense)
        max_labels = max(pw // 60, 2)
        step = max(len(runs) // max_labels, 1)
        for i in range(0, len(runs), step):
            sx = to_sx(runs[i])
            p.drawText(QRectF(sx - 30, py + ph + 2, 60, 18),
                       Qt.AlignmentFlag.AlignCenter, str(runs[i]))

        # error bars
        p.setPen(QPen(QColor("#8b949e"), 1))
        cap = 3
        for i in range(len(runs)):
            if i >= len(ratios):
                break
            sx = to_sx(runs[i])
            r = ratios[i]
            err = errors[i] if i < len(errors) else 0
            sy_top = to_sy(r + err)
            sy_bot = to_sy(r - err)
            p.drawLine(QPointF(sx, sy_top), QPointF(sx, sy_bot))
            p.drawLine(QPointF(sx - cap, sy_top), QPointF(sx + cap, sy_top))
            p.drawLine(QPointF(sx - cap, sy_bot), QPointF(sx + cap, sy_bot))

        # connecting line
        p.setPen(QPen(QColor("#58a6ff"), 1.5))
        for i in range(len(runs) - 1):
            if i + 1 >= len(ratios):
                break
            p.drawLine(QPointF(to_sx(runs[i]), to_sy(ratios[i])),
                       QPointF(to_sx(runs[i + 1]), to_sy(ratios[i + 1])))

        # points
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#58a6ff"))
        for i in range(len(runs)):
            if i >= len(ratios):
                break
            p.drawEllipse(QPointF(to_sx(runs[i]), to_sy(ratios[i])), 3, 3)

        p.end()


# ===========================================================================
#  Irregular channels table
# ===========================================================================

class IrregularTableWidget(QWidget):
    """Table of (module, run) outlier entries with search and type filter."""

    runClicked = pyqtSignal(int)  # emits run number when a row is clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: List[IrregularEntry] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # filter bar
        fbar = QHBoxLayout()
        fbar.setSpacing(6)

        lbl = QLabel("Search:")
        lbl.setFont(QFont("Consolas", 10))
        lbl.setStyleSheet("color:#c9d1d9;")
        fbar.addWidget(lbl)

        self._search = QLineEdit()
        self._search.setPlaceholderText("module name...")
        self._search.setFixedWidth(120)
        self._search.setFont(QFont("Consolas", 10))
        self._search.setStyleSheet(
            "QLineEdit{background:#161b22;color:#c9d1d9;"
            "border:1px solid #30363d;border-radius:3px;padding:2px 4px;}")
        self._search.textChanged.connect(self._apply_filter)
        fbar.addWidget(self._search)

        lbl2 = QLabel("Type:")
        lbl2.setFont(QFont("Consolas", 10))
        lbl2.setStyleSheet("color:#c9d1d9;")
        fbar.addWidget(lbl2)

        self._type_filter = QComboBox()
        self._type_filter.addItems(["All", "PbWO4", "PbGlass"])
        self._type_filter.setFixedWidth(100)
        self._type_filter.setFont(QFont("Consolas", 10))
        self._type_filter.setStyleSheet(
            "QComboBox{background:#161b22;color:#c9d1d9;"
            "border:1px solid #30363d;border-radius:3px;padding:2px 6px;}"
            "QComboBox::drop-down{border:none;width:18px;}"
            "QComboBox::down-arrow{border-left:4px solid transparent;"
            "border-right:4px solid transparent;border-top:5px solid #8b949e;"
            "margin-right:4px;}"
            "QComboBox QAbstractItemView{background:#161b22;color:#c9d1d9;"
            "border:1px solid #30363d;selection-background-color:#1f6feb;}")
        self._type_filter.currentIndexChanged.connect(
            lambda _: self._apply_filter())
        fbar.addWidget(self._type_filter)

        fbar.addStretch()

        count_lbl = QLabel("")
        count_lbl.setFont(QFont("Consolas", 10))
        count_lbl.setStyleSheet("color:#8b949e;")
        self._count_lbl = count_lbl
        fbar.addWidget(count_lbl)

        layout.addLayout(fbar)

        # table
        self._table = QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels(
            ["Module", "Type", "Run", "Gain", "Mean", "Std Dev", "Dev (sigma)"])
        self._table.setFont(QFont("Consolas", 10))
        self._table.setStyleSheet(
            "QTableWidget{background:#0d1117;color:#c9d1d9;"
            "gridline-color:#21262d;border:1px solid #30363d;}"
            "QTableWidget::item{padding:2px 6px;}"
            "QHeaderView::section{background:#161b22;color:#58a6ff;"
            "border:1px solid #30363d;font:bold 10pt Consolas;padding:4px;}")
        self._table.setAlternatingRowColors(True)
        pal = self._table.palette()
        pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#131820"))
        self._table.setPalette(pal)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSortingEnabled(True)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(24)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        layout.addWidget(self._table)

    def set_data(self, entries: List[IrregularEntry]):
        self._entries = entries
        self._apply_filter()

    def _apply_filter(self):
        search = self._search.text().strip().upper()
        type_sel = self._type_filter.currentText()

        filtered = []
        for e in self._entries:
            if search and search not in e.name.upper():
                continue
            if type_sel == "PbWO4" and e.mod_type != "PbWO4":
                continue
            if type_sel == "PbGlass" and e.mod_type != "PbGlass":
                continue
            filtered.append(e)

        self._count_lbl.setText(f"irregular gains: {len(filtered)} entries")
        self._populate_table(filtered)

    def _populate_table(self, entries: List[IrregularEntry]):
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            # Module name — amber to indicate issue
            item = QTableWidgetItem(e.name)
            item.setForeground(QColor("#d29922"))
            self._table.setItem(row, 0, item)

            self._table.setItem(row, 1, QTableWidgetItem(e.mod_type))

            # Run number — sortable numerically
            item_run = QTableWidgetItem()
            item_run.setData(Qt.ItemDataRole.DisplayRole, e.run_number)
            self._table.setItem(row, 2, item_run)

            for col, val in [(3, e.gain), (4, e.mean_gain),
                             (5, e.std_dev), (6, e.deviation_sigma)]:
                item_f = QTableWidgetItem()
                item_f.setData(Qt.ItemDataRole.DisplayRole, round(val, 5))
                self._table.setItem(row, col, item_f)

        self._table.setSortingEnabled(True)

    def _on_cell_clicked(self, row: int, _col: int):
        item = self._table.item(row, 2)  # Run column
        if item is not None:
            run_num = item.data(Qt.ItemDataRole.DisplayRole)
            if isinstance(run_num, int):
                self.runClicked.emit(run_num)


# ===========================================================================
#  Main window
# ===========================================================================

class GainMonitorWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self._all_modules: List[Module] = []
        self._mod_by_name: Dict[str, Module] = {}
        self._runs: List[RunData] = []
        self._current_run_idx: int = 0
        self._current_ref_idx: int = LMS_REF_DEFAULT
        self._palette_idx = 0
        self._auto_range = True
        self._manual_vmin = 0.9
        self._manual_vmax = 1.1
        self._log_scale = False

        self._load_geometry()
        self._build_ui()

    def _load_geometry(self):
        self._all_modules = load_modules()
        self._mod_by_name = {m.name: m for m in self._all_modules}

    # ---- UI ----

    def _build_ui(self):
        self.setWindowTitle("HyCal Gain Monitor")
        self.resize(1600, 1000)
        self._apply_dark_palette()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # -- top bar --
        top = QHBoxLayout()
        lbl = QLabel("HYCAL GAIN MONITOR")
        lbl.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
        lbl.setStyleSheet("color:#58a6ff;")
        top.addWidget(lbl)
        top.addStretch()

        self._process_btn = self._make_btn(
            "Process Folder...", "#58a6ff", self._on_process_folder)
        top.addWidget(self._process_btn)

        self._status_lbl = QLabel("No data loaded")
        self._status_lbl.setFont(QFont("Consolas", 11))
        self._status_lbl.setStyleSheet("color:#8b949e;")
        top.addWidget(self._status_lbl)
        root.addLayout(top)

        # -- body splitter (horizontal: left=map, right=charts+table) --
        body = QSplitter(Qt.Orientation.Horizontal)

        # ---- left panel ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        # controls
        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)

        ctrl.addWidget(self._slabel("Ref:"))
        self._ref_combo = QComboBox()
        self._ref_combo.addItems(LMS_NAMES)
        self._ref_combo.setCurrentIndex(LMS_REF_DEFAULT)
        self._ref_combo.setFixedWidth(90)
        self._ref_combo.setFont(QFont("Consolas", 10))
        self._ref_combo.setStyleSheet(
            "QComboBox{background:#161b22;color:#c9d1d9;"
            "border:1px solid #30363d;border-radius:3px;padding:2px 6px;}"
            "QComboBox::drop-down{border:none;width:18px;}"
            "QComboBox::down-arrow{border-left:4px solid transparent;"
            "border-right:4px solid transparent;border-top:5px solid #8b949e;"
            "margin-right:4px;}"
            "QComboBox QAbstractItemView{background:#161b22;color:#c9d1d9;"
            "border:1px solid #30363d;selection-background-color:#1f6feb;}")
        self._ref_combo.currentIndexChanged.connect(self._on_ref_changed)
        ctrl.addWidget(self._ref_combo)

        ctrl.addSpacing(10)
        ctrl.addWidget(self._slabel("Run:"))

        self._run_combo = QComboBox()
        self._run_combo.setMinimumWidth(100)
        self._run_combo.setFont(QFont("Consolas", 10))
        self._run_combo.setStyleSheet(
            "QComboBox{background:#161b22;color:#c9d1d9;"
            "border:1px solid #30363d;border-radius:3px;padding:2px 6px;}"
            "QComboBox::drop-down{border:none;width:18px;}"
            "QComboBox::down-arrow{border-left:4px solid transparent;"
            "border-right:4px solid transparent;border-top:5px solid #8b949e;"
            "margin-right:4px;}"
            "QComboBox QAbstractItemView{background:#161b22;color:#c9d1d9;"
            "border:1px solid #30363d;selection-background-color:#1f6feb;}")
        self._run_combo.currentIndexChanged.connect(self._on_run_changed)
        ctrl.addWidget(self._run_combo)

        self._prev_btn = self._make_btn("<", "#c9d1d9", self._on_prev_run)
        self._prev_btn.setFixedWidth(30)
        ctrl.addWidget(self._prev_btn)

        self._next_btn = self._make_btn(">", "#c9d1d9", self._on_next_run)
        self._next_btn.setFixedWidth(30)
        ctrl.addWidget(self._next_btn)

        ctrl.addStretch()
        left_layout.addLayout(ctrl)

        # range controls
        rng = QHBoxLayout()
        rng.setSpacing(6)

        _EDIT_SS = ("QLineEdit{background:#161b22;color:#c9d1d9;"
                    "border:1px solid #30363d;border-radius:3px;padding:2px 4px;}")

        rng.addWidget(self._slabel("Min:"))
        self._range_min = QLineEdit("0.9")
        self._range_min.setFixedWidth(70)
        self._range_min.setFont(QFont("Consolas", 10))
        self._range_min.setStyleSheet(_EDIT_SS)
        self._range_min.returnPressed.connect(self._on_apply_range)
        rng.addWidget(self._range_min)

        rng.addWidget(self._slabel("Max:"))
        self._range_max = QLineEdit("1.1")
        self._range_max.setFixedWidth(70)
        self._range_max.setFont(QFont("Consolas", 10))
        self._range_max.setStyleSheet(_EDIT_SS)
        self._range_max.returnPressed.connect(self._on_apply_range)
        rng.addWidget(self._range_max)

        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setFixedWidth(55)
        self._apply_btn.clicked.connect(self._on_apply_range)
        rng.addWidget(self._apply_btn)

        self._log_btn = QPushButton("Log")
        self._log_btn.setFixedWidth(45)
        self._log_btn.setCheckable(True)
        self._log_btn.clicked.connect(self._on_log_toggled)
        rng.addWidget(self._log_btn)

        self._auto_btn = QPushButton("Auto")
        self._auto_btn.setFixedWidth(55)
        self._auto_btn.setCheckable(True)
        self._auto_btn.setChecked(True)
        self._auto_btn.clicked.connect(self._on_auto_range)
        rng.addWidget(self._auto_btn)

        # common toggle-button style
        _TOGGLE_SS = (
            "QPushButton{background:#21262d;color:#c9d1d9;"
            "border:1px solid #30363d;padding:4px 8px;"
            "font:bold 11px Consolas;border-radius:3px;}"
            "QPushButton:hover{background:#30363d;}"
            "QPushButton:checked{background:#1f6feb;color:white;"
            "border-color:#388bfd;}")
        self._apply_btn.setStyleSheet(
            "QPushButton{background:#21262d;color:#c9d1d9;"
            "border:1px solid #30363d;padding:4px 8px;"
            "font:bold 11px Consolas;border-radius:3px;}"
            "QPushButton:hover{background:#30363d;}")
        self._log_btn.setStyleSheet(_TOGGLE_SS)
        self._auto_btn.setStyleSheet(_TOGGLE_SS)

        rng.addStretch()
        left_layout.addLayout(rng)

        # geo map
        self._map = HyCalGainMapWidget()
        self._map.set_modules(self._all_modules)
        self._map.moduleHovered.connect(self._on_module_hovered)
        self._map.paletteClicked.connect(self._on_cycle_palette)
        left_layout.addWidget(self._map, stretch=1)

        # info label
        self._info = QLabel("Hover over a module for details")
        self._info.setFont(QFont("Consolas", 10))
        self._info.setStyleSheet(
            "QLabel{background:#161b22;color:#c9d1d9;padding:4px 8px;"
            "border:1px solid #30363d;border-radius:4px;}")
        self._info.setFixedHeight(26)
        left_layout.addWidget(self._info)

        body.addWidget(left)

        # ---- right panel (vertical splitter: charts + table) ----
        right = QSplitter(Qt.Orientation.Vertical)

        charts = QWidget()
        charts_layout = QVBoxLayout(charts)
        charts_layout.setContentsMargins(0, 0, 0, 0)
        charts_layout.setSpacing(2)

        self._charts: List[LMSLineChartWidget] = []
        for i, name in enumerate(LMS_NAMES):
            chart = LMSLineChartWidget()
            chart.set_data([], [], [], f"{name} (lms/alpha)")
            self._charts.append(chart)
            charts_layout.addWidget(chart)

        right.addWidget(charts)

        self._irregular_table = IrregularTableWidget()
        self._irregular_table.runClicked.connect(self._on_jump_to_run)
        right.addWidget(self._irregular_table)

        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 2)

        body.addWidget(right)
        body.setStretchFactor(0, 1)
        body.setStretchFactor(1, 1)

        root.addWidget(body, stretch=1)

    # ---- helpers ----

    def _make_btn(self, text, fg, slot):
        btn = QPushButton(text)
        btn.setStyleSheet(
            f"QPushButton{{background:#21262d;color:{fg};"
            f"border:1px solid #30363d;padding:4px 12px;"
            f"font:bold 11px Consolas;border-radius:3px;}}"
            f"QPushButton:hover{{background:#30363d;}}"
            f"QPushButton:disabled{{color:#555;}}")
        btn.clicked.connect(slot)
        return btn

    def _slabel(self, text):
        lbl = QLabel(text)
        lbl.setFont(QFont("Consolas", 10))
        lbl.setStyleSheet("color:#c9d1d9;")
        return lbl

    def _apply_dark_palette(self):
        pal = self.palette()
        for role, colour in [
            (QPalette.ColorRole.Window, "#0d1117"),
            (QPalette.ColorRole.WindowText, "#c9d1d9"),
            (QPalette.ColorRole.Base, "#161b22"),
            (QPalette.ColorRole.Text, "#c9d1d9"),
            (QPalette.ColorRole.Button, "#21262d"),
            (QPalette.ColorRole.ButtonText, "#c9d1d9"),
            (QPalette.ColorRole.Highlight, "#58a6ff"),
        ]:
            pal.setColor(role, QColor(colour))
        self.setPalette(pal)

    # ---- keyboard navigation ----

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Left:
            self._on_prev_run()
        elif event.key() == Qt.Key.Key_Right:
            self._on_next_run()
        else:
            super().keyPressEvent(event)

    # ---- slots ----

    def _on_process_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Data Folder")
        if not folder:
            return

        self._status_lbl.setText("Loading...")
        self._status_lbl.setStyleSheet("color:#d29922;")
        QApplication.processEvents()

        self._runs = load_all_runs(folder)
        if not self._runs:
            self._status_lbl.setText("No data files found")
            self._status_lbl.setStyleSheet("color:#f85149;")
            return

        # populate run combo
        self._run_combo.blockSignals(True)
        self._run_combo.clear()
        for rd in self._runs:
            self._run_combo.addItem(str(rd.run_number))
        self._run_combo.setCurrentIndex(0)
        self._run_combo.blockSignals(False)

        self._current_run_idx = 0
        self._status_lbl.setText(f"{len(self._runs)} runs loaded")
        self._status_lbl.setStyleSheet("color:#3fb950;")

        self._update_all_views()

    def _on_run_changed(self, index: int):
        if index < 0 or index >= len(self._runs):
            return
        self._current_run_idx = index
        self._update_geo_view()

    def _on_ref_changed(self, index: int):
        self._current_ref_idx = index
        self._update_geo_view()
        self._update_irregular_table()

    def _on_prev_run(self):
        if self._current_run_idx > 0:
            self._run_combo.setCurrentIndex(self._current_run_idx - 1)

    def _on_next_run(self):
        if self._current_run_idx < len(self._runs) - 1:
            self._run_combo.setCurrentIndex(self._current_run_idx + 1)

    def _on_apply_range(self):
        try:
            vmin = float(self._range_min.text())
            vmax = float(self._range_max.text())
        except ValueError:
            return
        if vmin >= vmax:
            return
        self._auto_range = False
        self._auto_btn.setChecked(False)
        self._manual_vmin = vmin
        self._manual_vmax = vmax
        self._update_geo_view()

    def _on_log_toggled(self):
        self._log_scale = self._log_btn.isChecked()
        self._map.set_log_scale(self._log_scale)

    def _on_auto_range(self):
        self._auto_range = self._auto_btn.isChecked()
        self._update_geo_view()

    def _on_jump_to_run(self, run_number: int):
        for i, rd in enumerate(self._runs):
            if rd.run_number == run_number:
                self._run_combo.setCurrentIndex(i)
                return

    def _on_cycle_palette(self):
        self._palette_idx = (self._palette_idx + 1) % len(PALETTES)
        self._map.set_palette(self._palette_idx)

    def _on_module_hovered(self, name: str):
        mod = self._mod_by_name.get(name)
        if not mod:
            return
        if not self._runs:
            self._info.setText(f"{name} ({mod.mod_type})")
            return
        rd = self._runs[self._current_run_idx]
        mrec = rd.modules.get(name)
        if mrec:
            gain = mrec.gain_factors[self._current_ref_idx]
            ref = LMS_NAMES[self._current_ref_idx]
            self._info.setText(
                f"{name} ({mod.mod_type})  gain[{ref}]: {gain:.5f}"
                f"  lms_peak: {mrec.lms_peak:.2f}"
                f"  lms_sigma: {mrec.lms_sigma:.2f}")
        else:
            self._info.setText(f"{name} ({mod.mod_type})  no data")

    # ---- update views ----

    def _update_all_views(self):
        self._update_geo_view()
        self._update_line_charts()
        self._update_irregular_table()

    def _update_geo_view(self):
        if not self._runs:
            return
        rd = self._runs[self._current_run_idx]
        ref_idx = self._current_ref_idx

        values: Dict[str, float] = {}
        for mname, mrec in rd.modules.items():
            values[mname] = mrec.gain_factors[ref_idx]

        if self._auto_range:
            # auto-range using 2nd/98th percentile
            if values:
                sorted_v = sorted(values.values())
                n = len(sorted_v)
                lo_idx = max(0, int(n * 0.02))
                hi_idx = min(n - 1, int(n * 0.98))
                vmin = sorted_v[lo_idx]
                vmax = sorted_v[hi_idx]
                if vmin == vmax:
                    vmin -= 0.01
                    vmax += 0.01
            else:
                vmin, vmax = 0.9, 1.1
            # sync the edit boxes to show current auto values
            self._range_min.setText(f"{vmin:.4f}")
            self._range_max.setText(f"{vmax:.4f}")
        else:
            vmin = self._manual_vmin
            vmax = self._manual_vmax

        self._map.set_gain_data(values, vmin, vmax)

    def _update_line_charts(self):
        if not self._runs:
            return

        for i, lms_name in enumerate(LMS_NAMES):
            run_numbers: List[int] = []
            ratios: List[float] = []
            errors: List[float] = []

            for rd in self._runs:
                rec = rd.lms.get(lms_name)
                if rec is None or rec.alpha_peak == 0 or rec.lms_peak == 0:
                    continue
                ratio = rec.lms_peak / rec.alpha_peak
                # error propagation for ratio
                rel_lms = rec.lms_sigma / rec.lms_peak if rec.lms_peak != 0 else 0
                rel_alpha = rec.alpha_sigma / rec.alpha_peak if rec.alpha_peak != 0 else 0
                err = ratio * math.sqrt(rel_lms ** 2 + rel_alpha ** 2)

                run_numbers.append(rd.run_number)
                ratios.append(ratio)
                errors.append(err)

            self._charts[i].set_data(
                run_numbers, ratios, errors,
                f"{lms_name} (lms/alpha)")

    def _update_irregular_table(self):
        if not self._runs:
            return
        entries = compute_irregular_entries(
            self._runs, self._current_ref_idx, self._mod_by_name)
        self._irregular_table.set_data(entries)


# ===========================================================================
#  Entry point
# ===========================================================================

def main():
    app = QApplication(sys.argv)
    win = GainMonitorWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
