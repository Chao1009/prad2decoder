# PRad-II HyCal Gain Correction — Technical Reference

This document describes the workflow for monitoring and correcting the
time-dependent gain drift of HyCal PbWO4 crystals in the PRad-II
experiment, covering how the reference gain table is produced, how the
time-series corrections are calculated, and how they are applied during
data replay.

```
   Raw EVIO data
        |
        |--► prad2ana_refGain_produce ────────────────► prad_XXXXXX_LMS.dat
        |       (global reference gain, done once)         (reference gain table)
        |
        └──► prad2ana_replay_gainCorr ───────────────► prad_XXXXXX_gain_corr.root
                (batch-wise time-series corrections)       (time-series corrections)
                                                                   |
                                                                   ▼
                                                    prad2ana_replay_recon
                                                   (corrections applied per event)
```

---

## 1. Physics Background

The light output of HyCal PbWO4 crystals — i.e. the FADC integral
produced by a given energy deposit — drifts slowly over time due to
temperature variations and radiation damage.  Without correction, the
energy calibration of the same crystal will differ between time
intervals, introducing a systematic bias in reconstructed energies.

PRad-II uses two complementary online monitoring signals:

| Source | Name | Characteristic | Purpose |
|---|---|---|---|
| Laser flash | **LMS** (Light Monitoring System) | Illuminates all ~1735 readout channels simultaneously; number of channels with data > 1000 | Track the relative gain drift of each crystal |
| Alpha source | **Alpha source** | Excites only a small number of crystals; channel count 1–50 | Provide an absolute reference point for the LMS reference PMTs |

The LMS reference PMTs respond to both the laser and alpha particles.
By normalising each crystal's LMS peak to the LMS/alpha ratio of the
reference PMT, absolute gains can be reconstructed from purely relative
measurements, eliminating sensitivity to shot-by-shot laser intensity
variations.

### 1.1 Gain Factor

The **gain factor** $g_j$ of a module (for LMS channel $j$) is defined
as the module's mean FADC integral per unit alpha-equivalent energy:

$$
g_j \;\equiv\; \frac{\mu_{W,\,\text{LMS}}}{\mu_{\text{ref},\,j,\,\text{LMS}}}
                \times \mu_{\text{ref},\,j,\,\text{alpha}}
$$

Numerically it has units of ADC counts per alpha event.  It captures
the combined effect of crystal light yield, optical coupling, and
photodetector gain.  A higher $g_j$ means the detector produces more
ADC counts for the same deposited energy — i.e. the gain is larger.

Three independent values $g_1, g_2, g_3$ (one per LMS reference PMT)
are stored per module.  Agreement among the three is a cross-check on
the stability of each LMS PMT; their average is used when applying the
correction.

### 1.2 Correction Factor

When the detector gain drifts between a reference period and the
current data-taking period, the **correction factor** $c_j$ compensates
by rescaling the FADC integral back to the reference level:

$$
c_j \;\equiv\; \frac{g_j^{\text{ref}}}{g_j^{\text{current}}}
$$

Multiplying the raw FADC integral by $c_j$ is equivalent to asking:
"what integral would this event have produced had the detector been
operating at its reference gain?"

The three per-LMS values are averaged into a single scalar applied in
reconstruction:

$$
\bar{c} \;=\; \frac{1}{n_\text{valid}} \sum_{j=1}^{3} c_j
$$

where $n_\text{valid}$ counts only the channels for which both
$g_j^{\text{ref}}$ and $g_j^{\text{current}}$ are non-zero.

| Value | Interpretation |
|---|---|
| $\bar{c} = 1.0$ | Gain unchanged relative to reference |
| $\bar{c} > 1.0$ | Gain has dropped; integral is scaled **up** |
| $\bar{c} < 1.0$ | Gain has risen; integral is scaled **down** |

---

## 2. Reference Gain Table

### 2.1 Tool

