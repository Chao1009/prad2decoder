# PRad2 Decoder

FADC250 waveform decoder and event viewer for PRad-II at Jefferson Lab.

Reads CODA evio files, decodes composite data banks (`c,i,l,N(c,Ns)` format), performs waveform analysis (pedestal, peak finding, integration), and provides a web-based event display with geometry view, histograms, and occupancy maps.

## Building

```bash
mkdir build && cd build
cmake ..
make -j$(nproc)
```

Requires CMake ≥ 3.14 and a C++17 compiler. Dependencies (`evio`, `et`, `nlohmann/json`, `websocketpp`, `asio`) are fetched automatically. For prebuilt CODA libraries:

```bash
cmake .. -DEVIO_SOURCE=prebuilt -DET_SOURCE=prebuilt
```

## Usage

### Command-line test

```bash
./bin/evc_test data.evio         # channel hit counts (JSON)
./bin/evc_test data.evio -v      # print all waveforms
./bin/evc_test data.evio -t      # print bank tree
```

### Event viewer (file mode)

```bash
./bin/evc_viewer data.evio [port] [--hist] [--data-dir /path/to/data]
```

| Flag | Description |
|------|-------------|
| `--hist` | One-time pass to build per-channel histograms and occupancy |
| `--hist config.json` | Use custom histogram config (default: `database/hist_config.json`) |
| `--data-dir /path` | Enable in-browser file picker, sandboxed to this directory |

The viewer auto-discovers `database/*.json` and `resources/viewer.*` via compile-time `DATABASE_DIR` / `RESOURCE_DIR` paths.

#### GUI features

- **Geometry view** (left panel): HyCal modules colored by selectable metric with editable min/max range. Metrics: Peak Integral, Peak Height, Peak Time, Pedestal, Occupancy, Occupancy (time cut). Color range auto-syncs with histogram config for integral and time metrics.
- **Histograms** (right, top): per-channel integral histogram (time-cut) and peak position histogram. Titles show entries, underflow, overflow counts.
- **Waveform** (right, bottom-right): Plotly interactive plot with colored peak regions, shaded integral area, pedestal/threshold lines.
- **Peaks table** (right, bottom-left): per-peak position, time, height, integral, range, overflow flag.
- **File browser** (`--data-dir`): 📂 Open button opens a file picker with filter. "Process histograms" checkbox remembers last choice. Background loading with progress bar.
- **Navigation**: arrow keys, direct event number input.
- All panel dividers are adjustable by dragging.

### Online monitor (ET mode)

```bash
./bin/evc_monitor [port] [--config online_config.json]
```

Connects to an ET system and monitors events in real time. Same GUI as the file viewer, with additional controls:

- ET connection status indicator (green/red)
- Ring buffer dropdown to select recent events (default 20, configurable)
- **Clear Hist** button to reset all histograms and occupancy
- Auto-follows latest event; press **F** to resume after manual selection
- WebSocket push for live updates, throttled for performance

### Configuration files

`database/hist_config.json` (file viewer):
```json
{
    "hist": {
        "time_min": 170, "time_max": 190,
        "bin_min": 0, "bin_max": 20000, "bin_step": 100,
        "threshold": 3.0,
        "pos_min": 0, "pos_max": 400, "pos_step": 4
    }
}
```

`database/online_config.json` (monitor):
```json
{
    "et": { "host": "localhost", "port": 11111,
            "et_file": "/tmp/et_sys_prad2", "station": "prad2_monitor" },
    "ring_buffer_size": 20,
    "hist": { "...same fields as above..." }
}
```

| Field | Description |
|-------|-------------|
| `time_min`, `time_max` | Peak time window (ns) for integral histogram and time-cut occupancy |
| `bin_min`, `bin_max`, `bin_step` | Integral histogram range and bin width |
| `pos_min`, `pos_max`, `pos_step` | Peak position histogram range (ns) and bin width |
| `threshold` | Minimum peak height (ADC above pedestal) for histogram/occupancy counting |

## Project Structure

```
CMakeLists.txt
database/
    daq_map.json              DAQ channel map (crate/slot/channel → module name)
    hycal_modules.json        Module geometry (name, type, position, size)
    hist_config.json          Histogram/occupancy config for file viewer
    online_config.json        ET connection + histogram config for monitor
prad2dec/                     Static library: libprad2dec.a
    include/
        EvStruct.h  EvChannel.h  EtChannel.h  EtConfigWrapper.h
        Fadc250Data.h  Fadc250Decoder.h  WaveAnalyzer.h
    src/
        EvChannel.cpp  EtChannel.cpp  Fadc250Decoder.cpp  WaveAnalyzer.cpp
resources/
    viewer.html               HTML structure
    viewer.css                Styles
    viewer.js                 Application logic (Plotly.js + Canvas)
src/
    evc_viewer.cpp            File viewer: HTTP server + evio decoder
    evc_monitor.cpp           Online monitor: ET reader + WebSocket push
test/
    test_main.cpp             CLI test tool
```

## Data Format

Evio composite bank tag `0xe101`, format `c,i,l,N(c,Ns)` — packed native LE:

| Field | Type | Description |
|-------|------|-------------|
| `c` | uint8 | Slot number (3–20) |
| `i` | int32 | Event number |
| `l` | int64 | 48-bit timestamp |
| `N` | uint32 | Number of channels fired |
| `c` | uint8 | Channel number (0–15) |
| `N` | uint32 | Number of samples |
| `s` | int16[] | ADC sample values |

ROC bank tags: `0x80`–`0x8c` (adchycal1–7). Slots repeat back-to-back within one ROC's composite payload.

## Library API

```cpp
#include "EvChannel.h"
#include "Fadc250Data.h"
#include "WaveAnalyzer.h"

evc::EvChannel ch;
ch.Open("data.evio");

fdec::EventData event;
fdec::WaveAnalyzer ana;
fdec::WaveResult wres;

while (ch.Read() == evc::status::success) {
    if (!ch.Scan()) continue;
    for (int i = 0; i < ch.GetNEvents(); ++i) {
        ch.DecodeEvent(i, event);
        for (int r = 0; r < event.nrocs; ++r) {
            auto &roc = event.rocs[r];
            for (int s = 0; s < fdec::MAX_SLOTS; ++s) {
                if (!roc.slots[s].present) continue;
                for (int c = 0; c < fdec::MAX_CHANNELS; ++c) {
                    if (!(roc.slots[s].channel_mask & (1u << c))) continue;
                    auto &cd = roc.slots[s].channels[c];
                    ana.Analyze(cd.samples, cd.nsamples, wres);
                    // wres.ped.mean, wres.peaks[0].height, ...
                }
            }
        }
    }
}
```
