#!/usr/bin/env python3
"""
Visualize GEM clustering results from gem_dump -m evdump output.

Shows per-detector:
- Fired X strips (vertical, blue colormap) and Y strips (horizontal, red colormap)
  color-coded by charge; cross-talk strips shown dashed at lower opacity
- Cluster extent bands (light shading) and center position markers (triangles)
- 2D reconstructed hit positions (green stars)
- Beam hole region (yellow)

Strip geometry is derived from gem_map.json APV properties via gem_strip_map,
so beam-hole half-strips (+Y/-Y match) are drawn with correct length.

Usage:
    python gem_cluster_view.py <event.json> [gem_map.json] [--det N] [-o file.png]
"""

import json
import sys
import os
import argparse
from collections import defaultdict

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.collections import LineCollection
from matplotlib.colors import Normalize
import matplotlib.cm as cm

from gem_layout import load_gem_map, build_strip_layout
from gem_strip_map import map_strip


# ── data loading ─────────────────────────────────────────────────────────

def load_event(path):
    raw = open(path, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw.decode("utf-8-sig")
    else:
        text = raw.decode("utf-8")
    return json.loads(text)


# ── APV-driven strip hit geometry ────────────────────────────────────────

def build_apv_map(gem_map_apvs):
    """(crate, mpd, adc) -> APV entry from gem_map.json."""
    return {(a["crate"], a["mpd"], a["adc"]): a
            for a in gem_map_apvs if "crate" in a}


def process_zs_hits(zs_apvs, apv_map, detectors, hole, raw):
    """Convert zero-suppressed APV channels to drawable strip segments.

    Uses gem_strip_map.map_strip for channel->strip conversion and the APV's
    match attribute to determine half-strip extents near the beam hole.

    Returns {det_id: {"x": [...], "y": [...]}} where each entry is
        (strip_pos, line_start, line_end, charge, cross_talk)
    """
    apv_ch = raw.get("apv_channels", 128)
    ro_center = raw.get("readout_center", 32)

    # beam hole boundaries (layout coordinates)
    if hole and "x_center" in hole:
        hx, hy = hole["x_center"], hole["y_center"]
        hw, hh = hole["width"], hole["height"]
        hole_x0, hole_x1 = hx - hw / 2, hx + hw / 2
        hole_y0, hole_y1 = hy - hh / 2, hy + hh / 2
    else:
        hole_x0 = hole_x1 = hole_y0 = hole_y1 = -1

    result = defaultdict(lambda: {"x": [], "y": []})

    for apv_entry in zs_apvs:
        key = (apv_entry["crate"], apv_entry["mpd"], apv_entry["adc"])
        props = apv_map.get(key)
        if props is None:
            continue

        det_id = props["det"]
        plane = props["plane"]
        match = props.get("match", "")
        pos = props["pos"]
        orient = props["orient"]
        pin_rotate = props.get("pin_rotate", 0)
        shared_pos = props.get("shared_pos", -1)
        hybrid_board = props.get("hybrid_board", True)

        if det_id not in detectors:
            continue
        det = detectors[det_id]

        for ch_str, ch_data in apv_entry.get("channels", {}).items():
            ch = int(ch_str)
            _, plane_strip = map_strip(
                ch, pos, orient,
                pin_rotate=pin_rotate, shared_pos=shared_pos,
                hybrid_board=hybrid_board,
                apv_channels=apv_ch, readout_center=ro_center)

            charge = ch_data["charge"]
            cross_talk = ch_data.get("cross_talk", False)

            if plane == "X":
                strip_pos = plane_strip * det["x_pitch"]
                if match == "+Y" and hole_y1 > 0:
                    s0, s1 = hole_y1, det["y_size"]
                elif match == "-Y" and hole_y0 > 0:
                    s0, s1 = 0, hole_y0
                else:
                    s0, s1 = 0, det["y_size"]
                result[det_id]["x"].append((strip_pos, s0, s1, charge, cross_talk))

            elif plane == "Y":
                strip_pos = plane_strip * det["y_pitch"]
                if hole_y0 > 0 and hole_y0 < strip_pos < hole_y1:
                    result[det_id]["y"].append((strip_pos, 0, hole_x0, charge, cross_talk))
                    result[det_id]["y"].append((strip_pos, hole_x1, det["x_size"], charge, cross_talk))
                else:
                    result[det_id]["y"].append((strip_pos, 0, det["x_size"], charge, cross_talk))

    return dict(result)


# ── per-detector plotting ────────────────────────────────────────────────

def plot_detector(ax, det_geom, det_data, det_hits, hole, norm):
    x_size = det_geom["x_size"]
    y_size = det_geom["y_size"]
    x_pitch = det_geom["x_pitch"]
    y_pitch = det_geom["y_pitch"]

    # plane sizes for coordinate conversion (cluster/2D hit positions)
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

    x_hits = det_hits.get("x", [])
    y_hits = det_hits.get("y", [])

    if not x_hits and not y_hits:
        ax.set_title(f"{det_data.get('name', 'GEM?')} -- no hits")
        _format_axes(ax, x_size, y_size)
        return

    # ── fired strips (geometry from APV properties) ──────────────────
    _draw_strips(ax, x_hits, "X", cm.Blues, norm)
    _draw_strips(ax, y_hits, "Y", cm.Reds, norm)

    # ── cluster center markers (triangles at detector edge) ──────────
    for cl in det_data.get("x_clusters", []):
        cx = cl["position"] + x_plane_size / 2 - x_pitch / 2
        ax.plot(cx, -y_size * 0.02, "^", color="blue", markersize=6,
                clip_on=False, zorder=6)

    for cl in det_data.get("y_clusters", []):
        cy = cl["position"] + y_plane_size / 2 - y_pitch / 2
        ax.plot(-x_size * 0.02, cy, ">", color="red", markersize=6,
                clip_on=False, zorder=6)

    # ── 2D reconstructed hits ────────────────────────────────────────
    for h in det_data.get("hits_2d", []):
        hx = h["x"] + x_plane_size / 2 - x_pitch / 2
        hy = h["y"] + y_plane_size / 2 - y_pitch / 2
        ax.plot(hx, hy, "+", color="darkgreen", markersize=12,
                markeredgewidth=2, zorder=7)

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


def _draw_strips(ax, hits, plane, cmap, norm):
    """Draw strip hit segments as colored lines.

    hits: list of (strip_pos, line_start, line_end, charge, cross_talk)
    """
    normal_lines, normal_colors = [], []
    xtalk_lines, xtalk_colors = [], []

    for (pos, s0, s1, charge, xtalk) in hits:
        if plane == "X":
            line = [(pos, s0), (pos, s1)]
        else:
            line = [(s0, pos), (s1, pos)]
        color = cmap(norm(charge))
        if xtalk:
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
        plt.Line2D([], [], marker="^", color="blue", linestyle="None",
                   markersize=6, label="X cluster center"),
        plt.Line2D([], [], marker=">", color="red", linestyle="None",
                   markersize=6, label="Y cluster center"),
        plt.Line2D([], [], marker="+", color="darkgreen", linestyle="None",
                   markeredgewidth=2, markersize=10, label="2D hit"),
        plt.Line2D([], [], color="gray", linestyle="--", linewidth=0.6,
                   alpha=0.5, label="Cross-talk hit"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=4, fontsize=11,
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
    layers, gem_map_apvs, hole, raw = load_gem_map(gem_map_path)
    detectors = build_strip_layout(layers, gem_map_apvs, hole, raw)
    apv_map = build_apv_map(gem_map_apvs)

    # convert zero-suppressed APV data to drawable strip hits
    zs_apvs = event.get("zs_apvs", [])
    det_hits = process_zs_hits(zs_apvs, apv_map, detectors, hole, raw)

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

    # print summary and cluster table
    for dd in det_list:
        did = dd["id"]
        hits = det_hits.get(did, {"x": [], "y": []})
        nx = len(hits["x"])
        ny = len(hits["y"])
        xcl = dd.get("x_clusters", [])
        ycl = dd.get("y_clusters", [])
        n2d = len(dd.get("hits_2d", []))
        print(f"\n  {dd['name']}: {nx} X hits, {ny} Y hits, "
              f"{len(xcl)}+{len(ycl)} clusters, {n2d} 2D hits")

        if xcl or ycl:
            print(f"  {'plane':>5} {'pos(mm)':>8} {'peak':>8} {'total':>8} "
                  f"{'size':>4} {'tbin':>4} {'xtalk':>5}  strips")
            print(f"  {'-'*5:>5} {'-'*8:>8} {'-'*8:>8} {'-'*8:>8} "
                  f"{'-'*4:>4} {'-'*4:>4} {'-'*5:>5}  {'-'*10}")
            for cl in xcl:
                strips = cl.get("hit_strips", [])
                srange = f"{min(strips)}-{max(strips)}" if strips else ""
                print(f"  {'X':>5} {cl['position']:>8.2f} {cl['peak_charge']:>8.1f} "
                      f"{cl['total_charge']:>8.1f} {cl['size']:>4} "
                      f"{cl['max_timebin']:>4} {'y' if cl.get('cross_talk') else '':>5}  "
                      f"{srange}")
            for cl in ycl:
                strips = cl.get("hit_strips", [])
                srange = f"{min(strips)}-{max(strips)}" if strips else ""
                print(f"  {'Y':>5} {cl['position']:>8.2f} {cl['peak_charge']:>8.1f} "
                      f"{cl['total_charge']:>8.1f} {cl['size']:>4} "
                      f"{cl['max_timebin']:>4} {'y' if cl.get('cross_talk') else '':>5}  "
                      f"{srange}")

        if dd.get("hits_2d"):
            print(f"  2D hits: ", end="")
            print("  ".join(f"({h['x']:.1f}, {h['y']:.1f})" for h in dd["hits_2d"]))

    # figure size matched to detector aspect ratio (~1:2)
    ref_det = detectors[min(detectors.keys())]
    det_aspect = ref_det["y_size"] / ref_det["x_size"]
    cell_w = 6
    cell_h = cell_w * det_aspect

    cols = n
    rows = 1

    fig, axes = plt.subplots(rows, cols,
                             figsize=(cell_w * cols, cell_h * rows + 1.5),
                             constrained_layout=True)
    if n == 1:
        axes = [axes]
    else:
        axes = list(axes.flat) if hasattr(axes, "flat") else list(axes)

    # global charge normalization
    all_charges = []
    for hits in det_hits.values():
        all_charges += [h[3] for h in hits["x"]]  # charge is index 3
        all_charges += [h[3] for h in hits["y"]]
    vmax = max(all_charges) if all_charges else 1
    norm = Normalize(vmin=0, vmax=vmax)

    for i, det_data in enumerate(det_list):
        did = det_data["id"]
        det_geom = detectors.get(did, detectors[min(detectors.keys())])
        hits = det_hits.get(did, {"x": [], "y": []})
        plot_detector(axes[i], det_geom, det_data, hits, hole, norm)

    for i in range(len(det_list), len(axes)):
        axes[i].set_visible(False)

    # dual colorbars matching strip colors
    active_axes = axes[:len(det_list)]
    sm_x = cm.ScalarMappable(cmap=cm.Blues, norm=norm)
    sm_x.set_array([])
    sm_y = cm.ScalarMappable(cmap=cm.Reds, norm=norm)
    sm_y.set_array([])
    cb_x = fig.colorbar(sm_x, ax=active_axes, shrink=0.4, pad=0.01,
                         aspect=30, location="right")
    cb_y = fig.colorbar(sm_y, ax=active_axes, shrink=0.4, pad=0.01,
                         aspect=30, location="right")
    cb_x.set_label("X charge (ADC)", fontsize=11)
    cb_y.set_label("Y charge (ADC)", fontsize=11)
    cb_x.ax.tick_params(labelsize=10)
    cb_y.ax.tick_params(labelsize=10)

    fig.suptitle(f"GEM Cluster View -- Event #{ev_num}", fontsize=14)
    add_legend(fig)
    plt.savefig(args.output, dpi=150, bbox_inches="tight")
    print(f"Saved: {args.output}")
    plt.show()


if __name__ == "__main__":
    main()