```
prad2ana_refGain_produce <evio_file_or_dir> [more files/dirs...]
    [-o output.dat]      default: <db>/gain_factor/ref_gain/prad_XXXXXX_LMS.dat
    [-r hists.root]      default: <db>/gain_factor/ref_gain/prad_XXXXXX_LMS_hists.root
    [-c daq_config.json]
    [-d hycal_map.json]
    [-f max_files]       process at most this many EVIO files
    [-n max_events]      stop after this many physics events
```

Run this once on a stable reference run (good beam conditions, reliable
gain baseline).  The resulting `.dat` file serves as the reference point
for all subsequent correction calculations.

### 2.2 Event Classification

Events are classified by the number of FADC channels that contain
waveform data in a given physics event.  Trigger bits are not used
because they are unreliable in early data.

```
channels > 1000   →  LMS event  (laser illuminates everything)
1 ≤ channels < 50 →  Alpha event (alpha source fires a few crystals)
anything else     →  ignored
```

### 2.3 Histogram Accumulation

For every channel passing the single-peak requirement (`npeaks == 1`),
the FADC waveform integral is filled into the appropriate histogram:

- `mod_lms[W_id]` — integral distribution of PbWO4 module W_id in LMS
  events (1156 histograms, one per module)
- `ref_lms[j]` — LMS reference PMT j in LMS events (j = 0,1,2 for
  LMS1/LMS2/LMS3)
- `ref_alpha[j]` — LMS reference PMT j in alpha events

All histograms span 0–15 000 ADC counts with 600 bins.

### 2.4 Fitting and Gain Factor Definition

After reading all events, each histogram is fit with a truncated
Gaussian (`gain_hist_fitter`, lower bound at 10 % of the peak height).

For each PbWO4 module (1-based W_id), the gain factor for LMS channel j
is defined as:

$$
g_j = \frac{\mu_{W,\,\text{LMS}}}{\mu_{\text{ref},\,j,\,\text{LMS}}}
      \times \mu_{\text{ref},\,j,\,\text{alpha}}
\qquad (j = 1, 2, 3)
$$

where:
- $\mu_{W,\,\text{LMS}}$ — fitted LMS peak position of the module (ADC counts)
- $\mu_{\text{ref},\,j,\,\text{LMS}}$ — fitted LMS peak of reference PMT j
- $\mu_{\text{ref},\,j,\,\text{alpha}}$ — fitted alpha peak of reference PMT j

Intuitively $g_j$ has units of "module ADC counts / alpha ADC counts"
— the alpha signal anchors the laser-intensity scale to an
absolute physical reference.

### 2.5 Output Format (`.dat` file)

```
Name     lms_peak    lms_sigma   lms_chi2/ndf  alpha_peak(g1)  alpha_sigma(g2)  alpha_chi2/ndf(g3)
LMS1     4521.234    ...         ...           ...             ...              ...
LMS2     ...
LMS3     ...
W1       3812.100    82.431      1.023         2048.000        ...              ...
W2       ...
...
```

`g1/g2/g3` are the gain factors for the three LMS channels.
`lms_peak/lms_sigma/lms_chi2/ndf` are the raw LMS fit results kept for
diagnostic purposes.

---

## 3. Time-Series Gain Correction

### 3.1 Tool

```
prad2ana_replay_gainCorr <evio_file_or_dir> [more files/dirs...] -o output_dir
    [-f max_files]       process at most this many EVIO files
    [-j num_threads]     worker threads for Phase 1 (default: 4)
    [-b batch_size]      LMS events per correction batch (default: 2000)
    [-r ref_run]         run number of the reference .dat to use
                         (default: from general.json gain_ref_run field)
    [-c daq_config.json]
    [-d hycal_map.json]
    [-s]                 keep intermediate *_lms.root files (default: delete)
    [-p] [-w id1,id2,…]  write a diagnostic PDF; -w selects extra W modules to plot
```

The tool runs in two phases:

#### Phase 1 — EVIO → intermediate LMS ROOT files (multi-threaded)

