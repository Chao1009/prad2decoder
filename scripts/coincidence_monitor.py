#!/usr/bin/env python3
"""
Scintillator–HyCal Coincidence Monitor
=======================================
Connects to a running prad2_server (HTTP REST API, port 5051 by default),
iterates through every event in the loaded file, and accumulates per-module
coincidence statistics between the four upstream scintillators (V1–V4) and
every HyCal module.

  Coincidence rate(V_i, M_j) = N(V_i fired AND M_j fired) / N(M_j fired)

Two event-selection modes are provided:

  AND mode — only events where ALL 4 veto scintillators fired AND at least
             one HyCal module fired are included in the statistics.

  OR  mode — events where ANY veto scintillator fired OR ANY HyCal module
             fired are included (i.e. any detector above threshold).

A channel "fired" when it has at least one FADC peak whose integral (above
pedestal) exceeds the user-specified threshold.

The bottom half of the window shows individual waveforms: the currently
selected scintillator (V1–V4) and the HyCal module last clicked on the map,
both fetched from the server for the event number entered in the Event Browser.

Usage
-----
    python scripts/coincidence_monitor.py [--url http://HOST:PORT]
                                          [--theme dark|light]
"""
from __future__ import annotations

import argparse
import json as json_mod
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

import urllib.request
import urllib.error

import numpy as np

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QDoubleSpinBox, QLineEdit, QProgressBar,
    QSplitter, QSizePolicy, QButtonGroup, QRadioButton, QGroupBox,
    QSpinBox, QFrame, QMessageBox,
)
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal, QRectF, QPointF
from PyQt6.QtGui import (
    QColor, QFont, QPen, QPainter, QPolygonF,
)

from hycal_geoview import (
    Module, load_modules, HyCalMapWidget, PALETTES, PALETTE_NAMES,
    cmap_qcolor,
    apply_theme_palette, set_theme, available_themes, THEME, themed,
)


# ===========================================================================
#  Paths & constants
# ===========================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
DB_DIR = SCRIPT_DIR / ".." / "database"
MODULES_JSON  = DB_DIR / "hycal_modules.json"
DAQ_MAP_JSON  = DB_DIR / "daq_map.json"
DAQ_CFG_JSON  = DB_DIR / "daq_config.json"

DEFAULT_URL = "http://localhost:5051"

# Scintillator names (keys resolved at runtime from daq_map + daq_config)
SCINTILLATORS: Dict[str, str] = {
    "V1": "V1",
    "V2": "V2",
    "V3": "V3",
    "V4": "V4",
}

# Default thresholds (FADC integral above pedestal, ADC·sample units)
DEFAULT_SCINT_THR = 200.0
DEFAULT_HYCAL_THR = 50.0

# Event-selection modes
MODE_AND = "AND"   # all 4 veto scintillators AND any HyCal module must fire
MODE_OR  = "OR"    # any veto scintillator OR any HyCal module fires

# LMS rejection: skip events with LMS trigger bit OR too many modules fired
LMS_TRIGGER_BIT  = 1 << 24   # bit 24 in trigger_bits word
LMS_MAX_MODULES  = 1000       # more than this many modules above threshold → LMS

# Cluster-size cut: events with fewer than this many HyCal modules above threshold
# are discarded as isolated electronic noise / discharge artefacts.
DEFAULT_MIN_CLUSTER_MODS = 2

# Top-level display modes
DISPLAY_COINC   = "coinc"    # coincidence rate / occupancy statistics
DISPLAY_INSTANT = "instant"  # live event-by-event ADC display

# Map display modes (used within DISPLAY_COINC)
VIEW_COINC   = "coincidence"  # colour = coincidence rate with selected scintillator
VIEW_OCC     = "occupancy"    # colour = number of events module fired above threshold
VIEW_INSTANT = "instant"      # colour = per-module ADC signal for current event

# Veto scintillator motor PVs  (prad:vetoN.VAL = setpoint, .RBV = read-back)
VETO_PV_BASES: Dict[str, str] = {
    "V1": "prad:veto1",
    "V2": "prad:veto2",
    "V3": "prad:veto3",
    "V4": "prad:veto4",
}
VETO_POLL_MS = 500   # 2 Hz

N_WORKERS  = 16    # parallel HTTP workers for event scanning
BATCH_SIZE = 400   # events per processing batch

CLK_MHZ = 250.0    # FADC clock (for x-axis in ns)


# ===========================================================================
#  Veto motor position reader
# ===========================================================================

class VetoMotorController:
    """Reads and writes position PVs for the four veto scintillator motors.

    PVs per motor:
        prad:vetoN.VAL  — setpoint (write to command a move)
        prad:vetoN.RBV  — actual read-back position
        prad:vetoN.MOVN — 1 while moving, 0 when at rest

    Degrades gracefully when pyepics is not installed.
    """

    def __init__(self) -> None:
        self._pvs: Dict[str, Any] = {}
        self._epics_ok = False
        try:
            import epics as _epics
            for vname, base in VETO_PV_BASES.items():
                self._pvs[f"{vname}_val"]  = _epics.PV(f"{base}.VAL")
                self._pvs[f"{vname}_rbv"]  = _epics.PV(f"{base}.RBV")
                self._pvs[f"{vname}_movn"] = _epics.PV(f"{base}.MOVN")
            self._epics_ok = True
        except ImportError:
            pass

    def get(self, key: str) -> Optional[float]:
        pv = self._pvs.get(key)
        if pv is None or not pv.connected:
            return None
        v = pv.get()
        return float(v) if v is not None else None

    def put(self, key: str, value: float) -> bool:
        """Write value to a VAL PV. Returns True on success."""
        pv = self._pvs.get(key)
        if pv is None or not pv.connected:
            return False
        pv.put(value)
        return True

    @property
    def available(self) -> bool:
        return self._epics_ok


# ===========================================================================
#  HTTP helpers
# ===========================================================================

def _load_crate_to_roc(path: Path) -> Dict[int, int]:
    """Return {crate_index: roc_tag} from daq_config.json roc_tags list.
    Only 'roc' type entries are included (not ti_slave, tdc, gem, etc.).
    """
    mapping: Dict[int, int] = {}
    try:
        with open(path) as f:
            cfg = json_mod.load(f)
        for entry in cfg.get("roc_tags", []):
            if entry.get("type") != "roc":
                continue
            crate = entry.get("crate")
            tag_raw = entry.get("tag", "")
            try:
                tag = int(tag_raw, 16) if isinstance(tag_raw, str) else int(tag_raw)
                mapping[int(crate)] = tag
            except (ValueError, TypeError):
                pass
    except Exception:
        pass
    return mapping


def _load_daq_map(path: Path,
                  crate_to_roc: Optional[Dict[int, int]] = None) -> Dict[str, str]:
    """Return {module_name: "roc_tag_slot_channel"} from daq_map.json.

    The event JSON produced by the C++ server uses the actual ROC tag (not the
    sequential crate index) as the first component of the channel key.  Pass
    crate_to_roc so the keys produced here match what the server emits.
    """
    with open(path) as f:
        entries = json_mod.load(f)
    result: Dict[str, str] = {}
    for e in entries:
        crate = e["crate"]
        roc = crate_to_roc.get(crate, crate) if crate_to_roc else crate
        result[e["name"]] = f"{roc}_{e['slot']}_{e['channel']}"
    return result


def _build_w_module_layers(modules_path: Path) -> Dict[str, int]:
    """Return {W_module_name: layer} where layer 1 is immediately around the beam hole.

    The central hole occupies rows 17-18, cols 17-18.  A module's layer is its
    Chebyshev distance to the nearest hole cell:
        layer = max(max(0, 17-row, row-18), max(0, 17-col, col-18))
    """
    with open(modules_path) as f:
        mods = json_mod.load(f)
    result: Dict[str, int] = {}
    for m in mods:
        name = m['n']
        if not name.startswith('W'):
            continue
        dr = max(0, 17 - m['row'], m['row'] - 18)
        dc = max(0, 17 - m['col'], m['col'] - 18)
        result[name] = max(dr, dc)
    return result


