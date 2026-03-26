# Test & Diagnostic Tools

## evio_dump

EVIO file structure diagnostic tool.

```bash
evio_dump <file> [options]
```

| Option | Description |
|--------|-------------|
| `-m <mode>` | Analysis mode (default: summary) |
| `-n <N>` | Event count (tree mode) or event number (event mode) |
| `-D <file>` | Load DAQ configuration (for PRad etc.) |

Modes:

| Mode | Description |
|------|-------------|
| (default) | Summary: count events by tag |
| `tree` | Print bank tree for first N events (default N=5) |
| `tags` | List all unique bank tags with stats |
| `epics` | Dump all EPICS event text |
| `event` | Detailed dump of record N (1-based) |
| `triggers` | List trigger info for all events |

Examples:
```bash
evio_dump data.evio                                # event tag summary
evio_dump data.evio -m tree -n 10                  # bank tree for 10 events
evio_dump data.evio -m tags                        # all unique tags
evio_dump data.evio -m triggers -D prad_daq.json   # trigger bits (PRad config)
evio_dump data.evio -m event -n 100                # detailed dump of event 100
```

## evc_test

Basic smoke-test for the evc library. Reads EVIO buffers, decodes events, or connects to ET.

```bash
evc_test <file> [options]
```

| Option | Description |
|--------|-------------|
| `-m <mode>` | Mode: `scan` or `et` (default: read buffers) |
| `-s <N>` | Start event for scan mode (default: 1) |
| `-n <N>` | Number of events for scan mode (default: 50) |
| `-H <host>` | ET host (default: localhost) |
| `-P <port>` | ET port (default: 11111) |
| `-f <file>` | ET system file |
| `-S <station>` | ET station name |

Examples:
```bash
evc_test data.evio                                 # read and count buffers
evc_test data.evio -m scan -s 100 -n 20            # scan events 100-119
evc_test -m et -H localhost -P 11111 -f /tmp/et -S mon  # read from ET
```

## et_feeder

Replays an EVIO file into an ET system event-by-event at a configurable rate.

```bash
et_feeder <evio_file> [options]
```

| Option | Description |
|--------|-------------|
| `-h` | ET host (default: localhost) |
| `-p` | ET port (default: 11111) |
| `-f` | ET system file (default: /tmp/et_feeder) |
| `-i` | Interval between events in ms (default: 100) |
| `-s` | Start event number, 1-based (default: 1) |
| `-n` | Number of events to feed (default: all) |

Examples:
```bash
et_feeder data.evio -f /tmp/et_sys_prad -i 50       # feed all at 20 Hz
et_feeder data.evio -f /tmp/et_sys -s 1000 -n 500   # feed events 1000-1499
```

## evchan_test

Reads an EVIO file from both an ET station and disk simultaneously, printing word-by-word comparison. Used to verify ET transport fidelity.

```bash
evchan_test <evio_file> [-h host] [-p port] [-f et_file] [-i interval_ms]
```

Options are the same as `et_feeder`.

## ped_calc

Compute per-channel pedestals from EVIO data by selecting events with a specific trigger bit.

```bash
ped_calc <evio_file> -D <daq_config.json> [options]
```

| Option | Description |
|--------|-------------|
| `-D <file>` | DAQ configuration (required for PRad) |
| `-t <bit>` | Trigger bit to select (default: 3 = LMS_Alpha for PRad) |
| `-o <file>` | Output JSON file (default: pedestals_out.json) |
| `-n <N>` | Max events to process (default: all) |

Trigger bits (PRad):
- 0 = PHYS_LeadGlassSum (0x01)
- 1 = PHYS_TotalSum (0x02)
- 2 = LMS_Led (0x04)
- 3 = LMS_Alpha / pedestal (0x08)

Example:
```bash
ped_calc prad.evio -D prad_daq_config.json -t 3 -o pedestals.json
```

## gem_dump

GEM data diagnostic tool. Decodes SSP/MPD banks from EVIO data, optionally runs GEM reconstruction, and prints diagnostic output at each stage of the pipeline.

```bash
gem_dump <evio_file> -D <daq_config.json> [options]
```

| Option | Description |
|--------|-------------|
| `-D <file>` | DAQ configuration (required) |
| `-G <file>` | GEM map file (default: gem_map.json from DAQ config dir) |
| `-P <file>` | GEM pedestal file (optional) |
| `-m <mode>` | Output mode (see below) |
| `-n <N>` | Max physics events (default: 10, 0=all) |
| `-t <bit>` | Trigger bit filter (-1=all, default) |
| `-e <N>` | Dump only physics event N (1-based) |

Modes:
- `raw` — Dump raw SSP-decoded APV data (strips × time samples)
- `hits` — Strip hits after pedestal subtraction, CM correction, zero suppression
- `clusters` — Full reconstruction: 1D clusters + 2D GEM hits
- `summary` (default) — Per-event statistics table

Examples:
```bash
# Quick summary of first 10 events with GEM data
gem_dump data.evio -D database/daq_config.json

# Dump raw APV data for event 42
gem_dump data.evio -D database/daq_config.json -m raw -e 42

# Full reconstruction with pedestals
gem_dump data.evio -D database/daq_config.json -G database/gem_map.json -P gem_ped.dat -m clusters -n 50

# Summary with trigger filter (LMS events only)
gem_dump data.evio -D database/daq_config.json -t 3 -n 0
```
