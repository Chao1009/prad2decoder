# Offline Analysis Tools

Replay and physics analysis for PRad-II. **Requires ROOT 6.0+.**

```bash
cmake -B build -DBUILD_ANALYSIS=ON
cmake --build build -j$(nproc)
cmake --install build --prefix /your/install/prefix    # optional
```

The configuration produces three artefacts:

| Artefact | Location |
|----------|----------|
| `prad2ana_*` executables (replay, calibration, …) | `<build>/bin/`, installed to `<prefix>/bin/` |
| `libprad2ana.a` — static library exposing `analysis::*` (`Replay`, `PhysicsTools`).  HyCal↔GEM matching lives in `prad2::trk::TrackMatcher` (header `prad2det/TrackMatcher.h`). | `<build>/analysis/`, installed to `<prefix>/lib/` |
| Headers (`PhysicsTools.h`, `Replay.h`, `ConfigSetup.h`) | installed to `<prefix>/include/prad2ana/` |

The static library is what makes ACLiC scripts work in installed mode —
without it, `analysis::*` symbols would be unresolved at link time.

> All analysis executables are installed with a `prad2ana_` prefix to
> avoid name collisions in a shared `bindir`.  The tool names below
> refer to the built binary — e.g. `prad2ana_replay_rawdata`.

## Replay tools

Both replay programs are multi-threaded; point them at one or more EVIO
files (or a directory of segments) and tune parallelism with `-j`.
Output goes to `<output_dir>/<input_stem>_raw.root` (`replay_rawdata`)
or `_recon.root` (`replay_recon`), one ROOT file per input EVIO segment.
Each output file carries a per-event main tree (`events` or `recon`)
and two slow-control side trees (`scalers` from DSC2, `epics` from
0x001F text banks); see [REPLAYED_DATA.md](../docs/REPLAYED_DATA.md)
for the full branch list.

### replay_rawdata

EVIO → per-channel waveform tree (`events`).

```bash
prad2ana_replay_rawdata <evio_or_dir> [more...] -o <output_dir> \
    [-f max_files] [-n max_events] [-j num_threads] \
    [-c daq_config.json] [-d hycal_map.json] [-p]
```

`-p` adds peak branches: `hycal.peak_*` (soft analyzer) **and**
`hycal.daq_peak_*` (firmware Mode 1/2/3 emulation).  Without `-p`, only
raw samples + module info are written; the soft analyzer is skipped
entirely on the per-channel hot path.

### replay_recon

EVIO → reconstructed tree (`recon`): HyCal clustering, GEM hits, and
HyCal↔GEM straight-line matching, all transformed into the lab frame
via the `runinfo` geometry.  Wired through `prad2::PipelineBuilder` so
the offline reconstruction matches what the live monitor produces.

```bash
prad2ana_replay_recon <evio_or_dir> [more...] -o <output_dir> \
    [-f max_files] [-n max_events] [-j num_threads] \
    [-c daq_config.json] [-d hycal_map.json] \
    [-g gem_pedestal.json] [-z zerosup_threshold] [-p]
```

`-p` here selects **PRad-I data format** (no GEM, ADC1881M Fastbus
pedestals) — different semantics from the same flag on
`replay_rawdata`.  `-g` overrides the GEM pedestal file from
`runinfo`; `-z` overrides the zero-suppression sigma threshold.

### replay_filter

Applies slow-control quality cuts to a replayed run and writes a single
ROOT file containing only the physics events bracketed by two adjacent
passing slow-event checkpoints.

```bash
prad2ana_replay_filter <input.root> [more.root ...] \
    -o <output.root> -c <cuts.json> [-j <report.json>] [-r <run_num>]
```

Inputs may be either `replay_rawdata` (`events`) or `replay_recon`
(`recon`) outputs; the tree name is auto-detected and preserved.  All
splits of a run can be passed in a single invocation.

