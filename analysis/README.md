# Offline Analysis Tools

Replay and physics analysis for PRad2. **Requires ROOT 6.0+.**

```bash
cmake -B build -DBUILD_ANALYSIS=ON
cmake --build build -j$(nproc)
```

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

### LMS / Alpha Normalization

**scripts/lms_alpha_normalize.C** — ACLiC macro that scans a run's EVIO files (`prad_{run}.evio.00000`–`99999`), selects only **LMS** (bit 24) and **Alpha** (bit 25) trigger events via `trigger_bits`, and normalizes HyCal LMS signals using the Alpha source as a gain reference.

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
# from the build directory, after loading rootlogon:
root -l scripts/rootlogon.C
.x scripts/lms_alpha_normalize.C+("/path/to/data", 1234)

# or as a one-liner:
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

Shared sources (`Replay.cpp`, `PhysicsTools.cpp`, `MatchingTools.cpp`) and dependencies linked automatically.

## Contributors
Yuan Li, Weizhi Xiong — Shandong University
Chao Peng - Argonne National Laboratory
