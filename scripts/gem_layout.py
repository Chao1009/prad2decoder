#!/usr/bin/env python3
"""
Visualize PRad-II GEM strip layout from gem_map.json.

Shows:
- X strips (vertical lines, running along Y) in blue
- Y strips (horizontal lines, running along X) in red
- APV boundaries as thicker lines
- One subplot per detector (GEM0-GEM3)

Usage:
    python gem_layout.py [path/to/gem_map.json]

Defaults to database/gem_map.json if no argument given.
"""

import json
import sys
import os
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.collections import LineCollection
import numpy as np


def load_gem_map(path):
    with open(path, encoding="utf-8") as f:
        # strip JS-style comments (keys starting with "//")
        raw = json.load(f)

    layers = {l["id"]: l for l in raw["layers"]}

    # collect APV entries (skip comment-only objects)
    apvs = [e for e in raw["apvs"] if "crate" in e]

    return layers, apvs


def build_strip_layout(layers, apvs):
    """Build per-detector strip positions.

    Returns dict: det_id -> {
        "name", "x_size", "y_size", "x_pitch", "y_pitch",
        "x_strips": [(x_pos, y_start, y_end), ...],
        "y_strips": [(y_pos, x_start, x_end), ...],
        "x_apv_bounds": [x_pos, ...],
        "y_apv_bounds": [y_pos, ...],
    }
    """
    detectors = {}

    for det_id, layer in layers.items():
        n_x_apvs = layer["x_apvs"]
        n_y_apvs = layer["y_apvs"]
        x_pitch = layer["x_pitch"]  # mm per strip
        y_pitch = layer["y_pitch"]

        strips_per_apv = 128
        x_size = n_x_apvs * strips_per_apv * x_pitch  # total X dimension (mm)
        y_size = n_y_apvs * strips_per_apv * y_pitch  # total Y dimension (mm)

        detectors[det_id] = {
            "name": layer["name"],
            "x_size": x_size,
            "y_size": y_size,
            "x_pitch": x_pitch,
            "y_pitch": y_pitch,
            "x_strips": [],
            "y_strips": [],
            "x_apv_edges": set(),
            "y_apv_edges": set(),
        }

    # compute strip positions from APV mapping
    for apv in apvs:
        det_id = apv["det"]
        if det_id not in detectors:
            continue
        det = detectors[det_id]
        plane = apv["plane"]
        pos = apv["pos"]         # APV position index on the plane
        orient = apv["orient"]   # 0 = normal, 1 = reversed

        strips_per_apv = 128

        if plane == "X":
            # X-plane APVs: each APV covers 128 strips along X
            # strip position in X = (pos * 128 + strip_within_apv) * x_pitch
            pitch = det["x_pitch"]
            base_x = pos * strips_per_apv * pitch

            # APV boundary
            det["x_apv_edges"].add(base_x)
            det["x_apv_edges"].add(base_x + strips_per_apv * pitch)

            # each X strip is a vertical line spanning full Y
            for s in range(strips_per_apv):
                if orient == 0:
                    strip_x = base_x + s * pitch
                else:
                    strip_x = base_x + (strips_per_apv - 1 - s) * pitch
                det["x_strips"].append((strip_x, 0, det["y_size"]))

        elif plane == "Y":
            # Y-plane APVs: each APV covers 128 strips along Y
            pitch = det["y_pitch"]
            base_y = pos * strips_per_apv * pitch

            det["y_apv_edges"].add(base_y)
            det["y_apv_edges"].add(base_y + strips_per_apv * pitch)

            # each Y strip is a horizontal line spanning full X
            for s in range(strips_per_apv):
                if orient == 0:
                    strip_y = base_y + s * pitch
                else:
                    strip_y = base_y + (strips_per_apv - 1 - s) * pitch
                det["y_strips"].append((strip_y, 0, det["x_size"]))

    return detectors


def plot_detector(ax, det, det_id, show_every=8):
    """Plot one GEM detector's strip layout.

    show_every: draw every Nth strip to avoid visual clutter (default 8 = every APV boundary region)
    """
    name = det["name"]
    x_size = det["x_size"]
    y_size = det["y_size"]

    # detector outline
    ax.add_patch(plt.Rectangle((0, 0), x_size, y_size,
                                fill=False, edgecolor="gray", linewidth=1.5))

    # X strips (vertical lines) — blue
    x_lines = []
    for i, (x, y0, y1) in enumerate(sorted(det["x_strips"])):
        if i % show_every == 0:
            x_lines.append([(x, y0), (x, y1)])
    if x_lines:
        ax.add_collection(LineCollection(x_lines, colors="steelblue",
                                          linewidths=0.3, alpha=0.6))

    # Y strips (horizontal lines) — red
    y_lines = []
    for i, (y, x0, x1) in enumerate(sorted(det["y_strips"])):
        if i % show_every == 0:
            y_lines.append([(x0, y), (x1, y)])
    if y_lines:
        ax.add_collection(LineCollection(y_lines, colors="indianred",
                                          linewidths=0.3, alpha=0.6))

    # APV boundaries — thicker lines
    for x in sorted(det["x_apv_edges"]):
        ax.axvline(x, color="steelblue", linewidth=0.8, alpha=0.5, linestyle="--")
    for y in sorted(det["y_apv_edges"]):
        ax.axhline(y, color="indianred", linewidth=0.8, alpha=0.5, linestyle="--")

    ax.set_xlim(-x_size * 0.05, x_size * 1.05)
    ax.set_ylim(-y_size * 0.05, y_size * 1.05)
    ax.set_aspect("equal")
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")
    ax.set_title(f"{name} — {len(det['x_strips'])} X strips, {len(det['y_strips'])} Y strips")

    # legend
    ax.legend(handles=[
        mpatches.Patch(color="steelblue", alpha=0.6, label=f"X strips ({len(det['x_strips'])})"),
        mpatches.Patch(color="indianred", alpha=0.6, label=f"Y strips ({len(det['y_strips'])})"),
    ], loc="upper right", fontsize=8)


def main():
    # find gem_map.json
    if len(sys.argv) > 1:
        gem_map_path = sys.argv[1]
    else:
        # try common locations
        for candidate in [
            "database/gem_map.json",
            "../database/gem_map.json",
            "gem_map.json",
        ]:
            if os.path.exists(candidate):
                gem_map_path = candidate
                break
        else:
            print("Usage: python gem_layout.py [path/to/gem_map.json]")
            sys.exit(1)

    print(f"Loading: {gem_map_path}")
    layers, apvs = load_gem_map(gem_map_path)
    detectors = build_strip_layout(layers, apvs)

    n_det = len(detectors)
    print(f"Detectors: {n_det}")
    for det_id, det in sorted(detectors.items()):
        print(f"  {det['name']}: {det['x_size']:.1f} x {det['y_size']:.1f} mm, "
              f"{len(det['x_strips'])} X strips, {len(det['y_strips'])} Y strips")

    # plot: 2x2 grid
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("PRad-II GEM Strip Layout", fontsize=14)

    for det_id in sorted(detectors.keys()):
        row = det_id // 2
        col = det_id % 2
        plot_detector(axes[row][col], detectors[det_id], det_id)

    plt.tight_layout()
    plt.savefig("gem_layout.png", dpi=150, bbox_inches="tight")
    print("Saved: gem_layout.png")
    plt.show()


if __name__ == "__main__":
    main()
