# Test & Diagnostic Tools

Generic EVIO diagnostic tools.  GEM-specific tools live in [`gem/`](../gem/README.md).

## Installed tools

### evio_dump

EVIO file structure inspector.

```bash
evio_dump <file> [-m mode] [-n N] [-D daq_config.json]
```

Modes: (default) summary by tag, `tree`, `tags`, `epics`, `event` (detail for event N), `triggers`.

### ped_calc

Compute HyCal per-channel pedestals from trigger-selected events.

```bash
ped_calc <evio_file> -D <daq_config.json> [-t bit] [-o file.json] [-n N]
```

Default trigger bit 3 (LMS_Alpha/pedestal for PRad).

## Dev-only tools (not installed) — sources under `test/dev/`

Built alongside the installed tools but kept out of the install tree; useful
during development from a build tree only.

- **ts_dump** — dump TI timestamp + trigger info per event.
  ```bash
  ts_dump <file> [-n max_events] [-D daq_config.json]
  ```
- **livetime** — DAQ live-time calculator (DSC2 scalers + pulser).  Accepts
  a single file, a base name (auto-finds `.00000`, `.00001`, …), or a
  directory.
  ```bash
  livetime <input> [-D daq_config.json] [-f freq_hz] [-t interval_sec]
  ```

### ET-specific dev tools (`-DWITH_ET=ON`)

- **evc_scan** — three-mode smoketest (read evio buffers, scan with detail,
  or connect to an ET station).
- **et_feeder** — replay an evio file into an ET ring at a controlled rate.
- **evet_diff** — read an evio file and an ET ring in parallel and diff
  their raw buffers; pairs with `et_feeder`.