Each EVIO file is processed by one thread independently and produces a
`*_lms.root` file containing a TTree `lms_gain`.  Each row corresponds
to one LMS or alpha event and records:

- `event_num`, `event_type` (0 = LMS, 1 = alpha)
- `module_id[]`, `module_type[]`
- `npeaks[]`, `peak_integral[][]` — single-peak FADC integral per channel

#### Phase 2 — batch-wise correction computation (single-threaded)

All `*_lms.root` files are chained with TChain and scanned in sliding
windows of `batch_size` LMS events.  At the end of each window a set of
correction factors is computed and a row is written to the output TTree.

### 3.2 Per-batch Correction Calculation

For each batch of `batch_size` LMS events:

**Step 1** — fit the current reference PMT peaks and form the LMS/alpha ratio:

$$
r_j = \frac{\mu_{\text{ref},j,\,\text{LMS}}^{\text{current}}}
           {\mu_{\text{ref},j,\,\text{alpha}}^{\text{current}}}
\qquad (\texttt{refPMT\_ratio}[j])
$$

**Step 2** — fit each PbWO4 module's current LMS peak $\mu_W^{\text{current}}$
and compute the current gain:

$$
g_j^{\text{current}} = \frac{\mu_W^{\text{current}}}{r_j}
\qquad (\texttt{gain\_W}[W\_\text{id}][j])
$$

**Step 3** — compare with the reference gain to obtain the correction factor:

$$
c_j = \frac{g_j^{\text{ref}}}{g_j^{\text{current}}}
\qquad (\texttt{gain\_corr\_W}[W\_\text{id}][j])
$$

$c_j = 1.0$ means no change; $c_j > 1.0$ means the gain has dropped
(peak shifted to lower ADC values) and the integral must be scaled up
proportionally.

**Step 4** — average over the three LMS channels, skipping any zero entries:

$$
\bar{c} = \frac{1}{n_\text{valid}} \sum_{j} c_j
\qquad (\texttt{gain\_corr\_W}[W\_\text{id}].\texttt{avg})
$$

### 3.3 Output Format (`gain_corr.root`)

TTree `gain_corr`, one row per time batch:

| Branch | Type | Description |
|---|---|---|
| `batch_id` | I | Sequential batch index |
| `event_num_start` | I | Event number of the first LMS event in this batch |
| `event_num_end` | I | Event number of the last LMS event in this batch |
| `n_lms_events` | I | Number of LMS events in this batch |
| `n_alpha_events` | I | Number of alpha events in this batch |
| `ref_run` | I | Run number of the reference gain table used |
| `refPMT_ratio[3]` | F[3] | LMS/alpha ratio of each reference PMT |
| `gain_W[1156][3]` | F[1156][3] | Current gain of each PbWO4 module (three LMS channels) |
| `gain_W_ref[1156][3]` | F[1156][3] | Reference gain for the same modules |
| `gain_corr_W[1156][3]` | F[1156][3] | Correction factor = ref / current |
| `fit_mean_ref_lms[3]` | F[3] | Reference PMT LMS peak positions (diagnostic) |
| `fit_mean_ref_alpha[3]` | F[3] | Reference PMT alpha peak positions (diagnostic) |
| `fit_mean_W_lms[1156]` | F[1156] | Per-module LMS peak positions (diagnostic) |

Output file: `<db>/gain_factor/gain_correction/prad_XXXXXX_gain_corr.root`

---

## 4. Applying Corrections During Replay

### 4.1 Automatic Triggering

Both `prad2ana_replay_rawdata` and `prad2ana_replay_recon` check for the
existence of the gain correction file immediately after loading
`RunConfig`.  If no matching file is found, they spawn
`prad2ana_replay_gainCorr` as a child process via `fork + execvp`,
wait for it to finish, and then continue the replay.  Manual execution
of `replay_gainCorr` is therefore not required under normal conditions.

### 4.2 Loading the Time Series

