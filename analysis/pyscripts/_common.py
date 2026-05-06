#!/usr/bin/env python3
"""
_common.py — shared helpers for analysis/pyscripts/.

Both gem_hycal_matching.py and plot_hits_at_hycal.py do the same setup
(load DAQ config, runinfo, HyCal+GEM systems, discover EVIO splits) and
the same per-event boilerplate (waveform → cluster, GEM ProcessEvent +
Reconstruct, lab-frame transform).  This module factors that out so the
two scripts only differ in their per-event accumulation + output.

The heavy wiring lives in `prad2::PipelineBuilder` (prad2det) and is
reached here through `det.PipelineBuilder` — so the same C++ code that
the analysis scripts and the live server use also drives the Python
analyses.  No more parallel JSON parsers in three languages.

Requires:
  prad2py    (built from python/, exposes dec.* + det.*)
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

try:
    from prad2py import dec, det
except ImportError as e:
    raise SystemExit(
        f"[ERROR] cannot import prad2py: {e}\n"
        "        Build the python bindings (cmake -DBUILD_PYTHON=ON) and "
        "ensure the install directory is on PYTHONPATH."
    )


# ============================================================================
# Path / run-number helpers
# ============================================================================

_RUN_PAT = re.compile(r"(?:prad|run)_0*(\d+)", re.IGNORECASE)


def extract_run_number(path: str) -> int:
    """Sniff the run number out of 'prad_NNNNNN.evio.*'-style names. -1 if none."""
    if not path:
        return -1
    m = _RUN_PAT.search(path)
    if not m:
        return -1
    try:
        return int(m.group(1))
    except ValueError:
        return -1


def resolve_db_path(p: str) -> str:
    """Resolve a possibly-relative database path against PRAD2_DATABASE_DIR."""
    if not p:
        return p
    if os.path.isabs(p):
        return p
    db = os.environ.get("PRAD2_DATABASE_DIR")
    if db is None:
        return p
    return os.path.join(db, p)


def hycal_pos_resolution(A: float, B: float, C: float, energy_mev: float) -> float:
    """sigma(E) at the HyCal face (mm), mirroring HyCalSystem::PositionResolution."""
    E_GeV = energy_mev / 1000.0 if energy_mev > 0 else 1e-6
    a = A / math.sqrt(E_GeV)
    b = B / E_GeV
    return math.sqrt(a * a + b * b + C * C)


def discover_split_files(any_path: str) -> list[str]:
    """Three modes by input shape:
      * '*' in path  → literal shell-style glob: expanded by Python's glob
        module so users can pick a subset (e.g. 'prad_024236.evio.0000*'
        gets splits .00000–.00009).  Quote it on the shell to keep the
        shell from expanding it first.
      * directory    → enumerate every prad_<run>.evio.<digits> in the dir,
        sniff run from dir name, warn (stderr) on gaps.
      * anything else → return [any_path] unchanged (single-file mode)."""
    if not any_path:
        return []
    p = Path(any_path)
    wants_glob = "*" in any_path
    is_dir = p.is_dir()

    if not wants_glob and not is_dir:
        return [any_path]

    # ---- glob mode: honor the literal pattern ------------------------------
    if wants_glob:
        import glob as _glob
        matches = sorted(_glob.glob(any_path))
        if not matches:
            sys.stderr.write(
                f"[WARN] discover_split_files: glob {any_path!r} matched "
                f"no files.\n"
            )
            return [any_path]
        return matches

    # ---- directory mode: enumerate by run number ---------------------------
    directory = p
    run = extract_run_number(p.name)

    if run < 0 or not directory.is_dir():
        sys.stderr.write(
            f"[WARN] discover_split_files: cannot resolve run/dir from "
            f"{any_path!r} — passing through as a single file.\n"
        )
        return [any_path]

    pat = re.compile(rf"^prad_0*{run}\.evio\.(\d+)$", re.IGNORECASE)
    matched: list[tuple[int, str]] = []
    for entry in directory.iterdir():
        m = pat.match(entry.name)
        if m:
            try:
                matched.append((int(m.group(1)), str(entry)))
            except ValueError:
                pass
    matched.sort()

    if matched:
        last = matched[-1][0]
        seen = {idx for idx, _ in matched}
        missing = [i for i in range(0, last + 1) if i not in seen]
        if missing:
            miss_str = " ".join(f".{i:05d}" for i in missing)
            sys.stderr.write(
                f"[WARN] split-file gaps in run {run} (found "
                f"{len(matched)} file(s), max suffix .{last:05d}): "
                f"missing {miss_str}\n"
            )

    if not matched:
        sys.stderr.write(
            f"[WARN] discover_split_files: no files matched 'prad_{run}.evio.*' "
            f"in {directory}\n"
        )
        return [any_path]

    return [path for _, path in matched]


def project_to_z(x: float, y: float, z: float, target_z: float
                 ) -> tuple[float, float, float]:
    """Straight-line target→hit projection to z = target_z.  Mirrors
    analysis::GetProjection (single-vertex assumption).  z must be > 0."""
    if z == 0.0:
        return x, y, target_z
    s = target_z / z
    return x * s, y * s, target_z


# ============================================================================
# Pipeline state + setup
# ============================================================================

@dataclass
class Pipeline:
    """Initialized HyCal + GEM systems plus geometry & EVIO file list.  One
    call to setup_pipeline() produces a ready-to-loop bundle.

    The detector-side fields (`cfg`, `geo`, `hycal`, `gem_sys`,
    `hycal_xform`, `gem_xforms`, `crate_map`) come from the C++
    PipelineBuilder so the wiring is identical to what the live server
    does.  The clusterers (`hc_clusterer`, `gem_clusterer`,
    `wave_ana`) are constructed here because they hold per-event scratch
    state and stay outside the C++ Pipeline."""
    cfg:           "dec.DaqConfig"       = None
    crate_map:     dict[int, int]        = field(default_factory=dict)
    geo:           "det.RunConfig"       = None
    hycal:         "det.HyCalSystem"     = None
    hc_clusterer:  "det.HyCalCluster"    = None
    wave_ana:      "dec.WaveAnalyzer"    = None
    gem_sys:       "det.GemSystem"       = None
    gem_clusterer: "det.GemCluster"      = None
    evio_files:    list[str]             = field(default_factory=list)
    hycal_xform:   "det.DetectorTransform" = None
    gem_xforms:    list                  = field(default_factory=list)

    # Matching parameters from reconstruction_config.json:matching, copied
    # off the underlying C++ Pipeline so callers can read them without a
    # second JSON parse.
    hycal_pos_res:  list[float]          = field(default_factory=lambda: [2.6, 0.0, 0.0])
    gem_pos_res:    list[float]          = field(default_factory=lambda: [0.1, 0.1, 0.1, 0.1])
    target_pos_res: list[float]          = field(default_factory=lambda: [1.0, 1.0, 20.0])

    # The underlying det.Pipeline (kept alive so the borrowed `hycal`,
    # `gem_sys`, `hycal_xform`, `gem_xforms` references stay valid).
    _core: "det.Pipeline"                = None


def setup_pipeline(*,
                   evio_path: str,
                   max_events: int = 0,
                   run_num: int = -1,
                   gem_ped_file: str = "",
                   gem_cm_file: str = "",
                   hc_calib_file: str = "",
                   daq_config: str = "",
                   gem_map_file: str = "",
                   hc_map_file: str = "",
                   ) -> Pipeline:
    """Wire up HyCal + GEM detectors via the C++ PipelineBuilder, then add
    the per-event scratch (HyCalCluster + GemCluster + WaveAnalyzer) and
    file-discovery bits the analysis loop needs.

    `_file` args accept "" to fall back to runinfo / database defaults.
    Run number defaults to a sniff from the EVIO basename when -1."""
    b = det.PipelineBuilder()
    if daq_config:    b.set_daq_config(daq_config)
    if hc_calib_file: b.set_hycal_calib(hc_calib_file)
    if gem_ped_file:  b.set_gem_pedestal(gem_ped_file)
    if gem_cm_file:   b.set_gem_common_mode(gem_cm_file)
    if hc_map_file:   b.set_hycal_map(hc_map_file)
    if gem_map_file:  b.set_gem_map(gem_map_file)
    if run_num > 0:   b.set_run_number(run_num)
    elif evio_path:   b.set_run_number_from_evio(evio_path)
    core = b.build()

    p = Pipeline()
    p._core         = core
    p.cfg           = core.daq_cfg
    p.crate_map     = {int(r.tag): int(r.crate) for r in core.daq_cfg.roc_tags}
    p.geo           = core.run_cfg
    p.hycal         = core.hycal
    p.hc_clusterer  = det.HyCalCluster(core.hycal)
    p.hc_clusterer.set_config(core.hycal_cluster_cfg)
    p.wave_ana      = dec.WaveAnalyzer(core.daq_cfg.wave_cfg)
    p.gem_sys       = core.gem
    p.gem_clusterer = det.GemCluster()
    p.hycal_xform   = core.hycal_transform
    p.gem_xforms    = list(core.gem_transforms)
    p.hycal_pos_res  = list(core.hycal_pos_res)
    p.gem_pos_res    = list(core.gem_pos_res)
    p.target_pos_res = list(core.target_pos_res)

    p.evio_files = discover_split_files(evio_path or "")
    print(f"[setup] EVIO       : {len(p.evio_files)} split file(s) for "
          f"input {evio_path or '(null)'}", flush=True)
    for f in p.evio_files:
        print(f"           {f}", flush=True)
    return p


def load_matching_config(p: Optional[Pipeline] = None
        ) -> tuple[tuple[float, float, float],
                   list[float],
                   tuple[float, float, float]]:
    """Return ((A, B, C), gem_pos_res, (sx, sy, sz)) from the matching
    parameters resolved by setup_pipeline().  The Pipeline argument is
    the canonical path; passing None falls back to library defaults
    (no separate JSON parse — use a Pipeline)."""
    if p is None:
        return (2.6, 0.0, 0.0), [0.1, 0.1, 0.1, 0.1], (1.0, 1.0, 20.0)
    A, B, C = p.hycal_pos_res[0], p.hycal_pos_res[1], p.hycal_pos_res[2]
    gem = list(p.gem_pos_res)
    tgt = (p.target_pos_res[0], p.target_pos_res[1], p.target_pos_res[2])
    return (A, B, C), gem, tgt


# ============================================================================
# Per-event reconstruction helpers
# ============================================================================
# Both scripts call these inside their own event loop to keep the pipeline
# idempotent — the user passes the already-decoded EventData / SspEventData
# in (so trigger filtering happens before we pay the reco cost).

# HyCal trigger window (ns) for picking the largest peak — matches the live
# monitor's "100..200" cut in app_state_init.cpp / the C++ scripts.
HC_TIME_LO = 100.0
HC_TIME_HI = 200.0


def reconstruct_hycal(p: Pipeline, fadc_evt,
                      time_window: Optional[Tuple[float, float]] = None) -> list:
    """Run HyCal waveform → energy → cluster on one decoded EventData.
    Returns a list of det.ClusterHit (detector frame).

    Two pulse-selection modes, switched by `seed_time_window` on the
    HyCal cluster config:

    * `seed_time_window > 0` (multi-pulse): every analyzer-detected peak
      passing `time_window` (when provided) is fed into the clusterer
      with its time stamp.  HyCalCluster groups them via seed-anchored
      timing coincidence — one or more clusters per event, each with
      time-coherent constituents.
    * `seed_time_window <= 0` (legacy / default): one ModuleHit per
      module — the largest-integral peak passing `time_window` (or any
      detected peak when `time_window` is None) — matches `bestPeak()`
      in viewer_utils.h."""
    cfg = p.hc_clusterer.get_config()
    multi_pulse = cfg.seed_time_window > 0.0

    p.hc_clusterer.clear()
    for ri in range(fadc_evt.nrocs):
        roc = fadc_evt.roc(ri)
        if not roc.present:
            continue
        crate = p.crate_map.get(roc.tag)
        if crate is None:
            continue
        for s in roc.present_slots():
            slot = roc.slot(s)
            for c in slot.present_channels():
                mod = p.hycal.module_by_daq(crate, s, c)
                if mod is None or not mod.is_hycal():
                    continue
                cd = slot.channel(c)
                if cd.nsamples <= 0:
                    continue
                _, _, peaks = p.wave_ana.analyze(cd.samples)

                if multi_pulse:
                    for pk in peaks:
                        if time_window is not None:
                            if not (time_window[0] < pk.time < time_window[1]):
                                continue
                        p.hc_clusterer.add_hit(mod.index,
                                               mod.energize(pk.integral),
                                               float(pk.time))
                else:
                    best = None
                    best_i = -1.0
                    for pk in peaks:
                        if time_window is not None:
                            if not (time_window[0] < pk.time < time_window[1]):
                                continue
                        if pk.integral > best_i:
                            best = pk
                            best_i = pk.integral
                    if best is None:
                        continue
                    p.hc_clusterer.add_hit(mod.index,
                                           mod.energize(best.integral),
                                           float(best.time))
    p.hc_clusterer.form_clusters()
    return p.hc_clusterer.reconstruct_hits()


def reconstruct_gem(p: Pipeline, ssp_evt) -> None:
    """Run GEM ProcessEvent + Reconstruct.  After this the per-detector
    hit lists are accessible via p.gem_sys.get_hits(d)."""
    p.gem_sys.clear()
    p.gem_sys.process_event(ssp_evt)
    p.gem_sys.reconstruct(p.gem_clusterer)


# ============================================================================
# Argparse helpers
# ============================================================================

def add_common_args(ap: argparse.ArgumentParser) -> None:
    """Register the args every analysis script accepts.  Mirrors the C++
    signatures of gem_hycal_matching / plot_hits_at_hycal."""
    ap.add_argument("evio_path",
                    help="EVIO input.  glob ('prad_NNNNNN.evio.*'), directory, "
                         "or single split (prad_NNNNNN.evio.00000).")
    ap.add_argument("out_path",
                    help="Output table path (.tsv / .csv).")
    ap.add_argument("--max-events", type=int, default=0,
                    help="Stop after N raw physics events (0 = all).")
    ap.add_argument("--run-num", type=int, default=-1,
                    help="Run number override (default -1 = sniff from filename).")
    ap.add_argument("--gem-ped-file",  default="",
                    help='GEM pedestal file (default "" = via runinfo).')
    ap.add_argument("--gem-cm-file",   default="",
                    help='GEM common-mode file (default "" = via runinfo).')
    ap.add_argument("--hc-calib-file", default="",
                    help='HyCal calibration file (default "" = via runinfo).')
    ap.add_argument("--daq-config",    default="",
                    help='DAQ config (default "" = installed default).')
    ap.add_argument("--gem-map-file",  default="",
                    help='GEM map (default "" = database/gem_map.json).')
    ap.add_argument("--hc-map-file",   default="",
                    help='HyCal map (default "" = database/hycal_map.json).')
    ap.add_argument("--csv", action="store_true",
                    help="Emit CSV instead of TSV.")
    ap.add_argument("--no-header", action="store_true",
                    help="Skip the column-name header row.")


# Physics trigger bits accepted for reconstruction.  Mirrors the server's
# `physics.accept_trigger_bits` set in monitor_config.json — currently
# {SSP0, SSP1, SSP2, SSP3} = bits {8,9,10,11} = mask 0xf00.  The check is
# bitwise-AND (an event passes if ANY accepted bit is set), matching
# `TriggerFilter::operator()` in src/app_state.h.  The previous behaviour
# (== 0x100) restricted the audit to SSP0-only events and silently
# excluded the cluster-counting triggers, which produced a 2× sample-bias
# vs the server in the GEM efficiency audit.
PHYSICS_TRIGGER_MASK = 0xf00


def passes_physics_trigger(trigger_bits: int) -> bool:
    """C++-equivalent physics trigger gate (`bits & PHYSICS_TRIGGER_MASK`).
    Use this in scripts instead of `== PHYSICS_TRIGGER_BITS`."""
    return bool(trigger_bits & PHYSICS_TRIGGER_MASK)


# Backwards-compat alias — old scripts that did `!= PHYSICS_TRIGGER_BITS`
# still work but only accept SSP0.  New code should call
# `passes_physics_trigger`.
PHYSICS_TRIGGER_BITS = 0x100


def open_table_writer(out_path: str, csv_mode: bool):
    """Open `out_path` for writing.  Returns (file_handle, write_row callable)
    where write_row(seq) writes one row.  Caller closes the file."""
    import csv as _csv
    fh = open(out_path, "w", encoding="utf-8", newline="")
    if csv_mode:
        w = _csv.writer(fh, lineterminator="\n")
        return fh, w.writerow
    sep = "\t"
    def write_row(row):
        fh.write(sep.join("" if v is None else str(v) for v in row))
        fh.write("\n")
    return fh, write_row
