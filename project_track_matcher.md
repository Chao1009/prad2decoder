---
name: TrackMatcher consolidation
description: prad2det/TrackMatcher.h owns HyCal↔GEM matching; all C++ + Python callers migrated, MatchingTools deleted; planned next step is a multi-HC contention policy API (greedy-by-E / greedy-by-χ² / Hungarian).
type: project
originSessionId: 72902df1-6d6a-4884-af38-b7938d456033
---

`prad2::trk::TrackMatcher` (header `prad2det/include/TrackMatcher.h`) is the single source of truth for straight-line HyCal↔GEM matching in PRad-II.  Every former implementation — `analysis/MatchingTools`, the inline anchor builder in `app_state.cpp::runGemEfficiency`, the Python `_try_seed`/`best_track*` in gem_eff_audit, the closest-hit loop in gem_hycal_matching — has been replaced.  No magnetic field, so all tracks are straight lines; the class is stateless per call.

**Why:** four parallel implementations had drifted.  Centralizing in one class makes future tuning (σ models, χ² gates, target-in-fit knobs, new modes like multi-HC contention) land in a single file.

---

## API surface

| Entry point | Use case |
|---|---|
| `findBestTrack(hc, hits, free_seed?, require_planes?)` | Physics best track per HC; target-seed default. |
| `findBestTrack(hc, hits, seed_mode, seed_mask, fit_mask, target_in_fit, require_planes, diag?)` | Full-control overload. |
| `runLoo(test_plane, hc, hits, seed_mode, target_in_fit, diag?)` | Leave-one-out for efficiency studies. |
| `findPerPlaneMatches(hc, hits)` | Per-plane independent best-match (no fit, no χ²/dof gate); successor of legacy MatchPerChamber. |

Math helpers (`Line3D`, `seedLine`, `fitWeightedLine`, `projectLineToLocal`) live in `prad2det/include/TrackGeometry.h` — pull from there, don't re-port.

Funnel counters: `Stats { n_call, n_seed_tried, n_min_match, n_pass_chi2, n_pass_resid }`.  Caller owns lifetime + reset; only pass when needed.

Frame: lab is the contract.  Internal matching happens in plane-local because σ_plane is a local-frame quantity (strip pitch / resolution).  `LooResult.pred_x/y` is the naive lab pred at z=z_plane (line crosses the horizontal plane), not the on-surface point — re-project via `projectLineToLocal` + `xform.toLab` for on-surface lab coords.

Build: `prad2det` library; tests gated by `-DPRAD2DET_BUILD_TESTS=ON` → `prad2det_test_track_matcher` (10 cases).
Python: `from prad2py import det; m = det.TrackMatcher(det.MatcherConfig())`.

---

## Logic flow

### `findBestTrack` (single HC)
1. Build seed line(s) per `seed_mode`:
   - `TargetToHC`: one seed line, target → HC.
   - `HCAndPlaneHit`: iterate (plane, hit_idx) over `seed_planes_mask` capped by `max_hits_per_plane`.  Seed line goes HC → seed-plane hit.  Seed plane is auto-matched.
   - `FreeCombinatorial`: enumerate every fit-plane subset of size ≥ require_planes × per-plane hit cross-product.  No seed line — the fit IS the line.
2. For each seed line: project to every fit-eligible plane (in plane-local frame).  For each plane d, take the closest hit within `match_nsigma · σ_total[d]`, where `σ_total[d] = √(σ_HC@plane[d]² + σ_plane[d]²)` and `σ_HC@plane = σ_HC(E) · |(z_plane - z_target) / (z_HC - z_target)|`.
3. If ≥ `require_planes` matched: build weighted-LSQ fit through HC + matched planes (+ target if `require_target_in_fit`).  Target weights are anisotropic when target_z is non-zero (slope·σ_target_z couples into σ_x_eff and σ_y_eff).  Gate on `chi2_per_dof ≤ max_chi2`.
4. Per-plane post-fit residual gate: each matched hit's local-frame residual ≤ `match_nsigma · σ_plane[d]`.
5. Across all seeds, keep the lowest-χ²/dof candidate that passes both gates.  Return `nullopt` if none.

