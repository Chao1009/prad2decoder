# Scripts

Python utilities for detector visualization. Requires matplotlib, numpy, and pyepics (for scaler map).

## hycal_scaler_map.py

Live colour-coded HyCal FADC scaler map. Polls `B_DET_HYCAL_FADC_<name>` EPICS channels every 10 s.

```bash
python scripts/hycal_scaler_map.py          # real EPICS (default)
python scripts/hycal_scaler_map.py --sim    # simulation (random values)
```

## gem_layout.py

Visualize GEM strip layout from `gem_map.json`.

```bash
python scripts/gem_layout.py [gem_map.json]
```

## gem_cluster_view.py

Visualize GEM clustering from `gem_dump -m evdump` JSON output.

```bash
python scripts/gem_cluster_view.py <event.json> [gem_map.json] [--det N] [-o file.png]
```
