"""Stage 4: inspect one homogenize run (candidate problems and distributions; never edits data).

    pixi run inspect                          # baseline run
    pixi run python scripts/inspect_tables.py --run pilot_arm --figure-dir output/runs/pilot_arm/figures

Reads the feature and assembled tables listed in <outputs_dir>/runs/<run>/manifest.json
(run `pixi run homogenize` first). Thresholds: config/pipeline.yml `inspection:`.
Writes CSVs and summary.json to <outputs_dir>/cache/inspect/<key>/ (and a copy of
summary.json, with this stage's key, to the run folder) and the two report figures to paths.figure_dir
(docs/figures by default, so the report can commit them). Offline.
"""

import argparse
import json
import shutil
from pathlib import Path

from sm_drydown.cache import Stage, stage_key
from sm_drydown.config import code_hash, load_config
from sm_drydown.features import GROUPS
from sm_drydown.inspection.workflow import inspect
from sm_drydown.selections import load_selection
from sm_drydown.storage.files import write_json

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--run', default='baseline')
parser.add_argument('--paths', default=None)
parser.add_argument('--figure-dir', type=Path, default=None, help='default: paths.figure_dir')
args = parser.parse_args()
cfg = load_config(args.run, args.paths)
run_dir = Path(cfg.paths.outputs_dir) / 'runs' / cfg.run_name
manifest = json.loads((run_dir / 'manifest.json').read_text())
stages = {name: Stage(cfg.paths.outputs_dir, s['stage'], s['key']) for name, s in manifest['stages'].items()}
owner = {table: group for group in GROUPS for table in GROUPS[group].tables}
owner.update(stations='assemble', sensors='assemble', sensor_day='assemble')

selection = load_selection(cfg.selection.name)
stations = selection.subset(cfg.subset.networks, cfg.subset.station_ids)
networks = manifest['networks']
by_network = {n: [s for s in stations if s['network'] == n] for n in networks}
key = stage_key('inspect', cfg.inspection, cfg.gldas, {n: s['key'] for n, s in manifest['stages'].items()},
                code_hash('inspection'))
stage = Stage(cfg.paths.outputs_dir, 'inspect', key)
figures = args.figure_dir or Path(cfg.paths.figure_dir)
summary = inspect(cfg, lambda table, network: stages[owner[table]].read(table, [network]), networks, by_network,
                  stage.folder, figures)
summary['inputs'] = {'run_manifest': manifest['stages'], 'figure_dir': str(figures)}
summary['stage'] = {'stage': 'inspect', 'key': key, 'folder': f'cache/inspect/{key}'}
write_json(stage.folder / 'summary.json', summary)
shutil.copy2(stage.folder / 'summary.json', run_dir / 'inspection_summary.json')
print(f"Inspected {summary['stations']} stations: {summary['flatline_candidate_runs']} flatline runs, "
      f"{summary['chirps_extreme_station_days']} CHIRPS extremes, {summary['chirps_negative_station_days']} negatives, "
      f"GLDAS failures {summary['gldas']}. Outputs: {stage.folder}; figures: {figures}")
print('Inspection complete: no source data changed or measurements filled.')
