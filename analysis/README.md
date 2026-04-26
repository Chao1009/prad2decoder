# Offline Analysis Tools

Replay and physics analysis for PRad2. **Requires ROOT 6.0+.**

```bash
cmake -B build -DBUILD_ANALYSIS=ON
cmake --build build -j$(nproc)
cmake --install build --prefix /your/install/prefix    # optional
```

Builds three things:

| Artifact | Where |
|----------|-------|
| `prad2ana_*` executables (replay, calibration, …) | `<build>/bin/`, installed to `<prefix>/bin/` |
| **`libprad2ana.a`** — static library exposing `analysis::*` (Replay, PhysicsTools, MatchingTools) | `<build>/analysis/`, installed to `<prefix>/lib/` |
| Headers (`PhysicsTools.h`, `MatchingTools.h`, `Replay.h`, `ConfigSetup.h`) | installed to `<prefix>/include/prad2ana/` |

The library is what makes ACLiC scripts work in install mode — without it, `analysis::*` symbols would be unresolved at link time.

## Tools

### Replay

> All analysis executables are installed with a `prad2ana_` prefix to avoid
> name collisions in a shared bindir.  The tool names below refer to the
> built binary — e.g. `prad2ana_replay_rawdata`.

**replay_rawdata** — Single EVIO file to ROOT tree with per-channel waveform data.
```bash
prad2ana_replay_rawdata <input.evio> [-o output.root] [-n max_events] [-p]
```

**replay_rawdata_m** — Multi-file, multi-threaded version of `replay_rawdata`. Processes all EVIO segments in a directory.
```bash
prad2ana_replay_rawdata_m <evio_dir> [-f max_files] [-n max_events] [-p] [-j num_threads] [-D daq_config.json] [-o merged.root]
```

**replay_recon** — HyCal reconstruction replay with clustering and per-module energy histograms.
```bash
prad2ana_replay_recon <input.evio> [-o output.root] [-c config.json] [-D daq_config.json] [-n N]
```

**replay_recon_m** — Multi-file, multi-threaded version of `replay_recon`. Supports GEM pedestal and zero-suppression options.
```bash
prad2ana_replay_recon_m <evio_dir> [-f max_files] [-n max_events] [-p] [-j num_threads] [-D daq_config.json] [-g gem_pedestal.json] [-z zerosup_threshold] [-o merged.root]
```
- `-p`  read PRad-I data format (no GEM)

### Calibration

**epCalib** — Elastic e-p calibration. Fits the elastic peak per module from rawdata ROOT files (peak mode) and writes gain correction constants.
```bash
prad2ana_epCalib <input.root> [-o output_calib_file] [-D daq_config.json] [-n max_events]
```

### Physics Analysis

**analysis_example** — Example offline analysis reading reconstructed ROOT trees. Fills energy, hit-position, and Moller-event histograms with optional GEM matching.
```bash
prad2ana_analysis_example <input_recon.root> [-o output.root] [-n max_events]
```

**cosmic_test** — Cosmic-ray analysis tool for commissioning. Reads raw waveform data and produces per-channel signal distributions.
```bash
prad2ana_cosmic_test <input.root> [-o output.root] [-D daq_config.json] [-n max_events]
```

## ACLiC Scripts (`scripts/`)

ROOT macros that compile against `libprad2dec` / `libprad2det` / `libprad2ana` via ACLiC. They share one prelude — `rootlogon.C` — that auto-detects whether you're in a build tree or an install tree and configures include paths + linker line accordingly.

**Build-tree mode** (preferred, picks up the freshest libs):
```bash
cd build
root -l ../analysis/scripts/rootlogon.C        # CMakeCache.txt in cwd → build mode
# or from anywhere:
PRAD2_BUILD_DIR=/path/to/build root -l rootlogon.C
```

**Install-tree mode** (after `cmake --install`):
```bash
source <prefix>/bin/prad2_setup.sh             # exports PRAD2_DATABASE_DIR
root -l <prefix>/share/prad2evviewer/analysis/scripts/rootlogon.C
```

Each path probe is logged as `[+] tag : path` (resolved) or `[-] tag : path` (skipped/missing) so a failed setup is one glance to debug. Set `PRAD2_ROOTLOGON_QUIET=1` to suppress the per-probe lines and keep just the section headers.

| Env var | What it overrides |
|---------|-------------------|
| `PRAD2_BUILD_DIR`        | build dir if not the cwd |
| `PRAD2_DATABASE_DIR`     | install-mode prefix anchor (set by `prad2_setup.sh`) |
| `PRAD2_EVIO_LIB`         | explicit `libevio.a` path (skips all evio probes) |
| `PRAD2_CODA_ROOT`        | non-default Hall-B CODA install root |
| `PRAD2_ROOTLOGON_QUIET`  | suppress per-probe `[+]/[-]` lines |

Then `.x` any of the macros below.

### gem_raw_dump.C

