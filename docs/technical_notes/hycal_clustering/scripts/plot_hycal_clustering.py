#!/usr/bin/env python3
"""
plot_hycal_clustering.py — figures for the HyCal clustering technical note.

Drives the production reconstruction code (`fdec::HyCalCluster`) through
`prad2py.det` rather than reimplementing the algorithm in Python.  The
only Python-side mathematics is shower-input synthesis (Gaussian over
modules) and one explicit baseline (energy-weighted centroid) included
purely to motivate the log-weighted scheme in figure 2.

Figures generated (PNG, in ../plots/):

  hycal_fig1_layout.png         — HyCal sectors and module types
  hycal_fig2_single_cluster.png — DFS grouping, seed, log-weighted centroid
  hycal_fig3_split.png          — two overlapping showers, profile-based split
  hycal_fig4_params.png         — log_weight_thres effect + shower-depth curves
  hycal_fig5_timing_window.png  — seed-anchored timing coincidence on a 3×3 island
  hycal_fig6_multi_pulse.png    — two clusters at different times in the same event
  hycal_fig7_dt_landscape.png   — dt vs. distance population from collect_neighbor_timing

Run:
  cd docs/technical_notes/hycal_clustering
  python scripts/plot_hycal_clustering.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
from matplotlib.patches import Rectangle
from matplotlib.lines import Line2D


# ---------------------------------------------------------------------------
# Bring up prad2py.det and HyCalSystem
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
FIGS = HERE.parent / 'plots'
FIGS.mkdir(exist_ok=True)
REPO = HERE.parents[3]                      # docs/.../scripts → repo root
DB_MAP = REPO / 'database' / 'hycal_map.json'

# Allow running from a built tree (build/python/) or after install
# (PYTHONPATH already set up by the user).
for cand in (REPO / 'build' / 'python', REPO / 'install' / 'python'):
    if cand.is_dir():
        sys.path.insert(0, str(cand))

try:
    from prad2py import det
except ImportError as e:
    sys.exit(
        f"[ERROR] cannot import prad2py: {e}\n"
        "        Build the python bindings (cmake -DBUILD_PYTHON=ON) or set "
        "PYTHONPATH to a directory containing prad2py.*.so."
    )

hycal = det.HyCalSystem()
if hycal.init(str(DB_MAP)) <= 0:
    sys.exit(f"[ERROR] HyCalSystem.init({DB_MAP}) failed.")

ALL_MODS = [hycal.module(i) for i in range(hycal.module_count())]


# ---------------------------------------------------------------------------
# Geometry helpers — used purely to pick modules for the synthetic input.
# These do not implement any clustering logic; the algorithm runs in C++.
# ---------------------------------------------------------------------------
def find_mod(x, y, kind=None):
    """Return the HyCal module whose centre is closest to (x, y).
    `kind`: 'PbWO4' / 'PbGlass' / None (no filter)."""
    pool = ALL_MODS
    if kind == 'PbWO4':
        pool = [m for m in ALL_MODS if m.is_pwo4()]
    elif kind == 'PbGlass':
        pool = [m for m in ALL_MODS if m.is_glass()]
    return min(pool, key=lambda m: (m.x - x) ** 2 + (m.y - y) ** 2)


def module_box(center, radius):
    """Modules whose grid offset (in `center`'s module units) lies in
    [-radius, +radius] on both axes AND share the seed's sector — keeps
    synthetic showers inside one crystal type for a clean illustration."""
    sx, sy = center.size_x, center.size_y
    out = []
    for m in ALL_MODS:
        if m.sector != center.sector:
            continue
        dx = (m.x - center.x) / sx
        dy = (m.y - center.y) / sy
        if abs(dx) <= radius + 0.01 and abs(dy) <= radius + 0.01:
            out.append((m, dx, dy))
    return out


def shower_energy(modules, x0, y0, E_tot_MeV, sigma_mm):
    """Distribute `E_tot_MeV` over the listed modules using a 2-D
    Gaussian centred at (x0, y0) with width `sigma_mm`.  Returns a numpy
    array aligned with `modules`."""
    raw = np.array([
        np.exp(-((m.x - x0) ** 2 + (m.y - y0) ** 2) / (2 * sigma_mm ** 2))
        for m in modules
    ])
    return E_tot_MeV * raw / raw.sum()


def energy_weighted_centroid(positions, energies):
    """Naive Σ(E·x)/ΣE — used in fig 2 as a baseline against the
    log-weighted scheme that lives inside HyCalCluster."""
    e = np.asarray(energies, dtype=float)
    if e.sum() <= 0:
        return None
    px = np.asarray([p[0] for p in positions])
    py = np.asarray([p[1] for p in positions])
    return float((px * e).sum() / e.sum()), float((py * e).sum() / e.sum())


# ---------------------------------------------------------------------------
# Clustering — wraps fdec::HyCalCluster behind a closer-to-the-figure call
# ---------------------------------------------------------------------------
DEFAULT_CFG = dict(
    min_module_energy  = 1.0,
    min_center_energy  = 10.0,
    min_cluster_energy = 50.0,
    log_weight_thres   = 3.6,
    seed_time_window   = -1.0,         # disabled by default
)


def make_cluster(**overrides):
    """Construct a fresh HyCalCluster + ClusterConfig with `overrides`
    merged onto the defaults.  Returns (clusterer, config)."""
    cfg = det.HyCalClusterConfig()
    knobs = {**DEFAULT_CFG, **overrides}
    for k, v in knobs.items():
        setattr(cfg, k, v)
    cl = det.HyCalCluster(hycal)
    cl.set_config(cfg)
    return cl, cfg


def cluster_input(hits, **cfg_overrides):
    """Push (module, energy_MeV, time_ns) tuples into a fresh HyCalCluster
    and run form_clusters().  Returns (clusterer, [ClusterHit], [ModuleCluster])."""
    cl, _ = make_cluster(**cfg_overrides)
    cl.clear()
    for mod, E, t in hits:
        if E <= 0:
            continue
        cl.add_hit(mod.index, float(E), float(t))
    cl.form_clusters()
    return cl, list(cl.reconstruct_hits()), list(cl.get_clusters())


# ---------------------------------------------------------------------------
# Plotting primitives
# ---------------------------------------------------------------------------
COL_GLASS = '#fff3bf'
COL_PWO4  = '#dbeafe'
COL_EDGE  = '#888'
COL_SEED1 = '#d62728'
COL_SEED2 = '#1f77b4'
COL_BASE  = '#2ca02c'
COL_TRUTH = '#000000'


def draw_module(ax, m, facecolor=None, edgecolor=None, lw=0.4, alpha=1.0,
                hatch=None):
    fc = facecolor or (COL_PWO4 if m.is_pwo4() else COL_GLASS)
    ec = edgecolor or COL_EDGE
    ax.add_patch(Rectangle(
        (m.x - m.size_x / 2, m.y - m.size_y / 2),
        m.size_x, m.size_y,
        facecolor=fc, edgecolor=ec, lw=lw, alpha=alpha, hatch=hatch))


def shade_modules_by_energy(ax, modules, energies, threshold=1.0,
                            cmap='YlOrRd', annotate=True, fmt="{:.0f}"):
    """Colour each module by deposited energy on a log scale; annotate
    in-cluster modules with the energy in MeV."""
    pos_E = [E for E in energies if E >= threshold]
    if not pos_E:
        return
    emax = max(pos_E)
    norm_lo, norm_hi = np.log10(threshold), np.log10(emax)
    cm = plt.get_cmap(cmap)
    for m, E in zip(modules, energies):
        if E < threshold:
            draw_module(ax, m, facecolor='#f8f9fa')
            continue
        if norm_hi > norm_lo:
            t = (np.log10(E) - norm_lo) / (norm_hi - norm_lo)
        else:
            t = 1.0
        t = max(0.05, min(0.95, t))
        draw_module(ax, m, facecolor=cm(t), edgecolor='#666', lw=0.6)
        if annotate:
            ax.text(m.x, m.y, fmt.format(E), ha='center', va='center',
                    fontsize=8, color='#222')


# ===========================================================================
# Figure 1 — HyCal layout overview
# ===========================================================================
def fig1_layout():
    fig, ax = plt.subplots(figsize=(7.5, 7.5))
    for m in ALL_MODS:
        draw_module(ax, m)

    sector_pos = {'Top': (0, 410), 'Bottom': (0, -410),
                  'Left': (-410, 0), 'Right': (410, 0), 'Center': (0, -100)}
    for name, (px, py) in sector_pos.items():
        ax.text(px, py, name, fontsize=11, ha='center', va='center',
                color='#333', fontweight='bold',
                bbox=dict(facecolor='white', alpha=0.7, edgecolor='none', pad=2))

    ax.legend(
        [Rectangle((0, 0), 1, 1, facecolor=COL_GLASS, edgecolor=COL_EDGE),
         Rectangle((0, 0), 1, 1, facecolor=COL_PWO4,  edgecolor=COL_EDGE)],
        ['PbGlass (576 modules, 38.15 mm)',
         'PbWO₄ (1152 modules, 20.77 mm)'],
        loc='lower right', fontsize=9, framealpha=0.95,
    )
    ax.set_xlim(-600, 600); ax.set_ylim(-600, 600)
    ax.set_aspect('equal')
    ax.set_xlabel('x (mm, lab)'); ax.set_ylabel('y (mm, lab)')
    ax.set_title('HyCal module layout — 5 sectors, two crystal types')
    fig.tight_layout()
    fig.savefig(FIGS / 'hycal_fig1_layout.png', dpi=130)
    plt.close(fig)


# ===========================================================================
# Figure 2 — single cluster: log-weighted vs energy-weighted centroid
# ===========================================================================
def fig2_single_cluster():
    center  = find_mod(60, 100, kind='PbWO4')
    box     = module_box(center, radius=2)            # 5×5 region
    modules = [m for m, _, _ in box]
    x0, y0  = center.x + 5.0, center.y - 4.0
    E_TOT, SIG = 1100.0, 15.0
    energies = shower_energy(modules, x0, y0, E_TOT, SIG)

    THR = 1.0
    hits = [(m, float(E), 0.0) for m, E in zip(modules, energies)]
    _, recon, clusters = cluster_input(hits)
    if not recon:
        sys.exit("fig2: no cluster — check thresholds.")
    rh = recon[0]
    cl = clusters[0]
    seed = hycal.module(cl.center.index)

    # Energy-weighted baseline over the 3×3 around the seed (matches what
    # the log-weighted reconstructor would see, modulo the weighting).
    pos_3x3, e_3x3 = [], []
    for h in cl.hits:
        m = hycal.module(h.index)
        if abs(m.x - seed.x) < seed.size_x * 1.01 and \
           abs(m.y - seed.y) < seed.size_y * 1.01:
            pos_3x3.append((m.x, m.y))
            e_3x3.append(h.energy)
    xe, ye = energy_weighted_centroid(pos_3x3, e_3x3)

    fig, ax = plt.subplots(figsize=(7.5, 7.5))
    shade_modules_by_energy(ax, modules, energies, threshold=THR)

    ax.add_patch(Rectangle(
        (seed.x - seed.size_x * 1.5, seed.y - seed.size_y * 1.5),
        seed.size_x * 3, seed.size_y * 3,
        facecolor='none', edgecolor=COL_SEED2, lw=2.0, ls='--'))

    ax.plot(seed.x, seed.y, '*', color=COL_SEED1, ms=18, mec='white',
            mew=1.2, zorder=10)
    ax.plot(rh.x, rh.y, 'o', color=COL_SEED2, ms=10, mec='white', mew=1.5,
            zorder=11)
    ax.plot(xe,   ye,   'D', color=COL_BASE,  ms=8,  mec='white', mew=1.0,
            zorder=11)
    ax.plot(x0,   y0,   'x', color=COL_TRUTH, ms=12, mew=2.0, zorder=12)

    xlims = (center.x - seed.size_x * 3, center.x + seed.size_x * 3)
    ylims = (center.y - seed.size_y * 3, center.y + seed.size_y * 3)
    ax.set_xlim(xlims); ax.set_ylim(ylims)
    ax.set_aspect('equal')

    ax.legend(handles=[
        Line2D([], [], marker='*', color='w', markerfacecolor=COL_SEED1,
               ms=15, label='seed (local maximum)'),
        Line2D([], [], marker='o', color='w', markerfacecolor=COL_SEED2,
               ms=10, label='log-weighted centroid (T = 3.6)'),
        Line2D([], [], marker='D', color='w', markerfacecolor=COL_BASE,
               ms=8,  label='energy-weighted centroid (baseline)'),
        Line2D([], [], marker='x', color=COL_TRUTH, ls='', ms=10,
               label=f'true shower (E = {E_TOT:.0f} MeV)'),
        Line2D([], [], color=COL_SEED2, ls='--', lw=2.0,
               label='3×3 position window'),
    ], loc='upper left', fontsize=8, framealpha=0.95)

    ax.set_xlabel('x (mm)'); ax.set_ylabel('y (mm)')
    ax.set_title("Single cluster — module energies (MeV)\n"
                 f"E_total = {rh.energy:.0f} MeV, σ_shower = {SIG} mm")
    fig.tight_layout()
    fig.savefig(FIGS / 'hycal_fig2_single_cluster.png', dpi=130)
    plt.close(fig)

    err_lw = float(np.hypot(rh.x - x0, rh.y - y0))
    err_ew = float(np.hypot(xe - x0, ye - y0))
    print("Single cluster (HyCalCluster output):")
    print(f"  true position    : ({x0:.2f}, {y0:.2f}) mm")
    print(f"  seed module      : {seed.name} at ({seed.x:.2f}, {seed.y:.2f})")
    print(f"  energy-weighted  : ({xe:.2f}, {ye:.2f}) mm  |err| = {err_ew:.2f} mm")
    print(f"  log-weighted     : ({rh.x:.2f}, {rh.y:.2f}) mm  |err| = {err_lw:.2f} mm")
    print(f"  C++ recon: nblocks={rh.nblocks}, npos={rh.npos}, energy={rh.energy:.1f}")
    return modules, energies, seed, x0, y0, pos_3x3, e_3x3


# ===========================================================================
# Figure 3 — two overlapping showers, profile-based split
# ===========================================================================
def fig3_split():
    center  = find_mod(0, 0, kind='PbWO4')
    box     = module_box(center, radius=3)            # 7×7 region
    modules = [m for m, _, _ in box]

    x1, y1, E1 = center.x - 25.0, center.y + 5.0, 1500.0
    x2, y2, E2 = center.x + 25.0, center.y - 8.0,  900.0
    SIG = 14.0
    e1 = shower_energy(modules, x1, y1, E1, SIG)
    e2 = shower_energy(modules, x2, y2, E2, SIG)
    total = e1 + e2

    THR = 1.0
    hits = [(m, float(E), 0.0) for m, E in zip(modules, total)]
    _, recon, clusters = cluster_input(hits)
    if len(clusters) < 2:
        sys.exit(f"fig3: expected ≥2 clusters, got {len(clusters)}.")

    # Order clusters by descending centre energy so the colour mapping is
    # stable across runs (largest = ★1).
    clusters = sorted(clusters,
                      key=lambda c: c.center.energy, reverse=True)
    seeds = [hycal.module(c.center.index) for c in clusters[:2]]

    # Per-module split fractions: HyCalCluster::split_hits writes each
    # contributing module into both clusters with the share scaled by the
    # profile-driven fraction.  Recover that fraction from the per-cluster
    # hit energies vs. the input total.
    in_E1 = {h.index: h.energy for h in clusters[0].hits}
    in_E2 = {h.index: h.energy for h in clusters[1].hits}

    fig, (axA, axB) = plt.subplots(1, 2, figsize=(14, 6.5))
    shade_modules_by_energy(axA, modules, total, threshold=THR,
                            fmt="{:.0f}")

    for s, lbl, col in [(seeds[0], '★1', COL_SEED1),
                        (seeds[1], '★2', COL_SEED2)]:
        axA.plot(s.x, s.y, '*', color=col, ms=22, mec='white', mew=1.3,
                 zorder=10)
        axA.text(s.x, s.y + s.size_y * 0.65, lbl, ha='center', va='center',
                 fontsize=11, color=col, fontweight='bold')

    xlims = (center.x - center.size_x * 4, center.x + center.size_x * 4)
    ylims = (center.y - center.size_y * 4, center.y + center.size_y * 4)
    axA.set_xlim(xlims); axA.set_ylim(ylims)
    axA.set_aspect('equal')
    axA.set_xlabel('x (mm)'); axA.set_ylabel('y (mm)')
    axA.set_title(f"Input — total energy in MeV\n"
                  f"two local maxima found "
                  f"({clusters[0].center.energy:.0f}, "
                  f"{clusters[1].center.energy:.0f} MeV)")

    for m, E in zip(modules, total):
        if E < THR:
            draw_module(axB, m, facecolor='#f8f9fa')
            continue
        E1m = in_E1.get(m.index, 0.0)
        E2m = in_E2.get(m.index, 0.0)
        denom = E1m + E2m
        if denom <= 0:
            draw_module(axB, m, facecolor='#f8f9fa')
            continue
        f1 = E1m / denom
        f2 = E2m / denom
        col, frac = (COL_SEED1, f1) if f1 >= f2 else (COL_SEED2, f2)
        alpha = max(0.10, min(0.95, 0.25 + 0.7 * (frac - 0.5) / 0.5))
        draw_module(axB, m, facecolor=col, alpha=alpha,
                    edgecolor='#666', lw=0.5)
        if 0.10 < f1 < 0.90:
            axB.text(m.x, m.y, f"{f1*100:.0f}/{f2*100:.0f}",
                     ha='center', va='center', fontsize=7, color='#000',
                     bbox=dict(facecolor='white', alpha=0.6,
                               edgecolor='none', pad=0.5))

    for s, col in [(seeds[0], COL_SEED1), (seeds[1], COL_SEED2)]:
        axB.plot(s.x, s.y, '*', color=col, ms=22, mec='white', mew=1.3,
                 zorder=10)
    axB.set_xlim(xlims); axB.set_ylim(ylims)
    axB.set_aspect('equal')
    axB.set_xlabel('x (mm)'); axB.set_ylabel('y (mm)')
    axB.set_title(
        f"Profile split — fractions in % shown on boundary modules\n"
        f"E(★1) = {clusters[0].energy:.0f} MeV   "
        f"E(★2) = {clusters[1].energy:.0f} MeV   "
        f"(input {E1:.0f}, {E2:.0f})")

    fig.tight_layout()
    fig.savefig(FIGS / 'hycal_fig3_split.png', dpi=130)
    plt.close(fig)

    print("\nTwo-shower split (HyCalCluster output):")
    print(f"  injected E1 = {E1:.0f}, recovered = {clusters[0].energy:.0f} MeV "
          f"({clusters[0].energy/E1*100-100:+.1f}%)")
    print(f"  injected E2 = {E2:.0f}, recovered = {clusters[1].energy:.0f} MeV "
          f"({clusters[1].energy/E2*100-100:+.1f}%)")


# ===========================================================================
# Figure 4 — log_weight_thres scan + shower-depth curves
# ===========================================================================
def fig4_params(modules, energies, x0, y0, pos_3x3, e_3x3):
    """Sweep log_weight_thres through the C++ clusterer and plot |error|.
    Reuses the synthetic shower from fig 2."""
    THR = 1.0
    hits = [(m, float(E), 0.0) for m, E in zip(modules, energies)]

    t_values = np.linspace(2.0, 6.0, 9)
    errs = []
    for T in t_values:
        _, recon, _ = cluster_input(hits, log_weight_thres=float(T))
        rh = recon[0]
        errs.append(float(np.hypot(rh.x - x0, rh.y - y0)))

    xe, ye = energy_weighted_centroid(pos_3x3, e_3x3)
    err_e = float(np.hypot(xe - x0, ye - y0))

    fig, (axA, axB) = plt.subplots(1, 2, figsize=(13, 4.6))

    axA.plot(t_values, errs, 'o-', color=COL_SEED2, ms=6,
             label='log-weighted (HyCalCluster)')
    axA.axvline(3.6, color=COL_SEED1, lw=0.8, ls='--', label='default T = 3.6')
    axA.axhline(err_e, color=COL_BASE, lw=0.8, ls=':',
                label=f'energy-weighted ({err_e:.2f} mm)')
    axA.set_xlabel('log_weight_thres  T')
    axA.set_ylabel('|reconstructed − true|  (mm)')
    axA.set_title("Position error vs log-weight threshold\n"
                  "(single 1.1 GeV PbWO₄ shower from fig 2)")
    axA.grid(alpha=0.3)
    axA.legend(fontsize=9)

    E_axis = np.logspace(np.log10(20), np.log10(3000), 200)
    pwo4_id  = hycal.module_by_name('W735').id
    glass_id = hycal.module_by_name('G1').id
    depth_pwo4  = [det.shower_depth(pwo4_id,  float(e)) for e in E_axis]
    depth_glass = [det.shower_depth(glass_id, float(e)) for e in E_axis]
    axB.plot(E_axis, depth_pwo4,  color=COL_SEED2, lw=1.6,
             label='PbWO₄  (X₀ = 8.6 mm,  Eᶜ = 1.1 MeV)')
    axB.plot(E_axis, depth_glass, color=COL_SEED1, lw=1.6,
             label='PbGlass (X₀ = 26.7 mm, Eᶜ = 2.84 MeV)')
    axB.set_xscale('log')
    axB.set_xlabel('cluster energy  E (MeV)')
    axB.set_ylabel('shower-max depth  t (mm)')
    axB.set_title("Shower depth  t = X₀ · (ln(E/Eᶜ) − 0.5)\n"
                  "added to z_HyCal-face for cl_z")
    axB.legend(loc='lower right', fontsize=8.5)
    axB.grid(alpha=0.3, which='both')

    fig.tight_layout()
    fig.savefig(FIGS / 'hycal_fig4_params.png', dpi=130)
    plt.close(fig)


# ===========================================================================
# Figure 5 — seed-anchored timing coincidence
# ===========================================================================
def fig5_timing_window():
    """Build a seed module + 8 neighbours; on each neighbour place a
    real shower pulse near the seed time AND a background pulse outside
    the window.  Run HyCalCluster with seed_time_window enabled; show
    which pulses get claimed and which stay alive."""
    rng = np.random.default_rng(2026_05_06)
    seed_mod = find_mod(60, 100, kind='PbWO4')
    box      = module_box(seed_mod, radius=1)              # 3×3 region
    modules  = [m for m, _, _ in box]

    T_SEED, W = 120.0, 8.0          # ns
    T_BG_LO, T_BG_HI = 160.0, 220.0
    SHOWER_SIG_T = 1.5

    pulses = []                     # (mod, energy, time, label)
    pulses.append((seed_mod, 600.0, T_SEED, 'seed'))
    seed_E = shower_energy(modules, seed_mod.x, seed_mod.y, 1100.0, 15.0)
    for m, E in zip(modules, seed_E):
        if m.index == seed_mod.index:
            continue
        if E >= 1.0:
            t = T_SEED + rng.normal(0, SHOWER_SIG_T)
            pulses.append((m, float(E), float(t), 'real'))
        # background pulse on most neighbours.  Stay below
        # min_center_energy so the only cluster in the figure is the
        # seed-driven one — the demo is about coincidence selection,
        # not about secondary cluster formation (that lives in fig 6).
        if rng.random() < 0.7:
            t  = float(rng.uniform(T_BG_LO, T_BG_HI))
            Eb = float(rng.uniform(2.0, 8.0))
            pulses.append((m, Eb, t, 'bg'))

    cl, recon, clusters = cluster_input(
        [(p[0], p[1], p[2]) for p in pulses],
        seed_time_window=W,
    )
    # Map (module_index, time) → which cluster claimed it.  HyCalCluster
    # stores cluster.hits with energy possibly fractional after a split,
    # but the time is preserved verbatim from the input pulse.
    claimed = {}
    for ci, mc in enumerate(clusters):
        for h in mc.hits:
            claimed[(h.index, round(float(h.time), 3))] = ci

    fig, (axT, axS) = plt.subplots(
        1, 2, figsize=(14, 6.0),
        gridspec_kw=dict(width_ratios=[1.4, 1.0]))

    # --- Time axis (left) ---
    mod_order = sorted(modules,
                       key=lambda m: (m.row, m.column))
    row_for = {m.index: i for i, m in enumerate(mod_order)}
    for m in mod_order:
        i = row_for[m.index]
        axT.axhspan(i - 0.45, i + 0.45,
                    color='#f8f9fa' if i % 2 else '#ffffff', zorder=0)
    axT.axvspan(T_SEED - W, T_SEED + W, color=COL_SEED2, alpha=0.10,
                zorder=1, label=f'seed window ±{W:.0f} ns')
    axT.axvline(T_SEED, color=COL_SEED2, lw=1.0, ls='--', zorder=2)

    for mod, E, t, kind in pulses:
        i = row_for[mod.index]
        is_seed = (mod.index == seed_mod.index and abs(t - T_SEED) < 0.01)
        was_claimed = (mod.index, round(t, 3)) in claimed
        if is_seed:
            color, marker, ms = COL_SEED1, '*', 16
        elif was_claimed:
            color, marker, ms = COL_SEED1, 'o', 8
        else:
            color, marker, ms = '#888', 'x', 7
        axT.plot(t, i, marker, color=color, ms=ms,
                 mec='white' if marker != 'x' else color, mew=1.0)
    axT.set_yticks(range(len(mod_order)))
    axT.set_yticklabels([m.name + (" (seed)" if m.index == seed_mod.index else "")
                          for m in mod_order], fontsize=8)
    axT.set_xlabel('peak time (ns)')
    axT.set_xlim(80, 240)
    axT.set_ylim(-1, len(mod_order))
    axT.invert_yaxis()
    axT.set_title("Per-module pulse train\n"
                  "★ seed   ● claimed by seed (in window, largest E)   ✕ left in pool")
    axT.legend(loc='upper right', fontsize=9)

    # --- Spatial layout (right) ---
    for m in modules:
        is_in_cluster = any(h.index == m.index for h in clusters[0].hits)
        fc = '#fde0dc' if is_in_cluster else '#f8f9fa'
        draw_module(axS, m, facecolor=fc, edgecolor='#666', lw=0.6)
    for h in clusters[0].hits:
        m = hycal.module(h.index)
        axS.text(m.x, m.y, f"{h.energy:.0f}", ha='center', va='center',
                 fontsize=9, color='#222')
    axS.plot(seed_mod.x, seed_mod.y, '*', color=COL_SEED1, ms=20,
             mec='white', mew=1.2, zorder=10)
    axS.set_xlim(seed_mod.x - seed_mod.size_x * 2,
                 seed_mod.x + seed_mod.size_x * 2)
    axS.set_ylim(seed_mod.y - seed_mod.size_y * 2,
                 seed_mod.y + seed_mod.size_y * 2)
    axS.set_aspect('equal')
    axS.set_xlabel('x (mm)'); axS.set_ylabel('y (mm)')
    axS.set_title(f"Resulting cluster: nblocks = {recon[0].nblocks}, "
                  f"E = {recon[0].energy:.0f} MeV, t = {recon[0].time:.1f} ns")

    fig.tight_layout()
    fig.savefig(FIGS / 'hycal_fig5_timing_window.png', dpi=130)
    plt.close(fig)
    print(f"\nfig5 timing: {len(pulses)} input pulses → "
          f"{len(clusters)} cluster(s), seed_time={recon[0].time:.1f} ns, "
          f"window ±{W} ns")


# ===========================================================================
# Figure 6 — multi-pulse, two clusters at different timings
# ===========================================================================
def fig6_multi_pulse():
    """Two showers landing in overlapping module sets at different times.
    Demonstrates that pulses outside the first seed's window survive to
    seed a second cluster at a different time."""
    rng = np.random.default_rng(20260507)
    region_center = find_mod(60, 100, kind='PbWO4')
    box     = module_box(region_center, radius=3)
    modules = [m for m, _, _ in box]

    # Two showers, partially overlapping in space, distinct in time.
    x1, y1, T1, E1 = region_center.x - 18.0, region_center.y + 6.0, 110.0, 1200.0
    x2, y2, T2, E2 = region_center.x + 22.0, region_center.y - 12.0, 200.0, 700.0
    W = 12.0
    SIG_E, SIG_T = 14.0, 1.5
    e1 = shower_energy(modules, x1, y1, E1, SIG_E)
    e2 = shower_energy(modules, x2, y2, E2, SIG_E)

    pulses = []
    for m, E in zip(modules, e1):
        if E >= 1.0:
            pulses.append((m, float(E), float(T1 + rng.normal(0, SIG_T))))
    for m, E in zip(modules, e2):
        if E >= 1.0:
            pulses.append((m, float(E), float(T2 + rng.normal(0, SIG_T))))

    _, recon, clusters = cluster_input(pulses, seed_time_window=W)
    clusters = sorted(clusters, key=lambda c: c.center.energy, reverse=True)
    recon    = sorted(recon,    key=lambda r: r.energy,        reverse=True)

    # Match each cluster to the nearest input shower (in energy + time)
    # purely for plotting colours.
    seed_mods = [hycal.module(c.center.index) for c in clusters[:2]]
    pulses_by_cluster = [{}, {}]
    for ci, mc in enumerate(clusters[:2]):
        for h in mc.hits:
            pulses_by_cluster[ci][(h.index, round(float(h.time), 3))] = h.energy

    fig, (axS, axT) = plt.subplots(1, 2, figsize=(14, 6.5),
                                   gridspec_kw=dict(width_ratios=[1.0, 1.2]))

    # --- Spatial: both clusters in the same panel, colour by which cluster
    #     dominates each module ---
    in_E1 = {h.index: h.energy for h in clusters[0].hits}
    in_E2 = {h.index: h.energy for h in clusters[1].hits}
    for m in modules:
        E1m = in_E1.get(m.index, 0.0)
        E2m = in_E2.get(m.index, 0.0)
        if E1m + E2m <= 0:
            draw_module(axS, m, facecolor='#f8f9fa')
            continue
        if E1m >= E2m:
            col, frac = COL_SEED1, E1m / (E1m + E2m)
        else:
            col, frac = COL_SEED2, E2m / (E1m + E2m)
        alpha = max(0.15, min(0.95, 0.25 + 0.7 * (frac - 0.5) / 0.5))
        draw_module(axS, m, facecolor=col, alpha=alpha,
                    edgecolor='#666', lw=0.5)

    for s, lbl, col in [(seed_mods[0], '★1', COL_SEED1),
                        (seed_mods[1], '★2', COL_SEED2)]:
        axS.plot(s.x, s.y, '*', color=col, ms=22, mec='white', mew=1.3,
                 zorder=10)
        axS.text(s.x, s.y + s.size_y * 0.65, lbl, ha='center', va='center',
                 fontsize=11, color=col, fontweight='bold')

    xlims = (region_center.x - region_center.size_x * 5,
             region_center.x + region_center.size_x * 5)
    ylims = (region_center.y - region_center.size_y * 5,
             region_center.y + region_center.size_y * 5)
    axS.set_xlim(xlims); axS.set_ylim(ylims)
    axS.set_aspect('equal')
    axS.set_xlabel('x (mm)'); axS.set_ylabel('y (mm)')
    axS.set_title(f"Two clusters in one event\n"
                  f"★1 t = {recon[0].time:.0f} ns, "
                  f"E = {recon[0].energy:.0f} MeV    "
                  f"★2 t = {recon[1].time:.0f} ns, "
                  f"E = {recon[1].energy:.0f} MeV")

    # --- Time axis: every pulse, coloured by claiming cluster ---
    mod_order = sorted({mod.index for mod, _, _ in pulses})
    mod_order = sorted([hycal.module(i) for i in mod_order],
                       key=lambda m: (m.row, m.column))
    row_for = {m.index: i for i, m in enumerate(mod_order)}

    for i in range(len(mod_order)):
        axT.axhspan(i - 0.45, i + 0.45,
                    color='#f8f9fa' if i % 2 else '#ffffff', zorder=0)
    for s_t, c in [(recon[0].time, COL_SEED1), (recon[1].time, COL_SEED2)]:
        axT.axvspan(s_t - W, s_t + W, color=c, alpha=0.10, zorder=1)
        axT.axvline(s_t, color=c, lw=1.0, ls='--', zorder=2)

    for mod, E, t in pulses:
        i = row_for[mod.index]
        key = (mod.index, round(t, 3))
        if key in pulses_by_cluster[0]:
            color, marker = COL_SEED1, 'o'
        elif key in pulses_by_cluster[1]:
            color, marker = COL_SEED2, 'o'
        else:
            color, marker = '#888', 'x'
        axT.plot(t, i, marker, color=color,
                 ms=max(4, min(14, 4 + 0.04 * E)),
                 mec='white' if marker == 'o' else color, mew=0.8)

    for ci, (s, c) in enumerate([(seed_mods[0], COL_SEED1),
                                  (seed_mods[1], COL_SEED2)]):
        i = row_for[s.index]
        axT.plot(recon[ci].time, i, '*', color=c, ms=20,
                 mec='white', mew=1.0, zorder=12)

    axT.set_yticks(range(len(mod_order)))
    axT.set_yticklabels([m.name for m in mod_order], fontsize=7)
    axT.set_xlabel('peak time (ns)')
    axT.set_ylim(-1, len(mod_order))
    axT.invert_yaxis()
    axT.set_xlim(60, 250)
    axT.set_title("Per-module pulse train coloured by claiming cluster")

    fig.tight_layout()
    fig.savefig(FIGS / 'hycal_fig6_multi_pulse.png', dpi=130)
    plt.close(fig)
    print(f"\nfig6 multi-pulse: {len(pulses)} input pulses → "
          f"{len(clusters)} cluster(s) at t = "
          f"{', '.join(f'{r.time:.0f}' for r in recon)} ns")


# ===========================================================================
# Figure 7 — dt vs distance landscape from collect_neighbor_timing
# ===========================================================================
def fig7_dt_landscape():
    """Run a synthetic ensemble of events through HyCalCluster.
    collect_neighbor_timing and 2-D histogram (dt, dist_q) — the kind of
    plot the study tool produces from real data, used to pick
    seed_time_window."""
    rng = np.random.default_rng(20260508)

    SIGMA_T_SHOWER = 2.0   # ns rms — physical timing jitter
    PILEUP_MEAN_PER_EVENT = 8
    N_EVENTS = 1500

    cl, _ = make_cluster(seed_time_window=-1.0)
    all_dt = []
    all_dist = []

    centre_pool = [m for m in ALL_MODS if m.is_pwo4()
                   and abs(m.x) < 200 and abs(m.y) < 200]

    for _ in range(N_EVENTS):
        seed_mod = centre_pool[rng.integers(len(centre_pool))]
        box      = module_box(seed_mod, radius=4)
        modules  = [m for m, _, _ in box]
        T_SEED   = float(rng.uniform(80, 140))

        E_seed = float(rng.uniform(400, 1500))
        e_real = shower_energy(modules, seed_mod.x, seed_mod.y, E_seed, 14.0)

        cl.clear()
        for m, E in zip(modules, e_real):
            if E >= 1.0:
                t = T_SEED + rng.normal(0, SIGMA_T_SHOWER)
                cl.add_hit(m.index, float(E), float(t))

        n_pile = rng.poisson(PILEUP_MEAN_PER_EVENT)
        for _ in range(n_pile):
            m = modules[rng.integers(len(modules))]
            E = float(rng.uniform(2.0, 50.0))
            t = float(rng.uniform(0, 250))
            cl.add_hit(m.index, E, t)

        rows = cl.collect_neighbor_timing(5.0)
        for r in rows:
            all_dt.append(r.dt)
            all_dist.append(np.hypot(r.dx_q, r.dy_q))

    all_dt   = np.asarray(all_dt)
    all_dist = np.asarray(all_dist)

    fig, (axA, axB) = plt.subplots(1, 2, figsize=(13, 5.0),
                                   gridspec_kw=dict(width_ratios=[1.4, 1.0]))

    h, xedges, yedges, im = axA.hist2d(
        all_dt, all_dist,
        bins=[np.linspace(-100, 100, 101), np.linspace(0, 5, 41)],
        cmap='viridis',
        norm=LogNorm(vmin=1),
    )
    axA.set_xlabel('dt = t_neighbour − t_seed  (ns)')
    axA.set_ylabel('|distance from seed|  (module units)')
    axA.set_title("Density of (dt, dist) pairs over a synthetic ensemble\n"
                  f"({N_EVENTS} events, mean pile-up = "
                  f"{PILEUP_MEAN_PER_EVENT}/event)")
    fig.colorbar(im, ax=axA, label='counts (log)')

    proj = np.histogram(all_dt, bins=np.linspace(-100, 100, 201))
    centres = 0.5 * (proj[1][:-1] + proj[1][1:])
    axB.step(centres, proj[0], where='mid', color=COL_SEED2, lw=1.5)
    axB.fill_between(centres, proj[0], step='mid', color=COL_SEED2,
                     alpha=0.20)
    axB.axvspan(-3 * SIGMA_T_SHOWER, 3 * SIGMA_T_SHOWER,
                color=COL_SEED1, alpha=0.15,
                label=f'±3·σ_shower = ±{3*SIGMA_T_SHOWER:.0f} ns')
    axB.set_yscale('log')
    axB.set_xlabel('dt  (ns)')
    axB.set_ylabel('entries / bin')
    axB.set_title("dt projection — coincidence peak rides on flat pile-up")
    axB.legend(loc='upper right', fontsize=9)
    axB.grid(alpha=0.3, which='both')

    fig.tight_layout()
    fig.savefig(FIGS / 'hycal_fig7_dt_landscape.png', dpi=130)
    plt.close(fig)
    print(f"\nfig7 dt landscape: {N_EVENTS} events, {len(all_dt)} (seed, "
          f"neighbour) pairs from collect_neighbor_timing")


# ---------------------------------------------------------------------------
# Run everything
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    fig1_layout()
    modules, energies, seed, x0, y0, pos_3x3, e_3x3 = fig2_single_cluster()
    fig3_split()
    fig4_params(modules, energies, x0, y0, pos_3x3, e_3x3)
    fig5_timing_window()
    fig6_multi_pulse()
    fig7_dt_landscape()
    print(f"\nWrote PNGs to {FIGS}")
