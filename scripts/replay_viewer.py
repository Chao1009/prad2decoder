#!/usr/bin/env python3
"""Replay Viewer (PyQt6)
=======================
GUI tool for the PRad-2 replay pipeline:

  1. Get Data  — SCP evio files from clondaq2
  2. Replay    — run pradana_replay_recon with configurable parameters
  3. Quick Check — run pradana_quick_check and display results

Results are displayed via a HyCal map and various histogram panels.

Usage:
  python replay_viewer.py [quick_check_output.root]
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Workaround: on some Linux systems the system libQt6DBus.so.6 is compiled
# against a different Qt version than PyQt6's bundled Qt6Core, causing an
# undefined-symbol error with version Qt_6_PRIVATE_API.
# Fix: prepend PyQt6's bundled Qt6 lib dir to LD_LIBRARY_PATH so the linker
# picks up ALL bundled Qt6 libs (including DBus) before the system ones.
# We re-exec the interpreter once with the updated env so the setting takes
# effect before any shared library is loaded.  On machines without the
# conflict (e.g., system-installed PyQt6 matching system Qt) the bundled
# lib dir will not exist and this is a no-op.
# ---------------------------------------------------------------------------
def _fix_qt_lib_path() -> None:
    import site
    sp_list: list[str] = []
    try:
        sp_list += site.getsitepackages()
    except AttributeError:
        pass
    try:
        sp_list.append(site.getusersitepackages())
    except AttributeError:
        pass
    for sp in sp_list:
        qt6_lib = os.path.join(sp, "PyQt6", "Qt6", "lib")
        if os.path.isdir(qt6_lib):
            current = os.environ.get("LD_LIBRARY_PATH", "")
            if qt6_lib not in current.split(":"):
                # Re-exec with updated LD_LIBRARY_PATH so the dynamic linker
                # prefers the bundled Qt6 libs over system Qt6 libs.
                new_path = qt6_lib + (":" + current if current else "")
                env = os.environ.copy()
                env["LD_LIBRARY_PATH"] = new_path
                os.execve(sys.executable, [sys.executable] + sys.argv, env)
            return  # already set — carry on normally

_fix_qt_lib_path()

from PyQt6.QtCore import (
    QPointF, QProcess, QProcessEnvironment, QRectF, QThread, Qt, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QFont, QImage, QPainter, QPen, QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox,
    QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMenu, QPushButton, QScrollArea,
    QSizePolicy, QSlider, QSplitter, QSpinBox, QStackedWidget,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------------------
# Optional ROOT file reading
# ---------------------------------------------------------------------------
try:
    import numpy as np
    import uproot
    HAS_UPROOT = True
except ImportError:
    HAS_UPROOT = False

# ---------------------------------------------------------------------------
# Shared HyCal infrastructure
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from hycal_geoview import (  # noqa: E402
    PALETTES, PALETTE_NAMES, THEME, HyCalMapWidget,
    apply_theme_palette, available_themes, cmap_qcolor,
    load_modules, set_theme, themed,
)

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
DB_DIR       = os.path.join(os.path.dirname(SCRIPT_DIR), "database")
MODULES_JSON = os.path.join(DB_DIR, "hycal_map.json")

_REMOTE_HOST      = "clondaq2"
_REMOTE_DATA_BASE = "/data/stage2"
_LOCAL_DATA_BASE  = "/data/evio/data"
_EVIO_BYTES_PER_FILE_EST = int(2.1 * 1024 ** 3)

# Replay tools — match the names used by other scripts
_PRAD2_BIN_DIR    = "/data/soft/prad2evviewer/build/bin"
_REPLAY_RECON_CMD = os.path.join(_PRAD2_BIN_DIR, "pradana_replay_recon")
_QUICK_CHECK_CMD  = os.path.join(_PRAD2_BIN_DIR, "pradana_quick_check")


# ===========================================================================
#  Helpers
# ===========================================================================

def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b //= 1024
    return f"{b:.1f} PB"


def _nice_ticks(lo: float, hi: float, max_ticks: int = 6) -> List[float]:
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return [lo] if math.isfinite(lo) else []
    raw = (hi - lo) / max(max_ticks - 1, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = mag
    for c in (1, 2, 2.5, 5, 10):
        if c * mag >= raw:
            step = c * mag
            break
    start = math.ceil(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.01:
        ticks.append(v)
        v += step
    return ticks


# ===========================================================================
#  Background worker — loads quick_check ROOT data in a QThread
# ===========================================================================

class _RootLoader(QThread):
    """Load quick_check ROOT file off the UI thread."""

    finished = pyqtSignal(dict, str)   # data_dict, error_message

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path

    def run(self):
        if not HAS_UPROOT:
            self.finished.emit({}, "uproot not installed. Run: pip install uproot numpy")
            return
        try:
            data = _load_root(self._path)
            self.finished.emit(data, "")
        except Exception as exc:
            self.finished.emit({}, str(exc))


def _load_root(path: str) -> dict:
    """Read a quick_check ROOT output file into numpy arrays."""
    import numpy as _np
    out: dict = {}
    with uproot.open(path) as f:
        # ---- top-level 2D: hit position ----
        if "hit_pos" in f:
            h = f["hit_pos"]
            out["hit_pos"] = (h.values().tolist(),
                              h.axis(0).edges().tolist(),
                              h.axis(1).edges().tolist())

        # ---- energy_plots ----
        ep = f.get("energy_plots")
        if ep is not None:
            # 1D spectra (live inside energy_plots/)
            for key in ("one_cluster_energy", "two_cluster_energy",
                        "clusters_energy", "total_energy"):
                obj = ep.get(key)
                if obj is not None:
                    out[key] = (obj.values().tolist(),
                                obj.axis().edges().tolist())

            # 2D histograms in energy_plots
            for key in ("energy_vs_theta", "h2_energy_theta", "h2_energy_module"):
                obj = ep.get(key)
                if obj is not None:
                    try:
                        out[f"energy_plots/{key}"] = (
                            obj.values().tolist(),
                            obj.axis(0).edges().tolist(),
                            obj.axis(1).edges().tolist(),
                        )
                    except Exception:
                        pass

        # ---- physics_yields ----
        py_ = f.get("physics_yields")
        if py_ is not None:
            for key in ("ep_yield", "ee_yield", "yield_ratio"):
                obj = py_.get(key)
                if obj is not None:
                    out[f"physics_yields/{key}"] = (
                        obj.values().tolist(),
                        obj.axis().edges().tolist(),
                    )

        # ---- moller_analysis ----
        ma = f.get("moller_analysis")
        if ma is not None:
            # 1D histograms (actual names have h_ prefix)
            for key in ("h_moller_z", "h_moller_phi_diff",
                        "h_moller_x", "h_moller_y"):
                obj = ma.get(key)
                if obj is not None:
                    out[f"moller/{key}"] = (
                        obj.values().tolist(),
                        obj.axis().edges().tolist(),
                    )
            # 2D Moller position
            obj = ma.get("h2_moller_pos")
            if obj is not None:
                try:
                    out["moller/h2_moller_pos"] = (
                        obj.values().tolist(),
                        obj.axis(0).edges().tolist(),
                        obj.axis(1).edges().tolist(),
                    )
                except Exception:
                    pass

        # ---- module_energy: per-module hit counts and mean energies ----
        me = f.get("module_energy")
        if me is not None:
            module_counts: Dict[str, float] = {}
            module_means:  Dict[str, float] = {}
            for name in me.keys(cycle=False):
                h = me[name]
                if not hasattr(h, "values"):
                    continue
                # strip leading "h_" → "W432" matches hycal_map name "W432"
                mod_key = name[2:] if name.startswith("h_") else name
                vals  = h.values()
                total = float(vals.sum())
                module_counts[mod_key] = total
                if total > 0:
                    edges = h.axis().edges()
                    mids  = (edges[:-1] + edges[1:]) / 2.0
                    module_means[mod_key] = float((_np.asarray(vals) * mids).sum() / total)
                else:
                    module_means[mod_key] = 0.0
            out["module_counts"] = module_counts
            out["module_means"]  = module_means

    return out


# ===========================================================================
#  1-D histogram widget
# ===========================================================================

class Hist1DWidget(QWidget):
    """Lightweight 1-D histogram display with zoom drag and right-click unzoom."""

    PAD_L, PAD_R, PAD_T, PAD_B = 55, 16, 24, 36
    BAR_COLOR = "#3fb950"

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self._default_title = title
        self._values: List[float] = []
        self._edges:  List[float] = []
        self._title = title
        self._x_lo = 0.0
        self._x_hi = 1.0
        self._drag_start: Optional[float] = None
        self._drag_cur:   Optional[float] = None
        self._log_x = False
        self._log_y = False
        self.setMinimumSize(200, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        _lbss = ("QPushButton{background:#21262d;color:#8b949e;border:1px solid #30363d;"
                 "border-radius:3px;font:8pt Consolas;padding:0 3px;}"
                 "QPushButton:checked{background:#1f6feb;color:#fff;border-color:#388bfd;}"
                 "QPushButton:hover{border-color:#58a6ff;color:#c9d1d9;}")
        self._btn_log_x = QPushButton("logX", self)
        self._btn_log_x.setCheckable(True)
        self._btn_log_x.setFixedSize(36, 18)
        self._btn_log_x.setStyleSheet(_lbss)
        self._btn_log_x.clicked.connect(self._toggle_log_x)
        self._btn_log_y = QPushButton("logY", self)
        self._btn_log_y.setCheckable(True)
        self._btn_log_y.setFixedSize(36, 18)
        self._btn_log_y.setStyleSheet(_lbss)
        self._btn_log_y.clicked.connect(self._toggle_log_y)
        self._cache_pm: Optional[QPixmap] = None
        self._cached_sx_state: Optional[tuple] = None
        # Crystal Ball auto-fit
        self.auto_cb_fit: bool = False
        # Asymmetric fit window around peak: (left_width, right_width) in data units.
        # None means use the full histogram range.
        self.cb_fit_range: Optional[Tuple[float, float]] = None
        # Asymmetric fit window in units of estimated sigma: (left_nsigma, right_nsigma).
        # Takes priority over cb_fit_range when set.
        self.cb_fit_range_sigma: Optional[Tuple[float, float]] = None
        self._cb_fit_result: Optional[Tuple[float, float, float, float]] = None  # (mean, mean_err, sigma, sigma_err)
        self._cb_fit_curve: Optional[Tuple[List[float], List[float]]] = None  # (xs, ys)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.width()
        self._btn_log_y.move(w - 42, 3)
        self._btn_log_x.move(w - 80, 3)
        self._cache_pm = None

    def set_data(self, values: List[float], edges: List[float], title: str = ""):
        self._values = list(values)
        self._edges  = list(edges)
        self._title  = title or self._default_title
        if edges:
            self._x_lo = edges[0]
            self._x_hi = edges[-1]
        self._drag_start = self._drag_cur = None
        self._cb_fit_result = None
        self._cb_fit_curve  = None
        if self.auto_cb_fit:
            self._run_cb_fit()
        self._cache_pm = None
        self.update()

    # -- Crystal Ball fit --

    def _run_cb_fit(self):
        """Fit histogram with a Crystal Ball function using scipy.
        Stores fit result and curve; silently ignored if scipy is unavailable.
        """
        if not self._values or not self._edges or len(self._edges) < 3:
            return
        try:
            from scipy.optimize import curve_fit
            import numpy as _np
        except ImportError:
            return

        edges  = _np.array(self._edges)
        values = _np.array(self._values, dtype=float)
        mids   = (edges[:-1] + edges[1:]) / 2.0
        mask   = values > 0
        if mask.sum() < 5:
            return

        def crystal_ball(x, amp, mu, sigma, alpha, n):
            alpha = abs(alpha)
            n     = abs(n)
            t = (x - mu) / sigma
            result = _np.where(
                t > -alpha,
                amp * _np.exp(-0.5 * t * t),
                amp * (n / alpha) ** n * _np.exp(-0.5 * alpha * alpha)
                    / (n / alpha - alpha - t) ** n
            )
            return result

        # initial guesses
        amp0   = float(values.max())
        mu0    = float(mids[values.argmax()])
        # estimate sigma from RMS within half-max region
        half_max_mask = values > amp0 * 0.5
        sigma0 = float(mids[half_max_mask].std()) if half_max_mask.sum() > 1 else (edges[-1] - edges[0]) * 0.05
        if sigma0 <= 0:
            sigma0 = (edges[-1] - edges[0]) * 0.05

        # Determine fit window
        if self.cb_fit_range_sigma is not None:
            left_ns, right_ns = self.cb_fit_range_sigma
            fit_lo = mu0 - left_ns * sigma0
            fit_hi = mu0 + right_ns * sigma0
            fit_mask = mask & (mids >= fit_lo) & (mids <= fit_hi)
        elif self.cb_fit_range is not None:
            left_w, right_w = self.cb_fit_range
            fit_lo = mu0 - left_w
            fit_hi = mu0 + right_w
            fit_mask = mask & (mids >= fit_lo) & (mids <= fit_hi)
        else:
            fit_lo, fit_hi = float(edges[0]), float(edges[-1])
            fit_mask = mask
        if fit_mask.sum() < 5:
            fit_mask = mask  # fall back to full range
            fit_lo, fit_hi = float(edges[0]), float(edges[-1])

        try:
            popt, pcov = curve_fit(
                crystal_ball, mids[fit_mask], values[fit_mask],
                p0=[amp0, mu0, sigma0, 1.5, 3.0],
                bounds=([0, fit_lo, 0, 0.1, 1.1],
                        [_np.inf, fit_hi, (fit_hi - fit_lo), 10.0, 50.0]),
                maxfev=5000,
            )
            perr = _np.sqrt(_np.diag(pcov))
            mu_fit, mu_err     = float(popt[1]), float(perr[1])
            sigma_fit, sigma_err = abs(float(popt[2])), float(perr[2])
            self._cb_fit_result = (mu_fit, mu_err, sigma_fit, sigma_err)
            # build curve for drawing
            xs = _np.linspace(edges[0], edges[-1], 400)
            ys = crystal_ball(xs, *popt)
            self._cb_fit_curve = (xs.tolist(), ys.tolist())
        except Exception:
            pass

    def clear(self):
        self._values = []
        self._edges  = []
        self._title  = self._default_title
        self._cache_pm = None
        self.update()

    # -- geometry helpers --

    def _plot_rect(self):
        w, h = self.width(), self.height()
        return (self.PAD_L, self.PAD_T,
                w - self.PAD_L - self.PAD_R,
                h - self.PAD_T - self.PAD_B)

    def _sx_to_data(self, sx: float) -> float:
        px, _py, pw, _ph = self._plot_rect()
        if pw <= 0 or self._x_hi == self._x_lo:
            return self._x_lo
        return self._x_lo + (sx - px) / pw * (self._x_hi - self._x_lo)

    # -- mouse events --

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            px, py, pw, ph = self._plot_rect()
            mx, my = event.position().x(), event.position().y()
            if px <= mx <= px + pw and py <= my <= py + ph + self.PAD_B:
                self._drag_start = self._sx_to_data(mx)
                self._drag_cur   = self._drag_start
                self.update()

    def mouseMoveEvent(self, event):
        if self._drag_start is not None:
            self._drag_cur = self._sx_to_data(event.position().x())
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._drag_start is not None:
            d_end = self._sx_to_data(event.position().x())
            span = self._x_hi - self._x_lo
            if abs(d_end - self._drag_start) > span * 0.01:
                self._x_lo = min(self._drag_start, d_end)
                self._x_hi = max(self._drag_start, d_end)
                self._cache_pm = None
            self._drag_start = self._drag_cur = None
            self.update()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet(themed(
            "QMenu{background:#161b22;color:#c9d1d9;border:1px solid #30363d;}"
            "QMenu::item:selected{background:#1f6feb;}"))
        menu.addAction("Unzoom").triggered.connect(self._unzoom)
        menu.exec(event.globalPos())

    def _toggle_log_x(self):
        self._log_x = self._btn_log_x.isChecked()
        if self._log_x and self._edges:
            pos_lo = next((e for e in self._edges if e > 0), None)
            if pos_lo is not None:
                self._x_lo = max(self._x_lo, pos_lo)
        self._cache_pm = None
        self.update()

    def _toggle_log_y(self):
        self._log_y = self._btn_log_y.isChecked()
        self._cache_pm = None
        self.update()

    def _unzoom(self):
        if self._edges:
            self._x_lo, self._x_hi = self._edges[0], self._edges[-1]
            if self._log_x:
                pos = [e for e in self._edges if e > 0]
                if pos:
                    self._x_lo = pos[0]
        self._cache_pm = None
        self.update()

    # -- paint (QPixmap cache: bars cached, only drag overlay redrawn on mouse-move) --

    def _rebuild_cache(self):
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            self._cache_pm = None
            self._cached_sx_state = None
            return
        pm = QPixmap(w, h)
        pm.fill(QColor(THEME.CANVAS))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._title:
            p.setPen(QColor(THEME.ACCENT))
            p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            p.drawText(QRectF(self.PAD_L, 2, w - self.PAD_L - self.PAD_R, 20),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self._title)

        if not self._values or not self._edges or len(self._edges) < 2:
            p.setPen(QColor(THEME.TEXT_MUTED))
            p.setFont(QFont("Consolas", 10))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "No data")
            p.end()
            self._cache_pm = pm
            self._cached_sx_state = None
            return

        px, py, pw, ph = self._plot_rect()
        if pw < 10 or ph < 10:
            p.end()
            self._cache_pm = pm
            self._cached_sx_state = None
            return

        x_lo = self._x_lo
        x_hi = self._x_hi if self._x_hi > self._x_lo else self._x_lo + 1
        use_log_x = self._log_x and x_lo > 0 and x_hi > x_lo
        use_log_y = self._log_y
        log_x_lo = math.log10(x_lo) if use_log_x else x_lo
        log_x_hi = math.log10(x_hi) if use_log_x else x_hi

        def to_sx(v):
            if use_log_x:
                if v <= 0:
                    return px - 1
                lv = math.log10(v)
                return px + (lv - log_x_lo) / (log_x_hi - log_x_lo) * pw
            return px + (v - x_lo) / (x_hi - x_lo) * pw

        vis_vals = [
            v for i, v in enumerate(self._values)
            if i + 1 < len(self._edges)
            and self._edges[i + 1] > x_lo
            and self._edges[i] < x_hi
        ]
        vis_max = max(vis_vals, default=0.0)

        if use_log_y:
            vis_pos = [v for v in vis_vals if v > 0]
            y_hi_log = math.log10(vis_max * 1.5) if vis_max > 0 else 0.0
            y_lo_log = (math.log10(min(vis_pos)) if vis_pos else y_hi_log - 4)
            y_lo_log = min(y_lo_log, y_hi_log - 1)
            def to_sy(v):
                if v <= 0:
                    return py + ph + 1
                lv = math.log10(v)
                return py + ph * (1.0 - (lv - y_lo_log) / (y_hi_log - y_lo_log))
        else:
            y_hi_lin = vis_max * 1.1 if vis_max > 0 else 1.0
            def to_sy(v): return py + ph * (1.0 - v / y_hi_lin)

        # grid
        p.setPen(QPen(QColor(THEME.BUTTON), 1, Qt.PenStyle.DotLine))
        n_yticks = 5
        for i in range(n_yticks + 1):
            sy = py + ph * i / n_yticks
            p.drawLine(QPointF(px, sy), QPointF(px + pw, sy))

        # bars
        bar_color = QColor(self.BAR_COLOR)
        p.setPen(Qt.PenStyle.NoPen)
        for i, v in enumerate(self._values):
            if i + 1 >= len(self._edges):
                break
            b_lo, b_hi = self._edges[i], self._edges[i + 1]
            if b_hi <= x_lo or b_lo >= x_hi:
                continue
            sx1 = max(to_sx(b_lo), px)
            sx2 = min(to_sx(b_hi), px + pw)
            bar_top = to_sy(v)
            bar_h = (py + ph) - bar_top
            if bar_h > 0 and sx2 > sx1:
                p.fillRect(QRectF(sx1, bar_top, sx2 - sx1 - 0.5, bar_h), bar_color)

        # axes
        p.setPen(QPen(QColor(THEME.BORDER), 1))
        p.drawLine(QPointF(px, py), QPointF(px, py + ph))
        p.drawLine(QPointF(px, py + ph), QPointF(px + pw, py + ph))

        # y labels
        p.setPen(QColor(THEME.TEXT_DIM))
        p.setFont(QFont("Consolas", 8))
        if use_log_y:
            for i in range(n_yticks + 1):
                lv = y_hi_log - (y_hi_log - y_lo_log) * i / n_yticks
                val = 10 ** lv
                sy = py + ph * i / n_yticks
                p.drawText(QRectF(0, sy - 8, self.PAD_L - 4, 16),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           f"{val:.3g}")
        else:
            for i in range(n_yticks + 1):
                val = y_hi_lin * (n_yticks - i) / n_yticks
                sy = py + ph * i / n_yticks
                p.drawText(QRectF(0, sy - 8, self.PAD_L - 4, 16),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           f"{val:.0f}")

        # x labels
        if use_log_x:
            for xt in _nice_ticks(log_x_lo, log_x_hi, max(pw // 60, 2)):
                sx = px + (xt - log_x_lo) / (log_x_hi - log_x_lo) * pw
                p.drawText(QRectF(sx - 28, py + ph + 2, 56, 16),
                           Qt.AlignmentFlag.AlignCenter, f"10^{xt:.4g}")
        else:
            for xt in _nice_ticks(x_lo, x_hi, max(pw // 60, 2)):
                sx = to_sx(xt)
                p.drawText(QRectF(sx - 28, py + ph + 2, 56, 16),
                           Qt.AlignmentFlag.AlignCenter, f"{xt:.4g}")

        # Crystal Ball fit curve + annotation
        if self._cb_fit_curve is not None:
            xs, ys = self._cb_fit_curve
            if use_log_y:
                vis_max_fit = max((v for v in ys if v > 0), default=1.0)
                y_hi_fit = math.log10(vis_max_fit * 1.5) if vis_max_fit > 0 else 0.0
                y_lo_fit = y_lo_log
                def to_sy_fit(v):
                    if v <= 0: return py + ph + 1
                    lv = math.log10(v)
                    return py + ph * (1.0 - (lv - y_lo_fit) / (y_hi_fit - y_lo_fit))
            else:
                to_sy_fit = to_sy
            fit_pen = QPen(QColor("#ff7b00"), 1.5)
            p.setPen(fit_pen)
            pts = []
            for xv, yv in zip(xs, ys):
                if x_lo <= xv <= x_hi:
                    pts.append(QPointF(to_sx(xv), to_sy_fit(yv)))
            for i in range(1, len(pts)):
                p.drawLine(pts[i - 1], pts[i])

        if self._cb_fit_result is not None:
            mu, mu_err, sigma, sigma_err = self._cb_fit_result
            label1 = f"\u03bc = {mu:.2f} \u00b1 {mu_err:.2f}"
            label2 = f"\u03c3 = {sigma:.2f} \u00b1 {sigma_err:.2f}"
            p.setFont(QFont("Consolas", 9))
            p.setPen(QColor("#ff7b00"))
            p.drawText(QRectF(px + 4, py + 4, pw - 8, 16),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       label1)
            p.drawText(QRectF(px + 4, py + 20, pw - 8, 16),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       label2)

        p.end()
        self._cache_pm = pm
        self._cached_sx_state = (px, py, pw, ph, x_lo, x_hi,
                                  use_log_x, log_x_lo, log_x_hi)

    def paintEvent(self, event):
        if self._cache_pm is None or self._cache_pm.size() != self.size():
            self._rebuild_cache()
        if self._cache_pm is None:
            return
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache_pm)
        # drag-select overlay — not cached, drawn on top each frame
        if self._drag_start is not None and self._drag_cur is not None:
            state = self._cached_sx_state
            if state is not None:
                px, py, pw, ph, x_lo, x_hi, use_log_x, log_x_lo, log_x_hi = state
                def sx_of(v):
                    if use_log_x:
                        if v <= 0:
                            return px - 1
                        lv = math.log10(v)
                        return px + (lv - log_x_lo) / (log_x_hi - log_x_lo) * pw
                    return px + (v - x_lo) / (x_hi - x_lo) * pw
                d_lo = min(self._drag_start, self._drag_cur)
                d_hi = max(self._drag_start, self._drag_cur)
                sx1 = max(sx_of(d_lo), px)
                sx2 = min(sx_of(d_hi), px + pw)
                if sx2 > sx1:
                    p.fillRect(QRectF(sx1, py, sx2 - sx1, ph), QColor(255, 255, 100, 50))
                    p.setPen(QPen(QColor(255, 255, 100, 180), 1))
                    p.drawRect(QRectF(sx1, py, sx2 - sx1, ph))
        p.end()


# ===========================================================================
#  2-D histogram widget (for hit_pos, energy_vs_theta)
# ===========================================================================

class Hist2DWidget(QWidget):
    """Simple 2-D heatmap (painter-based, no external libs)."""

    PAD_L, PAD_R, PAD_T, PAD_B = 55, 80, 28, 36
    CB_W = 18   # colorbar width

    def __init__(self, title: str = "", x_label: str = "x", y_label: str = "y",
                 parent=None):
        super().__init__(parent)
        self._title = title
        self._x_label = x_label
        self._y_label = y_label
        self._values: Optional[List[List[float]]] = None  # [ix][iy]
        self._x_edges: List[float] = []
        self._y_edges: List[float] = []
        self._palette_idx = 0   # index into PALETTE_NAMES
        self._log_z = False
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        _lbss = ("QPushButton{background:#21262d;color:#8b949e;border:1px solid #30363d;"
                 "border-radius:3px;font:8pt Consolas;padding:0 3px;}"
                 "QPushButton:checked{background:#1f6feb;color:#fff;border-color:#388bfd;}"
                 "QPushButton:hover{border-color:#58a6ff;color:#c9d1d9;}")
        self._btn_log_z = QPushButton("logZ", self)
        self._btn_log_z.setCheckable(True)
        self._btn_log_z.setFixedSize(36, 18)
        self._btn_log_z.setStyleSheet(_lbss)
        self._btn_log_z.clicked.connect(self._toggle_log_z)
        self._cache_pm: Optional[QPixmap] = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.width()
        self._btn_log_z.move(w - self.PAD_R + 10, 4)
        self._cache_pm = None

    def set_data(self, values: List[List[float]],
                 x_edges: List[float], y_edges: List[float]):
        self._values = values
        self._x_edges = x_edges
        self._y_edges = y_edges
        self._cache_pm = None
        self.update()

    def clear(self):
        self._values = None
        self._x_edges = []
        self._y_edges = []
        self._cache_pm = None
        self.update()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet(themed(
            "QMenu{background:#161b22;color:#c9d1d9;border:1px solid #30363d;}"
            "QMenu::item:selected{background:#1f6feb;}"))
        pal_menu = menu.addMenu("Palette")
        for i, name in enumerate(PALETTE_NAMES):
            act = pal_menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(i == self._palette_idx)
            idx = i
            act.triggered.connect(lambda _c=False, ii=idx: self._set_palette(ii))
        menu.exec(event.globalPos())

    def _toggle_log_z(self):
        self._log_z = self._btn_log_z.isChecked()
        self._cache_pm = None
        self.update()

    def _set_palette(self, idx: int):
        self._palette_idx = idx
        self._cache_pm = None
        self.update()

    def _rebuild_cache(self):
        """Render entire 2-D heatmap to a QPixmap (cached until data/palette/size changes)."""
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            self._cache_pm = None
            return
        pm = QPixmap(w, h)
        pm.fill(QColor(THEME.CANVAS))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._title:
            p.setPen(QColor(THEME.ACCENT))
            p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            p.drawText(QRectF(self.PAD_L, 4, w - self.PAD_L - self.PAD_R, 20),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self._title)

        if self._values is None:
            p.setPen(QColor(THEME.TEXT_MUTED))
            p.setFont(QFont("Consolas", 10))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "No data")
            p.end()
            self._cache_pm = pm
            return

        vals = self._values
        xe = self._x_edges
        ye = self._y_edges
        if len(xe) < 2 or len(ye) < 2:
            p.end()
            self._cache_pm = pm
            return

        px = self.PAD_L
        py = self.PAD_T
        cb_x = w - self.PAD_R + 8
        pw = w - self.PAD_L - self.PAD_R
        ph = h - self.PAD_T - self.PAD_B

        if pw < 10 or ph < 10:
            p.end()
            self._cache_pm = pm
            return

        x_lo, x_hi = xe[0], xe[-1]
        y_lo, y_hi = ye[0], ye[-1]

        def to_sx(v): return px + (v - x_lo) / (x_hi - x_lo) * pw
        def to_sy(v): return py + ph - (v - y_lo) / (y_hi - y_lo) * ph

        flat = [v for row in vals for v in row if math.isfinite(v) and v > 0]
        vmax = max(flat) if flat else 1.0
        use_log_z = self._log_z and vmax > 0
        log_vmax = math.log10(vmax) if use_log_z else vmax
        log_vmin = math.log10(min(flat)) if (use_log_z and flat) else 0.0
        if use_log_z and log_vmin >= log_vmax:
            log_vmin = log_vmax - 1

        palette = PALETTES.get(PALETTE_NAMES[self._palette_idx],
                               PALETTES[PALETTE_NAMES[0]])

        # --- numpy fast path: render heatmap + colorbar as QImage in one shot ---
        drawn = False
        try:
            import numpy as _np
            nx, ny = len(xe) - 1, len(ye) - 1
            vals_np = _np.array(vals, dtype=_np.float64)          # (nx, ny)
            if use_log_z:
                with _np.errstate(divide='ignore', invalid='ignore'):
                    lv = _np.where(vals_np > 0,
                                   _np.log10(_np.maximum(vals_np, 1e-300)),
                                   _np.nan)
                span = log_vmax - log_vmin
                t_bins = ((lv - log_vmin) / span) if span > 0 else _np.zeros_like(lv)
            else:
                t_bins = (vals_np / vmax) if vmax > 0 else _np.zeros_like(vals_np)
            t_bins = _np.clip(t_bins, 0.0, 1.0)
            mask   = (vals_np > 0) & _np.isfinite(t_bins)         # (nx, ny)

            # map each screen pixel to its data bin
            xi = _np.clip(
                (_np.arange(pw, dtype=_np.float64) / pw * nx).astype(_np.int32),
                0, nx - 1)                                         # (pw,)
            yi = _np.clip(
                ((1.0 - _np.arange(ph, dtype=_np.float64) / ph) * ny
                 ).astype(_np.int32),
                0, ny - 1)                                         # (ph,)

            t_img   = t_bins[xi[_np.newaxis, :], yi[:, _np.newaxis]]  # (ph, pw)
            msk_img =  mask[xi[_np.newaxis, :], yi[:, _np.newaxis]]

            stops_t = _np.array([s[0]    for s in palette], dtype=_np.float64)
            stops_r = _np.array([s[1][0] for s in palette], dtype=_np.float64)
            stops_g = _np.array([s[1][1] for s in palette], dtype=_np.float64)
            stops_b = _np.array([s[1][2] for s in palette], dtype=_np.float64)

            tf   = t_img.ravel()
            r_ch = _np.interp(tf, stops_t, stops_r).astype(_np.uint8)
            g_ch = _np.interp(tf, stops_t, stops_g).astype(_np.uint8)
            b_ch = _np.interp(tf, stops_t, stops_b).astype(_np.uint8)
            a_ch = _np.where(msk_img.ravel(),
                             _np.uint32(255), _np.uint32(0)).astype(_np.uint32)
            argb = (a_ch << 24 | r_ch.astype(_np.uint32) << 16 |
                    g_ch.astype(_np.uint32) << 8 | b_ch.astype(_np.uint32))
            img = QImage(argb.tobytes(), pw, ph, pw * 4,
                         QImage.Format.Format_ARGB32)
            p.drawImage(px, py, img)

            # colorbar as 1×ph QImage scaled to CB_W wide
            cb_h = ph
            t_cb = 1.0 - _np.arange(cb_h, dtype=_np.float64) / max(cb_h - 1, 1)
            r_cb = _np.interp(t_cb, stops_t, stops_r).astype(_np.uint8)
            g_cb = _np.interp(t_cb, stops_t, stops_g).astype(_np.uint8)
            b_cb = _np.interp(t_cb, stops_t, stops_b).astype(_np.uint8)
            a_cb = _np.full(cb_h, _np.uint32(0xFF000000), dtype=_np.uint32)
            argb_cb = (a_cb | r_cb.astype(_np.uint32) << 16 |
                       g_cb.astype(_np.uint32) << 8 | b_cb.astype(_np.uint32))
            cb_img = QImage(argb_cb.tobytes(), 1, cb_h, 4,
                            QImage.Format.Format_ARGB32)
            p.drawImage(QRectF(cb_x, py, self.CB_W, cb_h), cb_img,
                        QRectF(0, 0, 1, cb_h))
            drawn = True
        except Exception:
            pass

        if not drawn:
            # fallback: original loop-based drawing
            p.setPen(Qt.PenStyle.NoPen)
            nx = len(xe) - 1
            ny = len(ye) - 1
            for ix in range(nx):
                bx0 = max(to_sx(xe[ix]), px)
                bx1 = min(to_sx(xe[ix + 1]), px + pw)
                if bx1 <= bx0:
                    continue
                col_vals = vals[ix] if ix < len(vals) else []
                for iy in range(ny):
                    v = col_vals[iy] if iy < len(col_vals) else 0.0
                    if not math.isfinite(v) or v <= 0:
                        continue
                    by1 = max(to_sy(ye[iy + 1]), py)
                    by0 = min(to_sy(ye[iy]), py + ph)
                    if by0 <= by1:
                        continue
                    t = ((math.log10(v) - log_vmin) / (log_vmax - log_vmin)
                         if use_log_z else v / vmax)
                    t = max(0.0, min(1.0, t))
                    c = cmap_qcolor(t, palette)
                    p.fillRect(QRectF(bx0, by1, bx1 - bx0, by0 - by1), c)
            cb_h = ph
            for i in range(cb_h):
                c = cmap_qcolor(1.0 - i / cb_h, palette)
                p.setPen(QPen(c, 1))
                p.drawLine(cb_x, py + i, cb_x + self.CB_W, py + i)

        # axes + tick labels + colorbar border (always)
        p.setPen(QPen(QColor(THEME.BORDER), 1))
        p.drawLine(QPointF(px, py), QPointF(px, py + ph))
        p.drawLine(QPointF(px, py + ph), QPointF(px + pw, py + ph))
        p.drawRect(QRectF(cb_x, py, self.CB_W, ph))
        p.setPen(QColor(THEME.TEXT_DIM))
        p.setFont(QFont("Consolas", 8))
        for xt in _nice_ticks(x_lo, x_hi, max(pw // 60, 2)):
            sx = to_sx(xt)
            p.drawText(QRectF(sx - 28, py + ph + 2, 56, 16),
                       Qt.AlignmentFlag.AlignCenter, f"{xt:.4g}")
        for yt in _nice_ticks(y_lo, y_hi, max(ph // 40, 2)):
            sy = to_sy(yt)
            p.drawText(QRectF(0, sy - 8, self.PAD_L - 4, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{yt:.4g}")
        if use_log_z:
            p.drawText(QRectF(cb_x, py + ph + 2, self.CB_W + 40, 14),
                       Qt.AlignmentFlag.AlignLeft, f"10^{log_vmin:.2g}")
            p.drawText(QRectF(cb_x, py - 12, self.CB_W + 40, 14),
                       Qt.AlignmentFlag.AlignLeft, f"10^{log_vmax:.2g}")
        else:
            p.drawText(QRectF(cb_x, py + ph + 2, self.CB_W + 20, 14),
                       Qt.AlignmentFlag.AlignLeft, "0")
            p.drawText(QRectF(cb_x, py - 12, self.CB_W + 40, 14),
                       Qt.AlignmentFlag.AlignLeft, f"{vmax:.3g}")
        p.end()
        self._cache_pm = pm

    def paintEvent(self, event):
        if self._cache_pm is None or self._cache_pm.size() != self.size():
            self._rebuild_cache()
        if self._cache_pm is None:
            return
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache_pm)
        p.end()


# ===========================================================================
#  HyCal replay map widget — shows per-module event counts
# ===========================================================================

class HyCalReplayMapWidget(HyCalMapWidget):
    """HyCal map showing per-module event counts from quick_check module_energy."""

    def __init__(self, parent=None):
        super().__init__(parent, shrink=0.92, margin_top=8,
                         enable_zoom_pan=True, include_lms=False)
        self._selected: Optional[str] = None

    def set_module_counts(self, counts: Dict[str, float]):
        if not counts:
            self._values = {}
            self._vmin = 0.0
            self._vmax = 1.0
            self.update()
            return
        # Seed all PbWO4 modules with 0 so the full crystal region is
        # visible (min-palette colour) even when the ROOT file lacks an
        # entry for some modules.
        base: Dict[str, float] = {
            m.name: 0.0 for m in self._modules if m.mod_type == "PbWO4"
        }
        base.update(counts)
        self._values = base
        self._vmin = 0.0
        self._vmax = max(counts.values()) if counts else 1.0
        self.update()

    def _fmt_value(self, v: float) -> str:
        return f"{v:.0f}"

    def _tooltip_text(self, name: str) -> str:
        v = self._values.get(name)
        if v is None:
            return name
        return f"{name}: {v:.0f} hits"

    def _paint_empty(self, p, w, h):
        if not self._values:
            p.setPen(QColor(THEME.TEXT_MUTED))
            p.setFont(QFont("Consolas", 12))
            p.drawText(QRectF(0, 0, w, h),
                       Qt.AlignmentFlag.AlignCenter, "Load a ROOT file to view")

    def _paint_overlays(self, p, w, h):
        if self._selected and self._selected in self._rects:
            p.setPen(QPen(QColor(THEME.SELECT_BORDER), 2.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self._rects[self._selected])
        super()._paint_overlays(p, w, h)

    def _handle_click(self, pos):
        if self._cb_rect and self._cb_rect.contains(pos):
            self.paletteClicked.emit()
            return
        hit = self._hit(pos)
        if hit is not None:
            self._selected = None if hit == self._selected else hit
            self.update()
            self.moduleClicked.emit(self._selected if self._selected else "")
        elif self._selected is not None:
            self._selected = None
            self.update()
            self.moduleClicked.emit("")


# ===========================================================================
#  Control Panel (left side)
# ===========================================================================

_BTN_PRIMARY = themed(
    "QPushButton{background:#1f6feb;color:white;border:1px solid #388bfd;"
    "padding:4px 14px;font:bold 10pt Consolas;border-radius:3px;}"
    "QPushButton:hover{background:#388bfd;}"
    "QPushButton:disabled{background:#21262d;color:#555;border-color:#30363d;}")

_BTN_NORMAL = themed(
    "QPushButton{background:#21262d;color:#c9d1d9;border:1px solid #30363d;"
    "padding:4px 12px;font:bold 10pt Consolas;border-radius:3px;}"
    "QPushButton:hover{background:#30363d;}"
    "QPushButton:disabled{color:#555;}")

_BTN_DANGER = themed(
    "QPushButton{background:#3d1f22;color:#f85149;border:1px solid #f85149;"
    "padding:4px 12px;font:bold 10pt Consolas;border-radius:3px;}"
    "QPushButton:hover{background:#5a2329;}"
    "QPushButton:disabled{color:#555;}")

_LINEEDIT_SS = themed(
    "QLineEdit{background:#161b22;color:#c9d1d9;"
    "border:1px solid #30363d;border-radius:3px;padding:2px 6px;"
    "font-family:Consolas;font-size:10pt;}")

_SPINBOX_SS = themed(
    "QSpinBox{background:#161b22;color:#c9d1d9;"
    "border:1px solid #30363d;border-radius:3px;padding:2px 6px;"
    "font-family:Consolas;font-size:10pt;}")

_COMBO_SS = themed(
    "QComboBox{background:#161b22;color:#c9d1d9;"
    "border:1px solid #30363d;border-radius:3px;padding:2px 6px;}"
    "QComboBox::drop-down{border:none;width:18px;}"
    "QComboBox::down-arrow{border-left:4px solid transparent;"
    "border-right:4px solid transparent;border-top:5px solid #8b949e;"
    "margin-right:4px;}"
    "QComboBox QAbstractItemView{background:#161b22;color:#c9d1d9;"
    "border:1px solid #30363d;selection-background-color:#1f6feb;}")

_GRPBOX_SS = themed(
    "QGroupBox{color:#58a6ff;font:bold 10pt Consolas;"
    "border:1px solid #30363d;border-radius:4px;margin-top:8px;padding-top:6px;}"
    "QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px;}")

_CHK_SS = themed(
    "QCheckBox{color:#c9d1d9;font-family:Consolas;font-size:10pt;spacing:6px;}"
    "QCheckBox::indicator{width:14px;height:14px;"
    "border:1px solid #30363d;border-radius:2px;background:#161b22;}"
    "QCheckBox::indicator:checked{background:#1f6feb;border-color:#388bfd;}")

_LBL_SS  = themed("QLabel{color:#c9d1d9;font-family:Consolas;font-size:10pt;}")
_LBL_MUT = themed("QLabel{color:#8b949e;font-family:Consolas;font-size:9pt;}")


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(themed(
        "QLabel{color:#8b949e;font:bold 9pt Consolas;"
        "border-bottom:1px solid #30363d;padding-bottom:2px;}"))
    return lbl


class ControlPanel(QWidget):
    """Left panel: SCP, replay, quick_check controls + log output."""

    # Emitted when a quick_check ROOT file has been generated (or opened).
    rootFileReady = pyqtSignal(str)   # path to ROOT file

    def __init__(self, parent=None):
        super().__init__(parent)
        self._process: Optional[QProcess] = None
        self._pending_steps: List[str] = []   # ["scp", "replay", "hadd", "qcheck"]
        self._current_step: str = ""          # step currently running
        self._evio_dir: str = ""              # set after SCP completes
        self._recon_dir: str = ""             # set after replay completes
        self._hadd_out: str = ""              # merged ROOT file from hadd
        self._hadd_inputs: List[str] = []     # individual files to delete after hadd
        self._qcheck_out: str = ""            # final ROOT file path
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ---- Do It All / Stop row (always visible at top) ----
        do_all_row = QHBoxLayout()
        self._do_all_btn = QPushButton("Do It All  (SCP → Replay → hadd → Quick Check)")
        self._do_all_btn.setStyleSheet(_BTN_PRIMARY)
        self._do_all_btn.clicked.connect(
            lambda: self._start_pipeline(["scp", "replay", "hadd", "qcheck"]))
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setStyleSheet(_BTN_DANGER)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        do_all_row.addWidget(self._do_all_btn)
        do_all_row.addWidget(self._stop_btn)
        do_all_row.addStretch()
        root.addLayout(do_all_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(themed(
            "QScrollArea{border:none;background:transparent;}"
            "QScrollBar:vertical{background:#0d1117;width:8px;}"
            "QScrollBar::handle:vertical{background:#30363d;border-radius:4px;}"))

        inner = QWidget()
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(2, 2, 2, 2)
        inner_lay.setSpacing(8)

        # ---- SCP section ----
        grp_scp = QGroupBox("1. Get Data (SCP from clondaq2)")
        grp_scp.setStyleSheet(_GRPBOX_SS)
        form_scp = QFormLayout(grp_scp)
        form_scp.setSpacing(5)
        form_scp.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._run_edit = QLineEdit()
        self._run_edit.setPlaceholderText("e.g. 024100")
        self._run_edit.setFont(QFont("Consolas", 10))
        self._run_edit.setStyleSheet(_LINEEDIT_SS)
        form_scp.addRow("Run number:", self._run_edit)

        self._host_edit = QLineEdit(_REMOTE_HOST)
        self._host_edit.setFont(QFont("Consolas", 10))
        self._host_edit.setStyleSheet(_LINEEDIT_SS)
        form_scp.addRow("Remote host:", self._host_edit)

        self._remote_base_edit = QLineEdit(_REMOTE_DATA_BASE)
        self._remote_base_edit.setFont(QFont("Consolas", 10))
        self._remote_base_edit.setStyleSheet(_LINEEDIT_SS)
        form_scp.addRow("Remote base dir:", self._remote_base_edit)

        self._local_base_edit, _br1 = self._dir_row(_LOCAL_DATA_BASE,
                                                     self._browse_local_base)
        form_scp.addRow("Local base dir:", _br1)

        self._f_start = QSpinBox()
        self._f_start.setRange(0, 9999)
        self._f_start.setValue(0)
        self._f_start.setFont(QFont("Consolas", 10))
        self._f_start.setStyleSheet(_SPINBOX_SS)

        self._f_end = QSpinBox()
        self._f_end.setRange(0, 9999)
        self._f_end.setValue(99)
        self._f_end.setFont(QFont("Consolas", 10))
        self._f_end.setStyleSheet(_SPINBOX_SS)

        f_row = QHBoxLayout()
        f_row.addWidget(self._f_start)
        f_row.addWidget(QLabel(" — "))
        f_row.addWidget(self._f_end)
        f_row.addStretch()
        form_scp.addRow("File index range:", f_row)

        self._disk_lbl = QLabel("(disk space: not checked)")
        self._disk_lbl.setStyleSheet(_LBL_MUT)
        form_scp.addRow("", self._disk_lbl)

        scp_btn_row = QHBoxLayout()
        self._check_disk_btn = QPushButton("Check Disk")
        self._check_disk_btn.setStyleSheet(_BTN_NORMAL)
        self._check_disk_btn.clicked.connect(self._on_check_disk)
        self._scp_btn = QPushButton("Get Data")
        self._scp_btn.setStyleSheet(_BTN_PRIMARY)
        self._scp_btn.clicked.connect(lambda: self._start_pipeline(["scp"]))
        scp_btn_row.addWidget(self._check_disk_btn)
        scp_btn_row.addWidget(self._scp_btn)
        scp_btn_row.addStretch()
        form_scp.addRow("", scp_btn_row)

        inner_lay.addWidget(grp_scp)

        # ---- Replay section ----
        grp_rep = QGroupBox("2. Replay Recon")
        grp_rep.setStyleSheet(_GRPBOX_SS)
        form_rep = QFormLayout(grp_rep)
        form_rep.setSpacing(5)
        form_rep.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._evio_edit, _bev = self._dir_row("", self._browse_evio)
        form_rep.addRow("EVIO dir / file:", _bev)

        self._outdir_edit, _bout = self._dir_row("", self._browse_outdir)
        form_rep.addRow("Output dir:", _bout)

        self._threads_spin = QSpinBox()
        self._threads_spin.setRange(1, 64)
        self._threads_spin.setValue(4)
        self._threads_spin.setFont(QFont("Consolas", 10))
        self._threads_spin.setStyleSheet(_SPINBOX_SS)
        form_rep.addRow("Threads (-j):", self._threads_spin)

        self._max_events_rep = QLineEdit("-1")
        self._max_events_rep.setFont(QFont("Consolas", 10))
        self._max_events_rep.setStyleSheet(_LINEEDIT_SS)
        self._max_events_rep.setToolTip("-1 means no limit")
        form_rep.addRow("Max events (-n):", self._max_events_rep)

        self._max_files_spin = QSpinBox()
        self._max_files_spin.setRange(-1, 9999)
        self._max_files_spin.setValue(-1)
        self._max_files_spin.setSpecialValueText("all")
        self._max_files_spin.setFont(QFont("Consolas", 10))
        self._max_files_spin.setStyleSheet(_SPINBOX_SS)
        form_rep.addRow("Max files (-f):", self._max_files_spin)

        self._daq_config_edit = QLineEdit()
        self._daq_config_edit.setPlaceholderText("(default)")
        self._daq_config_edit.setFont(QFont("Consolas", 10))
        self._daq_config_edit.setStyleSheet(_LINEEDIT_SS)
        form_rep.addRow("DAQ config (-c):", self._daq_config_edit)

        self._hycal_map_edit = QLineEdit()
        self._hycal_map_edit.setPlaceholderText("(default)")
        self._hycal_map_edit.setFont(QFont("Consolas", 10))
        self._hycal_map_edit.setStyleSheet(_LINEEDIT_SS)
        form_rep.addRow("HyCal map (-d):", self._hycal_map_edit)

        self._gem_ped_edit = QLineEdit()
        self._gem_ped_edit.setPlaceholderText("(none)")
        self._gem_ped_edit.setFont(QFont("Consolas", 10))
        self._gem_ped_edit.setStyleSheet(_LINEEDIT_SS)
        form_rep.addRow("GEM pedestal (-g):", self._gem_ped_edit)

        self._zerosup_edit = QLineEdit("5")
        self._zerosup_edit.setPlaceholderText("(default)")
        self._zerosup_edit.setFont(QFont("Consolas", 10))
        self._zerosup_edit.setStyleSheet(_LINEEDIT_SS)
        form_rep.addRow("Zero-sup thresh (-z):", self._zerosup_edit)

        self._prad1_chk = QCheckBox("PRad-1 mode (-p)")
        self._prad1_chk.setStyleSheet(_CHK_SS)
        form_rep.addRow("", self._prad1_chk)

        self._auto_delete_chk = QCheckBox("Auto-delete EVIO files after recon")
        self._auto_delete_chk.setStyleSheet(_CHK_SS)
        form_rep.addRow("", self._auto_delete_chk)

        rep_btn_row = QHBoxLayout()
        self._replay_btn = QPushButton("Run Replay")
        self._replay_btn.setStyleSheet(_BTN_PRIMARY)
        self._replay_btn.clicked.connect(lambda: self._start_pipeline(["replay"]))
        rep_btn_row.addWidget(self._replay_btn)
        rep_btn_row.addStretch()
        form_rep.addRow("", rep_btn_row)

        inner_lay.addWidget(grp_rep)

        # ---- Quick Check section ----
        grp_qc = QGroupBox("3. Quick Check")
        grp_qc.setStyleSheet(_GRPBOX_SS)
        form_qc = QFormLayout(grp_qc)
        form_qc.setSpacing(5)
        form_qc.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._qc_input_edit, _bqci = self._dir_row("", self._browse_qc_input)
        form_qc.addRow("Input (dir / file):", _bqci)

        self._qc_output_edit = QLineEdit()
        self._qc_output_edit.setPlaceholderText("output.root")
        self._qc_output_edit.setFont(QFont("Consolas", 10))
        self._qc_output_edit.setStyleSheet(_LINEEDIT_SS)
        form_qc.addRow("Output ROOT (-o):", self._qc_output_edit)

        self._max_events_qc = QLineEdit("-1")
        self._max_events_qc.setFont(QFont("Consolas", 10))
        self._max_events_qc.setStyleSheet(_LINEEDIT_SS)
        form_qc.addRow("Max events (-n):", self._max_events_qc)

        qc_btn_row = QHBoxLayout()
        self._qcheck_btn = QPushButton("Run Quick Check")
        self._qcheck_btn.setStyleSheet(_BTN_PRIMARY)
        self._qcheck_btn.clicked.connect(lambda: self._start_pipeline(["qcheck"]))

        self._open_root_btn = QPushButton("Open ROOT…")
        self._open_root_btn.setStyleSheet(_BTN_NORMAL)
        self._open_root_btn.clicked.connect(self._browse_root_file)

        qc_btn_row.addWidget(self._qcheck_btn)
        qc_btn_row.addWidget(self._open_root_btn)
        qc_btn_row.addStretch()
        form_qc.addRow("", qc_btn_row)

        inner_lay.addWidget(grp_qc)

        inner_lay.addStretch()
        scroll.setWidget(inner)
        root.addWidget(scroll, stretch=0)

        # ---- Log output ----
        log_header = QHBoxLayout()
        log_lbl = QLabel("Log")
        log_lbl.setStyleSheet(themed(
            "QLabel{color:#58a6ff;font:bold 10pt Consolas;}"))
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet(_LBL_MUT)
        clr_btn = QPushButton("Clear")
        clr_btn.setFixedWidth(54)
        clr_btn.setStyleSheet(_BTN_NORMAL)
        clr_btn.clicked.connect(lambda: self._console.clear())
        log_header.addWidget(log_lbl)
        log_header.addWidget(self._status_lbl)
        log_header.addStretch()
        log_header.addWidget(clr_btn)
        root.addLayout(log_header)

        self._console = QTextEdit()
        self._console.setReadOnly(True)
        self._console.setFont(QFont("Monospace", 9))
        self._console.document().setMaximumBlockCount(10000)
        self._console.setStyleSheet(themed(
            "QTextEdit{background:#0a0e14;color:#c9d1d9;"
            "border:1px solid #30363d;font-family:Monospace;font-size:9pt;}"))
        root.addWidget(self._console, stretch=1)

    # ------------------------------------------------------------------
    # Dir-row helper: QLineEdit + Browse button
    # ------------------------------------------------------------------

    def _dir_row(self, default: str, slot) -> Tuple[QLineEdit, QWidget]:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        edit = QLineEdit(default)
        edit.setFont(QFont("Consolas", 10))
        edit.setStyleSheet(_LINEEDIT_SS)
        btn = QPushButton("Browse…")
        btn.setFixedWidth(72)
        btn.setStyleSheet(_BTN_NORMAL)
        btn.clicked.connect(slot)
        row.addWidget(edit)
        row.addWidget(btn)
        return edit, container

    # -- browse slots --

    def _browse_local_base(self):
        d = QFileDialog.getExistingDirectory(self, "Local Base Directory",
                                             self._local_base_edit.text())
        if d:
            self._local_base_edit.setText(d)

    def _browse_evio(self):
        d = QFileDialog.getExistingDirectory(self, "EVIO Directory",
                                             self._evio_edit.text())
        if d:
            self._evio_edit.setText(d)

    def _browse_outdir(self):
        d = QFileDialog.getExistingDirectory(self, "Replay Output Directory",
                                             self._outdir_edit.text())
        if d:
            self._outdir_edit.setText(d)

    def _browse_qc_input(self):
        d = QFileDialog.getExistingDirectory(self, "Quick Check Input Directory",
                                             self._qc_input_edit.text())
        if d:
            self._qc_input_edit.setText(d)

    def _browse_root_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open ROOT File", "", "ROOT files (*.root);;All files (*)")
        if path:
            self._log(f"<span style='color:#8b949e'>Opening {path}</span>")
            self.rootFileReady.emit(path)

    # ------------------------------------------------------------------
    # Disk space check
    # ------------------------------------------------------------------

    def _on_check_disk(self):
        run_num = self._run_edit.text().strip()
        if not run_num:
            self._log("<span style='color:#f85149'>Enter a run number first.</span>")
            return
        host = self._host_edit.text().strip() or _REMOTE_HOST
        remote_base = self._remote_base_edit.text().strip() or _REMOTE_DATA_BASE
        local_base = self._local_base_edit.text().strip() or _LOCAL_DATA_BASE
        f_start = self._f_start.value()
        f_end = self._f_end.value()
        remote_run_dir = f"{remote_base}/run{int(run_num):06d}"
        self._disk_lbl.setText("Checking…")
        try:
            needed, free = _check_disk_space(host, remote_run_dir, local_base,
                                              f_start, f_end)
            ok = free >= needed
            color = "#3fb950" if ok else "#f85149"
            self._disk_lbl.setText(
                f"<span style='color:{color}'>"
                f"need {_fmt_bytes(needed)}, free {_fmt_bytes(free)}"
                f"{'  ✓' if ok else '  ✗ (insufficient)'}</span>")
        except RuntimeError as exc:
            self._disk_lbl.setText(
                f"<span style='color:#f85149'>SSH error: {exc}</span>")
        except Exception as exc:
            self._disk_lbl.setText(
                f"<span style='color:#f85149'>Error: {exc}</span>")

    # ------------------------------------------------------------------
    # Pipeline logic
    # ------------------------------------------------------------------

    def _start_pipeline(self, steps: List[str]):
        if self._process is not None and \
                self._process.state() != QProcess.ProcessState.NotRunning:
            self._log("<span style='color:#f85149'>A process is already running.</span>")
            return
        self._pending_steps = list(steps)
        self._set_running(True)
        self._run_next_step()

    def _run_next_step(self):
        if not self._pending_steps:
            self._set_running(False)
            self._log("<span style='color:#3fb950'>[All steps complete]</span>")
            return
        step = self._pending_steps.pop(0)
        self._current_step = step
        if step == "scp":
            self._run_scp()
        elif step == "replay":
            self._run_replay()
        elif step == "hadd":
            self._run_hadd()
        elif step == "qcheck":
            self._run_qcheck()
        else:
            self._run_next_step()

    # -- SCP step --

    def _run_scp(self):
        run_num = self._run_edit.text().strip()
        if not run_num:
            self._log("<span style='color:#f85149'>No run number specified.</span>")
            self._set_running(False)
            return
        host = self._host_edit.text().strip() or _REMOTE_HOST
        remote_base = self._remote_base_edit.text().strip() or _REMOTE_DATA_BASE
        local_base = self._local_base_edit.text().strip() or _LOCAL_DATA_BASE
        f_start = self._f_start.value()
        f_end = self._f_end.value()
        run_id = int(run_num)
        remote_run_dir = f"{remote_base}/run{run_id:06d}"
        local_run_dir  = os.path.join(local_base, f"run{run_id:06d}")
        os.makedirs(local_run_dir, exist_ok=True)
        self._evio_dir = local_run_dir

        # Build rsync pattern list: "*.evio.NNN" for each index
        patterns = []
        for i in range(f_start, f_end + 1):
            patterns += ["--include", f"*{i:04d}"]
        cmd = (["rsync", "-avz", "--progress"] +
               ["--include", "*/", "--include", "*.evio.*"] +
               patterns +
               ["--exclude", "*",
                f"{host}:{remote_run_dir}/",
                local_run_dir + "/"])
        self._log(f"<span style='color:#8b949e'>$ {' '.join(cmd)}</span>")
        self._status_lbl.setText("Getting data…")
        self._launch_process(cmd)

    # -- Replay step --

    def _run_replay(self):
        evio_path = self._evio_edit.text().strip() or self._evio_dir
        if not evio_path:
            self._log("<span style='color:#f85149'>No EVIO path specified for replay.</span>")
            self._set_running(False)
            return
        out_dir = self._outdir_edit.text().strip()
        if not out_dir:
            out_dir = evio_path + "_recon"
        os.makedirs(out_dir, exist_ok=True)
        self._recon_dir = out_dir

        cmd = [_REPLAY_RECON_CMD, evio_path]
        cmd += ["-o", out_dir]
        cmd += ["-j", str(self._threads_spin.value())]

        n_ev = self._max_events_rep.text().strip()
        if n_ev and n_ev != "-1":
            cmd += ["-n", n_ev]

        n_f = self._max_files_spin.value()
        if n_f > 0:
            cmd += ["-f", str(n_f)]

        daq_cfg = self._daq_config_edit.text().strip()
        if daq_cfg:
            cmd += ["-c", daq_cfg]

        hycal_map = self._hycal_map_edit.text().strip()
        if hycal_map:
            cmd += ["-d", hycal_map]

        gem_ped = self._gem_ped_edit.text().strip()
        if gem_ped:
            cmd += ["-g", gem_ped]

        zsup = self._zerosup_edit.text().strip()
        if zsup:
            cmd += ["-z", zsup]

        if self._prad1_chk.isChecked():
            cmd.append("-p")

        self._log(f"<span style='color:#8b949e'>$ {' '.join(cmd)}</span>")
        self._status_lbl.setText("Running replay recon…")
        self._launch_process(cmd)

    # -- hadd merge step --

    def _run_hadd(self):
        import glob
        recon_dir = self._recon_dir
        if not recon_dir or not os.path.isdir(recon_dir):
            self._log("<span style='color:#f85149'>No recon directory for hadd.</span>")
            self._set_running(False)
            return

        root_files = sorted(glob.glob(os.path.join(recon_dir, "*.root")))
        if not root_files:
            self._log("<span style='color:#8b949e'>No ROOT files in recon dir, skipping hadd.</span>")
            self._run_next_step()
            return

        if len(root_files) == 1:
            self._hadd_out = root_files[0]
            self._log(f"<span style='color:#8b949e'>Single ROOT file, skipping hadd: {self._hadd_out}</span>")
            self._run_next_step()
            return

        # Determine merged output filename
        run_num = self._run_edit.text().strip()
        if run_num:
            out_name = f"prad_{int(run_num):06d}_recon.root"
        else:
            # Derive from first file (e.g. prad_024436_recon_0000.root → prad_024436_recon.root)
            first = os.path.basename(root_files[0])
            m = re.match(r'(prad_\d+_recon).*\.root', first)
            out_name = (m.group(1) + ".root") if m else "merged_recon.root"

        self._hadd_out = os.path.join(recon_dir, out_name)
        self._hadd_inputs = list(root_files)

        cmd = ["hadd", "-f", self._hadd_out] + root_files
        self._log(f"<span style='color:#8b949e'>$ {' '.join(cmd)}</span>")
        self._status_lbl.setText("Merging ROOT files (hadd)\u2026")
        self._launch_process(cmd)

    # -- Quick check step --

    def _run_qcheck(self):
        qc_input = self._qc_input_edit.text().strip() or self._hadd_out or self._recon_dir
        if not qc_input:
            self._log("<span style='color:#f85149'>No input specified for quick_check.</span>")
            self._set_running(False)
            return

        qc_out = self._qc_output_edit.text().strip()
        if not qc_out:
            run_num = self._run_edit.text().strip()
            if run_num:
                out_name = f"prad_{int(run_num):06d}_quick.root"
            else:
                out_name = "quick_check_out.root"
            base_dir = (os.path.dirname(qc_input)
                        if not os.path.isdir(qc_input) else qc_input)
            qc_out = os.path.join(base_dir, out_name)
        self._qcheck_out = qc_out

        cmd = [_QUICK_CHECK_CMD, qc_input]
        cmd += ["-o", qc_out]

        n_ev = self._max_events_qc.text().strip()
        if n_ev and n_ev != "-1":
            cmd += ["-n", n_ev]

        self._log(f"<span style='color:#8b949e'>$ {' '.join(cmd)}</span>")
        self._status_lbl.setText("Running quick check…")
        self._launch_process(cmd)

    # ------------------------------------------------------------------
    # QProcess management
    # ------------------------------------------------------------------

    def _launch_process(self, cmd: List[str]):
        proc = QProcess(self)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.setProcessEnvironment(QProcessEnvironment.systemEnvironment())
        self._process = proc
        proc.start(cmd[0], cmd[1:])

    def _on_stdout(self):
        if self._process is None:
            return
        data = self._process.readAllStandardOutput().data().decode(errors="replace")
        self._log(data.replace("\n", "<br>"))

    def _on_stderr(self):
        if self._process is None:
            return
        data = self._process.readAllStandardError().data().decode(errors="replace")
        self._log(f"<span style='color:#f0883e'>{data.replace(chr(10), '<br>')}</span>")

    def _on_finished(self, exit_code: int, _status):
        self._process = None
        color = "#3fb950" if exit_code == 0 else "#f85149"
        self._log(f"<span style='color:{color}'>[Exit {exit_code}]</span>")

        # After hadd: delete the individual recon ROOT files
        if exit_code == 0 and self._current_step == "hadd" and self._hadd_inputs:
            self._log("<span style='color:#8b949e'>Deleting individual recon ROOT files\u2026</span>")
            for f in self._hadd_inputs:
                if os.path.isfile(f):
                    try:
                        os.remove(f)
                    except Exception as exc:
                        self._log(f"<span style='color:#f85149'>Delete failed ({f}): {exc}</span>")
            self._hadd_inputs = []
            self._log("<span style='color:#3fb950'>Individual recon files deleted.</span>")

        # Auto-delete evio files if requested
        if exit_code == 0 and self._auto_delete_chk.isChecked() \
                and self._pending_steps and self._pending_steps[0] == "qcheck":
            evio_path = self._evio_edit.text().strip() or self._evio_dir
            if evio_path and os.path.isdir(evio_path):
                self._log(f"<span style='color:#f0883e'>Auto-deleting EVIO dir: {evio_path}</span>")
                try:
                    shutil.rmtree(evio_path)
                    self._log("<span style='color:#3fb950'>EVIO files deleted.</span>")
                except Exception as exc:
                    self._log(f"<span style='color:#f85149'>Delete failed: {exc}</span>")

        if exit_code == 0:
            self._run_next_step()
        else:
            self._pending_steps.clear()
            self._set_running(False)
            # If quick_check produced output even on non-zero exit, try loading
            if self._qcheck_out and os.path.isfile(self._qcheck_out):
                self.rootFileReady.emit(self._qcheck_out)

        # Emit when quick_check succeeded
        if exit_code == 0 and self._qcheck_out and os.path.isfile(self._qcheck_out):
            self.rootFileReady.emit(self._qcheck_out)

    def _on_stop(self):
        self._pending_steps.clear()
        if self._process is not None:
            self._process.kill()
        self._set_running(False)

    def _set_running(self, running: bool):
        self._stop_btn.setEnabled(running)
        for btn in (self._scp_btn, self._replay_btn, self._qcheck_btn, self._do_all_btn):
            btn.setEnabled(not running)
        if not running:
            self._status_lbl.setText("Ready")

    # ------------------------------------------------------------------
    # Console helpers
    # ------------------------------------------------------------------

    def _log(self, html: str):
        self._console.moveCursor(self._console.textCursor().MoveOperation.End)
        self._console.insertHtml(html)
        self._console.insertHtml("<br>")
        self._console.moveCursor(self._console.textCursor().MoveOperation.End)


def _check_disk_space(remote_host, remote_run_dir, local_base, f_start, f_end):
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10",
         remote_host, f"ls -l {remote_run_dir}/ 2>/dev/null"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 255:
        raise RuntimeError(result.stderr.strip() or "SSH connection failed")

    needed = 0
    counted = 0
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 9:
            continue
        m = re.search(r'\.evio\.(\d+)$', parts[-1])
        if not m:
            continue
        n = int(m.group(1))
        if f_start <= n <= f_end:
            try:
                needed += int(parts[4])
                counted += 1
            except (ValueError, IndexError):
                pass

    if counted == 0:
        needed = (f_end - f_start + 1) * _EVIO_BYTES_PER_FILE_EST

    check_path = local_base
    while check_path and not os.path.exists(check_path):
        check_path = os.path.dirname(check_path)
    free = shutil.disk_usage(check_path or "/").free
    return needed, free


# ===========================================================================
#  Results Panel (right side)
# ===========================================================================

class ResultsPanel(QWidget):
    """Tabbed display of quick_check ROOT file contents."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._modules = []
        self._loader: Optional[_RootLoader] = None
        self._build_ui()
        self._try_load_modules()

    def _try_load_modules(self):
        if os.path.isfile(MODULES_JSON):
            try:
                self._modules = load_modules(MODULES_JSON)
                self._map_widget.set_modules(self._modules)
                self._map_widget.set_palette("viridis")
            except Exception:
                pass

    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # header
        hdr = QHBoxLayout()
        self._file_lbl = QLabel("No file loaded")
        self._file_lbl.setStyleSheet(themed(
            "QLabel{color:#8b949e;font-family:Consolas;font-size:9pt;}"))
        self._reload_btn = QPushButton("Reload")
        self._reload_btn.setFixedWidth(70)
        self._reload_btn.setStyleSheet(_BTN_NORMAL)
        self._reload_btn.clicked.connect(self._reload)
        self._reload_btn.setEnabled(False)
        hdr.addWidget(self._file_lbl)
        hdr.addStretch()
        hdr.addWidget(self._reload_btn)
        root.addLayout(hdr)

        # tabs
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(themed(
            "QTabWidget::pane{border:1px solid #30363d;background:#0d1117;}"
            "QTabBar::tab{background:#161b22;color:#8b949e;"
            "padding:4px 14px;border:1px solid #30363d;"
            "border-bottom:none;font-family:Consolas;font-size:10pt;}"
            "QTabBar::tab:selected{background:#0d1117;color:#c9d1d9;}"
            "QTabBar::tab:hover{background:#21262d;color:#c9d1d9;}"))

        # Tab 0: HyCal map
        self._map_widget = HyCalReplayMapWidget()
        map_container = QWidget()
        map_lay = QVBoxLayout(map_container)
        map_lay.setContentsMargins(4, 4, 4, 4)

        map_ctrl = QHBoxLayout()
        map_mode_lbl = QLabel("Show:")
        map_mode_lbl.setStyleSheet(_LBL_SS)
        self._map_mode_combo = QComboBox()
        self._map_mode_combo.addItems(["Module hits (count)", "Mean energy"])
        self._map_mode_combo.setStyleSheet(_COMBO_SS)
        self._map_mode_combo.setFont(QFont("Consolas", 10))
        self._map_mode_combo.currentIndexChanged.connect(self._refresh_map)
        pal_lbl = QLabel("Palette:")
        pal_lbl.setStyleSheet(_LBL_SS)
        self._pal_combo = QComboBox()
        self._pal_combo.addItems(PALETTE_NAMES)
        self._pal_combo.setCurrentText("viridis")
        self._pal_combo.setStyleSheet(_COMBO_SS)
        self._pal_combo.setFont(QFont("Consolas", 10))
        self._pal_combo.currentIndexChanged.connect(
            lambda i: self._map_widget.set_palette(PALETTE_NAMES[i]))
        map_ctrl.addWidget(map_mode_lbl)
        map_ctrl.addWidget(self._map_mode_combo)
        map_ctrl.addSpacing(12)
        map_ctrl.addWidget(pal_lbl)
        map_ctrl.addWidget(self._pal_combo)
        map_ctrl.addStretch()

        map_lay.addLayout(map_ctrl)
        map_lay.addWidget(self._map_widget, stretch=1)
        self._tabs.addTab(map_container, "HyCal Map")

        # Tab 1: Hit Position 2D
        self._hit_pos_widget = Hist2DWidget("Hit Position", "x (mm)", "y (mm)")
        self._tabs.addTab(self._hit_pos_widget, "Hit Position")

        # Tab 2: Energy Spectra
        energy_tab = QWidget()
        eg = QVBoxLayout(energy_tab)
        eg.setContentsMargins(4, 4, 4, 4)
        eg.setSpacing(4)
        self._h_1cl   = Hist1DWidget("1-cluster energy")
        self._h_2cl   = Hist1DWidget("2-cluster energy")
        self._h_all   = Hist1DWidget("All clusters energy")
        self._h_tot   = Hist1DWidget("Total energy")
        # Give each spectrum a distinct, readable colour
        self._h_1cl.BAR_COLOR = "#3fb950"   # green
        self._h_2cl.BAR_COLOR = "#58a6ff"   # blue
        self._h_all.BAR_COLOR = "#d29922"   # amber
        self._h_tot.BAR_COLOR = "#f85149"   # red
        for h in (self._h_1cl, self._h_2cl, self._h_all, self._h_tot):
            eg.addWidget(h)
        self._tabs.addTab(energy_tab, "Energy Spectra")

        # Tab 3: Energy vs Theta 2D
        self._ev_theta_widget = Hist2DWidget("Energy vs θ", "θ (deg)", "E (GeV)")
        self._tabs.addTab(self._ev_theta_widget, "Energy vs Theta")

        # Tab 4: Moller Analysis
        moller_tab = QWidget()
        mg = QVBoxLayout(moller_tab)
        mg.setContentsMargins(4, 4, 4, 4)
        mg.setSpacing(4)
        mol_top = QHBoxLayout()
        self._h_moller_z    = Hist1DWidget("Moller Z vertex")
        self._h_moller_z.auto_cb_fit = True
        self._h_moller_z.cb_fit_range_sigma = (3.0, 1.5)  # left 3σ, right 1.5σ
        self._h_moller_phi  = Hist1DWidget("Moller Φ diff")
        mol_top.addWidget(self._h_moller_z)
        mol_top.addWidget(self._h_moller_phi)
        mol_bot = QHBoxLayout()
        self._h_moller_x = Hist1DWidget("Moller X center")
        self._h_moller_x.auto_cb_fit = True
        self._h_moller_x.cb_fit_range = (5.0, 2.5)  # left 5 mm, right 2.5 mm from peak
        self._h_moller_y = Hist1DWidget("Moller Y center")
        self._h_moller_y.auto_cb_fit = True
        self._h_moller_y.cb_fit_range = (5.0, 2.5)  # left 5 mm, right 2.5 mm from peak
        mol_bot.addWidget(self._h_moller_x)
        mol_bot.addWidget(self._h_moller_y)
        mg.addLayout(mol_top, stretch=1)
        mg.addLayout(mol_bot, stretch=1)
        # 2-arm Moller position 2D
        self._moller_2arm = Hist2DWidget("2-arm Moller position", "x1 (mm)", "x2 (mm)")
        mg.addWidget(self._moller_2arm, stretch=2)
        self._tabs.addTab(moller_tab, "Moller")

        # Tab 5: Physics Yields
        yields_tab = QWidget()
        yg = QVBoxLayout(yields_tab)
        yg.setContentsMargins(4, 4, 4, 4)
        yg.setSpacing(4)
        self._h_ep    = Hist1DWidget("ep yield")
        self._h_ee    = Hist1DWidget("ee yield")
        self._h_ratio = Hist1DWidget("ep/ee ratio")
        self._h_ep.BAR_COLOR = "#3a86ff"
        self._h_ee.BAR_COLOR = "#ff6b6b"
        self._h_ratio.BAR_COLOR = "#ffd166"
        yg.addWidget(self._h_ep)
        yg.addWidget(self._h_ee)
        yg.addWidget(self._h_ratio)
        self._tabs.addTab(yields_tab, "Physics Yields")

        root.addWidget(self._tabs, stretch=1)

        # status bar
        self._loading_lbl = QLabel("")
        self._loading_lbl.setStyleSheet(_LBL_MUT)
        root.addWidget(self._loading_lbl)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_file(self, path: str):
        if not HAS_UPROOT:
            self._file_lbl.setText("uproot not installed — cannot read ROOT files")
            self._loading_lbl.setText(
                "Install: pip install uproot numpy")
            return
        if not os.path.isfile(path):
            self._loading_lbl.setText(f"File not found: {path}")
            return
        self._current_path = path
        self._file_lbl.setText(os.path.basename(path))
        self._reload_btn.setEnabled(True)
        self._loading_lbl.setText("Loading…")
        self._loader = _RootLoader(path, self)
        self._loader.finished.connect(self._on_loaded)
        self._loader.start()

    def _reload(self):
        if hasattr(self, "_current_path"):
            self.load_file(self._current_path)

    def _on_loaded(self, data: dict, error: str):
        self._loader = None
        if error:
            self._loading_lbl.setText(f"Error: {error}")
            return
        self._data = data
        self._loading_lbl.setText(
            f"Loaded: {len(data)} datasets")
        self._populate(data)

    # ------------------------------------------------------------------
    # Populate widgets from loaded data
    # ------------------------------------------------------------------

    def _populate(self, data: dict):
        # HyCal map
        self._refresh_map()

        # Hit position 2D
        if "hit_pos" in data:
            vals, xe, ye = data["hit_pos"]
            self._hit_pos_widget.set_data(vals, xe, ye)

        # Energy spectra
        mapping = {
            "one_cluster_energy": self._h_1cl,
            "two_cluster_energy": self._h_2cl,
            "clusters_energy":    self._h_all,
            "total_energy":       self._h_tot,
        }
        for key, widget in mapping.items():
            if key in data:
                vals, edges = data[key]
                widget.set_data(vals, edges)

        # Energy vs theta
        if "energy_plots/h2_energy_theta" in data:
            vals, xe, ye = data["energy_plots/h2_energy_theta"]
            self._ev_theta_widget.set_data(vals, xe, ye)

        # Moller 1D (actual histogram names have h_ prefix)
        for key, widget in (
            ("moller/h_moller_z",        self._h_moller_z),
            ("moller/h_moller_phi_diff", self._h_moller_phi),
            ("moller/h_moller_x",        self._h_moller_x),
            ("moller/h_moller_y",        self._h_moller_y),
        ):
            if key in data:
                v, e = data[key]
                widget.set_data(v, e)
        if "moller/h2_moller_pos" in data:
            vals, xe, ye = data["moller/h2_moller_pos"]
            self._moller_2arm.set_data(vals, xe, ye)

        # Physics yields
        for key, widget in (
            ("physics_yields/ep_yield",    self._h_ep),
            ("physics_yields/ee_yield",    self._h_ee),
            ("physics_yields/yield_ratio", self._h_ratio),
        ):
            if key in data:
                v, e = data[key]
                widget.set_data(v, e)

    def _refresh_map(self):
        if not hasattr(self, "_data"):
            return
        data = self._data
        mode = self._map_mode_combo.currentIndex()
        if mode == 1 and data.get("module_means"):
            self._map_widget.set_module_counts(data["module_means"])
        else:
            self._map_widget.set_module_counts(data.get("module_counts", {}))


