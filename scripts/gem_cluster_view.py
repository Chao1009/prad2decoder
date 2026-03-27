#!/usr/bin/env python3
"""
Visualize GEM clustering results from gem_dump -m evdump output.

Shows per-detector:
- Fired X strips (vertical, blue colormap) and Y strips (horizontal, red colormap)
  color-coded by charge; cross-talk strips shown dashed at lower opacity
- Cluster extent bands (light shading) and center position markers (triangles)
- 2D reconstructed hit positions (green stars)
- Beam hole region (yellow)

Usage:
    python gem_cluster_view.py <event.json> [gem_map.json] [--det N] [-o file.png]

Requires gem_map.json for geometry (beam hole, strip layout).
"""

import json
import sys
import os
import argparse

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.collections import LineCollection
from matplotlib.colors import Normalize
import matplotlib.cm as cm

from gem_layout import load_gem_map, build_strip_layout


# ── data loading ─────────────────────────────────────────────────────────

def load_event(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_strip_lookup(detectors):
    """Map (det_id, 'X'/'Y', strip_number) -> list of line segments.

    X segments: (strip_x, y0, y1)   — vertical line endpoints
    Y segments: (strip_y, x0, x1)   — horizontal line endpoints
    """
    lookup = {}
    for det_id, det in detectors.items():
        xp = det["x_pitch"]
        for (sx, y0, y1) in det["x_strips"]:
            s = int(round(sx / xp))
            lookup.setdefault((det_id, "X", s), []).append((sx, y0, y1))

        yp = det["y_pitch"]
        for (sy, x0, x1) in det["y_strips"]:
            s = int(round(sy / yp))
            lookup.setdefault((det_id, "Y", s), []).append((sy, x0, x1))

    return lookup


# ── per-detector plotting ────────────────────────────────────────────────

def plot_detector(ax, det_geom, det_data, strip_lookup, det_id, hole):
    x_size = det_geom["x_size"]
    y_size = det_geom["y_size"]
    x_pitch = det_geom["x_pitch"]
    y_pitch = det_geom["y_pitch"]

    # plane sizes from event JSON (for coordinate conversion)
    x_plane_size = det_data.get("x_strips", 0) * det_data.get("x_pitch", x_pitch)
    y_plane_size = det_data.get("y_strips", 0) * det_data.get("y_pitch", y_pitch)
    if x_plane_size == 0:
        x_plane_size = x_size
    if y_plane_size == 0:
        y_plane_size = y_size

    # detector outline
    ax.add_patch(plt.Rectangle((0, 0), x_size, y_size,
                                fill=False, edgecolor="gray", linewidth=1.5))

    # beam hole
    if hole and "x_center" in hole:
        hx, hy = hole["x_center"], hole["y_center"]
        hw, hh = hole["width"], hole["height"]
        ax.add_patch(plt.Rectangle((hx - hw / 2, hy - hh / 2), hw, hh,
                                    fill=True, facecolor="#ffcc0018",
                                    edgecolor="#ffcc00", linewidth=1.5,
                                    linestyle="-", zorder=1))

    x_hits = det_data.get("x_hits", [])
    y_hits = det_data.get("y_hits", [])
    all_charges = [h["charge"] for h in x_hits] + [h["charge"] for h in y_hits]

    if not all_charges:
        ax.set_title(f"{det_data.get('name', 'GEM?')} -- no hits")
        _format_axes(ax, x_size, y_size)
        return

    vmax = max(all_charges)
    norm = Normalize(vmin=0, vmax=vmax)
    x_cmap = cm.Blues
    y_cmap = cm.Reds

    # ── cluster extent bands ─────────────────────────────────────────
    for cl in det_data.get("x_clusters", []):
        strips = cl.get("hit_strips", [])
        if strips:
            x0 = min(strips) * x_pitch - x_pitch * 0.5
            x1 = (max(strips) + 1) * x_pitch - x_pitch * 0.5
            ax.axvspan(x0, x1, alpha=0.12, color="steelblue", zorder=1.5)

    for cl in det_data.get("y_clusters", []):
        strips = cl.get("hit_strips", [])
        if strips:
            y0 = min(strips) * y_pitch - y_pitch * 0.5
            y1 = (max(strips) + 1) * y_pitch - y_pitch * 0.5
            ax.axhspan(y0, y1, alpha=0.12, color="indianred", zorder=1.5)

    # ── fired X strips (vertical lines) ─────────────────────────────
    _draw_strip_hits(ax, x_hits, "X", det_id, strip_lookup, x_cmap, norm,
                     make_line=lambda sx, y0, y1: [(sx, y0), (sx, y1)])

    # ── fired Y strips (horizontal lines) ────────────────────────────
    _draw_strip_hits(ax, y_hits, "Y", det_id, strip_lookup, y_cmap, norm,
                     make_line=lambda sy, x0, x1: [(x0, sy), (x1, sy)])

    # ── cluster center markers ───────────────────────────────────────
    for cl in det_data.get("x_clusters", []):
        cx = cl["position"] + x_plane_size / 2 - x_pitch / 2
        label = f"Q={cl['total_charge']:.0f} n={cl['size']}"
        ax.plot(cx, -y_size * 0.025, "^", color="blue", markersize=7,
                clip_on=False, zorder=6)
        ax.annotate(label, (cx, -y_size * 0.025), fontsize=5,
                    ha="center", va="top", color="blue",
                    xytext=(0, -6), textcoords="offset points")

    for cl in det_data.get("y_clusters", []):
        cy = cl["position"] + y_plane_size / 2 - y_pitch / 2
        label = f"Q={cl['total_charge']:.0f} n={cl['size']}"
        ax.plot(-x_size * 0.025, cy, ">", color="red", markersize=7,
                clip_on=False, zorder=6)
        ax.annotate(label, (-x_size * 0.025, cy), fontsize=5,
                    ha="right", va="center", color="red",
                    xytext=(-6, 0), textcoords="offset points")

    # ── 2D reconstructed hits ────────────────────────────────────────
    for h in det_data.get("hits_2d", []):
        hx = h["x"] + x_plane_size / 2 - x_pitch / 2
        hy = h["y"] + y_plane_size / 2 - y_pitch / 2
        ax.plot(hx, hy, "*", color="lime", markersize=14,
                markeredgecolor="darkgreen", markeredgewidth=0.5, zorder=7)

    # ── title and formatting ─────────────────────────────────────────
    n_xh = len(x_hits)
    n_yh = len(y_hits)
    n_xcl = len(det_data.get("x_clusters", []))
    n_ycl = len(det_data.get("y_clusters", []))
    n_2d = len(det_data.get("hits_2d", []))
    ax.set_title(f"{det_data.get('name', 'GEM?')} -- "
                 f"X: {n_xh} hits / {n_xcl} cl   "
                 f"Y: {n_yh} hits / {n_ycl} cl   "
                 f"2D: {n_2d}", fontsize=10)
    _format_axes(ax, x_size, y_size)

    # colorbar
    sm = cm.ScalarMappable(cmap=cm.hot, norm=norm)
    sm.set_array([])
    cbar = plt.colorbar(sm, ax=ax, fraction=0.02, pad=0.01)
    cbar.set_label("Charge (ADC)", fontsize=8)
    cbar.ax.tick_params(labelsize=7)


def _draw_strip_hits(ax, hits, plane, det_id, strip_lookup, cmap, norm,
                     make_line):
    """Draw normal and cross-talk strip hits as two LineCollections."""
    normal_lines, normal_colors = [], []
    xtalk_lines, xtalk_colors = [], []

    for h in hits:
        segs = strip_lookup.get((det_id, plane, h["strip"]), [])
        color = cmap(norm(h["charge"]))
        for seg in segs:
            line = make_line(*seg)
            if h.get("cross_talk", False):
                xtalk_lines.append(line)
                xtalk_colors.append(color)
            else:
                normal_lines.append(line)
                normal_colors.append(color)

    if normal_lines:
        ax.add_collection(LineCollection(normal_lines, colors=normal_colors,
                                          linewidths=1.2, alpha=0.9, zorder=2))
    if xtalk_lines:
        ax.add_collection(LineCollection(xtalk_lines, colors=xtalk_colors,
                                          linewidths=0.6, linestyles="dashed",
                                          alpha=0.4, zorder=2))


def _format_axes(ax, x_size, y_size):
    ax.set_xlim(-x_size * 0.06, x_size * 1.06)
    ax.set_ylim(-y_size * 0.06, y_size * 1.06)
    ax.set_aspect("equal")
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")


# ── legend ───────────────────────────────────────────────────────────────

def add_legend(fig):
    handles = [
        mpatches.Patch(color="steelblue", alpha=0.6, label="X strip hits"),
        mpatches.Patch(color="indianred", alpha=0.6, label="Y strip hits"),
        mpatches.Patch(facecolor="steelblue", alpha=0.15, label="X cluster range"),
        mpatches.Patch(facecolor="indianred", alpha=0.15, label="Y cluster range"),
        plt.Line2D([], [], marker="^", color="blue", linestyle="None",
                   markersize=7, label="X cluster center"),
        plt.Line2D([], [], marker=">", color="red", linestyle="None",
                   markersize=7, label="Y cluster center"),
        plt.Line2D([], [], marker="*", color="lime", linestyle="None",
                   markeredgecolor="darkgreen", markersize=12,
                   label="2D hit"),
        plt.Line2D([], [], color="gray", linestyle="--", linewidth=0.6,
                   alpha=0.5, label="Cross-talk hit"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=4, fontsize=8,
               framealpha=0.9)


# ── main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Visualize GEM clustering from gem_dump -m evdump JSON")
    parser.add_argument("event_json", help="Event JSON from gem_dump -m evdump")
    parser.add_argument("gem_map", nargs="?",
                        help="GEM map JSON (default: auto-search)")
    parser.add_argument("--det", type=int, default=-1,
                        help="Show only detector N (default: all)")
    parser.add_argument("-o", "--output", default="gem_cluster_view.png",
                        help="Output image file (default: gem_cluster_view.png)")
    args = parser.parse_args()

    # find gem_map.json
    if args.gem_map:
        gem_map_path = args.gem_map
    else:
        for candidate in ["database/gem_map.json",
                          "../database/gem_map.json",
                          "gem_map.json"]:
            if os.path.exists(candidate):
                gem_map_path = candidate
                break
        else:
            print("Error: cannot find gem_map.json. Specify path as argument.")
            sys.exit(1)

    # load data
    print(f"Event data : {args.event_json}")
    event = load_event(args.event_json)

    print(f"GEM map    : {gem_map_path}")
    layers, apvs, hole, raw = load_gem_map(gem_map_path)
    detectors = build_strip_layout(layers, apvs, hole, raw)
    strip_lookup = build_strip_lookup(detectors)

    ev_num = event.get("event_number", "?")
    det_list = event.get("detectors", [])
    if args.det >= 0:
        det_list = [d for d in det_list if d["id"] == args.det]
        if not det_list:
            print(f"Error: detector {args.det} not in event data")
            sys.exit(1)

    n = len(det_list)
    if n == 0:
        print("No detector data in event JSON")
        sys.exit(1)

    # print summary
    for dd in det_list:
        nx = len(dd.get("x_hits", []))
        ny = len(dd.get("y_hits", []))
        nc = len(dd.get("x_clusters", [])) + len(dd.get("y_clusters", []))
        n2d = len(dd.get("hits_2d", []))
        print(f"  {dd['name']}: {nx} X hits, {ny} Y hits, "
              f"{nc} clusters, {n2d} 2D hits")

    # layout
    if n == 1:
        fig, ax = plt.subplots(1, 1, figsize=(10, 8))
        axes = [ax]
    elif n <= 2:
        fig, axes = plt.subplots(1, n, figsize=(10 * n, 8))
        axes = list(axes)
    else:
        cols = 2
        rows = (n + 1) // 2
        fig, axes = plt.subplots(rows, cols, figsize=(18, 8 * rows))
        axes = list(axes.flat)

    for i, det_data in enumerate(det_list):
        did = det_data["id"]
        det_geom = detectors.get(did, detectors[min(detectors.keys())])
        plot_detector(axes[i], det_geom, det_data, strip_lookup, did, hole)

    # hide unused subplots
    for i in range(len(det_list), len(axes)):
        axes[i].set_visible(False)

    fig.suptitle(f"GEM Cluster View -- Event #{ev_num}", fontsize=14)
    add_legend(fig)
    plt.tight_layout(rect=[0, 0.04, 1, 0.96])
    plt.savefig(args.output, dpi=150, bbox_inches="tight")
    print(f"Saved: {args.output}")
    plt.show()


if __name__ == "__main__":
    main()
