#!/usr/bin/env python3
"""
HyCal Pedestal Monitor
======================
Measures and monitors FADC250 pedestals for all HyCal channels.
Maps DAQ addresses (crate/slot/channel) to HyCal modules and generates
spatial map plots for pedestal mean, RMS, and drifts from the original.

Usage
-----
    # View original pedestals only
    python hycal_pedestal_monitor.py

    # Measure new pedestals then compare with originals
    python hycal_pedestal_monitor.py --measure

    # Compare a previously-measured set with originals
    python hycal_pedestal_monitor.py --latest-dir ./pedestal_latest

    # Quick test on any machine (no SSH / no pedestal files needed)
    python hycal_pedestal_monitor.py --sim
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.collections import PatchCollection
from matplotlib.colors import Normalize


# ===========================================================================
#  Paths & constants
# ===========================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
DB_DIR = SCRIPT_DIR / ".." / "database"
MODULES_JSON = DB_DIR / "hycal_modules.json"
DAQ_MAP_JSON = DB_DIR / "daq_map.json"

NUM_CRATES = 7
CRATE_NAMES = [f"adchycal{i}" for i in range(1, NUM_CRATES + 1)]
ORIGINAL_PED_DIR = Path("/usr/clas12/release/2.0.0/parms/fadc250/peds")
CHANNELS_PER_SLOT = 16


# ===========================================================================
#  Module database
# ===========================================================================

@dataclass
class Module:
    name: str
    mod_type: str   # "PbWO4", "PbGlass", "LMS"
    x: float        # centre-x in HyCal frame (mm)
    y: float        # centre-y in HyCal frame (mm)
    sx: float       # width  (mm)
    sy: float       # height (mm)


def load_modules(path: Path) -> List[Module]:
    with open(path) as f:
        data = json.load(f)
    return [Module(e["n"], e["t"], e["x"], e["y"], e["sx"], e["sy"])
            for e in data]


# ===========================================================================
#  DAQ map
# ===========================================================================

def load_daq_map(path: Path = DAQ_MAP_JSON) -> Dict[Tuple[int, int, int], str]:
    """Return mapping  (crate_index, slot, channel) -> module_name."""
    with open(path) as f:
        data = json.load(f)
    return {(d["crate"], d["slot"], d["channel"]): d["name"] for d in data}


# ===========================================================================
#  Pedestal file parser
# ===========================================================================

def parse_pedestal_file(filepath: Path) -> Dict[int, Dict[str, List[float]]]:
    """Parse one FADC250 pedestal .cnf file.

    Returns
    -------
    dict : slot_number -> {"ped": [16 floats], "noise": [16 floats]}
        The "noise" key is present only when FADC250_ALLCH_NOISE lines exist.
    """
    slots: Dict[int, Dict[str, List[float]]] = {}
    cur_slot: Optional[int] = None
    cur_key: Optional[str] = None      # "ped" or "noise"
    vals: List[float] = []

    def _flush():
        nonlocal cur_key, vals
        if cur_slot is not None and cur_key and vals:
            slots.setdefault(cur_slot, {})[cur_key] = vals[:CHANNELS_PER_SLOT]
        vals = []
        cur_key = None

    with open(filepath) as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue

            if line.startswith("FADC250_CRATE"):
                _flush()

            elif line.startswith("FADC250_SLOT"):
                _flush()
                cur_slot = int(line.split()[1])

            elif line.startswith("FADC250_ALLCH_PED"):
                _flush()
                cur_key = "ped"
                vals = [float(v) for v in
                        line[len("FADC250_ALLCH_PED"):].split()]
                if len(vals) >= CHANNELS_PER_SLOT:
                    _flush()

            elif line.startswith("FADC250_ALLCH_NOISE"):
                _flush()
                cur_key = "noise"
                vals = [float(v) for v in
                        line[len("FADC250_ALLCH_NOISE"):].split()]
                if len(vals) >= CHANNELS_PER_SLOT:
                    _flush()

            elif cur_key is not None:
                # continuation line (values that wrapped)
                try:
                    vals.extend(float(v) for v in line.split())
                    if len(vals) >= CHANNELS_PER_SLOT:
                        _flush()
                except ValueError:
                    _flush()

    _flush()
    return slots


def read_all_pedestals(
    ped_dir: Path,
    suffix: str,
    daq_map: Dict[Tuple[int, int, int], str],
) -> Dict[str, Dict[str, float]]:
    """Read pedestal files for all 7 crates.

    Parameters
    ----------
    ped_dir : directory containing adchycal{1..7}<suffix> files
    suffix  : e.g. "_ped.cnf" or "_ped_latest.cnf"
    daq_map : (crate, slot, ch) -> module name

    Returns
    -------
    dict : module_name -> {"ped": float, "noise": float}
    """
    result: Dict[str, Dict[str, float]] = {}
    for crate_idx, crate_name in enumerate(CRATE_NAMES):
        fpath = ped_dir / f"{crate_name}{suffix}"
        if not fpath.exists():
            print(f"  Warning: {fpath} not found")
            continue
        for slot, data in parse_pedestal_file(fpath).items():
            for ch in range(CHANNELS_PER_SLOT):
                mod = daq_map.get((crate_idx, slot, ch))
                if mod is None:
                    continue
                entry: Dict[str, float] = {}
                if "ped" in data and ch < len(data["ped"]):
                    entry["ped"] = data["ped"][ch]
                if "noise" in data and ch < len(data["noise"]):
                    entry["noise"] = data["noise"][ch]
                if entry:
                    result[mod] = entry
    return result


# ===========================================================================
#  Pedestal measurement via SSH
# ===========================================================================

def measure_pedestals(latest_dir: Path) -> bool:
    """Ask for confirmation, then SSH to all 7 crates to run faV3peds.

    Returns True if measurement completed, False if cancelled.
    """
    print()
    print("=" * 60)
    print("  WARNING: Pedestal measurement will INTERRUPT DAQ running!")
    print("  Only proceed when DAQ is IDLE.")
    print("=" * 60)
    resp = input("\nProceed with pedestal measurement? [yes/no]: ").strip().lower()
    if resp not in ("yes", "y"):
        print("Measurement cancelled.")
        return False

    latest_dir.mkdir(parents=True, exist_ok=True)

    # --- run faV3peds on each crate ---
    print("\nMeasuring pedestals on all crates ...")
    for cname in CRATE_NAMES:
        print(f"  {cname} ... ", end="", flush=True)
        cmd = f'ssh {cname} "faV3peds {cname}_ped_latest.cnf"'
        try:
            subprocess.run(cmd, shell=True, check=True, timeout=120)
            print("done")
        except subprocess.CalledProcessError as exc:
            print(f"FAILED (exit {exc.returncode})")
        except subprocess.TimeoutExpired:
            print("TIMEOUT")

    # --- retrieve result files ---
    print("\nRetrieving pedestal files ...")
    for cname in CRATE_NAMES:
        src = f"{cname}:~/{cname}_ped_latest.cnf"
        dst = latest_dir / f"{cname}_ped_latest.cnf"
        try:
            subprocess.run(f"scp {src} {dst}",
                           shell=True, check=True, timeout=30)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(f"  Warning: scp from {cname} failed: {exc}")

    print(f"Pedestal files saved to {latest_dir}/")
    return True


# ===========================================================================
#  Summary statistics
# ===========================================================================

def print_stats(label: str, peds: Dict[str, Dict[str, float]]):
    ped_vals = np.array([v["ped"] for v in peds.values() if "ped" in v])
    noise_vals = np.array([v["noise"] for v in peds.values() if "noise" in v])
    live = ped_vals[ped_vals != 0] if len(ped_vals) else ped_vals
    dead = int(np.sum(ped_vals == 0)) if len(ped_vals) else 0
    print(f"\n  {label}:")
    print(f"    Channels with data : {len(ped_vals)}")
    print(f"    Dead (ped == 0)    : {dead}")
    if len(live):
        print(f"    Pedestal  mean={np.mean(live):.1f}  "
              f"min={np.min(live):.1f}  max={np.max(live):.1f}")
    if len(noise_vals):
        print(f"    Noise     mean={np.mean(noise_vals):.2f}  "
              f"min={np.min(noise_vals):.2f}  max={np.max(noise_vals):.2f}")
    else:
        print("    (no noise/RMS data in files)")


# ===========================================================================
#  Plotting
# ===========================================================================

MOD_SHRINK = 0.92   # visual gap between neighbouring modules


def _plot_hycal_map(
    ax,
    modules: List[Module],
    values: Dict[str, float],
    title: str,
    cmap: str = "viridis",
    center_zero: bool = False,
):
    """Draw one colour-coded HyCal map onto *ax*."""
    patches: List[Rectangle] = []
    colors: List[float] = []
    grey: List[Rectangle] = []

    for m in modules:
        if m.mod_type == "LMS":
            continue
        w = m.sx * MOD_SHRINK
        h = m.sy * MOD_SHRINK
        rect = Rectangle((m.x - w / 2, m.y - h / 2), w, h)
        v = values.get(m.name)
        if v is not None:
            patches.append(rect)
            colors.append(v)
        else:
            grey.append(rect)

    # unmapped / missing channels shown in dark grey
    if grey:
        ax.add_collection(
            PatchCollection(grey, facecolors="#1a1a2e",
                            edgecolors="#333333", linewidths=0.3))

    if patches:
        ca = np.array(colors)
        # robust limits (ignore dead channels for percentile)
        live = ca[ca != 0] if np.any(ca != 0) else ca
        if len(live):
            vmin = float(np.percentile(live, 2))
            vmax = float(np.percentile(live, 98))
        else:
            vmin, vmax = 0.0, 1.0
        if center_zero:
            mx = max(abs(vmin), abs(vmax), 1e-9)
            vmin, vmax = -mx, mx

        norm = Normalize(vmin=vmin, vmax=vmax)
        pc = PatchCollection(patches, cmap=cmap, norm=norm,
                             edgecolors="#444444", linewidths=0.15)
        pc.set_array(ca)
        ax.add_collection(pc)
        plt.colorbar(pc, ax=ax, shrink=0.82, pad=0.02)

    # axis limits from module geometry
    det = [m for m in modules if m.mod_type != "LMS"]
    pad = 15
    ax.set_xlim(min(m.x - m.sx / 2 for m in det) - pad,
                max(m.x + m.sx / 2 for m in det) + pad)
    ax.set_ylim(min(m.y - m.sy / 2 for m in det) - pad,
                max(m.y + m.sy / 2 for m in det) + pad)
    ax.set_aspect("equal")
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_xlabel("x (mm)", fontsize=9)
    ax.set_ylabel("y (mm)", fontsize=9)


def _placeholder(ax, title: str, msg: str):
    """Put a centred text message on an otherwise-empty subplot."""
    ax.text(0.5, 0.5, msg, ha="center", va="center", fontsize=12,
            transform=ax.transAxes, color="#888888")
    ax.set_title(title, fontsize=11, fontweight="bold")


def generate_plots(
    modules: List[Module],
    original: Dict[str, Dict[str, float]],
    latest: Optional[Dict[str, Dict[str, float]]] = None,
    output: Optional[Path] = None,
    show: bool = True,
):
    """Create a 2x2 figure with pedestal mean, RMS, and deltas."""
    plt.style.use("dark_background")

    def _extract(peds, key):
        return {n: v[key] for n, v in peds.items() if key in v}

    has_latest = latest is not None and len(latest) > 0
    cur = latest if has_latest else original
    cur_label = "Current" if has_latest else "Original"

    ped_mean = _extract(cur, "ped")
    ped_noise = _extract(cur, "noise")
    has_noise = bool(ped_noise)

    fig, axes = plt.subplots(2, 2, figsize=(16, 14))
    fig.suptitle("HyCal Pedestal Monitor", fontsize=15,
                 fontweight="bold", y=0.98)

    # ---- top-left: pedestal mean ----
    _plot_hycal_map(axes[0, 0], modules, ped_mean,
                    f"{cur_label} Pedestal Mean")

    # ---- top-right: pedestal RMS / noise ----
    if has_noise:
        _plot_hycal_map(axes[0, 1], modules, ped_noise,
                        f"{cur_label} Pedestal RMS")
    else:
        _placeholder(axes[0, 1], f"{cur_label} Pedestal RMS",
                     "No noise/RMS data available\nin pedestal files")

    # ---- bottom row: differences (current - original) ----
    if has_latest:
        delta_mean: Dict[str, float] = {}
        delta_noise: Dict[str, float] = {}
        for n in latest:
            if n in original:
                if "ped" in latest[n] and "ped" in original[n]:
                    delta_mean[n] = latest[n]["ped"] - original[n]["ped"]
                if "noise" in latest[n] and "noise" in original[n]:
                    delta_noise[n] = latest[n]["noise"] - original[n]["noise"]

        _plot_hycal_map(axes[1, 0], modules, delta_mean,
                        "Pedestal Mean Difference\n(Current \u2212 Original)",
                        cmap="RdBu_r", center_zero=True)

        if delta_noise:
            _plot_hycal_map(axes[1, 1], modules, delta_noise,
                            "Pedestal RMS Difference\n(Current \u2212 Original)",
                            cmap="RdBu_r", center_zero=True)
        else:
            _placeholder(axes[1, 1],
                         "RMS Difference (Current \u2212 Original)",
                         "No noise/RMS data available\nin pedestal files")
    else:
        _placeholder(axes[1, 0], "Pedestal Mean Difference",
                     "No comparison data\n(use --measure or --latest-dir)")
        _placeholder(axes[1, 1], "Pedestal RMS Difference",
                     "No comparison data\n(use --measure or --latest-dir)")

    fig.tight_layout(rect=[0, 0, 1, 0.96])

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output, dpi=150, bbox_inches="tight")
        print(f"\nPlot saved to {output}")
    if show:
        plt.show()
    else:
        plt.close(fig)


# ===========================================================================
#  Simulation (for testing without DAQ access)
# ===========================================================================

def simulate_pedestals(
    modules: List[Module],
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]]]:
    """Return (original, latest) with random but realistic pedestal data."""
    rng = np.random.default_rng(42)
    original: Dict[str, Dict[str, float]] = {}
    latest:   Dict[str, Dict[str, float]] = {}

    for m in modules:
        if m.mod_type == "LMS":
            continue
        o_ped = float(rng.normal(160, 25))
        o_noi = float(abs(rng.normal(4.0, 1.0)))
        original[m.name] = {"ped": o_ped, "noise": o_noi}
        latest[m.name]   = {"ped": o_ped + float(rng.normal(0, 3)),
                            "noise": abs(o_noi + float(rng.normal(0, 0.3)))}

    # sprinkle dead channels
    names = [m.name for m in modules if m.mod_type != "LMS"]
    for n in rng.choice(names, size=15, replace=False):
        original[n]["ped"] = 0.0
        latest[n]["ped"]   = 0.0
    # one hot channel
    hot = str(rng.choice(names))
    original[hot]["ped"] = 2049.0
    latest[hot]["ped"]   = 2050.0

    return original, latest


# ===========================================================================
#  Main
# ===========================================================================

def main():
    ap = argparse.ArgumentParser(
        description="HyCal Pedestal Monitor - measure, read, and visualize "
                    "FADC250 pedestals for all HyCal channels")
    ap.add_argument(
        "--measure", action="store_true",
        help="Run pedestal measurement on all 7 crates via SSH "
             "(only when DAQ is idle)")
    ap.add_argument(
        "--original-dir", type=Path, default=ORIGINAL_PED_DIR,
        help="Directory with original pedestal files "
             f"(default: {ORIGINAL_PED_DIR})")
    ap.add_argument(
        "--latest-dir", type=Path, default=None,
        help="Directory with latest measured pedestal files "
             "(default: ./pedestal_latest when --measure is used)")
    ap.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Save plot to image file (e.g. pedestals.png)")
    ap.add_argument(
        "--no-show", action="store_true",
        help="Do not display the plot interactively")
    ap.add_argument(
        "--sim", action="store_true",
        help="Use simulated data for testing (no SSH or file access needed)")
    ap.add_argument(
        "--modules-db", type=Path, default=MODULES_JSON,
        help=f"Path to hycal_modules.json (default: {MODULES_JSON})")
    ap.add_argument(
        "--daq-map", type=Path, default=DAQ_MAP_JSON,
        help=f"Path to daq_map.json (default: {DAQ_MAP_JSON})")

    args = ap.parse_args()

    # ---- load module geometry ----
    modules = load_modules(args.modules_db)
    print(f"Loaded {len(modules)} modules from {args.modules_db}")

    # ---- simulation mode ----
    if args.sim:
        print("=== Simulation Mode ===")
        original, latest = simulate_pedestals(modules)
        print_stats("Original (simulated)", original)
        print_stats("Latest   (simulated)", latest)
        generate_plots(modules, original, latest,
                       args.output, show=not args.no_show)
        return

    # ---- load DAQ map ----
    daq_map = load_daq_map(args.daq_map)
    print(f"Loaded {len(daq_map)} DAQ channel mappings from {args.daq_map}")

    # ---- optional measurement ----
    latest_dir = args.latest_dir
    if args.measure:
        latest_dir = latest_dir or Path("./pedestal_latest")
        if not measure_pedestals(latest_dir):
            latest_dir = None           # cancelled

    # ---- read original pedestals ----
    print(f"\nReading original pedestals from {args.original_dir} ...")
    original = read_all_pedestals(args.original_dir, "_ped.cnf", daq_map)
    print_stats("Original pedestals", original)

    # ---- read latest pedestals (if available) ----
    latest = None
    if latest_dir and latest_dir.exists():
        print(f"\nReading latest pedestals from {latest_dir} ...")
        latest = read_all_pedestals(latest_dir, "_ped_latest.cnf", daq_map)
        print_stats("Latest pedestals", latest)

    if not original and not latest:
        print("\nERROR: No pedestal data found. Check file paths.")
        sys.exit(1)

    # ---- generate plots ----
    generate_plots(modules, original, latest,
                   args.output, show=not args.no_show)


if __name__ == "__main__":
    main()
