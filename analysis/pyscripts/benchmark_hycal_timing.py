#!/usr/bin/env python3
"""benchmark_hycal_timing.py — legacy vs. timing-coincident HyCal clustering.

Runs WaveAnalyzer once per HyCal channel and feeds the same peak set
into two HyCalCluster instances:

  legacy : seed_time_window ≤ 0, one ModuleHit per module — the
           largest-integral peak within [pre_lo, pre_hi] ns.
  gated  : seed_time_window = W, every in-window peak pushed as a
           separate ModuleHit; HyCalCluster applies the seed-anchored
           coincidence cut during BFS.

Headline selection: events with exactly one reconstructed cluster of
energy > --signal-min MeV (default 3000).  Per path the script counts
matches, histograms the cluster energy, and fits a Gaussian.

With --calibrate the script also fits per-seed-module gain corrections
on an inner-ring high-statistics sample (radius < --inner-radius mm,
seed energy ≥ --min-seed-energy MeV, ≥ --min-per-module events) and
re-fits the recalibrated spectrum with both Gaussian and Crystal-Ball
shapes — the headline observable is then σ_E/E, comparable across the
two paths under matched per-module calibration.  See the technical
note in docs/technical_notes/hycal_clustering for the methodology.

Outputs:
  <out>.tsv               — summary + histogram bins
  <out>.png               — energy spectrum + Gaussian fits
  <out>_calibrated.png    — recalibrated spectrum + CB fit (--calibrate only)
"""

from __future__ import annotations

import argparse
import math
import sys
import time

import numpy as np
import matplotlib.pyplot as plt

import _common as C
from prad2py import dec, det  # noqa: E402

try:
    from scipy.optimize import curve_fit
    HAVE_SCIPY = True
except ImportError:
    HAVE_SCIPY = False


def gauss(x, A, mu, sigma):
    return A * np.exp(-0.5 * ((x - mu) / sigma) ** 2)


def crystal_ball(x, A, mu, sigma, alpha, n):
    """Crystal-Ball PDF (left-side power-law tail).  α > 0, n > 1."""
    z = (np.asarray(x, dtype=float) - mu) / sigma
    abs_a = abs(alpha)
    A_coef = (n / abs_a) ** n * np.exp(-0.5 * abs_a ** 2)
    B_coef = n / abs_a - abs_a
    gauss_part = np.exp(-0.5 * z * z)
    arg = B_coef - z
    arg = np.where(arg > 0, arg, 1e-30)
    tail_part = A_coef * arg ** (-n)
    return A * np.where(z > -alpha, gauss_part, tail_part)


def fit_peak_cb(values, e_lo, e_hi, bin_width, mu0_hint=None):
    """Crystal-Ball fit of values in [e_lo, e_hi].  Returns
    (mu, sigma, alpha, n, A, n_in_fit, edges, counts).  μ seeded from
    `mu0_hint` (else histogram argmax); a wide ±400 MeV window
    includes the low-side tail."""
    edges = np.arange(e_lo, e_hi + bin_width, bin_width)
    counts, _ = np.histogram(values, bins=edges)
    centres = 0.5 * (edges[:-1] + edges[1:])
    nan = float('nan')
    if not HAVE_SCIPY or counts.sum() == 0:
        return nan, nan, nan, nan, nan, 0, edges, counts

    imax = int(np.argmax(counts))
    mu0  = float(mu0_hint) if mu0_hint is not None else float(centres[imax])
    win  = max(400.0, 12.0 * bin_width)
    mask = (centres > mu0 - win) & (centres < mu0 + 0.5 * win)
    x = centres[mask]
    y = counts[mask].astype(float)
    if y.sum() < 50:
        return nan, nan, nan, nan, nan, int(y.sum()), edges, counts
    try:
        p0     = [float(y.max()), mu0, max(60.0, bin_width * 3), 1.5, 2.0]
        bounds = ([0,        e_lo - 200.0, 1.0,  0.1,  1.01],
                  [np.inf,   e_hi + 200.0, win,  20.0, 50.0])
        popt, _ = curve_fit(crystal_ball, x, y, p0=p0, bounds=bounds,
                            maxfev=10000)
        A, mu, sig, alpha, n = popt
        return (float(mu), abs(float(sig)), float(alpha), float(n),
                float(A), int(y.sum()), edges, counts)
    except Exception as e:
        sys.stderr.write(f"[WARN] crystal-ball fit failed: {e}\n")
        return nan, nan, nan, nan, nan, int(y.sum()), edges, counts