Each worker thread loads the corrections before entering the event loop:

```cpp
auto gain_corr_ts = prad2::LoadGainCorrTimeSeries(
    gRunConfig.gain_data_dir + "/gain_correction", run_num);
```

`LoadGainCorrTimeSeries` opens the matching `gain_corr.root`, reads all
batches into memory, and constructs a `GainCorrTimeSeries` — a vector of
`Batch` objects sorted ascending by `event_num_start`.  Once
constructed, the object is read-only and safe to access from multiple
threads concurrently (requires `ROOT::EnableThreadSafety()` to have been
called before spawning worker threads).

**File selection rule**: the directory is scanned for all
`prad_XXXXXX_gain_corr.root` files; the one with the largest run number
that is **≤ the current run number** is selected.  When `run_num < 0`,
the latest file is used.

### 4.3 Per-event Lookup

At the start of each physics event:

```cpp
const auto &gain_corr = gain_corr_ts.GetCorr(static_cast<int>(ev->event_num));
```

`GetCorr(event_num)` performs a reverse linear scan to find the last
batch whose `event_num_start <= event_num` and returns its
`GainCorrTable`.  If `event_num` precedes all batches, the first batch
is returned.

### 4.4 Application in `ProcessWithRecon`

While decoding FADC data, for each PbWO4 or PbGlass module:

```cpp
// mod->id > 1000 → PbWO4 (W module);  mod->id ≤ 900 → PbGlass (G module)
const float gain = (mod->id > 1000)
    ? gain_corr.w[mod->id - 1000].avg
    : gain_corr.g[mod->id].avg;

float adc    = wres.peaks[p].integral * gain;          // gain-corrected integral
float energy = static_cast<float>(mod->energize(adc)); // convert to MeV
```

The FADC integral is multiplied by `avg = ref_gain / current_gain` before
being passed to the HyCal calibration function `energize()`.  PbGlass
has the same interface but its correction entries default to 1.0 (not yet
stored in `gain_corr.root`).

In `Process` (raw replay), the per-channel correction factor is stored in
the `ev->gain_factor[nch]` branch for use in downstream analysis.

---

## 5. File Layout

```
<db>/gain_factor/
    ref_gain/
        prad_XXXXXX_LMS.dat           ← refGain_produce output (reference gain table)
        prad_XXXXXX_LMS_hists.root    ← raw histograms before fitting (diagnostic)
    gain_correction/
        prad_XXXXXX_gain_corr.root    ← replay_gainCorr output (time-series corrections)
```

`<db>` defaults to the installed `database/` directory.  It can be
overridden with the `PRAD2_DATABASE_DIR` environment variable or via the
`gain_data_dir` field in the `RunInfoConfig` JSON; if that field is
empty it is automatically set to `<db>/gain_factor`.

---

## 6. Step-by-step Walkthrough

### Step 0 — Set up the environment

```bash
source /home/clasrun/prad2_daq/prad2_env.csh
echo $PRAD2_DATABASE_DIR   # verify the database path
```

### Step 1 — Produce the reference gain table (once per run period)

Pick a run with stable conditions (e.g. run 024246):

```bash
prad2ana_refGain_produce /data/evio/prad_024246 \
    -f 5      # use the first 5 EVIO files — sufficient statistics
```

Output is written automatically to
`<db>/gain_factor/ref_gain/prad_024246_LMS.dat`.

Inspect the fit histograms:

```bash
root -l -q -e '
TFile *f = TFile::Open("REPLACE_WITH_PATH/prad_024246_LMS_hists.root");
f->ls();
TH1F *h = (TH1F*)f->Get("ref/ref_lms_1");
h->Draw();
'
```

### Step 2 — Compute time-series gain corrections

```bash
mkdir -p /tmp/lms_work
prad2ana_replay_gainCorr /data/evio/prad_024327 \
    -o /tmp/lms_work   \   # directory for intermediate files
    -b 2000            \   # 2000 LMS events per batch
    -r 24246           \   # use run 024246 as the reference
    -j 8                   # 8 threads for Phase 1
```