### `runLoo`
1. Compute `fit_mask = all_planes \ {test_plane}`, `seed_mask = fit_mask`.
2. Call `findBestTrack` with `require_planes = n_planes - 1` (force every OTHER plane to match — the "clean basis" rule).
3. Project the resulting fit line into `test_plane`'s local frame.
4. Search closest hit at `test_plane` within `match_nsigma · σ_plane[test_plane]`.
5. Return `LooResult { anchor, test_plane, pred_x, pred_y (lab), hit_at_test (optional) }`.

### `findPerPlaneMatches`
1. Build target → HC seed line.
2. For each plane d: project seed to plane-local; pick closest hit within `match_nsigma · σ_total[d]`.  Same window as `findBestTrack`'s seed step.
3. No fit, no χ² gate, no per-plane residual gate — each plane's decision is independent.
4. Return `PerPlaneMatch { seed, matched[], hit[], residual[] }`.

### Multi-HC at the caller (today's pattern)
Three callers (`Replay.cpp`, `analysis/tools/matching.cpp`, `sim2replay.cpp`) all reinvent the same greedy-by-E loop:
```cpp
sort(HC by energy desc);
remaining = hits_by_plane;
for each HC in sorted order:
    track = matcher.findBestTrack(HC, remaining);
    if (track): remove track.hit[d] from remaining[d]; emit track.
```
This pattern is what the planned `findTracks` API (below) will encapsulate.

---

## Live callers (post-migration)

- `src/app_state.cpp::runGemEfficiency` — `runLoo` per test_plane; mirrors `Stats` counters into `gem_eff_diag_*` (TargetSeed mode only, matching the historical AppState behavior).
- `analysis/src/Replay.cpp` — `findPerPlaneMatches` for `matchGEMx/y/z[i][d]` + `matchFlag[i]`; greedy-by-E `findBestTrack` (require_planes=2 + caller-side both-pair check) for `mHit_*[i]`.
- `analysis/tools/matching.cpp`, `analysis/tools/sim2replay.cpp` — greedy-by-E `findBestTrack`; output ROOT trees gain a `chi2_per_dof` branch.
- `analysis/pyscripts/gem_eff_audit.py` — `run_loo` for the three LOO variants (loo / loo-target-in / loo-target-seed); LooResult bridged back to the existing `TrackResult`/`_record_loo` reporting code via `_loo_to_track_result`.
- `analysis/pyscripts/gem_hycal_matching.py` — `find_per_plane_matches` per HC; one TSV row per matched (HC, plane).  GEM aux fields (charge / size / peak / timing) flow via parallel `aux_by_plane[d][hit_idx]` arrays the matcher doesn't see.

## Things deleted

- `analysis/include/MatchingTools.h`, `analysis/src/MatchingTools.cpp`
- `RunConfig::matching_radius`, `RunConfig::matching_use_square` (loader silently ignores the legacy `"matching": {radius, use_square_cut}` JSON block — old database files still parse)
- Python `seed_line`, `fit_weighted_line`, `fit_residuals_within_window`, `_try_seed`, `best_track`, `best_track_target_seed`, `best_track_unbiased` from `gem_eff_audit.py`
- The Python inline closest-hit loop + `C.hycal_pos_resolution` call in `gem_hycal_matching.py`

---

## Planned: multi-HC contention policy API

**Goal.** Promote the greedy-by-energy loop currently duplicated in three C++ callers into a first-class TrackMatcher operation, with policy options so we can experiment with different conflict-resolution rules in one place.  When a Moller-style 2-track analysis lands, having a single tunable knob is much cheaper than patching three callers.

**Spec.**

```cpp
enum class MultiClusterPolicy : uint8_t {
    GreedyByEnergy,   // highest-E HC claims first (= today's caller behavior)
    GreedyByChi2,     // best-fitting HC claims first (lowest χ²/dof of candidate Track)
    Hungarian,        // global min-total-χ² assignment (deferred — needs top-K tracks)
};

std::vector<std::optional<Track>>
findTracks(const std::vector<ClusterHit> &hcs,
           const std::vector<std::vector<PlaneHit>> &hits_by_plane,
           MultiClusterPolicy policy = MultiClusterPolicy::GreedyByEnergy,
           bool free_seed = false,
           int  require_planes = 3,
           Stats *diag = nullptr) const;
```

Returns one `optional<Track>` per input HC, in **input order** (callers may need to map results back to their cluster index).  `nullopt` where the policy denied this HC a track — either no candidate fit existed, or all its candidate hits were claimed by a higher-priority HC.