def per_module_peak(values, bin_width):
    """Per-module peak position for the gain refinement.  The narrow
    ±200 MeV window keeps the per-module radiative tail from pulling
    the fit away from the elastic position; CB lets us include a
    little of the tail without bias.  Tries in order: Crystal-Ball →
    narrow Gaussian → histogram argmax → median (each fall-through
    only on the previous step's failure)."""
    vals = np.asarray(values, dtype=float)
    if len(vals) == 0:
        return float('nan')
    med = float(np.median(vals))
    if len(vals) < 10 or not HAVE_SCIPY:
        return med

    e_lo, e_hi = med - 600.0, med + 600.0
    edges  = np.arange(e_lo, e_hi + bin_width, bin_width)
    counts, _ = np.histogram(vals, bins=edges)
    if counts.sum() < 10:
        return med
    centres = 0.5 * (edges[:-1] + edges[1:])
    mu0 = float(centres[int(np.argmax(counts))])

    win  = 200.0
    mask = (centres > mu0 - win) & (centres < mu0 + 0.5 * win)
    x, y = centres[mask], counts[mask].astype(float)
    if y.sum() < 10:
        return mu0

    try:
        popt, _ = curve_fit(
            crystal_ball, x, y,
            p0     = [float(y.max()), mu0, 50.0, 1.5, 2.0],
            bounds = ([0, e_lo, 5.0, 0.1, 1.01],
                      [np.inf, e_hi, win, 20.0, 50.0]),
            maxfev = 5000)
        return float(popt[1])
    except Exception:
        pass
    try:
        popt, _ = curve_fit(
            gauss, x, y,
            p0     = [float(y.max()), mu0, 50.0],
            bounds = ([0, e_lo, 5.0], [np.inf, e_hi, win]),
            maxfev = 2000)
        return float(popt[1])
    except Exception:
        pass
    return mu0


def fit_per_module_gains(events, *, target_peak, inner_r, min_seed_E,
                         min_per_mod, n_iter=2, exclude_rowcol=None,
                         method='median', bin_width=20.0):
    """Per-seed-module gain refinement on the inner-ring high-statistics
    sample.  See the technical note for the methodology.

    `events`         list of (seed_id, x, y, row, col, seed_E, cluster_E).
    `exclude_rowcol` (R_LO, R_HI, C_LO, C_HI): drop seeds whose row/col
                     falls inside the rectangle (used to peel the
                     leakage-prone ring around the beam hole).
    `method`         'median' (cheap, biased low by the radiative tail)
                     or 'cb-peak' (per_module_peak()).

    Returns (gains, raw, corrected, n_modules) where `raw` and
    `corrected` are aligned numpy arrays of cluster energies for the
    events whose seed module had ≥ min_per_mod entries."""
    inner_r2 = inner_r * inner_r
    excl = exclude_rowcol

    def keep(e):
        _sid, x, y, row, col, se, _ce = e
        if (x * x + y * y) >= inner_r2 or se < min_seed_E:
            return False
        if excl is not None and excl[0] <= row <= excl[1] and excl[2] <= col <= excl[3]:
            return False
        return True

    pool = [e for e in events if keep(e)]
    if not pool:
        return {}, np.array([]), np.array([]), 0

    seed_ids = sorted({e[0] for e in pool})
    gains = {sid: 1.0 for sid in seed_ids}

    for _ in range(n_iter):
        per_mod = {sid: [] for sid in seed_ids}
        for sid, *_, ce in pool:
            per_mod[sid].append(ce * gains[sid])
        for sid, vals in per_mod.items():
            if len(vals) < min_per_mod:
                continue
            ref = (per_module_peak(vals, bin_width) if method == 'cb-peak'
                   else float(np.median(vals)))
            if ref and ref > 0 and not math.isnan(ref):
                gains[sid] *= target_peak / ref

    counts = {sid: 0 for sid in seed_ids}
    for sid, *_ in pool:
        counts[sid] += 1
    converged = {sid: g for sid, g in gains.items()
                 if counts[sid] >= min_per_mod}
    raw  = np.array([e[6] for e in pool if e[0] in converged])
    corr = np.array([e[6] * converged[e[0]] for e in pool if e[0] in converged])
    return converged, raw, corr, len(converged)