Smallest GEM example — opens an EVIO file, finds every GEM raw bank (tags from `daq_config.json`'s `bank_tags.ssp_raw`, currently `0xE10C` / `0x0DE9`), and prints the first N raw 32-bit words per bank per event. No decoding. Useful for verifying firmware layout or feeding raw words into a custom parser.

```bash
.x ../analysis/scripts/gem_raw_dump.C+( \
    "/data/stage6/prad_023867/prad_023867.evio.00000", 5, 8)
# args: evio_path, max_events (0=all), n_words_show
```

### gem_clusters_to_root.C

Full **HyCal + GEM** reconstruction pipeline with straight-line cluster matching → ROOT tree of matched HC↔GEM pairs and the constituent X/Y GEM strip waveforms.

Per event:
- `EvChannel.DecodeEvent` → FADC + SSP buffers
- HyCal: `WaveAnalyzer` → `mod->energize` → `HyCalCluster.FormClusters() / ReconstructHits()`
- GEM: `GemSystem.ProcessEvent` (pedestal + CM + ZS) → `Reconstruct(GemCluster)` → 2D X×Y matched hits per detector
- Lab-frame transform via `RotateDetData` / `TransformDetData` (uses `runinfo` geometry for HyCal + each GEM)
- For each HyCal cluster, draw a line from `(0,0,0)` target through the lab-frame centroid (z = `hycal_z` + shower depth); intersect each GEM plane and accept GEM hits within `N · σ_total` of the projection
- For each match, look up the X & Y constituent `StripCluster` on the corresponding plane and copy every strip's full 6-sample waveform

Matching geometry:
```
σ_hc(face) = 2.6 / sqrt(E / 1 GeV)     [mm at HyCal face]
σ_hc@gem   = σ_hc(face) · (z_gem / z_hc)
σ_gem      = 0.1 mm
σ_total    = sqrt(σ_hc@gem² + σ_gem²)
match if  |residual| < N · σ_total     [N defaults to 3, configurable]
```
The actual residual + `σ_total` are stored per match so downstream cuts can be tuned without re-running.

Tree layout (`match`, one entry per physics event):

| Group | Branches | Sized by |
|-------|----------|----------|
| event   | `event_num`, `trigger_bits` | scalar |
| HyCal   | `hc_x`, `hc_y`, `hc_z`, `hc_energy`, `hc_center`, `hc_nblocks`, `hc_flag`, `hc_sigma` | `ncl` |
| match   | `m_hc_idx`, `m_det`, `m_gem_x`, `m_gem_y`, `m_gem_z`, `m_gem_x_charge`, `m_gem_y_charge`, `m_gem_x_size`, `m_gem_y_size`, `m_proj_x`, `m_proj_y`, `m_residual`, `m_sigma_total` | `nmatch` |
| match X cl | `m_xcl_position`, `m_xcl_total`, `m_xcl_peak`, `m_xcl_max_tb`, `m_xcl_first`, `m_xcl_nstrips` | `nmatch` |
| match Y cl | `m_ycl_position`, `m_ycl_total`, `m_ycl_peak`, `m_ycl_max_tb`, `m_ycl_first`, `m_ycl_nstrips` | `nmatch` |
| strips  | `s_match_idx`, `s_plane` (0=X, 1=Y), `s_strip`, `s_position`, `s_charge`, `s_max_tb`, `s_cross_talk`, `s_ts0`…`s_ts5` | `nstrips` |

`m_xcl_first` + `m_xcl_nstrips` slice the strip arrays per matched X cluster (and `m_ycl_*` for Y); `s_match_idx` is the back-pointer hit→match.

Pedestals, common-mode files, and HyCal calibration are auto-discovered from `database/config.json` → `runinfo` (the same path `app_state_init.cpp` uses on the live monitor), so the analysis tree's reconstruction matches what the monitor sees. Pass an explicit path to override.

```bash
# simplest — everything auto-discovered:
.x ../analysis/scripts/gem_clusters_to_root.C+( \
    "/data/stage6/prad_023867/prad_023867.evio.00000", \
    "match_023867.root")

# tighter matching cut (2σ instead of 3σ default) — 5-arg overload:
.x ../analysis/scripts/gem_clusters_to_root.C+( \
    "/data/.../prad_023867.evio.00000", "out.root", \
    0L, -1, 2.0f)

# explicit overrides (paths relative to PRAD2_DATABASE_DIR or absolute):
.x ../analysis/scripts/gem_clusters_to_root.C+( \
    "/data/.../prad_023867.evio.00000", "out.root", \
    "gem_peds/peds_23867.txt", "gem_peds/cm_23867.txt", \
    "calibration/calibration_factor_0.json")
```

Convenience overloads (sidestep a cling default-arg-marshalling SEGV):
- `gem_clusters_to_root(evio, out)`
- `gem_clusters_to_root(evio, out, max_events)`
- `gem_clusters_to_root(evio, out, max_events, run_num)`
- `gem_clusters_to_root(evio, out, max_events, run_num, match_nsigma)`

Full 11-arg version (for explicit overrides — pass `""` to auto-discover any path):
`gem_clusters_to_root(evio_path, out_path, gem_ped_file, gem_cm_file, hc_calib_file, max_events, run_num, match_nsigma, daq_config, gem_map_file, hc_map_file)`.

### plot_hits_at_hycal.C

Side-by-side 2D occupancy maps of **GEM hits projected to the HyCal surface** (left) and **HyCal cluster centroids on the HyCal surface** (right). Both plots share the same lab/target-centered, beam-aligned frame at z = `hycal_z`, so structure overlays directly between the two.

Per event:
- HyCal: `WaveAnalyzer` → `mod->energize` → `HyCalCluster.FormClusters / ReconstructHits`. HC hits are built with `z = 0` (no shower-depth applied) so the lab transform places them at exactly z = `hycal_z`.
- GEM: `GemSystem.ProcessEvent` → `Reconstruct(GemCluster)` → per-detector hit list.
- Both go through the prad2det/prad2ana transforms: `RotateDetData` (per-detector tilt) → `TransformDetData` (per-detector position offset).
- GEM hits are then projected via `analysis::GetProjection(hits, hycal_z)` — straight line from target through each (x, y, z) intersected at z = `hycal_z`.
- All four GEM detectors fill a single combined left histogram.

Outputs the canvas in whatever format the extension implies (`.pdf`, `.png`, `.svg`, …) and a sibling `.root` file with both `TH2F`s and the canvas saved for re-plotting.

```bash
# simplest — everything auto-discovered, all events:
.x ../analysis/scripts/plot_hits_at_hycal.C+( \
    "/data/stage6/prad_023867/prad_023867.evio.00000", \
    "hits_at_hycal.pdf")

# subset of events:
.x ../analysis/scripts/plot_hits_at_hycal.C+( \
    "/data/.../prad_023867.evio.00000", "hits.png", 50000L)
```

Convenience overloads:
- `plot_hits_at_hycal(evio, out)`
- `plot_hits_at_hycal(evio, out, max_events)`
- `plot_hits_at_hycal(evio, out, max_events, run_num)`

Full 10-arg version (pass `""` to auto-discover any path):
`plot_hits_at_hycal(evio_path, out_path, max_events, run_num, gem_ped_file, gem_cm_file, hc_calib_file, daq_config, gem_map_file, hc_map_file)`.

### tagger_hycal_correlation.C

Two-phase study of T10R↔E49…E58 tagger pairs vs HyCal PbWO4 sums. Phase 1 caches per-event TDC tuples and fits a Gaussian to each ΔT spectrum; Phase 2 applies an N-σ timing cut per pair and fills global W-sum height/integral histograms for events with at least one matched pair plus a W channel above threshold. Outputs a 12-panel summary canvas.

```bash
.x ../analysis/scripts/tagger_hycal_correlation.C+( \
    "/data/stage6/prad_023686/prad_023686.evio.00000", \
    "tagger_wsum_corr.root", 500000)
# args: evio_path, out_path, max_events
```

### lms_alpha_normalize.C

Scans a run's EVIO files (`prad_{run}.evio.00000`–`99999`), selects only **LMS** (bit 24) and **Alpha** (bit 25) trigger events via `trigger_bits`, and normalizes HyCal LMS signals using the Alpha source as a gain reference.

Uses the project's decoder (`EvChannel`, `WaveAnalyzer`, `DaqConfig`) — no manual FADC parsing needed. Channel identity (HyCal module vs LMS ref) is read from `daq_map.json`.

| Trigger | What fires | Purpose |
|---------|-----------|---------|
| LMS     | All HyCal modules + LMS 1/2/3 | Monitor module response via LED/laser pulser |
| Alpha   | LMS 1/2/3 only | Provide stable gain reference (Am-241 source) |

Normalization per module *i* (averaged across 3 references *j*):
```
norm_i = integral_i × mean( alpha_ref_j / lms_ref_j )
```
LMS events before the first Alpha event in the run are skipped. Each LMS event uses the most recent Alpha reading.

```bash
# one-liner, after rootlogon:
root -l rootlogon.C 'lms_alpha_normalize.C+("/path/to/data", 1234)'

# with explicit config overrides:
root -l rootlogon.C 'lms_alpha_normalize.C+("/path/to/data", 1234, "daq_config.json", "daq_map.json")'
```

**Output:** `lms_alpha_run{N}.root` (per-module normalized LMS, reference time-series TGraphs) and a 6-panel summary PNG.

## Adding a Tool

Create `tools/my_tool.cpp`, then add to `CMakeLists.txt`:
```cmake
add_analysis_tool(my_tool tools/my_tool.cpp)
```

The helper takes care of the rest:
- compiles your `.cpp` into `prad2ana_my_tool` (binary prefix matches the install convention)
- links `libprad2ana.a` (transitively pulls in `prad2dec`, `prad2det`, ROOT)
- defines `DATABASE_DIR=...` so install-relative paths resolve
- routes the binary to `<build>/bin/`

If you add a *shared* source (something callable from multiple tools and from ACLiC scripts), put the implementation in `src/` and the declaration in `include/`, then list the new `.cpp` inside the `add_library(prad2ana STATIC ...)` call near the top of `CMakeLists.txt`. ACLiC scripts that link `libprad2ana.a` will pick up the new symbols automatically.

## Contributors
Yuan Li, Weizhi Xiong — Shandong University
Chao Peng - Argonne National Laboratory