def _http_get(url: str, timeout: float = 5.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json_mod.loads(resp.read())
    except Exception:
        return None


def _channel_fired(ch_data: dict, threshold: float) -> bool:
    """True if any peak in ch_data has integral > threshold."""
    for pk in ch_data.get("pk", []):
        if pk.get("i", 0.0) > threshold:
            return True
    return False


# ===========================================================================
#  Statistics container
# ===========================================================================

class Stats:
    """Thread-safe coincidence accumulator."""

    def __init__(self, module_names):
        self._lock = threading.Lock()
        self.module_hits:  Dict[str, int] = {m: 0 for m in module_names}
        self.scint_hits:   Dict[str, int] = {s: 0 for s in SCINTILLATORS}
        self.coincidences: Dict[str, Dict[str, int]] = {
            s: {m: 0 for m in module_names} for s in SCINTILLATORS
        }
        self.processed = 0

    def update(self, scint_fired: Dict[str, bool],
               module_fired: Dict[str, bool]) -> None:
        """Accumulate one event.

        For every module Mj that fired:
            module_hits[Mj]          += 1
            coincidences[Vi][Mj]     += 1  for each Vi that also fired

        This gives  rate(Vi, Mj) = coincidences[Vi][Mj] / module_hits[Mj]
                                 = N(Vi fired AND Mj fired) / N(Mj fired)
        """
        with self._lock:
            for sname, sf in scint_fired.items():
                if sf:
                    self.scint_hits[sname] += 1
            for mname, mf in module_fired.items():
                if mf:
                    self.module_hits[mname] += 1
                    for sname, sf in scint_fired.items():
                        if sf:
                            self.coincidences[sname][mname] += 1
            self.processed += 1

    def snapshot(self) -> dict:
        with self._lock:
            rates = {}
            for sname in SCINTILLATORS:
                rates[sname] = {}
                for mname, hits in self.module_hits.items():
                    if hits > 0:
                        rates[sname][mname] = self.coincidences[sname][mname] / hits
                    else:
                        rates[sname][mname] = float("nan")
            return {
                "rates": rates,
                "module_hits": dict(self.module_hits),
                "scint_hits": dict(self.scint_hits),
                "coincidences": {s: dict(c) for s, c in self.coincidences.items()},
                "processed": self.processed,
            }


# ===========================================================================
#  Coincidence scan worker
# ===========================================================================

def _fetch_event(server_url: str, ev: int) -> Optional[dict]:
    return _http_get(f"{server_url}/api/event/{ev}")


class ProcessWorker(QThread):
    progress     = pyqtSignal(int, int)
    stats_update = pyqtSignal(dict)
    finished     = pyqtSignal(str)

    def __init__(self, server_url: str, n_events: int,
                 module_keys: Dict[str, str],
                 scint_keys: Dict[str, str],
                 scint_thr: float, hycal_thr: float,
                 mode: str = MODE_AND,
                 min_mods: int = DEFAULT_MIN_CLUSTER_MODS,
                 parent=None):
        super().__init__(parent)
        self._url        = server_url
        self._n          = n_events
        self._mod_keys   = module_keys
        self._scint_keys = scint_keys
        self._scint_thr  = scint_thr
        self._hycal_thr  = hycal_thr
        self._mode       = mode
        self._min_mods   = min_mods
        self._stop_evt   = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def run(self):
        stats     = Stats(list(self._mod_keys.keys()))
        last_emit = time.monotonic()

        with ThreadPoolExecutor(max_workers=N_WORKERS) as pool:
            batch_start = 1
            while batch_start <= self._n and not self._stop_evt.is_set():
                batch_end = min(batch_start + BATCH_SIZE - 1, self._n)
                futures = {
                    pool.submit(_fetch_event, self._url, ev): ev
                    for ev in range(batch_start, batch_end + 1)
                }

                for fut in as_completed(futures):
                    if self._stop_evt.is_set():
                        break
                    data = fut.result()
                    if not data or "error" in data:
                        with stats._lock:
                            stats.processed += 1
                        continue

                    # Reject LMS events by trigger bit (fast path)
                    if data.get("trigger_bits", 0) & LMS_TRIGGER_BIT:
                        with stats._lock:
                            stats.processed += 1
                        continue

                    channels = data.get("channels", {})
                    scint_fired = {
                        sname: (skey in channels
                                and _channel_fired(channels[skey], self._scint_thr))
                        for sname, skey in self._scint_keys.items()
                    }

                    # Compute per-module ADC (max peak integral above threshold).
                    module_adc: Dict[str, float] = {}
                    for mname, mkey in self._mod_keys.items():
                        if mkey in channels:
                            peaks = channels[mkey].get("pk", [])
                            adc = max((float(pk.get("i", 0.0)) for pk in peaks),
                                      default=0.0)
                            if adc > self._hycal_thr:
                                module_adc[mname] = adc

                    # Reject LMS events by module occupancy (>1000 modules fired)
                    if len(module_adc) > LMS_MAX_MODULES:
                        with stats._lock:
                            stats.processed += 1
                        continue

                    # Reject isolated single-module noise: require a cluster.
                    if len(module_adc) < self._min_mods:
                        with stats._lock:
                            stats.processed += 1
                        continue

                    # Only the module with the highest ADC gets event credit.
                    # The cluster cut above ensures it is a genuine cluster, not
                    # an isolated discharge.
                    best = max(module_adc, key=module_adc.get)
                    module_fired = {best: True}

                    # AND mode: skip events where not all scintillators fired.
                    if self._mode == MODE_AND and not all(scint_fired.values()):
                        with stats._lock:
                            stats.processed += 1
                        continue

                    stats.update(scint_fired, module_fired)

                batch_start = batch_end + 1
                now = time.monotonic()
                if now - last_emit > 0.5:
                    snap = stats.snapshot()
                    self.progress.emit(snap["processed"], self._n)
                    self.stats_update.emit(snap)
                    last_emit = now

        snap = stats.snapshot()
        self.progress.emit(snap["processed"], self._n)
        self.stats_update.emit(snap)
        self.finished.emit("" if not self._stop_evt.is_set() else "stopped")


class ProcessWorkerET(QThread):
    """Accumulates coincidence statistics from live ET events.

    Polls /api/ring at ~5 Hz, fetches each new sequence number from the
    ring buffer, and processes it exactly once.  Runs until stopped.
    """
    progress     = pyqtSignal(int, int)   # (processed, -1)  — -1 signals ET mode
    stats_update = pyqtSignal(dict)
    finished     = pyqtSignal(str)

    POLL_INTERVAL = 0.2   # seconds between /api/ring polls

    def __init__(self, server_url: str,
                 module_keys: Dict[str, str],
                 scint_keys: Dict[str, str],
                 scint_thr: float, hycal_thr: float,
                 mode: str = MODE_AND,
                 min_mods: int = DEFAULT_MIN_CLUSTER_MODS,
                 parent=None):
        super().__init__(parent)
        self._url        = server_url
        self._mod_keys   = module_keys
        self._scint_keys = scint_keys
        self._scint_thr  = scint_thr
        self._hycal_thr  = hycal_thr
        self._mode       = mode
        self._min_mods   = min_mods
        self._stop_evt   = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def run(self):
        stats     = Stats(list(self._mod_keys.keys()))
        seen_seqs: set = set()
        last_emit = time.monotonic()

        while not self._stop_evt.is_set():
            ring_data = _http_get(f"{self._url}/api/ring",
                                  timeout=2.0)
            if ring_data is None:
                time.sleep(self.POLL_INTERVAL)
                continue

            new_seqs = [s for s in ring_data.get("ring", [])
                        if s not in seen_seqs]

            for seq in new_seqs:
                if self._stop_evt.is_set():
                    break
                seen_seqs.add(seq)

                data = _http_get(f"{self._url}/api/event/{seq}")
                if not data or "error" in data:
                    with stats._lock:
                        stats.processed += 1
                    continue

                # Reject LMS events by trigger bit (fast path)
                if data.get("trigger_bits", 0) & LMS_TRIGGER_BIT:
                    with stats._lock:
                        stats.processed += 1
                    continue

                channels = data.get("channels", {})
                scint_fired = {
                    sname: (skey in channels
                            and _channel_fired(channels[skey], self._scint_thr))
                    for sname, skey in self._scint_keys.items()
                }

                # Compute per-module ADC (max peak integral above threshold).
                module_adc: Dict[str, float] = {}
                for mname, mkey in self._mod_keys.items():
                    if mkey in channels:
                        peaks = channels[mkey].get("pk", [])
                        adc = max((float(pk.get("i", 0.0)) for pk in peaks),
                                  default=0.0)
                        if adc > self._hycal_thr:
                            module_adc[mname] = adc

                # Reject LMS events by module occupancy (>1000 modules fired)
                if len(module_adc) > LMS_MAX_MODULES:
                    with stats._lock:
                        stats.processed += 1
                    continue

                # Reject isolated single-module noise: require a cluster.
                if len(module_adc) < self._min_mods:
                    with stats._lock:
                        stats.processed += 1
                    continue

                # Only the module with the highest ADC gets event credit.
                best = max(module_adc, key=module_adc.get)
                module_fired = {best: True}

                # AND mode: skip events where not all scintillators fired.
                if self._mode == MODE_AND and not all(scint_fired.values()):
                    with stats._lock:
                        stats.processed += 1
                    continue

                stats.update(scint_fired, module_fired)

            now = time.monotonic()
            if now - last_emit > 0.5:
                snap = stats.snapshot()
                self.progress.emit(snap["processed"], -1)
                self.stats_update.emit(snap)
                last_emit = now

            time.sleep(self.POLL_INTERVAL)

        snap = stats.snapshot()
        self.progress.emit(snap["processed"], -1)
        self.stats_update.emit(snap)
        self.finished.emit("" if not self._stop_evt.is_set() else "stopped")


class InstantDisplayWorker(QThread):
    """Live event-by-event display from the ET ring buffer.

    Polls /api/ring at ~10 Hz, fetches each new latest event, computes
    per-module ADC values (max peak integral above pedestal), and emits
    event_ready with the raw channels dict so the main thread can render
    both the map and the waveform panels.
    """
    event_ready = pyqtSignal(dict)   # {adc_vals, channels, seq, max_mod}
    finished    = pyqtSignal(str)

    POLL_INTERVAL = 0.1   # 10 Hz

    def __init__(self, server_url: str,
                 module_keys: Dict[str, str],
                 hycal_thr: float = 0.0,
                 min_mods: int = DEFAULT_MIN_CLUSTER_MODS,
                 parent=None):
        super().__init__(parent)
        self._url       = server_url
        self._mod_keys  = module_keys
        self._hycal_thr = hycal_thr
        self._min_mods  = min_mods
        self._stop_evt  = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def run(self):
        last_seq = 0

        while not self._stop_evt.is_set():
            ring_data = _http_get(f"{self._url}/api/ring", timeout=2.0)
            if ring_data is None:
                time.sleep(self.POLL_INTERVAL)
                continue

            latest = ring_data.get("latest", 0)
            if latest == 0 or latest <= last_seq:
                time.sleep(self.POLL_INTERVAL)
                continue

            last_seq = latest
            data = _http_get(f"{self._url}/api/event/{latest}")
            if not data or "error" in data:
                time.sleep(self.POLL_INTERVAL)
                continue

            channels = data.get("channels", {})

            # Per-module ADC = max peak integral above pedestal, zeroed if below threshold.
            adc_vals: Dict[str, float] = {}
            for mname, mkey in self._mod_keys.items():
                ch = channels.get(mkey, {})
                peaks = ch.get("pk", [])
                val = max((float(pk.get("i", 0.0)) for pk in peaks), default=0.0)
                adc_vals[mname] = val if val > self._hycal_thr else 0.0

            # Skip events with no module above threshold — keep the previous display.
            n_above = sum(1 for v in adc_vals.values() if v > 0.0)
            if n_above < max(self._min_mods, 1):
                time.sleep(self.POLL_INTERVAL)
                continue

            max_mod = max(adc_vals, key=adc_vals.get) if adc_vals else ""

            self.event_ready.emit({
                "adc_vals":  adc_vals,
                "channels":  channels,
                "seq":       latest,
                "max_mod":   max_mod,
            })

            time.sleep(self.POLL_INTERVAL)

        self.finished.emit("" if not self._stop_evt.is_set() else "stopped")


# ===========================================================================
#  Waveform fetcher
# ===========================================================================

class WaveformFetcher(QThread):
    """Fetches scintillator and HyCal module waveforms for one event.

    File mode: calls /api/waveform/{ev}/{key} for each channel.
    ET mode:   calls /api/event/latest and extracts channel data from
               the full event JSON (ring events include samples).
    """
    waveform_ready = pyqtSignal(str, dict)   # label ("scint"|"module"), data

    def __init__(self, url: str, event_n: int,
                 scint_key: str, module_key: str,
                 et_mode: bool = False,
                 parent=None):
        super().__init__(parent)
        self._url        = url
        self._ev         = event_n
        self._scint_key  = scint_key
        self._module_key = module_key
        self._et_mode    = et_mode

    def run(self):
        if self._et_mode:
            event_data = _http_get(f"{self._url}/api/event/latest")
            if not event_data or "error" in event_data:
                err = {"error": "no ET event available"}
                self.waveform_ready.emit("scint",   err)
                self.waveform_ready.emit("module",  err)
                return
            channels = event_data.get("channels", {})
            for label, key in (("scint", self._scint_key),
                               ("module", self._module_key)):
                if not key:
                    self.waveform_ready.emit(label, {"error": "no channel"})
                    continue
                ch = channels.get(key)
                self.waveform_ready.emit(
                    label,
                    ch if ch else {"error": f"channel {key} not in event"})
        else:
            for label, key in (("scint", self._scint_key),
                               ("module", self._module_key)):
                if not key:
                    self.waveform_ready.emit(label, {"error": "no channel"})
                    continue
                data = _http_get(f"{self._url}/api/waveform/{self._ev}/{key}")
                self.waveform_ready.emit(label, data or {"error": "no response"})


# ===========================================================================
#  Waveform plot widget
# ===========================================================================

class WavePanel(QWidget):
    """Simple FADC waveform display driven by the server's waveform JSON.

    Expected input (from /api/waveform/<n>/<key>):
        {"s": [int, ...], "pm": float, "pr": float,
         "pk": [{"p": int, "h": float, "i": float,
                 "l": int, "r": int, "t": float, "o": int}, ...]}
    """

    PAD_L, PAD_R, PAD_T, PAD_B = 52, 14, 28, 32

    _PEAK_COLORS = (
        "#00b4d8", "#ff6b6b", "#51cf66", "#ffd43b",
        "#cc5de8", "#ff922b", "#20c997", "#f06595",
    )

    def __init__(self, label: str = "", parent=None):
        super().__init__(parent)
        self._label       = label          # "Scintillator" / "HyCal Module"
        self._title       = label          # updated when data arrives
        self._samples: List[int] = []
        self._peaks:   List[dict] = []
        self._ped_mean    = 0.0
        self._ped_rms     = 0.0
        self._threshold   = 0.0
        self._fired       = False
        self._placeholder = "No data — select an event and click Fetch"

        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def set_data(self, wave_json: dict, threshold: float,
                 title: Optional[str] = None) -> None:
        """Load waveform from the server JSON response."""
        if "error" in wave_json:
            self.clear(title or self._label,
                       wave_json.get("error", "channel not found"))
            return

        self._title     = title or self._label
        self._samples   = list(wave_json.get("s", []))
        self._peaks     = list(wave_json.get("pk", []))
        self._ped_mean  = float(wave_json.get("pm", 0))
        self._ped_rms   = float(wave_json.get("pr", 0))
        self._threshold = threshold
        self._fired     = any(pk.get("i", 0) > threshold for pk in self._peaks)
        self.update()

    def clear(self, title: Optional[str] = None,
              placeholder: Optional[str] = None) -> None:
        self._title     = title or self._label
        self._samples   = []
        self._peaks     = []
        self._fired     = False
        if placeholder is not None:
            self._placeholder = placeholder
        self.update()

    # ------------------------------------------------------------------
    #  Painting
    # ------------------------------------------------------------------

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor(THEME.BG))

        r = QRectF(self.PAD_L, self.PAD_T,
                   max(1.0, self.width() - self.PAD_L - self.PAD_R),
                   max(1.0, self.height() - self.PAD_T - self.PAD_B))
        p.setPen(QColor(THEME.BORDER))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)

        self._draw_title(p, r)

        n = len(self._samples)
        if n < 2:
            p.setPen(QColor(THEME.TEXT_DIM))
            p.setFont(QFont("Monospace", 10))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, self._placeholder)
            return

        ymin, ymax = self._y_range()

        def sx(i: float) -> float:
            return r.left() + i / (n - 1) * r.width()

        def sy(v: float) -> float:
            return r.bottom() - (v - ymin) / (ymax - ymin) * r.height()

        y_ped = sy(self._ped_mean) if self._ped_mean != 0 else None
        self._draw_pedestal(p, r, y_ped)
        self._draw_peak_fills(p, sx, sy, y_ped, n)
        self._draw_waveform(p, sx, sy, n)
        self._draw_peak_markers(p, sx, sy, n)
        self._draw_axes(p, r, ymin, ymax, n, sx)
        self._draw_info(p, r)

    def _y_range(self):
        s = self._samples
        ymin, ymax = float(min(s)), float(max(s))
        if ymax - ymin < 5.0:
            ymax = ymin + 5.0
        pad = (ymax - ymin) * 0.06
        return ymin - pad, ymax + pad

    def _draw_title(self, p: QPainter, r: QRectF):
        f = QFont("Monospace", 10)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(THEME.TEXT))
        p.drawText(int(r.left()), int(r.top() - 8), self._title)
        if self._samples:
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(self._title)
            fired_txt = "  [FIRED]" if self._fired else "  [—]"
            p.setPen(QColor(THEME.SUCCESS) if self._fired else QColor(THEME.TEXT_DIM))
            p.drawText(int(r.left() + tw), int(r.top() - 8), fired_txt)

    def _draw_pedestal(self, p: QPainter, r: QRectF, y_ped: Optional[float]):
        if y_ped is None:
            return
        p.setPen(QPen(QColor(THEME.TEXT_DIM), 1, Qt.PenStyle.DashLine))
        p.drawLine(int(r.left()), int(y_ped), int(r.right()), int(y_ped))

    def _draw_peak_fills(self, p, sx, sy, y_ped, n):
        if y_ped is None:
            return
        for i, pk in enumerate(self._peaks):
            base  = QColor(self._PEAK_COLORS[i % len(self._PEAK_COLORS)])
            # dim peaks that don't exceed the threshold
            if pk.get("i", 0) <= self._threshold:
                base.setAlphaF(0.4)
            fill = QColor(base)
            fill.setAlphaF(fill.alphaF() * 0.25)
            lft = max(0, int(pk.get("l", pk["p"])))
            rgt = min(n - 1, int(pk.get("r", pk["p"])))
            poly = QPolygonF()
            for k in range(lft, rgt + 1):
                poly.append(QPointF(sx(k), sy(self._samples[k])))
            poly.append(QPointF(sx(rgt), y_ped))
            poly.append(QPointF(sx(lft), y_ped))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawPolygon(poly)

    def _draw_waveform(self, p, sx, sy, n):
        p.setPen(QPen(QColor(THEME.ACCENT), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        s = self._samples
        for i in range(n - 1):
            p.drawLine(int(sx(i)),     int(sy(s[i])),
                       int(sx(i + 1)), int(sy(s[i + 1])))

    def _draw_peak_markers(self, p, sx, sy, n):
        s = self._samples
        for i, pk in enumerate(self._peaks):
            pos = int(pk.get("p", 0))
            if pos < 0 or pos >= n:
                continue
            col = QColor(self._PEAK_COLORS[i % len(self._PEAK_COLORS)])
            if pk.get("i", 0) <= self._threshold:
                col.setAlphaF(0.4)
            p.setPen(QPen(col, 1.2))
            p.setBrush(col)
            cx, cy = sx(pos), sy(float(s[pos]))
            diamond = QPolygonF([
                QPointF(cx,     cy - 4),
                QPointF(cx + 4, cy),
                QPointF(cx,     cy + 4),
                QPointF(cx - 4, cy),
            ])
            p.drawPolygon(diamond)

            # integral label above the diamond
            integ = pk.get("i", 0)
            p.setFont(QFont("Monospace", 8))
            p.setPen(col)
            p.drawText(int(cx - 16), int(cy - 7), f"{integ:.0f}")

    def _draw_axes(self, p, r, ymin, ymax, n, sx):
        p.setPen(QColor(THEME.TEXT_DIM))
        p.setFont(QFont("Monospace", 8))
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = r.bottom() - frac * r.height()
            p.drawLine(int(r.left() - 3), int(y), int(r.left()), int(y))
            val = ymin + frac * (ymax - ymin)
            p.drawText(int(r.left() - self.PAD_L + 2), int(y + 4), f"{val:.0f}")
        tick_every = max(1, n // 8)
        for i in range(0, n, tick_every):
            x = r.left() + i / max(1, n - 1) * r.width()
            p.drawLine(int(x), int(r.bottom()), int(x), int(r.bottom() + 3))
            ns_val = i * 1e3 / CLK_MHZ
            p.drawText(int(x - 18), int(r.bottom() + 14), f"{ns_val:g}")
        p.setFont(QFont("Monospace", 9))
        p.drawText(int(r.left() + r.width() / 2 - 10),
                   int(r.bottom() + 26), "ns")

    def _draw_info(self, p, r):
        above = sum(1 for pk in self._peaks if pk.get("i", 0) > self._threshold)
        info = (f"ped={self._ped_mean:.1f}  rms={self._ped_rms:.2f}"
                f"  peaks={len(self._peaks)} ({above} above thr)")
        p.setFont(QFont("Monospace", 9))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(info)
        th = fm.height()
        box = QRectF(r.right() - tw - 8, r.top() + 4, tw + 6, th + 2)
        bg = QColor(THEME.BG)
        bg.setAlphaF(0.75)
        p.fillRect(box, bg)
        p.setPen(QColor(THEME.TEXT_DIM))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, info)


# ===========================================================================
#  Coincidence map widget
# ===========================================================================

class CoincidenceMapWidget(HyCalMapWidget):
    """HyCal map coloured by coincidence rate, with informative tooltip."""

    def __init__(self, parent=None):
        super().__init__(parent, enable_zoom_pan=True, show_colorbar=True)
        self._snapshot: dict = {}
        self._active_scint = "V1"
        self._view_mode = VIEW_COINC
        self._instant_max_mod = ""
        self.set_palette("rainbow")

    def set_snapshot(self, snap: dict, scint: str,
                     view_mode: str = VIEW_COINC) -> None:
        self._snapshot    = snap
        self._active_scint = scint
        self._view_mode   = view_mode

        if view_mode == VIEW_OCC:
            hits = snap.get("module_hits", {})
            self.set_values(hits if hits else {})
            max_hits = max(hits.values()) if hits else 1
            self.set_range(0, max_hits)
        else:
            rates = snap.get("rates", {}).get(scint, {})
            valid = {m: v for m, v in rates.items() if not math.isnan(v)}
            self.set_values(valid if valid else {})
            self.set_range(0.0, 1.0)
        self.update()

    def set_instant_event(self, adc_vals: dict, max_mod: str = "") -> None:
        """Display per-module ADC values for a single live event."""
        self._view_mode = VIEW_INSTANT
        self._instant_max_mod = max_mod
        self.set_values(adc_vals)
        max_val = max(adc_vals.values(), default=0.0)
        self.set_range(0.0, max(max_val, 1.0))
        self.update()

    # Dark purple used for modules that have been seen (module_hits > 0) but
    # have zero coincidence rate or zero occupancy count.  Distinct from the
    # NO_DATA_COLOR (dark grey) which means the module was never seen at all.
    _SEEN_ZERO = QColor(55, 0, 75)

    def _paint_modules(self, p: QPainter):
        if self._view_mode not in (VIEW_COINC, VIEW_OCC):
            super()._paint_modules(p)
            return
        stops = self.palette_stops()
        no_data = self.NO_DATA_COLOR
        seen_zero = self._SEEN_ZERO
        vmin, vmax = self._vmin, self._vmax
        for name, rect in self._rects.items():
            v = self._values.get(name)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                p.fillRect(rect, no_data)
            elif v == 0.0:
                p.fillRect(rect, seen_zero)
            else:
                t = ((v - vmin) / (vmax - vmin)) if vmax > vmin else 0.5
                p.fillRect(rect, cmap_qcolor(max(t, 0.0), stops))

    def _tooltip_text(self, name: str) -> str:
        if self._view_mode == VIEW_INSTANT:
            adc = self._values.get(name, 0.0)
            suffix = "  ★ max" if name == self._instant_max_mod else ""
            return f"{name}\nADC: {adc:.0f}{suffix}"

        snap  = self._snapshot
        scint = self._active_scint
        nhits = snap.get("module_hits", {}).get(name, 0)

        if self._view_mode == VIEW_OCC:
            processed = snap.get("processed", 0)
            frac = nhits / processed if processed > 0 else float("nan")
            if processed == 0:
                return f"{name}\nNo data"
            return (f"{name}\n"
                    f"Module hits: {nhits:,}\n"
                    f"Occupancy: {frac:.4f}")

        rate   = snap.get("rates", {}).get(scint, {}).get(name, float("nan"))
        ncoinc = snap.get("coincidences", {}).get(scint, {}).get(name, 0)
        if math.isnan(rate):
            return f"{name}\nNo data"
        return (f"{name}\n"
                f"Coincidence rate: {rate:.4f}\n"
                f"Coincidences: {ncoinc:,}\n"
                f"Module hits: {nhits:,}")


# ===========================================================================
#  Main window
# ===========================================================================

class MainWindow(QMainWindow):

    def __init__(self, server_url: str):
        super().__init__()
        self.setWindowTitle("Scintillator–HyCal Coincidence Monitor")
        self.resize(1400, 900)

        self._server_url = server_url
        self._n_events   = 0
        self._stats_worker:   Optional[ProcessWorker]        = None
        self._instant_worker: Optional[InstantDisplayWorker] = None
        self._fetcher:  Optional[WaveformFetcher] = None
        self._snapshot: dict = {}
        self._selected_module: str = ""   # module last clicked on the map

        crate_to_roc = _load_crate_to_roc(DAQ_CFG_JSON)
        daq = _load_daq_map(DAQ_MAP_JSON, crate_to_roc)
        self._modules = load_modules(MODULES_JSON)
        self._w_layers: Dict[str, int] = _build_w_module_layers(MODULES_JSON)
        physics_names = {m.name for m in self._modules if m.mod_type != "LMS"}
        self._mod_keys: Dict[str, str] = {
            name: key for name, key in daq.items() if name in physics_names
        }
        # Scintillator channel keys resolved from daq_map (V1-V4 are listed there)
        self._scint_keys: Dict[str, str] = {
            sname: daq[sname] for sname in SCINTILLATORS if sname in daq
        }

        self._veto_ctrl    = VetoMotorController()
        self._et_mode      = False         # True when server is in online/ET mode
        self._map_view     = VIEW_COINC
        self._display_mode = DISPLAY_COINC # top-level mode switch
        self._display_paused = False       # True while instant display is frozen

        self._build_ui()
        self._map.set_modules(self._modules)
        self._map.set_palette("rainbow")
        self._map.set_range(0.0, 1.0)

        self._veto_timer = QTimer(self)
        self._veto_timer.timeout.connect(self._poll_veto_positions)
        self._veto_timer.start(VETO_POLL_MS)

    # ------------------------------------------------------------------
    #  UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        h_split = QSplitter(Qt.Orientation.Horizontal)

        # ---- left control panel ----------------------------------------
        left = QWidget()
        left.setMinimumWidth(340)
        left.setMaximumWidth(480)
        left.setStyleSheet(themed(f"QWidget{{background:{THEME.PANEL};}}"))
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 10, 10)
        lv.setSpacing(8)

        # Server
        srv_box = QGroupBox("Server")
        srv_box.setStyleSheet(self._groupbox_style())
        sv = QVBoxLayout(srv_box)
        sv.setSpacing(4)
        self._url_edit = QLineEdit(self._server_url)
        self._url_edit.setStyleSheet(self._input_style())
        sv.addWidget(self._url_edit)
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setStyleSheet(self._btn_style())
        self._connect_btn.clicked.connect(self._on_connect)
        sv.addWidget(self._connect_btn)
        self._conn_label = QLabel("Not connected")
        self._conn_label.setStyleSheet(f"color:{THEME.TEXT_DIM};font-size:11px;")
        sv.addWidget(self._conn_label)
        self._mode_btn = QPushButton("Switch to ET Mode")
        self._mode_btn.setEnabled(False)
        self._mode_btn.setStyleSheet(self._btn_style())
        self._mode_btn.clicked.connect(self._on_mode_switch)
        sv.addWidget(self._mode_btn)
        lv.addWidget(srv_box)

        # Display Mode
        disp_box = QGroupBox("Display Mode")
        disp_box.setStyleSheet(self._groupbox_style())
        dpv = QVBoxLayout(disp_box)
        dpv.setSpacing(2)
        self._disp_group = QButtonGroup(self)
        self._rb_disp_coinc   = QRadioButton("Coincidence Stats")
        self._rb_disp_instant = QRadioButton("Instant Event Display")
        for rb in (self._rb_disp_coinc, self._rb_disp_instant):
            rb.setStyleSheet(f"QRadioButton{{color:{THEME.TEXT};font-size:12px;}}")
            self._disp_group.addButton(rb)
            dpv.addWidget(rb)
        self._rb_disp_coinc.setChecked(True)
        self._disp_group.buttonClicked.connect(self._on_display_mode_changed)
        lv.addWidget(disp_box)

        # Scintillator selector
        sci_box = QGroupBox("Scintillator")
        sci_box.setStyleSheet(self._groupbox_style())
        scv = QVBoxLayout(sci_box)
        scv.setSpacing(2)
        self._scint_group = QButtonGroup(self)
        for name in SCINTILLATORS:
            rb = QRadioButton(name)
            rb.setStyleSheet(f"QRadioButton{{color:{THEME.TEXT};font-size:12px;}}")
            self._scint_group.addButton(rb)
            scv.addWidget(rb)
            if name == "V1":
                rb.setChecked(True)
        self._scint_group.buttonClicked.connect(self._on_scint_changed)
        lv.addWidget(sci_box)

        # Map view mode (Coincidence Stats only)
        self._mapview_box = QGroupBox("Map View")
        self._mapview_box.setStyleSheet(self._groupbox_style())
        mvv = QVBoxLayout(self._mapview_box)
        mvv.setSpacing(2)
        self._mapview_group = QButtonGroup(self)
        self._rb_coinc = QRadioButton("Coincidence Rate")
        self._rb_occ   = QRadioButton("Occupancy (hit count)")
        for rb in (self._rb_coinc, self._rb_occ):
            rb.setStyleSheet(
                f"QRadioButton{{color:{THEME.TEXT};font-size:12px;}}")
            self._mapview_group.addButton(rb)
            mvv.addWidget(rb)
        self._rb_coinc.setChecked(True)
        self._mapview_group.buttonClicked.connect(self._on_mapview_changed)
        lv.addWidget(self._mapview_box)

        # Thresholds
        thr_box = QGroupBox("Thresholds (ADC integral)")
        thr_box.setStyleSheet(self._groupbox_style())
        tv = QVBoxLayout(thr_box)
        tv.setSpacing(4)
        lbl_s = QLabel("Scintillator:")
        lbl_s.setStyleSheet(f"color:{THEME.TEXT_DIM};font-size:11px;")
        tv.addWidget(lbl_s)
        self._scint_thr_spin = QDoubleSpinBox()
        self._scint_thr_spin.setRange(0, 100000)
        self._scint_thr_spin.setValue(DEFAULT_SCINT_THR)
        self._scint_thr_spin.setDecimals(0)
        self._scint_thr_spin.setSingleStep(50)
        self._scint_thr_spin.setStyleSheet(self._input_style())
        tv.addWidget(self._scint_thr_spin)
        lbl_h = QLabel("HyCal module:")
        lbl_h.setStyleSheet(f"color:{THEME.TEXT_DIM};font-size:11px;")
        tv.addWidget(lbl_h)
        self._hycal_thr_spin = QDoubleSpinBox()
        self._hycal_thr_spin.setRange(0, 100000)
        self._hycal_thr_spin.setValue(DEFAULT_HYCAL_THR)
        self._hycal_thr_spin.setDecimals(0)
        self._hycal_thr_spin.setSingleStep(10)
        self._hycal_thr_spin.setStyleSheet(self._input_style())
        tv.addWidget(self._hycal_thr_spin)
        lbl_excl = QLabel("Inner W exclusion layers (0 = none, 16 = all W):")
        lbl_excl.setStyleSheet(f"color:{THEME.TEXT_DIM};font-size:11px;")
        tv.addWidget(lbl_excl)
        self._excl_spin = QSpinBox()
        self._excl_spin.setRange(0, 16)
        self._excl_spin.setValue(0)
        self._excl_spin.setStyleSheet(self._input_style())
        tv.addWidget(self._excl_spin)
        lbl_minmods = QLabel("Min HyCal modules fired (cluster cut, 1 = off):")
        lbl_minmods.setStyleSheet(f"color:{THEME.TEXT_DIM};font-size:11px;")
        tv.addWidget(lbl_minmods)
        self._min_mods_spin = QSpinBox()
        self._min_mods_spin.setRange(1, 50)
        self._min_mods_spin.setValue(DEFAULT_MIN_CLUSTER_MODS)
        self._min_mods_spin.setStyleSheet(self._input_style())
        tv.addWidget(self._min_mods_spin)
        lv.addWidget(thr_box)

        # Event selection mode (Coincidence Stats only)
        self._selmode_box = QGroupBox("Event Selection Mode")
        self._selmode_box.setStyleSheet(self._groupbox_style())
        mv = QVBoxLayout(self._selmode_box)
        mv.setSpacing(4)
        self._mode_grp = QButtonGroup(self)
        self._rb_and = QRadioButton("AND — all veto + any HyCal fire")
        self._rb_or  = QRadioButton("OR  — any veto or any HyCal fires")
        for rb in (self._rb_and, self._rb_or):
            rb.setStyleSheet(
                f"QRadioButton{{color:{THEME.TEXT};font-size:12px;}}")
            self._mode_grp.addButton(rb)
            mv.addWidget(rb)
        self._rb_and.setChecked(True)
        lv.addWidget(self._selmode_box)

        # Processing
        proc_box = QGroupBox("Processing")
        proc_box.setStyleSheet(self._groupbox_style())
        pv = QVBoxLayout(proc_box)
        pv.setSpacing(6)
        self._start_btn = QPushButton("Start")
        self._start_btn.setEnabled(False)
        self._start_btn.setStyleSheet(self._btn_style(accent=True))
        self._start_btn.clicked.connect(self._on_start_stop)
        pv.addWidget(self._start_btn)

        pause_row = QHBoxLayout()
        pause_row.setSpacing(4)
        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setEnabled(False)
        self._pause_btn.setStyleSheet(self._btn_style())
        self._pause_btn.clicked.connect(self._on_pause)
        pause_row.addWidget(self._pause_btn)
        self._resume_btn = QPushButton("Resume")
        self._resume_btn.setEnabled(False)
        self._resume_btn.setStyleSheet(self._btn_style())
        self._resume_btn.clicked.connect(self._on_resume)
        pause_row.addWidget(self._resume_btn)
        pv.addLayout(pause_row)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setStyleSheet(
            f"QProgressBar{{background:{THEME.PANEL};border:1px solid "
            f"{THEME.BORDER};border-radius:4px;height:14px;}}"
            f"QProgressBar::chunk{{background:{THEME.ACCENT_STRONG};"
            f"border-radius:3px;}}")
        pv.addWidget(self._progress)
        self._status_label = QLabel("Ready")
        self._status_label.setStyleSheet(f"color:{THEME.TEXT_DIM};font-size:11px;")
        self._status_label.setWordWrap(True)
        pv.addWidget(self._status_label)
        lv.addWidget(proc_box)

        # Event browser
        ev_box = QGroupBox("Event Browser")
        ev_box.setStyleSheet(self._groupbox_style())
        ev = QVBoxLayout(ev_box)
        ev.setSpacing(4)

        ev_row = QHBoxLayout()
        ev_row.setSpacing(4)
        lbl_ev = QLabel("Event #")
        lbl_ev.setStyleSheet(f"color:{THEME.TEXT_DIM};font-size:11px;")
        ev_row.addWidget(lbl_ev)
        self._ev_spin = QSpinBox()
        self._ev_spin.setRange(1, 1)
        self._ev_spin.setValue(1)
        self._ev_spin.setStyleSheet(self._input_style())
        ev_row.addWidget(self._ev_spin, 1)
        ev.addLayout(ev_row)

        self._fetch_btn = QPushButton("Fetch waveforms")
        self._fetch_btn.setEnabled(False)
        self._fetch_btn.setStyleSheet(self._btn_style())
        self._fetch_btn.clicked.connect(self._on_fetch_waveforms)
        ev.addWidget(self._fetch_btn)

        self._sel_mod_label = QLabel("Module: (click map)")
        self._sel_mod_label.setStyleSheet(
            f"color:{THEME.TEXT_DIM};font-size:11px;")
        self._sel_mod_label.setWordWrap(True)
        ev.addWidget(self._sel_mod_label)
        lv.addWidget(ev_box)

        # Statistics
        stats_box = QGroupBox("Statistics")
        stats_box.setStyleSheet(self._groupbox_style())
        stv = QVBoxLayout(stats_box)
        self._stats_label = QLabel("—")
        self._stats_label.setStyleSheet(f"color:{THEME.TEXT_DIM};font-size:11px;")
        self._stats_label.setWordWrap(True)
        stv.addWidget(self._stats_label)
        lv.addWidget(stats_box)

        # Veto motor positions — two sub-rows per motor:
        #   row A: name  |  "RBV:"  |  readback value  |  moving indicator
        #   row B: ""    |  "Set:"  |  spinbox          |  Move button
        veto_box = QGroupBox("Veto Motor Positions")
        veto_box.setStyleSheet(self._groupbox_style())
        vg = QGridLayout(veto_box)
        vg.setHorizontalSpacing(6)
        vg.setVerticalSpacing(2)
        vg.setContentsMargins(6, 8, 6, 8)
        self._veto_widgets: Dict[str, dict] = {}
        for i, vname in enumerate(VETO_PV_BASES):
            ra = i * 3       # row A: readback
            rb = i * 3 + 1  # row B: setpoint
            rc = i * 3 + 2  # row C: thin separator

            # -- row A: readback -----------------------------------------
            lbl_name = QLabel(vname)
            lbl_name.setStyleSheet(
                f"color:{THEME.ACCENT};font-size:12px;font-weight:bold;")
            vg.addWidget(lbl_name, ra, 0)

            lbl_rbv_key = QLabel("RBV:")
            lbl_rbv_key.setStyleSheet(
                f"color:{THEME.TEXT_DIM};font-size:11px;")
            vg.addWidget(lbl_rbv_key, ra, 1)

            lbl_rbv = QLabel("—")
            lbl_rbv.setStyleSheet(
                f"color:{THEME.TEXT};font-size:12px;font-weight:bold;")
            lbl_rbv.setMinimumWidth(70)
            vg.addWidget(lbl_rbv, ra, 2)

            lbl_movn = QLabel("● moving")
            lbl_movn.setStyleSheet(
                f"color:{THEME.TEXT_DIM};font-size:10px;")
            vg.addWidget(lbl_movn, ra, 3)

            # -- row B: setpoint + move ----------------------------------
            lbl_set_key = QLabel("Set:")
            lbl_set_key.setStyleSheet(
                f"color:{THEME.TEXT_DIM};font-size:11px;")
            vg.addWidget(lbl_set_key, rb, 1)

            spin = QDoubleSpinBox()
            spin.setRange(-9999, 9999)
            spin.setDecimals(2)
            spin.setSingleStep(0.1)
            spin.setValue(0.0)
            spin.setStyleSheet(self._input_style())
            vg.addWidget(spin, rb, 2)

            btn = QPushButton("Move")
            btn.setStyleSheet(self._btn_style(accent=True))
            btn.clicked.connect(lambda _, v=vname: self._move_veto(v))
            vg.addWidget(btn, rb, 3)

            # -- row C: separator ----------------------------------------
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet(f"color:{THEME.BORDER};")
            sep.setFixedHeight(6)
            vg.addWidget(sep, rc, 0, 1, 4)

            self._veto_widgets[vname] = {
                "rbv": lbl_rbv, "movn": lbl_movn,
                "spin": spin, "btn": btn,
            }

        if not self._veto_ctrl.available:
            warn = QLabel("pyepics not available")
            warn.setStyleSheet(f"color:{THEME.WARN};font-size:10px;")
            vg.addWidget(warn, len(VETO_PV_BASES) * 3, 0, 1, 4)
        lv.addWidget(veto_box)

        lv.addStretch(1)

        # ---- right side: map on top, waveforms on bottom ---------------
        v_split = QSplitter(Qt.Orientation.Vertical)

        self._map = CoincidenceMapWidget()
        self._map.moduleClicked.connect(self._on_module_clicked)
        v_split.addWidget(self._map)

        wave_row = QWidget()
        wave_row.setMinimumHeight(280)
        wrl = QHBoxLayout(wave_row)
        wrl.setContentsMargins(0, 0, 0, 0)
        wrl.setSpacing(4)
        self._wave_scint  = WavePanel("Scintillator")
        self._wave_module = WavePanel("HyCal Module")
        wrl.addWidget(self._wave_scint)
        wrl.addWidget(self._wave_module)
        v_split.addWidget(wave_row)

        v_split.setStretchFactor(0, 1)
        v_split.setStretchFactor(1, 1)

        h_split.addWidget(left)
        h_split.addWidget(v_split)
        h_split.setStretchFactor(0, 2)
        h_split.setStretchFactor(1, 3)

        self.setCentralWidget(h_split)

    # ------------------------------------------------------------------
    #  Style helpers
    # ------------------------------------------------------------------

    def _groupbox_style(self) -> str:
        return themed(
            f"QGroupBox{{color:{THEME.TEXT_DIM};font-size:11px;font-weight:bold;"
            f"border:1px solid {THEME.BORDER};border-radius:6px;margin-top:6px;"
            f"padding-top:4px;}}"
            f"QGroupBox::title{{subcontrol-origin:margin;left:8px;padding:0 4px;}}")

    def _input_style(self) -> str:
        return themed(
            f"QLineEdit,QDoubleSpinBox,QSpinBox{{background:{THEME.PANEL};"
            f"color:{THEME.TEXT};border:1px solid {THEME.BORDER};"
            f"border-radius:4px;padding:3px 6px;font-size:12px;}}"
            f"QLineEdit:focus,QDoubleSpinBox:focus,QSpinBox:focus{{"
            f"border-color:{THEME.ACCENT_BORDER};}}")

    def _btn_style(self, accent: bool = False) -> str:
        bg  = THEME.ACCENT_STRONG if accent else THEME.BUTTON
        hov = THEME.ACCENT        if accent else THEME.BUTTON_HOVER
        fg  = "#ffffff"           if accent else THEME.TEXT
        return themed(
            f"QPushButton{{background:{bg};color:{fg};border:1px solid "
            f"{THEME.BORDER};border-radius:6px;padding:5px 10px;font-size:12px;}}"
            f"QPushButton:hover{{background:{hov};}}"
            f"QPushButton:disabled{{background:{THEME.PANEL};"
            f"color:{THEME.TEXT_MUTED};}}")

    # ------------------------------------------------------------------
    #  Slots — connection & scan
    # ------------------------------------------------------------------

    def _on_connect(self):
        url = self._url_edit.text().rstrip("/")
        self._server_url = url
        self._conn_label.setText("Connecting…")
        self._connect_btn.setEnabled(False)
        QApplication.processEvents()

        cfg = _http_get(f"{url}/api/config")
        if cfg is None:
            self._conn_label.setText("Connection failed")
            self._conn_label.setStyleSheet(f"color:{THEME.DANGER};font-size:11px;")
            self._connect_btn.setEnabled(True)
            return

        self._apply_config(cfg)
        self._connect_btn.setEnabled(True)

    def _apply_config(self, cfg: dict) -> None:
        """Update UI state from a /api/config response dict."""
        n            = cfg.get("event_count", 0)
        mode         = cfg.get("mode", "unknown")
        et_connected = cfg.get("et_connected", False)
        self._n_events = n
        self._et_mode  = (mode == "online")

        if n > 0:
            self._conn_label.setText(f"Connected  ({n:,} events, mode={mode})")
            self._conn_label.setStyleSheet(f"color:{THEME.SUCCESS};font-size:11px;")
            self._start_btn.setEnabled(True)
            self._fetch_btn.setEnabled(True)
            self._ev_spin.setRange(1, n)
        elif self._et_mode and et_connected:
            self._conn_label.setText("Connected — ET online (live accumulation)")
            self._conn_label.setStyleSheet(f"color:{THEME.SUCCESS};font-size:11px;")
            self._start_btn.setEnabled(True)
            self._fetch_btn.setEnabled(True)   # fetches latest ring event
        elif self._et_mode:
            self._conn_label.setText("Connected — ET online (waiting for DAQ)")
            self._conn_label.setStyleSheet(f"color:{THEME.WARN};font-size:11px;")
            self._start_btn.setEnabled(True)
            self._fetch_btn.setEnabled(False)
        else:
            self._conn_label.setText(f"Connected — no file loaded (mode={mode})")
            self._conn_label.setStyleSheet(f"color:{THEME.WARN};font-size:11px;")
            self._start_btn.setEnabled(False)
            self._fetch_btn.setEnabled(False)

        self._mode_btn.setEnabled(True)
        if self._et_mode:
            self._mode_btn.setText("Switch to File Mode")
        else:
            self._mode_btn.setText("Switch to ET Mode")

    def _any_worker_running(self) -> bool:
        return (
            (self._stats_worker   is not None and self._stats_worker.isRunning()) or
            (self._instant_worker is not None and self._instant_worker.isRunning())
        )

    def _on_mode_switch(self):
        """Toggle the server between online (ET) and file mode."""
        if self._any_worker_running():
            self._stop_worker()
            if self._stats_worker:   self._stats_worker.wait(2000)
            if self._instant_worker: self._instant_worker.wait(2000)

        endpoint = "/api/mode/file" if self._et_mode else "/api/mode/online"
        self._mode_btn.setEnabled(False)
        self._mode_btn.setText("Switching…")
        QApplication.processEvents()

        result = _http_get(f"{self._server_url}{endpoint}")
        if result is None:
            self._conn_label.setText("Mode switch failed")
            self._conn_label.setStyleSheet(f"color:{THEME.DANGER};font-size:11px;")
            self._mode_btn.setEnabled(True)
            self._mode_btn.setText(
                "Switch to File Mode" if self._et_mode else "Switch to ET Mode")
            return

        cfg = _http_get(f"{self._server_url}/api/config")
        if cfg:
            self._apply_config(cfg)

    def _active_scint(self) -> str:
        btn = self._scint_group.checkedButton()
        return btn.text() if btn else "V1"

    def _on_display_mode_changed(self):
        instant = self._rb_disp_instant.isChecked()
        self._display_mode = DISPLAY_INSTANT if instant else DISPLAY_COINC

        # Pause/Resume only active in Instant mode while the instant worker runs.
        worker_live = self._instant_worker is not None and self._instant_worker.isRunning()
        if instant and worker_live:
            if not self._display_paused:
                self._pause_btn.setEnabled(True)
                self._resume_btn.setEnabled(False)
            else:
                self._pause_btn.setEnabled(False)
                self._resume_btn.setEnabled(True)
        else:
            self._display_paused = False
            self._pause_btn.setEnabled(False)
            self._resume_btn.setEnabled(False)

        # Show/hide coinc-only panels
        self._mapview_box.setVisible(not instant)
        self._selmode_box.setVisible(not instant)

        # Switch the map view without stopping workers or resetting stats.
        if instant:
            self._wave_module.clear("Max ADC Module")
            # If no live instant data yet, blank the map until the next event.
            if not (self._instant_worker and self._instant_worker.isRunning()):
                self._map.set_values({})
                self._map.update()
        else:
            self._wave_module.clear("HyCal Module")
            # Restore the accumulated stats immediately.
            if self._snapshot:
                self._map.set_snapshot(self._snapshot, self._active_scint(),
                                       self._map_view)
            else:
                self._map.set_values({})
                self._map.update()

    def _on_scint_changed(self):
        if self._snapshot:
            self._map.set_snapshot(self._snapshot, self._active_scint(), self._map_view)
            self._update_stats_label(self._snapshot)
        # refresh scintillator waveform panel title & re-fetch if event is available
        if self._n_events > 0 or self._et_mode:
            self._wave_scint.clear(self._active_scint())
            self._on_fetch_waveforms()

    def _on_mapview_changed(self):
        self._map_view = VIEW_OCC if self._rb_occ.isChecked() else VIEW_COINC
        if self._snapshot:
            self._map.set_snapshot(self._snapshot, self._active_scint(), self._map_view)
            self._update_stats_label(self._snapshot)

    def _on_start_stop(self):
        if self._any_worker_running():
            self._stop_worker()
        else:
            self._start_worker()

    def _on_pause(self):
        self._display_paused = True
        self._pause_btn.setEnabled(False)
        self._resume_btn.setEnabled(True)

    def _on_resume(self):
        self._display_paused = False
        self._pause_btn.setEnabled(True)
        self._resume_btn.setEnabled(False)

    def _start_worker(self):
        self._snapshot = {}
        self._display_paused = False
        self._map.set_values({})
        self._map.update()

        self._connect_btn.setEnabled(False)
        self._mode_btn.setEnabled(False)
        self._start_btn.setText("Stop")
        self._start_btn.setStyleSheet(self._btn_style(accent=False))

        # Apply inner-layer exclusion: drop W modules whose layer ≤ excl_layers.
        excl = self._excl_spin.value()
        if excl > 0:
            active_mod_keys = {
                name: key for name, key in self._mod_keys.items()
                if not name.startswith('W') or self._w_layers.get(name, 0) > excl
            }
        else:
            active_mod_keys = self._mod_keys

        sel_mode = MODE_AND if self._rb_and.isChecked() else MODE_OR

        min_mods = self._min_mods_spin.value()

        # --- Stats worker (always) ---
        if self._et_mode:
            self._stats_worker = ProcessWorkerET(
                server_url=self._server_url,
                module_keys=active_mod_keys,
                scint_keys=self._scint_keys,
                scint_thr=self._scint_thr_spin.value(),
                hycal_thr=self._hycal_thr_spin.value(),
                mode=sel_mode,
                min_mods=min_mods,
                parent=self,
            )
            self._progress.setRange(0, 0)
        else:
            self._stats_worker = ProcessWorker(
                server_url=self._server_url,
                n_events=self._n_events,
                module_keys=active_mod_keys,
                scint_keys=self._scint_keys,
                scint_thr=self._scint_thr_spin.value(),
                hycal_thr=self._hycal_thr_spin.value(),
                mode=sel_mode,
                min_mods=min_mods,
                parent=self,
            )
            self._progress.setRange(0, 100)

        self._stats_worker.progress.connect(self._on_progress)
        self._stats_worker.stats_update.connect(self._on_stats_update)
        self._stats_worker.finished.connect(self._on_finished)
        self._stats_worker.start()

        # --- Instant display worker (ET mode only) ---
        if self._et_mode:
            self._instant_worker = InstantDisplayWorker(
                server_url=self._server_url,
                module_keys=active_mod_keys,
                hycal_thr=self._hycal_thr_spin.value(),
                min_mods=min_mods,
                parent=self,
            )
            self._instant_worker.event_ready.connect(self._on_instant_event)
            self._instant_worker.finished.connect(self._on_finished)
            self._instant_worker.start()
        else:
            self._instant_worker = None

        # Pause/Resume only meaningful in Instant Event Display mode (ET).
        if self._et_mode and self._display_mode == DISPLAY_INSTANT:
            self._pause_btn.setEnabled(True)
            self._resume_btn.setEnabled(False)
        else:
            self._pause_btn.setEnabled(False)
            self._resume_btn.setEnabled(False)

        src = "ET live" if self._et_mode else "file"
        self._status_label.setText(
            f"{'Accumulating' if self._et_mode else 'Processing'}… "
            f"({src}, mode: {sel_mode})")
        self._scint_thr_spin.setEnabled(False)
        self._hycal_thr_spin.setEnabled(False)
        self._excl_spin.setEnabled(False)
        self._min_mods_spin.setEnabled(False)
        self._rb_and.setEnabled(False)
        self._rb_or.setEnabled(False)

    def _stop_worker(self):
        if self._stats_worker:   self._stats_worker.stop()
        if self._instant_worker: self._instant_worker.stop()
        self._status_label.setText("Stopping…")
        self._start_btn.setEnabled(False)

    def _on_progress(self, processed: int, total: int):
        if self._display_mode == DISPLAY_INSTANT:
            return   # status label owned by _on_instant_event in this mode
        if total < 0:   # ET mode — no total
            self._status_label.setText(f"Accumulated {processed:,} events")
        else:
            pct = int(100 * processed / total) if total > 0 else 0
            self._progress.setValue(pct)
            self._status_label.setText(f"Processed {processed:,} / {total:,}")

    def _on_stats_update(self, snap: dict):
        self._snapshot = snap
        # Only repaint the map when the user is viewing coinc/occ stats.
        if self._display_mode == DISPLAY_COINC:
            self._map.set_snapshot(snap, self._active_scint(), self._map_view)
        self._update_stats_label(snap)

    def _update_stats_label(self, snap: dict):
        scint = self._active_scint()
        mhits = snap.get("module_hits", {})
        processed = snap.get("processed", 0)
        if self._map_view == VIEW_OCC:
            total_hits = sum(mhits.values())
            max_hits   = max(mhits.values()) if mhits else 0
            nonzero    = sum(1 for v in mhits.values() if v > 0)
            mean_hits  = total_hits / nonzero if nonzero else 0.0
            self._stats_label.setText(
                f"Events processed: {processed:,}\n"
                f"Module hits (total): {total_hits:,}\n"
                f"Non-zero modules: {nonzero}\n"
                f"Mean hits: {mean_hits:.1f}\n"
                f"Max hits:  {max_hits:,}"
            )
        else:
            shits = snap.get("scint_hits", {}).get(scint, 0)
            rates = snap.get("rates", {}).get(scint, {})
            valid = [v for v in rates.values() if not math.isnan(v) and v > 0]
            mean_rate = sum(valid) / len(valid) if valid else 0.0
            max_rate  = max(valid)              if valid else 0.0
            self._stats_label.setText(
                f"{scint} hits: {shits:,}\n"
                f"Module hits (total): {sum(mhits.values()):,}\n"
                f"Non-zero modules: {len(valid)}\n"
                f"Mean rate: {mean_rate:.4f}\n"
                f"Max rate:  {max_rate:.4f}"
            )

    def _on_instant_event(self, data: dict):
        """Handle a new live event from InstantDisplayWorker."""
        if self._display_mode != DISPLAY_INSTANT:
            return   # accumulating in background; don't touch the map or panels
        if self._display_paused:
            return   # paused: freeze display while stats keep accumulating

        adc_vals = data.get("adc_vals", {})
        channels = data.get("channels", {})
        seq      = data.get("seq", 0)
        max_mod  = data.get("max_mod", "")

        self._map.set_instant_event(adc_vals, max_mod)

        scint_name = self._active_scint()
        scint_key  = self._scint_keys.get(scint_name, "")
        scint_ch   = channels.get(scint_key, {}) if scint_key else {}
        self._wave_scint.set_data(
            scint_ch if scint_ch else {"error": "no signal"},
            self._scint_thr_spin.value(),
            f"{scint_name}  (seq {seq})")

        if max_mod:
            max_key = self._mod_keys.get(max_mod, "")
            max_ch  = channels.get(max_key, {}) if max_key else {}
            max_adc = adc_vals.get(max_mod, 0.0)
            self._wave_module.set_data(
                max_ch if max_ch else {"error": "no waveform"},
                self._hycal_thr_spin.value(),
                f"{max_mod}  ADC={max_adc:.0f}  (seq {seq})")

        self._status_label.setText(
            f"Seq {seq}  |  max: {max_mod}  {adc_vals.get(max_mod, 0):.0f}")

    def _on_finished(self, msg: str):
        # Both workers emit finished; wait until neither is running.
        if self._any_worker_running():
            return
        self._display_paused = False
        self._pause_btn.setEnabled(False)
        self._resume_btn.setEnabled(False)
        self._progress.setRange(0, 100)
        self._progress.setValue(100 if msg == "" else self._progress.value())
        self._start_btn.setText("Start")
        self._start_btn.setStyleSheet(self._btn_style(accent=True))
        self._start_btn.setEnabled(True)
        self._connect_btn.setEnabled(True)
        self._mode_btn.setEnabled(True)
        self._scint_thr_spin.setEnabled(True)
        self._hycal_thr_spin.setEnabled(True)
        self._excl_spin.setEnabled(True)
        self._min_mods_spin.setEnabled(True)
        self._rb_and.setEnabled(True)
        self._rb_or.setEnabled(True)
        if msg == "stopped":
            self._status_label.setText("Stopped.")
        else:
            n = self._snapshot.get("processed", 0)
            self._status_label.setText(f"Done — {n:,} events processed.")

    # ------------------------------------------------------------------
    #  Slots — waveform browser
    # ------------------------------------------------------------------

    def _on_module_clicked(self, name: str):
        if not name:
            return
        if name in SCINTILLATORS:
            # Scintillator clicked on map → select its radio button and refresh left panel
            for btn in self._scint_group.buttons():
                if btn.text() == name:
                    btn.setChecked(True)
                    break
            self._on_scint_changed()
        else:
            # HyCal module clicked → update right waveform panel
            self._selected_module = name
            self._sel_mod_label.setText(f"Module: {name}")
            self._on_fetch_waveforms()

    def _on_fetch_waveforms(self):
        if self._n_events == 0 and not self._et_mode:
            return

        # Stop any in-flight fetcher
        if self._fetcher and self._fetcher.isRunning():
            self._fetcher.wait(500)

        ev          = self._ev_spin.value()
        scint_name  = self._active_scint()
        scint_key   = self._scint_keys.get(scint_name, "")
        module_key  = self._mod_keys.get(self._selected_module, "")

        ev_label = "latest ET event" if self._et_mode else f"event {ev}"
        self._wave_scint.clear(scint_name, f"Fetching {ev_label}…")
        self._wave_module.clear(
            self._selected_module or "HyCal Module",
            f"Fetching {ev_label}…" if self._selected_module
            else "Click a module on the map")

        if not self._selected_module:
            self._fetcher = WaveformFetcher(
                self._server_url, ev, scint_key, "",
                et_mode=self._et_mode, parent=self)
        else:
            self._fetcher = WaveformFetcher(
                self._server_url, ev, scint_key, module_key,
                et_mode=self._et_mode, parent=self)

        self._fetcher.waveform_ready.connect(self._on_waveform_ready)
        self._fetcher.start()

    def _on_waveform_ready(self, label: str, data: dict):
        ev = self._ev_spin.value()
        if label == "scint":
            scint_name = self._active_scint()
            title = f"{scint_name}  (event {ev})"
            self._wave_scint.set_data(
                data, self._scint_thr_spin.value(), title)
        else:
            mod_name = self._selected_module or "HyCal Module"
            title = f"{mod_name}  (event {ev})"
            self._wave_module.set_data(
                data, self._hycal_thr_spin.value(), title)

    # ------------------------------------------------------------------
    #  Veto position polling
    # ------------------------------------------------------------------

    def _poll_veto_positions(self):
        for vname in VETO_PV_BASES:
            rbv  = self._veto_ctrl.get(f"{vname}_rbv")
            movn = self._veto_ctrl.get(f"{vname}_movn")
            w = self._veto_widgets[vname]

            if rbv is not None:
                w["rbv"].setText(f"{rbv:.2f}")
                w["rbv"].setStyleSheet(f"color:{THEME.TEXT};font-size:11px;")
            else:
                w["rbv"].setText("—")
                w["rbv"].setStyleSheet(f"color:{THEME.TEXT_DIM};font-size:11px;")

            moving = movn is not None and int(movn) == 1
            w["movn"].setText("● moving" if moving else "")
            w["movn"].setStyleSheet(
                f"color:{THEME.SUCCESS};font-size:10px;" if moving
                else f"color:transparent;font-size:10px;")

    def _move_veto(self, vname: str):
        """Write the spinbox setpoint to the motor VAL PV."""
        target = self._veto_widgets[vname]["spin"].value()
        ok = self._veto_ctrl.put(f"{vname}_val", target)
        if not ok:
            self._status_label.setText(
                f"{vname} move failed — PV not connected")

    # ------------------------------------------------------------------
    #  Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._veto_timer.stop()
        if self._any_worker_running():
            self._stop_worker()
            if self._stats_worker:   self._stats_worker.wait(3000)
            if self._instant_worker: self._instant_worker.wait(3000)
        if self._fetcher and self._fetcher.isRunning():
            self._fetcher.wait(1000)
        event.accept()


# ===========================================================================
#  Entry point
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Scintillator–HyCal coincidence rate monitor")
    parser.add_argument("--url", default=DEFAULT_URL,
                        help=f"prad2_server base URL (default: {DEFAULT_URL})")
    parser.add_argument("--theme", choices=available_themes(), default="dark")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("Coincidence Monitor")

    set_theme(args.theme)

    win = MainWindow(server_url=args.url)
    apply_theme_palette(win)
    win.setStyleSheet(themed(f"QMainWindow{{background:{THEME.BG};}}"))
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