def fit_peak(values, e_lo, e_hi, bin_width):
    """Gaussian fit around the histogram argmax in a ±150 MeV window.
    Returns (mu, sigma, A, n_in_fit, edges, counts); mu/sigma NaN if
    the fit failed."""
    edges  = np.arange(e_lo, e_hi + bin_width, bin_width)
    counts, _ = np.histogram(values, bins=edges)
    centres = 0.5 * (edges[:-1] + edges[1:])
    nan = float('nan')
    if not HAVE_SCIPY or counts.sum() == 0:
        return nan, nan, nan, 0, edges, counts

    mu0  = float(centres[int(np.argmax(counts))])
    win  = max(150.0, 4.0 * bin_width)
    mask = (centres > mu0 - win) & (centres < mu0 + win)
    x, y = centres[mask], counts[mask].astype(float)
    if y.sum() < 20:
        return nan, nan, nan, int(y.sum()), edges, counts
    try:
        popt, _ = curve_fit(
            gauss, x, y,
            p0     = [float(y.max()), mu0, max(bin_width, 60.0)],
            bounds = ([0, e_lo, 1.0], [np.inf, e_hi, win]))
        A, mu, sig = popt
        return float(mu), abs(float(sig)), float(A), int(y.sum()), edges, counts
    except Exception as e:
        sys.stderr.write(f"[WARN] gauss fit failed: {e}\n")
        return nan, nan, nan, int(y.sum()), edges, counts


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    C.add_common_args(ap)
    ap.add_argument("--window", type=float, default=8.0,
                    help="seed_time_window (ns) for the new method (default 8).")
    ap.add_argument("--pre-window", type=float, nargs=2, default=(130.0, 200.0),
                    metavar=("LO", "HI"),
                    help="Pre-window applied to peaks before clustering for "
                         "both paths (default 130 200 ns — runinfo for 24386).")
    ap.add_argument("--signal-min", type=float, default=3000.0,
                    help="Single-cluster energy cut in MeV (default 3000).")
    ap.add_argument("--bin-width", type=float, default=20.0,
                    help="Energy histogram bin width in MeV (default 20).")
    ap.add_argument("--e-range", type=float, nargs=2, default=(2500.0, 4000.0),
                    metavar=("LO", "HI"),
                    help="Energy histogram range in MeV (default 2500 4000).")
    ap.add_argument("--calibrate", action="store_true",
                    help="After the main loop, free per-seed-module gain "
                         "constants on the inner-ring high-statistics sample "
                         "(target peak 3488.5 MeV by default), then refit the "
                         "Gaussian to compare σ_E between methods under "
                         "matched per-module calibration.")
    ap.add_argument("--target-peak", type=float, default=3488.5,
                    help="Target μ for per-module gain refinement (default "
                         "3488.5 MeV).")
    ap.add_argument("--inner-radius", type=float, default=200.0,
                    help="Restrict per-module gain fits to seeds within this "
                         "radius (mm) from beam (default 200).")
    ap.add_argument("--min-seed-energy", type=float, default=200.0,
                    help="Skip events whose seed energy is below this when "
                         "computing per-module gains (default 200 MeV).")
    ap.add_argument("--min-per-module", type=int, default=30,
                    help="Minimum events per module before a gain is fit "
                         "(default 30).")
    ap.add_argument("--exclude-rowcol", type=int, nargs=4, default=None,
                    metavar=("R_LO", "R_HI", "C_LO", "C_HI"),
                    help="Drop seed modules with row in [R_LO, R_HI] AND "
                         "column in [C_LO, C_HI] from the per-module "
                         "calibration sample (e.g. --exclude-rowcol 16 19 "
                         "16 19 removes the 4x4 PbWO4 block surrounding the "
                         "beam hole, where leakage biases the per-module mean).")
    ap.add_argument("--cal-method", choices=['median', 'cb-peak'],
                    default='median',
                    help="Per-module reference for the gain refinement: "
                         "'median' (legacy, biased by the radiative tail) "
                         "or 'cb-peak' (Crystal-Ball fitted peak per module, "
                         "robust against the tail).  cb-peak falls back to a "
                         "narrow-window Gaussian and then the histogram "
                         "argmax for low-statistics modules.")
    args = ap.parse_args(argv)

    p = C.setup_pipeline(
        evio_path     = args.evio_path,
        max_events    = args.max_events,
        run_num       = args.run_num,
        gem_ped_file  = args.gem_ped_file,
        gem_cm_file   = args.gem_cm_file,
        hc_calib_file = args.hc_calib_file,
        daq_config    = args.daq_config,
        gem_map_file  = args.gem_map_file,
        hc_map_file   = args.hc_map_file,
    )
    pre_lo, pre_hi = args.pre_window
    W = args.window
    print(f"[setup] pre-window  : [{pre_lo}, {pre_hi}] ns", flush=True)
    print(f"[setup] seed window : ±{W} ns (new method only)", flush=True)
    print(f"[setup] signal cut  : single cluster E > {args.signal_min:.0f} MeV",
          flush=True)

    # Two clusterers sharing the geometry; differ only in seed_time_window.
    base_cfg = p.hc_clusterer.get_config()
    def _make_cluster(seed_window):
        cfg = det.HyCalClusterConfig()
        for f in ("min_module_energy", "min_center_energy", "min_cluster_energy",
                  "min_cluster_size", "corner_conn", "split_iter",
                  "least_split", "log_weight_thres"):
            setattr(cfg, f, getattr(base_cfg, f))
        cfg.seed_time_window = seed_window
        cl = det.HyCalCluster(p.hycal)
        cl.set_config(cfg)
        return cl
    cl_legacy = _make_cluster(-1.0)
    cl_new    = _make_cluster(W)

    # Bulk: single-cluster energies for the bulk Gaussian fit.
    # Per-event: (seed_id, x, y, row, col, seed_E, cluster_E) — populated
    # only when --calibrate is set, so the memory cost is opt-in.
    energies_legacy, energies_new = [], []
    events_legacy,   events_new   = [], []
    n_legacy = n_new = n_phys = n_kept = n_read = n_files_open = 0

    ch = dec.EvChannel()
    ch.set_config(p.cfg)

    t0 = time.monotonic()
    try:
        for fpath in p.evio_files:
            if ch.open_auto(fpath) != dec.Status.success:
                print(f"[WARN] skip (cannot open): {fpath}", flush=True)
                continue
            n_files_open += 1
            print(f"[file {n_files_open}/{len(p.evio_files)}] {fpath}",
                  flush=True)

            done = False
            while ch.read() == dec.Status.success:
                n_read += 1
                if not ch.scan():
                    continue
                if ch.get_event_type() != dec.EventType.Physics:
                    continue

                for i in range(ch.get_n_events()):
                    decoded = ch.decode_event(i, with_ssp=False)
                    if not decoded["ok"]:
                        continue
                    n_phys += 1
                    fadc_evt = decoded["event"]
                    if fadc_evt.info.trigger_bits != C.PHYSICS_TRIGGER_BITS:
                        if args.max_events > 0 and n_phys >= args.max_events:
                            done = True; break
                        continue
                    n_kept += 1

                    cl_legacy.clear()
                    cl_new.clear()

                    # One waveform pass; both clusterers consume the same peaks.
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

                                best_int  = -1.0
                                best_pk   = None
                                for pk in peaks:
                                    if pk.time <= pre_lo or pk.time >= pre_hi:
                                        continue
                                    cl_new.add_hit(mod.index,
                                                   mod.energize(pk.integral),
                                                   float(pk.time))
                                    if pk.integral > best_int:
                                        best_int = pk.integral
                                        best_pk  = pk
                                if best_pk is not None:
                                    cl_legacy.add_hit(mod.index,
                                                      mod.energize(best_pk.integral),
                                                      float(best_pk.time))

                    cl_legacy.form_clusters()
                    cl_new.form_clusters()
                    # reconstruct_matched gives us seed energy + center
                    # module without a second lookup; needed only by the
                    # --calibrate branch but cheap enough to use always.
                    rec_l = cl_legacy.reconstruct_matched()
                    rec_n = cl_new.reconstruct_matched()
                    big_l = [r for r in rec_l if r.hit.energy > args.signal_min]
                    big_n = [r for r in rec_n if r.hit.energy > args.signal_min]
                    if len(big_l) == 1:
                        r = big_l[0]
                        n_legacy += 1
                        energies_legacy.append(r.hit.energy)
                        if args.calibrate:
                            sm = p.hycal.module_by_id(r.hit.center_id)
                            events_legacy.append((r.hit.center_id, sm.x, sm.y,
                                                  sm.row, sm.column,
                                                  r.cluster.center.energy,
                                                  r.hit.energy))
                    if len(big_n) == 1:
                        r = big_n[0]
                        n_new += 1
                        energies_new.append(r.hit.energy)
                        if args.calibrate:
                            sm = p.hycal.module_by_id(r.hit.center_id)
                            events_new.append((r.hit.center_id, sm.x, sm.y,
                                               sm.row, sm.column,
                                               r.cluster.center.energy,
                                               r.hit.energy))

                    if args.max_events > 0 and n_phys >= args.max_events:
                        done = True; break

                if done:
                    break
                if n_phys > 0 and n_phys % 5000 == 0:
                    elapsed = time.monotonic() - t0
                    rate = n_phys / max(elapsed, 1e-3)
                    print(f"[progress] {n_phys} physics  legacy={n_legacy} "
                          f"new={n_new}  ({rate:.1f} ev/s)", flush=True)

            ch.close()
            if done:
                break
    finally:
        elapsed = time.monotonic() - t0

    # ---- summary + Gaussian fits ------------------------------------------
    e_lo, e_hi = args.e_range
    mu_l, sig_l, A_l, nf_l, edges, counts_l = fit_peak(
        energies_legacy, e_lo, e_hi, args.bin_width)
    mu_n, sig_n, A_n, nf_n, _,     counts_n = fit_peak(
        energies_new,    e_lo, e_hi, args.bin_width)
    centres = 0.5 * (edges[:-1] + edges[1:])

    rate = n_phys / max(elapsed, 1e-3)
    print()
    print("=" * 72)
    print("benchmark_hycal_timing — legacy vs. seed-time-gated clustering")
    print("=" * 72)
    print(f"  files {n_files_open}/{len(p.evio_files)},  "
          f"records {n_read},  physics {n_phys},  triggered {n_kept}")
    print(f"  elapsed {elapsed:.1f} s  ({rate:.1f} ev/s)")
    print(f"  selection: single cluster E > {args.signal_min:.0f} MeV")
    print()
    print(f"               | legacy        | gated (W={W:g} ns)")
    print(f"  events kept  | {n_legacy:>13d} | {n_new:>13d}    "
          f"({(n_new-n_legacy)/max(n_legacy,1)*100:+.1f} % vs. legacy)")
    if HAVE_SCIPY:
        print(f"  Gauss μ (MeV)| {mu_l:>13.1f} | {mu_n:>13.1f}")
        print(f"  Gauss σ (MeV)| {sig_l:>13.1f} | {sig_n:>13.1f}")
        print(f"  N in fit win | {nf_l:>13d} | {nf_n:>13d}")
    else:
        print("  (scipy not available — fit skipped)")

    # ---- Optional: per-seed-module calibration on the inner ring ----------
    cal = None
    if args.calibrate:
        print()
        print(f"  --calibrate mode  : target μ = {args.target_peak:.1f} MeV, "
              f"inner r < {args.inner_radius:.0f} mm,")
        print(f"                      seed E ≥ {args.min_seed_energy:.0f} MeV, "
              f"≥ {args.min_per_module} events/module")

        excl = tuple(args.exclude_rowcol) if args.exclude_rowcol else None
        if excl:
            print(f"                      exclude rows {excl[0]}..{excl[1]} "
                  f"AND cols {excl[2]}..{excl[3]}")
        print(f"                      per-module reference: {args.cal_method}")
        gains_l, raw_l, corr_l, nmod_l = fit_per_module_gains(
            events_legacy,
            target_peak=args.target_peak, inner_r=args.inner_radius,
            min_seed_E=args.min_seed_energy, min_per_mod=args.min_per_module,
            exclude_rowcol=excl, method=args.cal_method,
            bin_width=args.bin_width)
        gains_n, raw_n, corr_n, nmod_n = fit_per_module_gains(
            events_new,
            target_peak=args.target_peak, inner_r=args.inner_radius,
            min_seed_E=args.min_seed_energy, min_per_mod=args.min_per_module,
            exclude_rowcol=excl, method=args.cal_method,
            bin_width=args.bin_width)

        # Fit window wider than the post-cal peak so a poorly-calibrated
        # pre-cal histogram still falls inside.
        cal_lo, cal_hi = args.target_peak - 800, args.target_peak + 800
        bw = args.bin_width
        mu_l_pre,  sig_l_pre,  _, _, _,         counts_l_pre  = fit_peak(raw_l,  cal_lo, cal_hi, bw)
        mu_n_pre,  sig_n_pre,  _, _, _,         counts_n_pre  = fit_peak(raw_n,  cal_lo, cal_hi, bw)
        mu_l_post, sig_l_post, _, _, edges_cal, counts_l_post = fit_peak(corr_l, cal_lo, cal_hi, bw)
        mu_n_post, sig_n_post, _, _, _,         counts_n_post = fit_peak(corr_n, cal_lo, cal_hi, bw)
        cal_centres = 0.5 * (edges_cal[:-1] + edges_cal[1:])

        cb_l = fit_peak_cb(corr_l, cal_lo, cal_hi, bw, mu0_hint=mu_l_post)
        cb_n = fit_peak_cb(corr_n, cal_lo, cal_hi, bw, mu0_hint=mu_n_post)
        mu_cb_l, sig_cb_l, alpha_l, n_l, A_cb_l, *_ = cb_l
        mu_cb_n, sig_cb_n, alpha_n, n_n, A_cb_n, *_ = cb_n

        def _ratio(s, m):
            return s / m if (m and m > 0 and not math.isnan(m)) else float('nan')

        cal = dict(
            n_modules_l=nmod_l, n_modules_n=nmod_n,
            n_events_l=len(raw_l), n_events_n=len(raw_n),
            mu_l_pre=mu_l_pre, sig_l_pre=sig_l_pre,
            mu_n_pre=mu_n_pre, sig_n_pre=sig_n_pre,
            mu_l_post=mu_l_post, sig_l_post=sig_l_post,
            mu_n_post=mu_n_post, sig_n_post=sig_n_post,
            mu_cb_l=mu_cb_l, sig_cb_l=sig_cb_l, alpha_l=alpha_l, n_l=n_l, A_cb_l=A_cb_l,
            mu_cb_n=mu_cb_n, sig_cb_n=sig_cb_n, alpha_n=alpha_n, n_n=n_n, A_cb_n=A_cb_n,
            edges=edges_cal, centres=cal_centres,
            counts_l_pre=counts_l_pre, counts_n_pre=counts_n_pre,
            counts_l_post=counts_l_post, counts_n_post=counts_n_post,
        )

        sigE_l_g, sigE_n_g  = _ratio(sig_l_post, mu_l_post), _ratio(sig_n_post, mu_n_post)
        sigE_l_cb, sigE_n_cb = _ratio(sig_cb_l, mu_cb_l),    _ratio(sig_cb_n, mu_cb_n)
        print()
        print(f"                  | legacy        | gated (W={W:g} ns)")
        print(f"  inner events    | {len(raw_l):>13d} | {len(raw_n):>13d}")
        print(f"  modules used    | {nmod_l:>13d} | {nmod_n:>13d}")
        print(f"  μ before  (MeV) | {mu_l_pre:>13.1f} | {mu_n_pre:>13.1f}")
        print(f"  σ before  (MeV) | {sig_l_pre:>13.1f} | {sig_n_pre:>13.1f}")
        print(f"  μ  after  (MeV) | {mu_l_post:>13.1f} | {mu_n_post:>13.1f}  "
              f"(target {args.target_peak:.1f})")
        print(f"  σ  Gaussian     | {sig_l_post:>13.1f} | {sig_n_post:>13.1f}")
        print(f"  σ_E / E (G)     | {sigE_l_g*100:>12.2f}% | {sigE_n_g*100:>12.2f}%")
        print(f"  μ  Crystal Ball | {mu_cb_l:>13.1f} | {mu_cb_n:>13.1f}")
        print(f"  σ  Crystal Ball | {sig_cb_l:>13.1f} | {sig_cb_n:>13.1f}  "
              f"(α_l={alpha_l:.2f} n_l={n_l:.1f}  α_n={alpha_n:.2f} n_n={n_n:.1f})")
        print(f"  σ_E / E (CB)    | {sigE_l_cb*100:>12.2f}% | {sigE_n_cb*100:>12.2f}%  "
              f"(Δ = {(sigE_n_cb - sigE_l_cb)*100:+.2f} pp)")

    # ---- TSV output --------------------------------------------------------
    tsv_path = args.out_path if args.out_path.endswith('.tsv') \
               else args.out_path + ".tsv"
    with open(tsv_path, "w") as f:
        f.write("# benchmark_hycal_timing\n")
        f.write(f"# evio_path={args.evio_path} max_events={args.max_events}\n")
        f.write(f"# pre_window=[{pre_lo},{pre_hi}] ns  seed_time_window={W} ns\n")
        f.write(f"# selection: single cluster E > {args.signal_min} MeV\n")
        f.write(f"# n_phys={n_phys} n_kept={n_kept} elapsed_s={elapsed:.1f}\n")
        f.write("\n")
        f.write("path\tn_events\tgauss_mu_MeV\tgauss_sigma_MeV\tn_in_fit\n")
        f.write(f"legacy\t{n_legacy}\t{mu_l:.2f}\t{sig_l:.2f}\t{nf_l}\n")
        f.write(f"new\t{n_new}\t{mu_n:.2f}\t{sig_n:.2f}\t{nf_n}\n")
        f.write("\n")
        f.write("# energy histogram bin centres (MeV) and counts\n")
        f.write("e_centre\tcount_legacy\tcount_new\n")
        for c, l, n in zip(centres, counts_l, counts_n):
            f.write(f"{c:.1f}\t{l}\t{n}\n")
        if cal is not None:
            f.write("\n")
            f.write(f"# --calibrate inner ring (r<{args.inner_radius:.0f} mm,"
                    f" seed E≥{args.min_seed_energy:.0f} MeV,"
                    f" min/mod={args.min_per_module},"
                    f" target μ={args.target_peak:.1f} MeV)\n")
            f.write("path\tn_modules\tn_inner_events\tmu_pre_MeV\tsigma_pre_MeV"
                    "\tmu_post_MeV\tsigma_post_MeV\tsigma_over_E_post"
                    "\tmu_cb_MeV\tsigma_cb_MeV\talpha_cb\tn_cb"
                    "\tsigma_over_E_cb\n")
            for path, nmod, ne, mu_pre, sig_pre, mu_post, sig_post, \
                mu_cb, sig_cb, a_cb, n_cb in [
                ("legacy", cal['n_modules_l'], cal['n_events_l'],
                 cal['mu_l_pre'], cal['sig_l_pre'],
                 cal['mu_l_post'], cal['sig_l_post'],
                 cal['mu_cb_l'], cal['sig_cb_l'], cal['alpha_l'], cal['n_l']),
                ("new", cal['n_modules_n'], cal['n_events_n'],
                 cal['mu_n_pre'], cal['sig_n_pre'],
                 cal['mu_n_post'], cal['sig_n_post'],
                 cal['mu_cb_n'], cal['sig_cb_n'], cal['alpha_n'], cal['n_n']),
            ]:
                ratio    = sig_post / mu_post if mu_post > 0 else float('nan')
                ratio_cb = (sig_cb / mu_cb if (mu_cb and mu_cb > 0
                                               and not math.isnan(mu_cb))
                            else float('nan'))
                f.write(f"{path}\t{nmod}\t{ne}\t{mu_pre:.2f}\t{sig_pre:.2f}"
                        f"\t{mu_post:.2f}\t{sig_post:.2f}\t{ratio:.4f}"
                        f"\t{mu_cb:.2f}\t{sig_cb:.2f}\t{a_cb:.3f}\t{n_cb:.3f}"
                        f"\t{ratio_cb:.4f}\n")
            f.write("\n# inner-ring histogram bin centres + counts (pre/post recal)\n")
            f.write("e_centre\tlegacy_pre\tlegacy_post\tnew_pre\tnew_post\n")
            for c, lp, lq, np_, nq in zip(cal['centres'],
                                          cal['counts_l_pre'], cal['counts_l_post'],
                                          cal['counts_n_pre'], cal['counts_n_post']):
                f.write(f"{c:.1f}\t{lp}\t{lq}\t{np_}\t{nq}\n")
    print(f"\n  wrote: {tsv_path}")

    # ---- Plot --------------------------------------------------------------
    png_path = args.out_path if args.out_path.endswith('.png') \
               else args.out_path + ".png"
    fig, ax = plt.subplots(figsize=(9, 6))
    ax.step(centres, counts_l, where='mid', color='#888',
            lw=1.5, label=f'legacy   N={n_legacy}')
    ax.step(centres, counts_n, where='mid', color='#1f77b4',
            lw=1.5, label=f'new W={W:g} ns   N={n_new}')

    if HAVE_SCIPY and not math.isnan(mu_l):
        x_fit = np.linspace(mu_l - 4 * sig_l, mu_l + 4 * sig_l, 200)
        ax.plot(x_fit, gauss(x_fit, A_l, mu_l, sig_l),
                color='#888', ls='--', lw=1.0,
                label=f'   μ={mu_l:.0f} σ={sig_l:.0f} MeV')
    if HAVE_SCIPY and not math.isnan(mu_n):
        x_fit = np.linspace(mu_n - 4 * sig_n, mu_n + 4 * sig_n, 200)
        ax.plot(x_fit, gauss(x_fit, A_n, mu_n, sig_n),
                color='#1f77b4', ls='--', lw=1.0,
                label=f'   μ={mu_n:.0f} σ={sig_n:.0f} MeV')

    ax.axvline(args.signal_min, color='#d62728', lw=0.8, ls=':',
               label=f'cut E > {args.signal_min:.0f} MeV')
    ax.set_xlabel('cluster energy (MeV)')
    ax.set_ylabel('events / bin')
    ax.set_title(f"Single-cluster energy: legacy vs seed-time-gated\n"
                 f"run window [{pre_lo:.0f}, {pre_hi:.0f}] ns, "
                 f"{n_phys} physics events, {n_kept} triggered")
    ax.grid(alpha=0.3)
    ax.legend(loc='upper right', fontsize=9)
    ax.set_xlim(e_lo, e_hi)
    fig.tight_layout()
    fig.savefig(png_path, dpi=130)
    plt.close(fig)
    print(f"  wrote: {png_path}")

    # ---- Optional: calibration before/after plot --------------------------
    if cal is not None:
        cal_png = png_path[:-4] + "_calibrated.png"
        fig, axes = plt.subplots(1, 2, figsize=(14, 5.5), sharey=True)

        for ax, label, c_pre, c_post, mu_pre, sig_pre, mu_post, sig_post, \
            mu_cb, sig_cb, alpha_cb, n_cb, A_cb, base_col in [
            (axes[0], 'legacy',
             cal['counts_l_pre'], cal['counts_l_post'],
             cal['mu_l_pre'], cal['sig_l_pre'],
             cal['mu_l_post'], cal['sig_l_post'],
             cal['mu_cb_l'], cal['sig_cb_l'], cal['alpha_l'],
             cal['n_l'], cal['A_cb_l'], '#888'),
            (axes[1], f'new (W={W:g} ns)',
             cal['counts_n_pre'], cal['counts_n_post'],
             cal['mu_n_pre'], cal['sig_n_pre'],
             cal['mu_n_post'], cal['sig_n_post'],
             cal['mu_cb_n'], cal['sig_cb_n'], cal['alpha_n'],
             cal['n_n'], cal['A_cb_n'], '#1f77b4'),
        ]:
            ax.step(cal['centres'], c_pre, where='mid', color=base_col,
                    alpha=0.45, lw=1.3,
                    label=f'pre   μ={mu_pre:.0f}  σ_G={sig_pre:.0f} MeV')
            ax.step(cal['centres'], c_post, where='mid', color=base_col,
                    lw=1.8,
                    label=f'post  μ={mu_post:.0f}  σ_G={sig_post:.0f} MeV')
            # Crystal-ball curve overlay
            if not math.isnan(mu_cb) and A_cb > 0:
                xfine = np.linspace(mu_cb - 6 * sig_cb, mu_cb + 4 * sig_cb,
                                    400)
                yfine = crystal_ball(xfine, A_cb, mu_cb, sig_cb,
                                     alpha_cb, n_cb)
                ax.plot(xfine, yfine, color='#d62728', lw=1.4,
                        label=f'CB    μ={mu_cb:.0f}  σ={sig_cb:.0f} MeV  '
                              f'(α={alpha_cb:.2f} n={n_cb:.1f})')
            ax.axvline(args.target_peak, color='#444', ls=':', lw=0.8)
            ax.set_xlabel('cluster energy (MeV)')
            ax.set_title(label)
            ax.grid(alpha=0.3)
            ax.legend(loc='upper right', fontsize=8.5)
        axes[0].set_ylabel('events / bin')
        excl_str = (f",  exclude rows [{args.exclude_rowcol[0]},"
                    f"{args.exclude_rowcol[1]}]×cols "
                    f"[{args.exclude_rowcol[2]},{args.exclude_rowcol[3]}]"
                    if args.exclude_rowcol else "")
        sigEcb_l = (cal['sig_cb_l']/cal['mu_cb_l']*100
                    if cal['mu_cb_l'] and not math.isnan(cal['mu_cb_l'])
                    else float('nan'))
        sigEcb_n = (cal['sig_cb_n']/cal['mu_cb_n']*100
                    if cal['mu_cb_n'] and not math.isnan(cal['mu_cb_n'])
                    else float('nan'))
        fig.suptitle(
            f"Inner-ring per-module gain refinement "
            f"(r < {args.inner_radius:.0f} mm,  "
            f"seed E ≥ {args.min_seed_energy:.0f} MeV,  "
            f"≥ {args.min_per_module} ev/mod{excl_str})\n"
            f"σ_E/E (Gauss):  legacy = {cal['sig_l_post']/cal['mu_l_post']*100:.2f} %   "
            f"new = {cal['sig_n_post']/cal['mu_n_post']*100:.2f} %     "
            f"(CB):  legacy = {sigEcb_l:.2f} %   new = {sigEcb_n:.2f} %",
            fontsize=10.5)
        fig.tight_layout()
        fig.savefig(cal_png, dpi=130)
        plt.close(fig)
        print(f"  wrote: {cal_png}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
