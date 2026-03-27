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


# ── cluster table printout ────────────────────────────────────────────────

def print_event_summary(det_list, det_hits):
    for dd in det_list:
        did = dd["id"]
        hits = det_hits.get(did, {"x": [], "y": []})
        xcl = dd.get("x_clusters", [])
        ycl = dd.get("y_clusters", [])
        print(f"\n  {dd['name']}: {len(hits['x'])} X hits, {len(hits['y'])} Y hits, "
              f"{len(xcl)}+{len(ycl)} clusters, {len(dd.get('hits_2d',[]))} 2D hits")
        if xcl or ycl:
            print(f"  {'plane':>5} {'pos(mm)':>8} {'peak':>8} {'total':>8} "
                  f"{'size':>4} {'tbin':>4} {'xtalk':>5}  strips")
            print(f"  {'-'*5:>5} {'-'*8:>8} {'-'*8:>8} {'-'*8:>8} "
                  f"{'-'*4:>4} {'-'*4:>4} {'-'*5:>5}  {'-'*10}")
            for plane, cls in [("X", xcl), ("Y", ycl)]:
                for cl in cls:
                    strips = cl.get("hit_strips", [])
                    srange = f"{min(strips)}-{max(strips)}" if strips else ""
                    print(f"  {plane:>5} {cl['position']:>8.2f} {cl['peak_charge']:>8.1f} "
                          f"{cl['total_charge']:>8.1f} {cl['size']:>4} "
                          f"{cl['max_timebin']:>4} {'y' if cl.get('cross_talk') else '':>5}  "
                          f"{srange}")
        if dd.get("hits_2d"):
            print("  2D hits: " +
                  "  ".join(f"({h['x']:.1f}, {h['y']:.1f})" for h in dd["hits_2d"]))


# ── batch / single-file rendering ────────────────────────────────────────

def render_event(event_path, gem_map_path, detectors, apv_map, hole, raw,
                 det_filter=-1, output=None):
    """Render one event JSON to a PNG file. Returns output path."""
    event = load_event(event_path)
    det_list = event.get("detectors", [])
    if det_filter >= 0:
        det_list = [d for d in det_list if d["id"] == det_filter]
    det_hits = process_zs_hits(event.get("zs_apvs", []), apv_map,
                               detectors, hole, raw)
    n = len(det_list)
    if n == 0:
        return None

    ref = detectors[min(detectors.keys())]
    cell_w = 6
    cell_h = cell_w * ref["y_size"] / ref["x_size"]

    fig, axes = plt.subplots(1, n,
                             figsize=(cell_w * n, cell_h + 1.5),
                             constrained_layout=True)
    if n == 1:
        axes = [axes]
    else:
        axes = list(axes.flat) if hasattr(axes, "flat") else axes

    all_q = []
    for h in det_hits.values():
        all_q += [x[3] for x in h["x"]] + [x[3] for x in h["y"]]
    norm = Normalize(vmin=0, vmax=max(all_q) if all_q else 1)

    for i, dd in enumerate(det_list):
        did = dd["id"]
        dg = detectors.get(did, detectors[min(detectors.keys())])
        plot_detector(axes[i], dg, dd,
                      det_hits.get(did, {"x": [], "y": []}), hole, norm)
    for i in range(n, len(axes)):
        axes[i].set_visible(False)

    active = axes[:n]
    for cmap_obj, label in [(cm.Blues, "X charge (ADC)"),
                            (cm.Reds, "Y charge (ADC)")]:
        sm = cm.ScalarMappable(cmap=cmap_obj, norm=norm); sm.set_array([])
        cb = fig.colorbar(sm, ax=active, shrink=0.4, pad=0.01,
                          aspect=30, location="right")
        cb.set_label(label, fontsize=11); cb.ax.tick_params(labelsize=10)

    ev_num = event.get("event_number", "?")
    fig.suptitle(f"GEM Cluster View -- Event #{ev_num}", fontsize=14)
    add_legend(fig)

    if output is None:
        output = os.path.splitext(event_path)[0] + ".png"
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output


# ── main ─────────────────────────────────────────────────────────────────

def main():
    import glob as globmod

    parser = argparse.ArgumentParser(
        description="Visualize GEM clustering from gem_dump -m evdump JSON. "
                    "Accepts a single file, directory, or glob pattern.")
    parser.add_argument("event_json",
                        help="Event JSON file, directory, or glob pattern")
    parser.add_argument("gem_map", nargs="?",
                        help="GEM map JSON (default: auto-search)")
    parser.add_argument("--det", type=int, default=-1,
                        help="Show only detector N (default: all)")
    parser.add_argument("-o", "--output", default=None,
                        help="Output PNG (single file mode only)")
    args = parser.parse_args()

    # find gem_map
    gem_map_path = args.gem_map
    if not gem_map_path:
        for c in ["database/gem_map.json", "../database/gem_map.json", "gem_map.json"]:
            if os.path.exists(c):
                gem_map_path = c; break
    if not gem_map_path:
        print("Error: cannot find gem_map.json"); sys.exit(1)

    # collect files
    path = args.event_json
    if os.path.isdir(path):
        files = sorted(globmod.glob(os.path.join(path, "gem_event*.json")))
    elif "*" in path or "?" in path:
        files = sorted(globmod.glob(path))
    else:
        files = [path]
    files = [f for f in files if f.lower().endswith(".json")]
    if not files:
        print("Error: no JSON files found"); sys.exit(1)

    # load geometry once
    print(f"GEM map    : {gem_map_path}")
    layers, gem_map_apvs, hole, raw = load_gem_map(gem_map_path)
    detectors = build_strip_layout(layers, gem_map_apvs, hole, raw)
    apv_map = build_apv_map(gem_map_apvs)

    print(f"Files      : {len(files)}")

    for i, fpath in enumerate(files):
        fname = os.path.basename(fpath)
        event = load_event(fpath)
        if not isinstance(event, dict) or "detectors" not in event:
            print(f"\n[{i+1}/{len(files)}] {fname} -- skipped (not an event file)")
            continue
        det_list = event.get("detectors", [])
        if args.det >= 0:
            det_list = [d for d in det_list if d["id"] == args.det]
        det_hits = process_zs_hits(event.get("zs_apvs", []), apv_map,
                                   detectors, hole, raw)

        print(f"\n[{i+1}/{len(files)}] {fname}")
        print_event_summary(det_list, det_hits)

        out = args.output if (args.output and len(files) == 1) else None
        result = render_event(fpath, gem_map_path, detectors, apv_map, hole,
                              raw, args.det, out)
        if result:
            print(f"  -> {result}")

    print(f"\nDone: {len(files)} file(s) processed.")


if __name__ == "__main__":
    main()