**Algorithm.** The DSC2 livetime (from the `scalers` tree) and the
EPICS slow-control values (from the `epics` tree) form a merged
timeline of checkpoints, ordered by event number.  At each checkpoint
the configured cuts are evaluated; a checkpoint is accepted only when
*every* cut passes.  Physics events with `event_num` strictly inside
the half-open interval `(ev_i, ev_{i+1}]` between two adjacent
accepted checkpoints are written to the output `events` / `recon`
tree; events bracketed by a rejected endpoint (or by an undefined
endpoint at the run boundaries) are discarded.  The `scalers` and
`epics` trees are concatenated verbatim from the inputs and tagged
with an additional `good` boolean recording each row's overall
verdict.

Cut JSON (see [`cuts/prad2_default.json`](cuts/prad2_default.json) for
a checked-in template):

```json
{
  "livetime": {
    "source":  "ref",                         // ref | trg | tdc
    "channel": 0,                             // ignored for "ref"
    "abs":     { "min": 90, "max": 100 }
  },
  "epics": {
    "hallb_IPM2C21A_CUR":  { "abs": { "min": 0.3 } },
    "hallb_IPM2C21A_XPOS": { "rel_rms": 3,
                             "gated_by": "hallb_IPM2C21A_CUR" },
    "hallb_IPM2C21A_YPOS": { "rel_rms": 3,
                             "gated_by": "hallb_IPM2C21A_CUR" }
  },
  "charge": {
    "beam_current": "hallb_IPM2C21A_CUR"
  }
}
```

- `abs.min` / `abs.max` — hard thresholds; either bound is optional.
- `rel_rms: N` — accept points within `N · σ̂` of the channel's robust
  centre, where the centre is the median and `σ̂ = 1.4826 · MAD`
  (the consistent estimator of the standard deviation under a Gaussian,
  robust to outliers).  The robust statistics are computed once per
  run from the channel's full slow-control history.
- `gated_by` — restrict the median/MAD computation to checkpoints
  where the named channel's cut passed (single string or array of
  strings, ANDed).  Useful for channels that are only meaningful in a
  particular regime (e.g. beam position is well defined only when the
  current is above a floor).  The cut itself still applies to every
  checkpoint; only the *statistics* are conditioned.  EPICS-on-EPICS,
  one level deep.
- `charge.beam_current` — name of the EPICS channel carrying beam
  current (assumed to publish in **nA**, true for the Hall B IPM
  scalers).  Enables live-charge integration over kept intervals:
  each adjacent passing-passing checkpoint pair contributes
  `live_fraction × Δt × ½(I_i + I_{i+1})` to the running sum, where
  `live_fraction` is the slice-local DSC2 livetime at the right
  endpoint and `I` is forward-filled from the named channel.  Pairs
  with a missing input on either side are skipped, not zero-counted.
  The reported `live_charge.value_nC` is therefore in **nC** (nA·s).

**Report (`-j`, default `<output_stem>.report.json`).** One JSON entry
per (cut channel, checkpoint), each with `associated_evn`, the
TI-tick-relative `associated_timestamp`, the EPICS-native `unix_time`
(EPICS rows only), the cut's `value`, and a `pass` / `fail` `status`.
The top level carries a `summary` with overall and per-channel
pass/fail counts and rates, a `stats` block listing each `rel_rms`
cut's robust centre, σ̂, MAD, gating channels, and post-gate sample
count, and — when `charge` is configured — a `live_charge` block
with `value_nC`, the accumulated live time, the beam-current channel
name, and the count of integrated vs. skipped checkpoint pairs.
Suitable for direct ingestion into a per-run quality dashboard.

### live_charge

Standalone live-charge integrator.  Reads the `scalers` and `epics`
side trees from any replayed ROOT file — `replay_rawdata`,
`replay_recon`, or `replay_filter` output — and accumulates
`Σ live_fraction · Δt · ½(I_i + I_{i+1})` over adjacent slow-event
checkpoints.  When the trees carry replay_filter's per-row `good`
bool, only passing-passing pairs contribute (post-cut live charge);
otherwise every adjacent pair contributes (total live charge over
the run).  Beam current is assumed to publish in nA, so the result
is reported in **nC**.

```bash
prad2ana_live_charge <input.root> [more.root ...] \
    [-c hallb_IPM2C21A_CUR] [-s ref|trg|tdc] [-n channel] [-j out.json]
```