Output is written to
`<db>/gain_factor/gain_correction/prad_024327_gain_corr.root`.

Inspect the batch table:

```bash
root -l -q -e '
TFile *f = TFile::Open("REPLACE_WITH_PATH/prad_024327_gain_corr.root");
TTree *t = (TTree*)f->Get("gain_corr");
t->Print();
// correction factor for W565 (index 564) vs. event number:
t->Draw("gain_corr_W[564][0]:event_num_start", "", "L");
'
```

Generate a diagnostic PDF showing per-batch histograms (optional):

```bash
prad2ana_replay_gainCorr /data/evio/prad_024327 \
    -o /tmp/lms_work -b 2000 -r 24246 \
    -p -w 1,565,892   # also show histograms for W1, W565, W892
```

### Step 3 — Replay with corrections applied automatically

Once steps 1 and 2 are complete, run the normal replay — corrections
are loaded and applied event-by-event without any extra flags:

```bash
mkdir -p /data/replay_recon/prad_024327
prad2ana_replay_recon /data/evio/prad_024327 \
    -o /data/replay_recon/prad_024327 \
    -j 8
```

If `gain_corr.root` is absent, the replay will run `replay_gainCorr`
automatically before proceeding.

### Step 4 — Verify the corrections

The most physically meaningful check is the pi0 invariant mass peak
position and width before and after correction.  A quick numerical
check reads the correction time series directly:

```python
import uproot, numpy as np, matplotlib.pyplot as plt

f = uproot.open("prad_024327_gain_corr.root")
t = f["gain_corr"]

ev_start  = t["event_num_start"].array(library="np")
corr_W565 = t["gain_corr_W"].array(library="np")[:, 564, 0]   # W565, LMS1

plt.plot(ev_start, corr_W565, "-o", ms=3)
plt.axhline(1.0, ls="--", color="gray")
plt.xlabel("event_num_start")
plt.ylabel("gain_corr  (W565, LMS1)")
plt.title("Gain correction time series — Run 024327")
plt.tight_layout()
plt.savefig("gain_corr_W565.png")
```

Correction factors should vary slowly around 1.0 (normal drift is
typically within a few percent).  A sudden jump or a large number of
zero entries indicates a failed fit for that batch — inspect the
histograms with `-s` (keep intermediate files) or `-p` (generate PDF).

---

## 7. Troubleshooting

### All correction factors are 1.0 / file not found

- Check that `<db>/gain_factor/ref_gain/` contains a `.dat` file whose
  run number is **<= the current run number**.
- Verify that `gRunConfig.gain_data_dir` points to the correct
  `<db>/gain_factor` directory.
- Specify the reference run explicitly with `-r`.

### Many batches have `gain_corr_W` = 0

Fit failure (empty histogram or unconverged peak).  Likely causes:

- Insufficient LMS statistics — reduce `-b` or increase `-f` to process
  more files.
- The module has no valid single-peak events in this batch (dead channel
  or excessive noise).
- The reference PMT alpha signal is absent (`refPMT_ratio = 0` causes
  the whole batch gain to be zero).

### PbGlass (G modules) correction

`gain_corr.root` currently stores only PbWO4 (W module) correction
factors; G module entries default to `avg = 1.0`.  To add PbGlass
corrections, accumulate G module histograms in Phase 2 of
`replay_gainCorr` and write the corresponding branch.

### Loading correction factors in analysis code

```cpp
#include "gain_factor.h"

// One-time initialisation (single-threaded):
auto ts = prad2::LoadGainCorrTimeSeries(
    db_dir + "/gain_factor/gain_correction", run_num);

// Event loop (read-only — safe from multiple threads after init):
const auto &corr = ts.GetCorr(event_num);
float corrected_adc = raw_integral * corr.w[w_module_id].avg;  // w_module_id: 1-based
```
