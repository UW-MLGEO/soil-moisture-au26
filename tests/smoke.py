"""End-to-end smoke run on the synthetic 2-station selection (no downloads, < 1 min).

    pixi run smoke

Builds synthetic caches (tests/smoke_fixture.py) in a temporary folder, then runs
the real scripts exactly as documented: homogenize -> inspect -> export to
drydown-q (twice for homogenize, to check that every stage is reused), and checks
a few numbers the fixture fixes. Prints one summary line.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

from smoke_fixture import build

REPO = Path(__file__).resolve().parents[1]


def run(*args):
    result = subprocess.run([sys.executable, *map(str, args)], cwd=REPO, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f'{args[0]} failed:\n{result.stdout}\n{result.stderr}')
    return result.stdout + result.stderr


with tempfile.TemporaryDirectory() as folder:
    paths, locks = build(folder)
    common = ['--run', 'smoke', '--paths', paths]
    run(REPO / 'scripts/homogenize.py', *common, '--locks-dir', locks)
    second = run(REPO / 'scripts/homogenize.py', *common, '--locks-dir', locks)
    assert second.count('reused') == 5, second
    inspected = run(REPO / 'scripts/inspect_tables.py', *common)
    run(REPO / 'scripts/export_drydown_q.py', *common)
    output = Path(folder) / 'output'
    manifest = json.loads((output / 'runs/smoke/manifest.json').read_text())
    sensor_day = pd.concat(pd.read_parquet(p) for p in (output / manifest['stages']['assemble']['folder'] /
                                                         'sensor_day').glob('*.parquet'))
    reasons = sensor_day.theta_m3m3__missing_reason.value_counts().to_dict()
    assert reasons == {'available': 38, 'no_observations': 3, 'quality_masked': 1, 'missing_values': 1}, reasons
    summary = json.loads((output / 'runs/smoke/inspection_summary.json').read_text())
    # Flatlines: ProbeA 3 days at 0.30; ProbeB 0.25 after its NaN and masked day; ProbeC 0.15 on both sides of its gap.
    found = (summary['flatline_candidate_runs'], summary['chirps_extreme_station_days'])
    assert found == (4, 1), found
    exported = pd.read_parquet(output / 'drydown_q/smoke/station_day.parquet')
    assert len(exported) == len(sensor_day) and exported.igbp_class.isin(['GRA', '']).all()
print(f'Smoke test passed: {len(sensor_day)} sensor-days from 2 stations; missing reasons {reasons}; '
      f'stages reused on rerun; inspection and drydown-q export written.')