**Shared invariants** across all policies:
- Hits consumed by a winning cluster (i.e. `track.matched[d]` true → `track.hit[d]` is "consumed") are removed from the candidate pool before any subsequent HC is processed.
- Each call uses the same `findBestTrack` core under the hood; only the order and the pool-shrinking rule change.
- `Stats` (when passed) accumulates across every per-HC call.

**Per-policy semantics:**

`GreedyByEnergy` — drop-in replacement for the current Replay/matching.cpp/sim2replay.cpp loops:
1. Sort HC indices by `hcs[i].energy` descending.
2. `remaining = hits_by_plane`.
3. For each `i` in sorted order: `track = findBestTrack(hcs[i], remaining)`.  If `track`: remove `track.hit[d]` from `remaining[d]` for every matched `d`, store at `output[i]`.
4. Return `output`.

`GreedyByChi2` — useful when energy isn't the best discriminator (e.g. two low-E clusters of similar energy):
1. First pass: compute candidate `track_i = findBestTrack(hcs[i], full_pool)` for every HC.
2. Sort HC indices by `track_i.fit.chi2_per_dof` ascending; HCs with no candidate sort last.
3. Second pass: re-run `findBestTrack(hcs[i], remaining)` in sorted order (the second call is necessary because the pool shrinks and may change the optimal track for HC i).  If still present, claim hits.
4. `O(N)` first pass + `O(N)` refined pass; fine for ≤ 10 HCs per event.

`Hungarian` — global min-total-χ² assignment.  Deferred until a real Moller analysis needs it; spec sketch:
- Extend `findBestTrack` to return the top-K tracks per HC (instead of just the lowest-χ² one).
- Build an `(N × K)` cost matrix with `cost[i][k] = chi2_per_dof of HC i's k-th candidate`.
- Solve min-cost bipartite matching with the constraint that no two assigned tracks share a `(plane, hit)` pair.
- ~150 lines + the top-K extension; not needed for single-electron physics.

**Caller migration after `findTracks` lands:**
- `Replay.cpp` mHit_* loop, `analysis/tools/matching.cpp`, `analysis/tools/sim2replay.cpp` — replace ~30 lines of greedy-by-E orchestration with one `matcher.findTracks(hcs, hits_by_plane)` call.
- Per-chamber outputs (`matchGEMx/y/z[i][d]`) stay on `findPerPlaneMatches` because they're explicitly non-exclusive — independent per HC.

**Trigger to start this work.** The greedy-loop pattern already shows up in three callers.  Promotion to API isn't urgent (the duplication is small and the wrappers are well-tested), but if a fourth caller appears, or a Moller analysis wants `GreedyByChi2`, that's the cue.

---

## Phase V — verification (user-driven, still pending)

- **Phase 2 (AppState).** Run viewer on a fixed EVIO before/after; diff `gem_eff_num/den`, `gem_eff_diag_*`, `gemEffSnapshotJson()`.  Expected **bit-equivalent** for the TargetSeed loo_mode (GEM-seeded modes drift because the seed-iteration order is preserved but seed-window matching is now in plane-local rather than lab frame — sub-mm differences only).
- **Phase 3 (Replay/matching/sim2replay).** Per-cluster `matchGEMx/y/z`, `matchFlag`, `mHit_*` branches will *differ* from pre-migration ROOT (legacy 15 mm fixed cut → χ²-gated σ-based).  Calibrations are per-detector-alone so downstream is unaffected.  Spot-check that residuals look tighter and matchNum is sensible.
- **Phase 4/5 (Python).** `gem_eff_audit.py`: per-detector eff numbers should now match the C++ AppState path bit-for-bit (both use TrackMatcher).  `gem_hycal_matching.py`: TSV output should be very close to the previous Python loop since both use target-seed σ_total cut, but the new path matches in plane-local — sub-mm differences possible on tilted planes.

## Other deferred items (low priority, no current blocker)

- **Per-role minimum match rule** (e.g. "≥1 from {downstream pair} AND ≥1 from {upstream pair}") as a first-class API parameter.  Today callers do a post-Track check (see `Replay.cpp` mHit_* loop's `has_down && has_up`).  Promote when the post-check pattern shows up in a third caller.
- **True anisotropic σ_x × σ_y ellipse cut**.  Today's `planeSigma()` falls back to `√(σ_x · σ_y)` scalar when σ_x ≠ σ_y.  Real ellipse cut would require restructuring the residual gate to use a 2D Mahalanobis distance.
