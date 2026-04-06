"""
Automatic Gain Equalizer for HyCal
===================================
Moves the beam to each crystal module, collects peak height histogram data
from prad2_server, finds the right edge of the Bremsstrahlung spectrum, and
adjusts HV via prad2hvd until the edge aligns with a target ADC value.

Classes:
    SpectrumAnalyzer  — pure analysis (edge finding, voltage step)
    ServerClient      — HTTP client for prad2_server
    HVClient          — WebSocket client for prad2hvd
    GainScanEngine    — orchestrates the full scan loop
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from scan_utils import Module, module_to_ptrans, ptrans_in_limits
from scan_epics import epics_move_to, epics_is_moving, epics_stop, SPMG
from scan_engine import build_scan_path


# ============================================================================
#  Spectrum Analysis
# ============================================================================

class SpectrumAnalyzer:
    """Analyse peak height histograms to find the spectrum right edge."""

    def __init__(self, target_adc: float = 3200.0,
                 bin_step: float = 10.0, bin_min: float = 0.0,
                 min_bin_count: int = 10):
        self.target_adc = target_adc
        self.bin_step = bin_step
        self.bin_min = bin_min
        self.min_bin_count = min_bin_count

    def find_right_edge(self, bins: List[int]) -> Optional[int]:
        """Find the right-edge bin index of the continuum spectrum.

        Walks from the rightmost bin leftward, skipping sparse pile-up.
        The edge is the first bin (from the right) where:
          1. bin count >= min_bin_count (default 10)
          2. average of the 3 bins to its left > this bin's count
             (confirms a falling edge, not the spectrum body)

        Returns the bin index, or None if no edge found.
        """
        n = len(bins)
        for i in range(n - 1, 3, -1):
            if bins[i] < self.min_bin_count:
                continue
            left_avg = sum(bins[i - 3:i]) / 3.0
            if left_avg > bins[i]:
                return i
        return None

    def edge_to_adc(self, bin_index: int) -> float:
        """Convert bin index to ADC value (centre of bin)."""
        return self.bin_min + (bin_index + 0.5) * self.bin_step

    def compute_voltage_step(self, edge_adc: float,
                             current_vset: float,
                             gain_factor: float = 0.5) -> float:
        """Compute HV adjustment to move the spectrum edge toward target.

        Uses proportional scaling, then snaps to the nearest allowed step
        from [5, 10, 20, 30] V.  Positive = increase voltage.
        """
        if self.target_adc <= 0 or current_vset <= 0:
            return 0.0
        ratio = (self.target_adc - edge_adc) / self.target_adc
        raw_dv = ratio * current_vset * gain_factor

        sign = 1 if raw_dv > 0 else -1
        abs_dv = abs(raw_dv)
        for step in [30, 20, 10, 5]:
            if abs_dv >= step * 0.7:
                return sign * step
        return sign * 5  # minimum step


# ============================================================================
#  prad2_server HTTP Client
# ============================================================================

class ServerClient:
    """HTTP client for prad2_server histogram and occupancy APIs."""

    def __init__(self, url: str = "http://clondaq6:5051", log_fn=None,
                 read_only: bool = False):
        self.url = url.rstrip("/")
        self._key_map: Dict[str, str] = {}
        self._log = log_fn or (lambda msg, **kw: None)
        self._read_only = read_only

    def _get(self, path: str) -> Any:
        import urllib.request
        with urllib.request.urlopen(f"{self.url}{path}", timeout=10) as r:
            return json.loads(r.read())

    def _post(self, path: str) -> Any:
        import urllib.request
        req = urllib.request.Request(f"{self.url}{path}", method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def get_config(self) -> dict:
        self._log("Server: GET /api/config")
        return self._get("/api/config")

    def clear_histograms(self):
        if self._read_only:
            self._log("Server: POST /api/hist/clear [BLOCKED — read-only]", level="warn")
            return {}
        self._log("Server: POST /api/hist/clear")
        return self._post("/api/hist/clear")

    def get_occupancy(self) -> dict:
        return self._get("/api/occupancy")

    def get_height_histogram(self, key: str) -> dict:
        self._log(f"Server: GET /api/heighthist/{key}")
        return self._get(f"/api/heighthist/{key}")

    def build_key_map(self) -> Dict[str, str]:
        """Build module_name -> histogram_key mapping from server config."""
        cfg = self.get_config()
        daq = cfg.get("daq", [])
        crate_roc = cfg.get("crate_roc", {})
        key_map: Dict[str, str] = {}
        for entry in daq:
            name = entry.get("name", "")
            crate = str(entry.get("crate", ""))
            slot = entry.get("slot", 0)
            ch = entry.get("channel", 0)
            roc = crate_roc.get(crate, crate)
            key_map[name] = f"{roc}_{slot}_{ch}"
        self._key_map = key_map
        return key_map

    def get_module_counts(self, module_name: str) -> int:
        """Get hit count for a single module from occupancy."""
        key = self._key_map.get(module_name, "")
        if not key:
            return 0
        occ = self.get_occupancy()
        return occ.get("occ", {}).get(key, 0)


# ============================================================================
#  prad2hvd WebSocket Client
# ============================================================================

class HVClient:
    """WebSocket client for prad2hvd voltage control."""

    def __init__(self, url: str = "ws://clonpc19:8765", log_fn=None,
                 read_only: bool = False):
        self.url = url
        self._ws: Any = None
        self._lock = threading.Lock()
        self._log = log_fn or (lambda msg, **kw: None)
        self._read_only = read_only

    def connect(self, password: str = ""):
        import websocket
        self._log(f"HV: connecting to {self.url}")
        self._ws = websocket.create_connection(self.url, timeout=10)
        init_msg = json.loads(self._ws.recv())
        auth_required = init_msg.get("auth_required", False)
        if auth_required and password:
            self._log("HV: authenticating (Expert)")
            self._send({"type": "auth", "password": password})
            resp = self._recv_until("auth_response", timeout=5)
            if not resp or resp.get("access_level", 0) < 2:
                raise RuntimeError("HV authentication failed (Expert level required)")
            self._log("HV: authenticated OK")
        self._log("HV: connected")

    def _send(self, msg: dict):
        with self._lock:
            self._ws.send(json.dumps(msg))

    def _recv_until(self, msg_type: str, timeout: float = 10) -> Optional[dict]:
        """Read messages until we get the expected type or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._ws.settimeout(max(0.1, deadline - time.time()))
            try:
                raw = self._ws.recv()
                msg = json.loads(raw)
                if msg.get("type") == msg_type:
                    return msg
            except Exception:
                break
        return None

    def get_voltage(self, name: str) -> Optional[dict]:
        """Query current voltage info for a module by name.

        Returns dict with vset, vmon, limit, on, status, or None on error.
        """
        self._send({"type": "get_voltage", "name": name})
        resp = self._recv_until("get_voltage_response", timeout=5)
        if resp:
            self._log(f"HV: GET {name} → vset={resp.get('vset'):.1f} "
                      f"vmon={resp.get('vmon'):.1f} limit={resp.get('limit'):.0f}")
        else:
            self._log(f"HV: GET {name} → no response", level="warn")
        return resp

    def set_voltage(self, name: str, value: float) -> bool:
        """Set voltage by module name. Returns True if command sent."""
        if self._read_only:
            self._log(f"HV: SET {name} = {value:.2f} V [BLOCKED — read-only]", level="warn")
            return True  # pretend success so scan continues
        self._log(f"HV: SET {name} = {value:.2f} V")
        try:
            self._send({"type": "set_voltage_by_name",
                         "name": name, "value": round(value, 2)})
            return True
        except Exception:
            return False

    def close(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None


# ============================================================================
#  Gain Scan Engine
# ============================================================================

class GainScanState:
    IDLE       = "IDLE"
    MOVING     = "MOVING"
    COLLECTING = "COLLECTING"
    ANALYZING  = "ANALYZING"
    ADJUSTING  = "ADJUSTING"
    CONVERGED  = "CONVERGED"
    FAILED     = "FAILED"
    COMPLETED  = "COMPLETED"


class GainScanEngine:
    """Orchestrates the automatic gain equalization scan."""

    # defaults (configurable before start)
    target_adc: float = 3200.0
    min_counts: int = 10000
    max_iterations: int = 8
    convergence_tol: float = 100.0   # ADC units
    hv_settle_time: float = 10.0     # seconds
    pos_threshold: float = 0.5       # mm
    beam_threshold: float = 0.3      # nA
    collect_poll_sec: float = 2.0    # occupancy poll interval
    move_timeout: float = 300.0      # seconds
    edge_adc_min: float = 500.0      # reject edges below this
    edge_adc_max: float = 3900.0     # reject edges above this

    def __init__(self, motor_ep, server: ServerClient, hv: HVClient,
                 modules: List[Module], log_fn,
                 key_map: Dict[str, str]):
        self.ep = motor_ep
        self.server = server
        self.hv = hv
        self.path, _ = build_scan_path(modules)
        self.log = log_fn
        self.key_map = key_map
        self.analyzer = SpectrumAnalyzer(target_adc=self.target_adc)

        self.state = GainScanState.IDLE
        self.current_idx = 0
        self.current_iteration = 0
        self.last_edge_adc: Optional[float] = None
        self.last_edge_bin: Optional[int] = None
        self.last_dv: Optional[float] = None
        self.last_bins: List[int] = []
        self.last_vset: Optional[float] = None
        self.module_counts = 0
        self.collect_rate: float = 0.0  # Hz, updated during collection

        # per-iteration history for the current module (for screenshot report)
        self.iteration_history: List[Dict] = []
        # directory for saving reports
        self.report_dir: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "logs")

        self.converged: set = set()
        self.failed: set = set()

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._skip = threading.Event()
        self._paused = False

    @property
    def current_module(self) -> Optional[Module]:
        if 0 <= self.current_idx < len(self.path):
            return self.path[self.current_idx]
        return None

    def start(self, start_idx: int = 0, count: int = 0):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._skip.clear()
        self._paused = False
        self.current_idx = start_idx
        self._start_idx = start_idx
        self._end_idx = min(start_idx + count, len(self.path)) if count > 0 else len(self.path)
        self.converged.clear()
        self.failed.clear()
        self.state = GainScanState.MOVING
        self.analyzer.target_adc = self.target_adc
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self):
        if self.state not in (GainScanState.IDLE, GainScanState.COMPLETED):
            self._paused = True
            self.log("Gain scan paused", level="warn")

    def resume(self):
        if self._paused:
            self._paused = False
            self.log("Gain scan resumed")

    def stop(self):
        self._stop.set()
        self._skip.set()
        self._paused = False
        epics_stop(self.ep)
        self.log("Gain scan stopped", level="warn")

    def skip_module(self):
        self._skip.set()

    # -- main loop ----------------------------------------------------------

    def _run(self):
        n = self._end_idx - self._start_idx
        self.log(f"Gain scan started: {n} modules, target ADC {self.target_adc:.0f}")
        try:
            for i in range(self._start_idx, self._end_idx):
                if self._stop.is_set():
                    break
                self.current_idx = i
                self.current_iteration = 0
                self.last_edge_adc = None
                self.last_edge_bin = None
                self.last_dv = None
                self.last_bins = []
                self.last_vset = None
                self.iteration_history = []
                mod = self.path[i]
                px, py = module_to_ptrans(mod.x, mod.y)

                # -- move --
                self.state = GainScanState.MOVING
                self.log(f"[{i+1}/{len(self.path)}] Moving to {mod.name}")
                if not epics_move_to(self.ep, px, py):
                    self.log(f"SKIPPED {mod.name}: outside limits", level="warn")
                    continue
                if not self._wait_move_done():
                    break

                # check DAQ key
                key = self.key_map.get(mod.name)
                if not key:
                    self.log(f"SKIPPED {mod.name}: no DAQ mapping", level="warn")
                    continue

                # -- iterate: collect → analyze → adjust --
                success = False
                for iteration in range(self.max_iterations):
                    if self._stop.is_set():
                        break
                    if self._skip.is_set():
                        self._skip.clear()
                        self.log(f"Module {mod.name} skipped")
                        break
                    self.current_iteration = iteration + 1

                    # collect
                    self.state = GainScanState.COLLECTING
                    self.module_counts = 0
                    try:
                        self.server.clear_histograms()
                    except Exception as e:
                        self.log(f"Server error (clear): {e}", level="error")
                        self._mark_failed(i, mod)
                        break
                    if not self._wait_for_counts(mod, key):
                        if self._stop.is_set():
                            break
                        self._mark_failed(i, mod)
                        break

                    # read current HV before analysis (for report)
                    try:
                        hv_info = self.hv.get_voltage(mod.name)
                        if hv_info:
                            self.last_vset = hv_info.get("vset")
                    except Exception:
                        pass

                    # analyze
                    self.state = GainScanState.ANALYZING
                    try:
                        hist = self.server.get_height_histogram(key)
                    except Exception as e:
                        self.log(f"Server error (hist): {e}", level="error")
                        self._mark_failed(i, mod)
                        break
                    bins = hist.get("bins", [])
                    self.last_bins = bins
                    edge = self.analyzer.find_right_edge(bins)
                    self.last_edge_bin = edge
                    if edge is None:
                        self.log(f"{mod.name} iter {iteration+1}: no edge found",
                                 level="error")
                        self._mark_failed(i, mod)
                        break
                    edge_adc = self.analyzer.edge_to_adc(edge)
                    self.last_edge_adc = edge_adc

                    # record iteration snapshot for report
                    self.iteration_history.append({
                        "iteration": iteration + 1,
                        "bins": list(bins),
                        "edge_bin": edge,
                        "edge_adc": edge_adc,
                        "vset": self.last_vset,
                        "time": datetime.now().strftime("%H:%M:%S"),
                    })

                    if edge_adc < self.edge_adc_min or edge_adc > self.edge_adc_max:
                        self.log(f"{mod.name} iter {iteration+1}: edge {edge_adc:.0f} "
                                 f"out of range [{self.edge_adc_min:.0f}, {self.edge_adc_max:.0f}]",
                                 level="error")
                        self._mark_failed(i, mod)
                        break

                    # check convergence
                    if abs(edge_adc - self.target_adc) <= self.convergence_tol:
                        self.converged.add(i)
                        self.state = GainScanState.CONVERGED
                        self.log(f"{mod.name}: CONVERGED at {edge_adc:.0f} "
                                 f"(iter {iteration+1})")
                        self._save_module_report(mod, "success")
                        success = True
                        break

                    # adjust HV
                    self.state = GainScanState.ADJUSTING
                    info = self.hv.get_voltage(mod.name)
                    if info is None:
                        self.log(f"{mod.name}: HV read failed", level="error")
                        self._mark_failed(i, mod)
                        break
                    current_v = info.get("vset", 0)
                    self.last_vset = current_v
                    limit_v = info.get("limit", 99999)
                    dv = self.analyzer.compute_voltage_step(edge_adc, current_v)
                    self.last_dv = dv
                    new_v = current_v + dv

                    if new_v > limit_v:
                        self.log(f"{mod.name}: would exceed limit "
                                 f"({new_v:.1f} > {limit_v:.1f})", level="error")
                        self._mark_failed(i, mod)
                        break

                    self.log(f"{mod.name} iter {iteration+1}: edge={edge_adc:.0f} "
                             f"ΔV={dv:+.0f} ({current_v:.1f}→{new_v:.1f})")
                    if not self.hv.set_voltage(mod.name, new_v):
                        self.log(f"{mod.name}: HV set failed", level="error")
                        self._mark_failed(i, mod)
                        break

                    # wait for HV to settle
                    self._wait_paused(self.hv_settle_time)
                    if self._stop.is_set():
                        break
                else:
                    if not success and i not in self.failed:
                        self.log(f"{mod.name}: max iterations reached "
                                 f"(last edge={self.last_edge_adc:.0f})", level="warn")
                        self._mark_failed(i, mod)
        finally:
            if self._stop.is_set():
                self.state = GainScanState.IDLE
                self.log("Gain scan stopped by user")
            else:
                self.state = GainScanState.COMPLETED
                self.log(f"Gain scan COMPLETE: {len(self.converged)} converged, "
                         f"{len(self.failed)} failed", level="warn")

    def _mark_failed(self, idx: int, mod: Module):
        self.failed.add(idx)
        self.state = GainScanState.FAILED
        self._save_module_report(mod, "failure")

    def _save_module_report(self, mod: Module, status: str):
        """Save a vertically concatenated histogram screenshot for a module.

        Each iteration is drawn as a small histogram panel, stacked top to
        bottom in time order.  Filename: GE_{name}_{status}_{time}.png
        """
        history = self.iteration_history
        if not history:
            return
        try:
            from PyQt6.QtGui import QImage, QPainter, QColor, QPen, QFont
            from PyQt6.QtCore import Qt, QRectF
        except ImportError:
            self.log("Cannot save report: PyQt6 not available in thread", level="warn")
            return

        PANEL_W, PANEL_H = 600, 160
        PAD_L, PAD_R, PAD_T, PAD_B = 50, 12, 32, 20
        img_h = PANEL_H * len(history)
        img = QImage(PANEL_W, img_h, QImage.Format.Format_RGB32)
        img.fill(QColor("#0d1117"))

        p = QPainter(img)
        target_adc = self.target_adc
        bin_step = self.analyzer.bin_step
        bin_min = self.analyzer.bin_min

        for panel_idx, snap in enumerate(history):
            y0 = panel_idx * PANEL_H
            bins = snap["bins"]
            n = len(bins)
            if n == 0:
                continue
            vmax = max(bins) if bins else 1
            if vmax == 0: vmax = 1

            pw = PANEL_W - PAD_L - PAD_R
            ph = PANEL_H - PAD_T - PAD_B

            # header
            p.setPen(QColor("#58a6ff"))
            p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            title = f"{mod.name}  iter {snap['iteration']}"
            p.drawText(QRectF(PAD_L, y0 + 4, pw, PAD_T - 4),
                       Qt.AlignmentFlag.AlignLeft, title)
            p.setPen(QColor("#8b949e"))
            p.setFont(QFont("Consolas", 9))
            info = f"{snap['time']}"
            if snap.get("vset") is not None:
                info += f"  V={snap['vset']:.1f}"
            info += f"  edge={snap['edge_adc']:.0f}"
            p.drawText(QRectF(PAD_L, y0 + 4, pw, PAD_T - 4),
                       Qt.AlignmentFlag.AlignRight, info)

            # axes
            ax, ay = PAD_L, y0 + PAD_T
            p.setPen(QPen(QColor("#30363d"), 1))
            p.drawLine(ax, ay, ax, ay + ph)
            p.drawLine(ax, ay + ph, ax + pw, ay + ph)

            # bars
            bar_w = pw / n
            p.setPen(Qt.PenStyle.NoPen)
            for bi, v in enumerate(bins):
                if v <= 0: continue
                bh = v / vmax * ph
                bx = ax + bi * bar_w
                by = ay + ph - bh
                p.fillRect(QRectF(bx, by, max(bar_w - 0.3, 0.3), bh),
                           QColor("#58a6ff"))

            # target line (red dashed)
            target_bin = int((target_adc - bin_min) / bin_step) if bin_step > 0 else -1
            if 0 <= target_bin < n:
                tx = ax + (target_bin + 0.5) * bar_w
                p.setPen(QPen(QColor("#f85149"), 1.5, Qt.PenStyle.DashLine))
                p.drawLine(int(tx), ay, int(tx), ay + ph)

            # edge line (green)
            edge_bin = snap.get("edge_bin")
            if edge_bin is not None and 0 <= edge_bin < n:
                ex = ax + (edge_bin + 0.5) * bar_w
                p.setPen(QPen(QColor("#3fb950"), 2))
                p.drawLine(int(ex), ay, int(ex), ay + ph)

            # separator
            p.setPen(QPen(QColor("#30363d"), 1))
            p.drawLine(0, y0 + PANEL_H - 1, PANEL_W, y0 + PANEL_H - 1)

        p.end()

        os.makedirs(self.report_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"GE_{mod.name}_{status}_{ts}.png"
        path = os.path.join(self.report_dir, fname)
        img.save(path)
        self.log(f"Report saved: {fname}")

    def _wait_move_done(self) -> bool:
        t0 = time.time()
        while not self._stop.is_set():
            self._check_paused()
            if self._stop.is_set():
                return False
            if not epics_is_moving(self.ep):
                return True
            if time.time() - t0 > self.move_timeout:
                self.log(f"MOVE TIMEOUT after {self.move_timeout:.0f}s", level="error")
                return False
            time.sleep(0.1)
        return False

    LOW_RATE_THRESHOLD = 10.0  # Hz — warn if collection rate drops below this

    def _wait_for_counts(self, mod: Module, key: str) -> bool:
        """Poll occupancy until the target module has min_counts hits."""
        retries = 0
        prev_counts = 0
        prev_time = time.time()
        low_rate_warned = False
        self.collect_rate = 0.0
        while not self._stop.is_set() and not self._skip.is_set():
            self._check_paused()
            if self._stop.is_set():
                return False
            # beam trip check
            if self.beam_threshold > 0:
                bc = self.ep.get("beam_cur", None)
                if bc is not None and bc < self.beam_threshold:
                    self.log(f"BEAM TRIP: {bc:.3f} nA — waiting", level="warn")
                    while not self._stop.is_set():
                        bc2 = self.ep.get("beam_cur", 0.0)
                        if bc2 is not None and bc2 >= self.beam_threshold:
                            break
                        time.sleep(0.5)
                    if self._stop.is_set():
                        return False
                    self.log("BEAM RECOVERED — restarting collection", level="warn")
                    try:
                        self.server.clear_histograms()
                    except Exception:
                        pass
                    prev_counts = 0; prev_time = time.time(); low_rate_warned = False
            try:
                occ = self.server.get_occupancy()
                counts = occ.get("occ", {}).get(key, 0)
                self.module_counts = counts
                # compute rate
                now = time.time()
                dt = now - prev_time
                if dt > 0.5:
                    self.collect_rate = (counts - prev_counts) / dt
                    if self.collect_rate < self.LOW_RATE_THRESHOLD and counts > 0:
                        if not low_rate_warned:
                            self.log(f"{mod.name}: low rate {self.collect_rate:.1f} Hz "
                                     f"(< {self.LOW_RATE_THRESHOLD:.0f} Hz)", level="warn")
                            low_rate_warned = True
                    else:
                        low_rate_warned = False
                    prev_counts = counts; prev_time = now
                if counts >= self.min_counts:
                    return True
                retries = 0
            except Exception as e:
                retries += 1
                if retries > 3:
                    self.log(f"Server unreachable: {e}", level="error")
                    return False
            time.sleep(self.collect_poll_sec)
        return False

    def _check_paused(self):
        while self._paused and not self._stop.is_set():
            time.sleep(0.1)

    def _wait_paused(self, seconds: float):
        """Sleep for *seconds*, respecting pause and stop."""
        end = time.time() + seconds
        while time.time() < end:
            if self._stop.is_set():
                return
            self._check_paused()
            time.sleep(min(0.2, end - time.time()))