# ===========================================================================
#  Main Window
# ===========================================================================

class MainWindow(QMainWindow):
    def __init__(self, initial_root: str = ""):
        super().__init__()
        self.setWindowTitle("PRad-2 Replay Viewer")
        self.resize(1600, 900)
        self.setStyleSheet(themed(
            "QMainWindow{background:#0d1117;}"
            "QWidget{background:#0d1117;color:#c9d1d9;}"
            "QSplitter::handle{background:#21262d;}"
            "QLabel{color:#c9d1d9;}"))

        # menu bar
        mb = self.menuBar()
        mb.setStyleSheet(themed(
            "QMenuBar{background:#161b22;color:#c9d1d9;"
            "font-family:Consolas;font-size:10pt;}"
            "QMenuBar::item:selected{background:#21262d;}"
            "QMenu{background:#161b22;color:#c9d1d9;border:1px solid #30363d;}"
            "QMenu::item:selected{background:#1f6feb;}"))

        file_menu = mb.addMenu("File")
        open_act = file_menu.addAction("Open ROOT file…")
        open_act.triggered.connect(self._open_root)
        file_menu.addSeparator()
        quit_act = file_menu.addAction("Quit")
        quit_act.triggered.connect(self.close)

        view_menu = mb.addMenu("View")
        theme_menu = view_menu.addMenu("Theme")
        for t in available_themes():
            act = theme_menu.addAction(t.capitalize())
            act.triggered.connect(lambda _c=False, tn=t: self._change_theme(tn))

        # main splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(4)

        self._ctrl = ControlPanel()
        self._results = ResultsPanel()

        self._ctrl.rootFileReady.connect(self._results.load_file)

        splitter.addWidget(self._ctrl)
        splitter.addWidget(self._results)
        splitter.setSizes([420, 1180])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)

        self.setCentralWidget(splitter)

        if initial_root:
            self._results.load_file(initial_root)

    def _open_root(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open ROOT File", "", "ROOT files (*.root);;All files (*)")
        if path:
            self._results.load_file(path)

    def _change_theme(self, name: str):
        set_theme(name)
        apply_theme_palette(QApplication.instance())
        # Re-apply stylesheet
        self.setStyleSheet(themed(
            "QMainWindow{background:#0d1117;}"
            "QWidget{background:#0d1117;color:#c9d1d9;}"
            "QSplitter::handle{background:#21262d;}"
            "QLabel{color:#c9d1d9;}"))


# ===========================================================================
#  Entry point
# ===========================================================================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PRad-2 Replay Viewer")

    set_theme("dark")
    apply_theme_palette(app)

    initial = sys.argv[1] if len(sys.argv) > 1 else ""
    win = MainWindow(initial_root=initial)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
