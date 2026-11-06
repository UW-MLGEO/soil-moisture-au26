"""Content-addressed stage outputs, partitioned by ISMN network.

A stage writes ``<outputs_dir>/cache/<stage>/<key>/<table>/<partition>.parquet``.
The key is the SHA-256 of the stage's config sections, its upstream keys and
request hashes, and its code, so a stage is recomputed exactly when one of those
changes; anything else is reused. ``_complete/<partition>.json`` is written last
and records the partition's input hashes (e.g. the network's ISMN archive) and
the SHA-256 of every file it wrote; a partition without a matching marker is
rebuilt, so an interrupted run resumes where it stopped.
"""

import json
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