The text summary on stdout lists the value, accumulated live time,
average current, and the count of total / kept / integrated / skipped
checkpoint pairs.  `-j` additionally writes a JSON dump suitable for
diff-checking against `replay_filter`'s `live_charge` block.

## Calibration

### epCalib

Elastic *e-p* calibration. Fits the elastic peak per module from
`replay_rawdata` ROOT files (peak mode) and writes gain-correction
constants.

```bash
prad2ana_epCalib <input.root> [-o output_calib_file] \
    [-D daq_config.json] [-n max_events]
```

## Physics analysis

### analysis_example

Worked offline analysis reading reconstructed ROOT trees. Fills energy,
hit-position, and Møller-event histograms with optional GEM matching.

```bash
prad2ana_analysis_example <input_recon.root> [-o output.root] [-n max_events]
```

### cosmic_test

Cosmic-ray analysis for commissioning. Reads raw waveform data and
produces per-channel signal distributions.

```bash
prad2ana_cosmic_test <input.root> [-o output.root] \
    [-D daq_config.json] [-n max_events]
```

## ACLiC scripts (`scripts/`)

ROOT macros that compile against `libprad2dec` / `libprad2det` /
`libprad2ana` via ACLiC. They share one prelude — `rootlogon.C` —
which auto-detects whether you are in a build tree or an install tree
and configures include paths and the linker line accordingly.

**Build-tree mode** (preferred — picks up the freshest libraries):

```bash
cd build
root -l ../analysis/scripts/rootlogon.C        # CMakeCache.txt in cwd → build mode
# or, from anywhere:
PRAD2_BUILD_DIR=/path/to/build root -l rootlogon.C
```

**Install-tree mode** (after `cmake --install`):

```bash
source <prefix>/bin/prad2_setup.sh             # exports PRAD2_DATABASE_DIR
root -l <prefix>/share/prad2evviewer/analysis/scripts/rootlogon.C
```

Each path probe is logged as `[+] tag : path` (resolved) or
`[-] tag : path` (skipped/missing) so a failed setup is debuggable at a
glance.  Set `PRAD2_ROOTLOGON_QUIET=1` to suppress per-probe lines.

| Env var | Overrides |
|---------|-----------|
| `PRAD2_BUILD_DIR`        | Build directory if not the cwd. |
| `PRAD2_DATABASE_DIR`     | Install-mode prefix anchor (set by `prad2_setup.sh`). |
| `PRAD2_EVIO_LIB`         | Explicit `libevio.a` path (skips all evio probes). |
| `PRAD2_CODA_ROOT`        | Non-default Hall-B CODA install root. |
| `PRAD2_ROOTLOGON_QUIET`  | Suppress per-probe `[+]/[-]` lines. |

Then `.x` any of the macros below.

### gem_raw_dump.C

Smallest GEM example — opens an EVIO file, finds every GEM raw bank
(tags from `daq_config.json`'s `bank_tags.ssp_raw`, currently `0xE10C`
and `0x0DE9`), and prints the first *N* raw 32-bit words per bank per
event. No decoding; useful for verifying firmware layout or feeding raw
words into a custom parser.

```bash
.x ../analysis/scripts/gem_raw_dump.C+( \
    "/data/stage6/prad_023867/prad_023867.evio.00000", 5, 8)
# args: evio_path, max_events (0 = all), n_words_show
```

### gem_hycal_matching.C

Full HyCal + GEM reconstruction pipeline with straight-line
cluster-to-hit matching → ROOT tree of matched HC↔GEM pairs and the
constituent X/Y GEM strip waveforms.

