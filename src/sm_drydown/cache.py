"""Content-addressed stage outputs, partitioned by ISMN network.

A stage writes ``<outputs_dir>/cache/<stage>/<key>/<table>/<partition>.parquet``.
The key is the SHA-256 of the stage's config sections, its upstream keys and
request hashes, and its code, so a stage is recomputed exactly when one of those
changes; anything else is reused. ``_complete/<partition>.json`` is written last
and records the partition's input hashes (e.g. the network's ISMN archive) and
the SHA-256 of every file it wrote; a partition without a matching marker is
rebuilt, so an interrupted run resumes where it stopped.

Every config or code change creates a new key, so old key folders accumulate.
``cache_usage`` sorts them into those a run record still names and those it does
not (``scripts/clean_cache.py`` removes the latter).
"""

import json
import re
from pathlib import Path
from urllib.parse import quote

import pyarrow as pa
import pyarrow.parquet as pq

from sm_drydown.config import config_hash
from sm_drydown.schemas import assert_schema
from sm_drydown.storage.files import sha256, utc_now, write_json


def stage_key(stage, *parts):
    return config_hash(stage, *parts)


class Stage:
    def __init__(self, outputs_dir, stage, key):
        self.stage, self.key = stage, key
        self.folder = Path(outputs_dir) / 'cache' / stage / key

    def _marker(self, partition):
        return self.folder / '_complete' / f'{quote(partition, safe="")}.json'

    def done(self, partition, inputs=None):
        """True if ``partition`` is complete for these inputs and its files are unchanged."""
        marker = self._marker(partition)
        if not marker.exists():
            return False
        record = json.loads(marker.read_text())
        if record['inputs'] != (inputs or {}):
            return False
        return all((self.folder / f).exists() and (self.folder / f).stat().st_size == meta['bytes']
                   for f, meta in record['files'].items())

    def write(self, partition, tables, inputs=None, metadata=None):
        """Write each table (schema-checked) for one partition, then its completion marker."""
        files = {}
        for name, table in tables.items():
            if table is None:
                continue
            assert_schema(table, name)
            path = self.folder / name / f'{quote(partition, safe="")}.parquet'
            path.parent.mkdir(parents=True, exist_ok=True)
            arrow = pa.Table.from_pandas(table, preserve_index=False)
            arrow = arrow.replace_schema_metadata({**(arrow.schema.metadata or {}),
                                                   b'stage': self.stage.encode(), b'key': self.key.encode()})
            temporary = path.with_suffix('.parquet.part')
            pq.write_table(arrow, temporary, compression='zstd')
            temporary.replace(path)
            files[path.relative_to(self.folder).as_posix()] = {'sha256': sha256(path), 'bytes': path.stat().st_size,
                                                               'rows': len(table)}
        record = {'stage': self.stage, 'key': self.key, 'partition': partition, 'inputs': inputs or {},
                  'files': files, 'written_at': utc_now(), **(metadata or {})}
        write_json(self._marker(partition), record)
        return record

    def markers(self):
        return {json.loads(p.read_text())['partition']: json.loads(p.read_text())
                for p in sorted((self.folder / '_complete').glob('*.json'))}

    def table_files(self, name, partitions=None):
        names = None if partitions is None else {quote(p, safe='') for p in partitions}
        return [p for p in sorted((self.folder / name).glob('*.parquet')) if names is None or p.stem in names]

    def read(self, name, partitions=None, columns=None):
        """One table across partitions (all, or the listed ones)."""
        files = self.table_files(name, partitions)
        if not files:
            return None
        return pa.concat_tables([pq.read_table(f, columns=columns) for f in files],
                                promote_options='default').to_pandas()


KEY_NAME = re.compile(r'[0-9a-f]{64}')


def _newest_mtime(folder):
    return max([folder.stat().st_mtime, *(p.stat().st_mtime for p in folder.rglob('*'))])


def run_records(outputs_dir):
    """{cache folder relative to outputs_dir: [runs]} named by run records, newest record time, runs lacking an inspect key.

    Run records are ``runs/<run>/manifest.json`` (homogenize: feature and assemble
    keys) and ``runs/<run>/inspection_summary.json`` (inspect key, under ``stage``).
    """
    used, times, unrecorded = {}, [], []
    for manifest in sorted((Path(outputs_dir) / 'runs').glob('*/manifest.json')):
        run, summary = manifest.parent.name, manifest.parent / 'inspection_summary.json'
        folders = [s['folder'] for s in json.loads(manifest.read_text())['stages'].values()]
        times.append(manifest.stat().st_mtime)
        if summary.exists():
            times.append(summary.stat().st_mtime)
            stage = json.loads(summary.read_text()).get('stage')
            if stage:
                folders.append(stage['folder'])
            else:
                unrecorded.append(run)
        for folder in folders:
            used.setdefault(folder, []).append(run)
    return used, max(times, default=None), unrecorded


def cache_usage(outputs_dir):
    """One entry per ``cache/<stage>/<key>/`` folder: (path, stage, status, reason, bytes).

    status is ``in_use`` (a run record names it), ``kept`` (not named, but written
    after the newest run record, so it may belong to a run in progress; or an inspect
    key while some run's inspection summary predates key recording) or ``unused``.
    With no run records at all, every folder is ``kept``.
    """
    outputs = Path(outputs_dir)
    used, newest, unrecorded = run_records(outputs)
    entries = []
    for folder in sorted(p for p in (outputs / 'cache').rglob('*') if p.is_dir() and KEY_NAME.fullmatch(p.name)):
        relative = folder.relative_to(outputs).as_posix()
        stage = folder.parent.relative_to(outputs / 'cache').as_posix()
        size = sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
        if relative in used:
            status, reason = 'in_use', 'named by run ' + ', '.join(used[relative])
        elif newest is None:
            status, reason = 'kept', 'no run records to compare against'
        elif _newest_mtime(folder) > newest:
            status, reason = 'kept', 'written after the newest run record (may be a run in progress)'
        elif stage == 'inspect' and unrecorded:
            status, reason = 'kept', 'inspection summary without a key: rerun inspect for ' + ', '.join(unrecorded)
        else:
            status, reason = 'unused', 'no run record names it'
        entries.append((folder, stage, status, reason, size))
    return entries
