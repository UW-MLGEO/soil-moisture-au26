"""Stage 6: export one homogenize run as drydown-q's `sensors` and `station_day` tables.

    pixi run export-drydown-q                     # baseline run
    pixi run python scripts/export_drydown_q.py --run pilot_arm

Writes sensors.parquet, station_day.parquet and export_manifest.json to
paths.drydown_q_prepared_dir (default <outputs_dir>/drydown_q/<run>/); point
drydown-q's `paths.prepared_dir` there and skip its `prepare` step. Columns we do
not have yet (PET, tree cover, dPET cell) are NaN/False and listed in the
manifest. Rules: config/pipeline.yml `export_drydown_q:`. Offline.
"""

import argparse
import json
from pathlib import Path

import pandas as pd

from sm_drydown.cache import Stage
from sm_drydown.config import as_dict, load_config
from sm_drydown.export import MISSING_FEATURES, export_network
from sm_drydown.storage.files import sha256, utc_now, write_json

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--run', default='baseline')
parser.add_argument('--paths', default=None)
args = parser.parse_args()
cfg = load_config(args.run, args.paths)
run_dir = Path(cfg.paths.outputs_dir) / 'runs' / cfg.run_name
manifest = json.loads((run_dir / 'manifest.json').read_text())
stages = {name: Stage(cfg.paths.outputs_dir, s['stage'], s['key']) for name, s in manifest['stages'].items()}
target = Path(cfg.paths.drydown_q_prepared_dir or Path(cfg.paths.outputs_dir) / 'drydown_q' / cfg.run_name)
target.mkdir(parents=True, exist_ok=True)

sensors, days = [], []
for network in manifest['networks']:
    s, d = export_network(cfg, stages['assemble'].read('sensor_day', [network]),
                          stages['assemble'].read('stations', [network]),
                          stages['assemble'].read('sensors', [network]),
                          stages['chirps'].read('precip_chirps_mm_day', [network]))
    sensors.append(s)
    days.append(d)
    print(f'{network}: {len(s)} sensors, {len(d)} sensor-days', flush=True)
pd.concat(sensors, ignore_index=True).to_parquet(target / 'sensors.parquet', index=False)
pd.concat(days, ignore_index=True).to_parquet(target / 'station_day.parquet', index=False)
write_json(target / 'export_manifest.json', {
    'created_at': utc_now(), 'run': cfg.run_name, 'run_manifest_sha256': sha256(run_dir / 'manifest.json'),
    'rules': as_dict(cfg.export_drydown_q), 'lai_product': cfg.modis_lai.collection,
    'texture_source': 'GLDAS FAO-16 class and sand/silt/clay fractions (not ISMN metadata)',
    'not_available_written_as_nan': list(MISSING_FEATURES) + ['dpet_offset_km', 'dpet_cell_is_land=False'],
    'files': {name: sha256(target / name) for name in ('sensors.parquet', 'station_day.parquet')}})
print(f'Wrote drydown-q tables to {target}')