> **Python counterpart**: `analysis/pyscripts/gem_hycal_matching.py` —
> identical pipeline via `prad2py`, no ROOT, flat TSV/CSV out, identical
> best-match rule. See [Python counterparts](#python-counterparts-pyscripts).

**Trigger filter.** Only events with `trigger_bits == 0x100`
(production physics trigger) are reconstructed and written; everything
else (LMS / Alpha / cosmic / …) is skipped.  The summary reports raw
physics count vs. kept count.

**Multi-file inputs** are chosen by the path:

- `prad_023881.evio.*` → glob mode: enumerate every sibling
  `prad_023881.evio.<digits>` in the enclosing directory and process
  them all into one output tree (suffix-sorted).  Warns to stderr
  about any gap in the suffix sequence — including a missing
  `.00000` start.
- A directory (e.g. `/data/prad_023881/`) → same enumeration, run
  number sniffed from the directory name.
- `prad_023881.evio.00000` → single-file mode: just that one split.

The summary reports `EVIO files opened : M / N`.

Per event (after the trigger cut):

- `EvChannel.DecodeEvent` → FADC + SSP buffers.
- HyCal: `WaveAnalyzer` → `mod->energize` →
  `HyCalCluster.FormClusters() / ReconstructHits()`.
- GEM: `GemSystem.ProcessEvent` (pedestal + CM + ZS) →
  `Reconstruct(GemCluster)` → 2-D X×Y matched hits per detector.
- Lab-frame transform via `RotateDetData` / `TransformDetData` (uses
  the `runinfo` geometry for HyCal and each GEM).
- For each HyCal cluster, draw a line from `(0,0,0)` target through
  the lab-frame centroid (`z = hycal_z` + shower depth); intersect
  each GEM plane and find the closest GEM hit within `N · σ_total`
  of the projection.
- **Best-match rule** (HyCal cluster as baseline): per
  (HC cluster, GEM detector) pair, retain at most one row — the GEM
  hit with the smallest 2-D residual still inside the window.  A
  given GEM hit can win against multiple HC clusters (no GEM-side
  exclusivity).  The Python counterpart applies the identical rule.
- For each match, look up the X & Y constituent `StripCluster` on the
  corresponding plane and copy every strip's full 6-sample waveform.

**Matching geometry** (driven by `reconstruction_config.json:matching`):

```
σ_hc(face) = sqrt((A/sqrt(E_GeV))² + (B/E_GeV)² + C²)   [mm at HyCal face]
             A,B,C = matching.hycal_pos_res
σ_hc@gem   = σ_hc(face) · (z_gem / z_hc)
σ_gem      = matching.gem_pos_res[det_id]               [mm, per detector]
σ_total    = sqrt(σ_hc@gem² + σ_gem²)
match if  |residual| < N · σ_total                      [N defaults to 3]
```

The actual residual and `σ_total` are stored per match so downstream
cuts can be re-tuned without rerunning the macro.  The C++ formula
lives on `HyCalSystem::PositionResolution(E)` (set via
`SetPositionResolutionParams(A, B, C)`); the loader helper is
`script_helpers.h::load_matching_config(...)`.  The Python counterpart
uses `_common.load_matching_config()` and `_common.hycal_pos_resolution(...)`.

**Tree layout** (`match`, one entry per physics event):

| Group | Branches | Sized by |
|-------|----------|----------|
| event   | `event_num`, `trigger_bits` | scalar |
| HyCal   | `hc_x`, `hc_y`, `hc_z`, `hc_energy`, `hc_center`, `hc_nblocks`, `hc_flag`, `hc_sigma` | `ncl` |
| match   | `m_hc_idx`, `m_det`, `m_gem_x`, `m_gem_y`, `m_gem_z`, `m_gem_x_charge`, `m_gem_y_charge`, `m_gem_x_size`, `m_gem_y_size`, `m_proj_x`, `m_proj_y`, `m_residual`, `m_sigma_total` | `nmatch` |
| match X cl | `m_xcl_position`, `m_xcl_total`, `m_xcl_peak`, `m_xcl_max_tb`, `m_xcl_first`, `m_xcl_nstrips` | `nmatch` |
| match Y cl | `m_ycl_position`, `m_ycl_total`, `m_ycl_peak`, `m_ycl_max_tb`, `m_ycl_first`, `m_ycl_nstrips` | `nmatch` |
| strips  | `s_match_idx`, `s_plane` (0=X, 1=Y), `s_strip`, `s_position`, `s_charge`, `s_max_tb`, `s_cross_talk`, `s_ts0`…`s_ts5` | `nstrips` |

`m_xcl_first` + `m_xcl_nstrips` slice the strip arrays per matched X
cluster (and `m_ycl_*` for Y); `s_match_idx` is the back-pointer
hit → match.

Pedestals, common-mode files, and HyCal calibration constants are
auto-discovered from `database/reconstruction_config.json` →
`runinfo` (the same path `app_state_init.cpp` uses on the live
monitor), so the analysis tree's reconstruction matches what the
monitor sees.  Pass an explicit path to override.

```bash
# full-run replay — glob discovers every split, warns on gaps:
.x ../analysis/scripts/gem_hycal_matching.C+( \
    "/data/stage6/prad_023867/prad_023867.evio.*", \
    "match_023867.root")

# debug a single split file:
.x ../analysis/scripts/gem_hycal_matching.C+( \
    "/data/stage6/prad_023867/prad_023867.evio.00000", \
    "match_023867_seg0.root")

# tighter matching cut (2σ instead of 3σ default) — 5-arg overload:
.x ../analysis/scripts/gem_hycal_matching.C+( \
    "/data/.../prad_023867.evio.*", "out.root", \
    0L, -1, 2.0f)
```

Convenience overloads (sidestep a cling default-arg-marshalling SEGV):

- `gem_hycal_matching(evio, out)`
- `gem_hycal_matching(evio, out, max_events)`
- `gem_hycal_matching(evio, out, max_events, run_num)`
- `gem_hycal_matching(evio, out, max_events, run_num, match_nsigma)`

Full 11-arg version (for explicit overrides — pass `""` to
auto-discover any path):
`gem_hycal_matching(evio_path, out_path, gem_ped_file, gem_cm_file, hc_calib_file, max_events, run_num, match_nsigma, daq_config, gem_map_file, hc_map_file)`.

### plot_hits_at_hycal.C

Side-by-side 2-D occupancy maps of GEM hits projected onto the HyCal
surface (left) and HyCal cluster centroids on the HyCal surface
(right).  Both plots share the same lab/target-centred, beam-aligned
frame at `z = hycal_z`, so structure overlays directly between the two.

> **Python counterpart**: `analysis/pyscripts/plot_hits_at_hycal.py` —
> same pipeline via `prad2py`, no ROOT.

Trigger filter, multi-file behaviour, and per-event reconstruction
chain are identical to `gem_hycal_matching.C`.  HyCal hits are built
with `z = 0` (no shower-depth correction applied) so the lab transform
places them at exactly `z = hycal_z`.  All four GEM detectors fill a
single combined left histogram.

Outputs the canvas in whatever format the extension implies (`.pdf`,
`.png`, `.svg`, …) plus a sibling `.root` file with both `TH2F`s and
the canvas saved for re-plotting.

```bash
.x ../analysis/scripts/plot_hits_at_hycal.C+( \
    "/data/stage6/prad_023867/prad_023867.evio.*", \
    "hits_at_hycal.pdf")
```

Convenience overloads:
`plot_hits_at_hycal(evio, out)`,
`plot_hits_at_hycal(evio, out, max_events)`,
`plot_hits_at_hycal(evio, out, max_events, run_num)`,
plus a full 10-arg form mirroring `gem_hycal_matching.C`.

### tagger_hycal_correlation.C

Two-phase study of T10R↔E49…E58 tagger pairs vs HyCal PbWO₄ sums.
Phase 1 caches per-event TDC tuples and Gauss-fits each ΔT spectrum;
Phase 2 applies an *N*-σ timing cut per pair and fills global W-sum
height/integral histograms for events with at least one matched pair
plus a W channel above threshold.  Outputs a 12-panel summary canvas.

```bash
.x ../analysis/scripts/tagger_hycal_correlation.C+( \
    "/data/stage6/prad_023686/prad_023686.evio.00000", \
    "tagger_wsum_corr.root", 500000)
# args: evio_path, out_path, max_events
```

### lms_alpha_normalize.C

Scans a run's EVIO files (`prad_{run}.evio.00000`–`99999`), selects only
**LMS** (bit 24) and **Alpha** (bit 25) trigger events via
`trigger_bits`, and normalises HyCal LMS signals using the Alpha
source as a gain reference.

| Trigger | What fires | Purpose |
|---------|-----------|---------|
| LMS     | All HyCal modules + LMS 1/2/3 | Monitor module response via the LED/laser pulser. |
| Alpha   | LMS 1/2/3 only | Provide a stable gain reference (Am-241 source). |

Normalisation per module *i* (averaged across the three references *j*):

```
norm_i = integral_i × mean( alpha_ref_j / lms_ref_j )
```

LMS events before the first Alpha event in the run are skipped; each
LMS event uses the most recent Alpha reading.

```bash
root -l rootlogon.C 'lms_alpha_normalize.C+("/path/to/data", 1234)'
```

Output: `lms_alpha_run{N}.root` (per-module normalised LMS, reference
time-series `TGraph`s) and a six-panel summary PNG.

## Python counterparts (`pyscripts/`)

These scripts mirror the ROOT analysis macros via the `prad2py`
pybind11 module — same EVIO decoding and HyCal/GEM reconstruction, but
**no ROOT** and **flat TSV/CSV** outputs instead of trees / canvases.
Run them anywhere `prad2py` is importable (`cmake -DBUILD_PYTHON=ON`
and put the install dir on `PYTHONPATH`).  `_common.py` factors out
runinfo loading, the lab-frame transforms, file discovery, the trigger
gate `0x100`, and the multi-file glob/dir/single mode shared between
scripts.

### gem_hycal_matching.py

Same pipeline and best-match rule as the C macro of the same name.
One row per matched tuple:

| Column | Notes |
|--------|-------|
| `event_num`, `trigger_bits` | event-level |
| `hc_idx`, `hc_x/y/z`, `hc_energy`, `hc_center`, `hc_nblocks`, `hc_sigma` | HyCal cluster (lab frame, z includes shower depth) |
| `det_id` (0..3) | which GEM won this row |
| `gem_x/y/z` | best-matched GEM hit, lab/target-centred mm |
| `gem_x_local`, `gem_y_local` | same hit in the GEM detector frame |
| `gem_x_charge`, `gem_y_charge` | total ADC of the X / Y constituent cluster |
| `gem_x_peak`, `gem_y_peak` | max-strip ADC of the X / Y cluster |
| `gem_x_max_tb`, `gem_y_max_tb` | time-sample index of the max-ADC strip; multiply by `ts_period` (default 25 ns) for ns |
| `gem_x_size`, `gem_y_size` | strip count of the X / Y cluster |
| `proj_x`, `proj_y`, `residual`, `sigma_total` | matching geometry |

```bash
python analysis/pyscripts/gem_hycal_matching.py \
    /data/stage6/prad_023867/prad_023867.evio.* match_023867.tsv

python analysis/pyscripts/gem_hycal_matching.py input.evio.* out.csv \
    --csv --match-nsigma 2.0 --max-events 50000
```

### plot_hits_at_hycal.py

Dumps **all** hits (not just matched) — one row per HyCal cluster
centroid and per GEM hit projected to `z = hycal_z`. Plot externally
with pandas / matplotlib.

| Column | Notes |
|--------|-------|
| `event_num`, `trigger_bits` | event-level |
| `kind` | `"hycal"` or `"gem"` |
| `det_id` | 0..3 for GEM, −1 for HyCal |
| `x`, `y`, `z` | lab/target-centred mm at `z = hycal_z` |
| `energy` | MeV (HyCal); empty for GEM |

```bash
python analysis/pyscripts/plot_hits_at_hycal.py \
    /data/stage6/prad_023867/prad_023867.evio.* hits_023867.tsv
```

### plot_match_summary.py

Reads the per-match TSV/CSV from `gem_hycal_matching.py` and emits four
PNG plots — no EVIO replay, just a post-processing pass on a table
already produced.

| Output | What it shows |
|--------|---------------|
| `{prefix}_local_hits.png`  | 2×2 grid of 2-D heatmaps — `(gem_x_local, gem_y_local)` per detector (acceptance / dead regions). |
| `{prefix}_lab_scatter.png` | Scatter of `(gem_x, gem_y)` lab-frame, colour-coded by `det_id`. |
| `{prefix}_peak_adc.png`    | 2×2 histograms — `gem_x_peak` and `gem_y_peak` overlaid per detector. |
| `{prefix}_timing.png`      | 2×2 histograms — timing of the max-ADC strip per X / Y cluster, in ns. |

Requires the post-2026-04 `gem_hycal_matching.py` columns (`gem_*_local`,
`gem_*_peak`, `gem_*_max_tb`).

```bash
python analysis/pyscripts/plot_match_summary.py match_023867.tsv \
    --out-dir plots/ --bins 200 --no-show
```

Flags: `--csv`, `--out-dir`, `--prefix` (default = input stem),
`--bins` (default 120), `--ts-period` (default 25.0 ns), `--no-show`.

### gem_eff_audit.py

Offline leave-one-out (LOO) audit of the GEM tracking-efficiency
monitor.  For each test detector *D* in {0,1,2,3}, the other three
GEMs together with HyCal define a straight-line anchor track; the
anchor is then projected onto *D* and the test asks whether *D*
recorded a hit at the predicted position.  *D* itself contributes
neither to the candidate set nor to the line fit, so the test is
genuinely unbiased.  Three LOO variants are run side by side:

| Variant | Anchor seed | Fit constraint |
|---|---|---|
| `loo` | HyCal cluster + each candidate hit on every other GEM | HyCal + 3 other GEMs (vertex-agnostic). |
| `loo-target-in` | Same as `loo` | HyCal + 3 other GEMs + target as a weighted measurement (σ = `--sigma-target`). |
| `loo-target-seed` | Single seed: target → HyCal cluster | HyCal + 3 other GEMs (target only seeds, never enters the fit). |

The 2026-05 commit fixing the live-monitor's GEM efficiency calculation
used this script as the parity oracle.

### deconv_pileup_demo.py

Walks an EVIO file, finds channel-events that the soft `WaveAnalyzer`
flagged with `Q_PEAK_PILED`, runs the C++ deconvolution
(`fdec::WaveAnalyzer::Deconvolve`) on each one, and writes a small
gallery of before/after PNGs.  The deconvolution core is the same code
that production replay uses when `daq_config.json:nnls_deconv.enabled =
true`; the demo bypasses the master switch and runs given a usable
template.

### fit_pulse_template.py

Per-channel pulse-shape characterisation.  Collects clean, isolated
pulses (single peak, no pile-up flag, no overflow, peak well inside
the buffer), fits each with the two-tau model

```
p(t) = A · (1 − exp(−(t−t₀)/τ_r)) · exp(−(t−t₀)/τ_f)    for t ≥ t₀
```

summarises `(τ_r, τ_f, A, t₀, χ²/dof)` per channel using median + MAD,
and writes one JSON entry per channel — the input to the deconvolution
templates documented in
[`docs/technical_notes/waveform_analysis/wave_analysis.md`](../docs/technical_notes/waveform_analysis/wave_analysis.md).

## Adding a tool

Create `tools/my_tool.cpp`, then add to `CMakeLists.txt`:

```cmake
add_analysis_tool(my_tool tools/my_tool.cpp)
```

The helper takes care of the rest:

- compiles the source into `prad2ana_my_tool` (matching the install
  prefix convention),
- links `libprad2ana.a` (transitively pulling in `prad2dec`,
  `prad2det`, ROOT),
- defines `DATABASE_DIR=...` so install-relative paths resolve,
- routes the binary to `<build>/bin/`.

If the new code is *shared* (callable from multiple tools and from
ACLiC scripts), put the implementation under `src/` and the
declaration under `include/`, then list the new `.cpp` in the
`add_library(prad2ana STATIC ...)` call near the top of
`CMakeLists.txt`.  ACLiC scripts that link `libprad2ana.a` will pick
up the new symbols automatically.

## Contributors

Yuan Li, Weizhi Xiong — Shandong University\
Chao Peng — Argonne National Laboratory
