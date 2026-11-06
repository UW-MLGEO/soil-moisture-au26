"""Raw archives, approval, resumable caches, published labels and immutable storage.

Run from the repository root: pixi run test
All fixtures are synthetic; no credentials, network or existing datasets.
"""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
import zipfile

from botocore.exceptions import ClientError
import requests

from fixtures import HTTPResponse, MemoryStore

from sm_drydown.providers import published
from sm_drydown.providers.ismn import Page, load_config, overlaps, request_payload
from sm_drydown.providers.ismn_extract import extract_zip, expand_exports
from sm_drydown.providers.ismn_workflow import make_plan, require_approval, row_key, submit_exports
from sm_drydown.storage.files import ObjectStore, download, sha256, write_json
from sm_drydown.storage.restore import restore_extracts, restore_ismn


def selection():
    return {'start': '2002-07-04', 'end': '2025-12-31',
            'requested_variables': ['soil_moisture', 'precipitation'],
            'provider_options': {'format': 'own', 'fill_nan': False, 'g_flag_only': False,
                                 'depth_range_cm': [0, 300], 'climates': ['Temperate'],
                                 'landcovers': ['Crop land']}, 'stations': [
        {'network': 'TEST', 'station_id': 3, 'soil_moisture_sensors': [
            {'timeseries_id': 4, 'depth_from': 0.05, 'depth_to': 0.05},
            {'timeseries_id': 5, 'depth_from': None, 'depth_to': None},
            {'timeseries_id': 6, 'depth_from': 3.0, 'depth_to': 3.0}]}]}


class ISMNTests(unittest.TestCase):

    def test_content_encoding_length_is_not_confused_with_decoded_body_length(self):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.headers = {'Content-Encoding': 'gzip', 'Content-Length': '10'}
        response.iter_content.return_value = iter([b'decoded provider resource'])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'resource.txt'
            download(session, 'https://example.test/resource.txt', path)
            self.assertEqual(path.read_bytes(), b'decoded provider resource')

    def test_interrupted_stream_restarts_from_zero_then_validates_zip(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as z:
            z.writestr('station_sm.stm', b'original observations')
        body = content.getvalue()
        def interrupted():
            yield body[:20]
            raise requests.ConnectionError('reset')
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.iter_content.side_effect = [interrupted(), iter([body])]
        response.headers = {'Content-Length': str(len(body))}
        with tempfile.TemporaryDirectory() as folder, patch('sm_drydown.storage.files.time.sleep'):
            path = Path(folder)/'raw.zip'
            meta = download(session, 'https://example.test/raw.zip', path, archive=True)
            self.assertEqual(session.get.call_count, 2)
            self.assertEqual(path.read_bytes(), body)
            self.assertEqual(meta['sha256'], sha256(path))

    def test_stream_retry_is_bounded_and_never_publishes_partial_bytes(self):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.iter_content.side_effect = requests.Timeout('interrupted')
        with tempfile.TemporaryDirectory() as folder, patch('sm_drydown.storage.files.time.sleep'):
            path = Path(folder)/'raw.zip'
            with self.assertRaises(requests.Timeout):
                download(session, 'https://example.test/raw.zip', path, archive=True)
            self.assertEqual(session.get.call_count, 3)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name(path.name+'.provenance.json').exists())

    def test_soil_temperature_is_added_without_provider_masking_or_gap_filling(self):
        c = load_config('locks/ismn.json')
        p = request_payload(c, 'ARM')
        self.assertEqual(p['variables'], 'Soil Moisture, Precipitation, Soil Temperature')
        self.assertFalse(p['fill_nan'])
        self.assertFalse(p['g_flag_only'])
        self.assertTrue(c['collection_id'].endswith('_sm_soil_temperature'))

    def test_extract_preserves_bytes_repairs_corruption_and_reuses_valid_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'network.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('station/sm.stm', b'raw observation\r\n-9999 U\r\n')
                z.writestr('Readme.txt', b'provider metadata')
            destination = root / 'extracted'
            first = extract_zip(archive, destination)
            target = destination / 'station/sm.stm'
            timestamp = target.stat().st_mtime_ns
            self.assertEqual(extract_zip(archive, destination), first)
            self.assertEqual(target.stat().st_mtime_ns, timestamp)
            target.write_bytes(b'x' * target.stat().st_size)
            self.assertEqual(extract_zip(archive, destination), first)
            self.assertEqual(target.read_bytes(), b'raw observation\r\n-9999 U\r\n')

    def test_extract_rejects_traversal_before_writing_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'unsafe.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('good.txt', b'ok')
                z.writestr('../escape.txt', b'bad')
            with self.assertRaises(ValueError):
                extract_zip(archive, root / 'extracted')
            self.assertFalse((root / 'extracted/good.txt').exists())
            self.assertFalse((root / 'escape.txt').exists())

    def test_expand_restores_raw_archive_and_only_uploads_separate_extracted_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.zip'
            with zipfile.ZipFile(source, 'w') as z:
                z.writestr('station/sm.stm', b'unchanged\n')
            config = root / 'selection.json'
            config.write_text(json.dumps({'collection_id': 'collection'}))
            store = Mock(prefix='scratch/project', bucket='gaia')
            store.client.head_object.return_value = {
                'ContentLength': source.stat().st_size, 'Metadata': {'sha256': sha256(source)}}
            store.client.download_file.side_effect = lambda bucket, key, path: Path(path).write_bytes(source.read_bytes())
            store.client.get_object.side_effect = ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
            first = expand_exports(store, config, root / 'cache', ['TEST'], workers=1)
            second = expand_exports(store, config, root / 'cache', ['TEST'], workers=1)
            self.assertEqual(first, second)
            self.assertEqual(first[0]['bytes'], 10)
            self.assertEqual(store.client.download_file.call_count, 1)
            self.assertEqual({c.args[1] for c in store.upload.call_args_list}, {
                'ismn/collection/extracted/TEST/station/sm.stm',
                'ismn/collection/extraction_provenance/TEST.json'})



    def test_dates_include_endpoints_and_reject_missing_or_reversed_spans(self):
        self.assertTrue(overlaps('2002-07-03', '2002-07-04', '2002-07-04', '2025-12-31'))
        self.assertTrue(overlaps('2025-12-31', '2026-01-01', '2002-07-04', '2025-12-31'))
        self.assertFalse(overlaps('2000-01-01', '2002-07-03', '2002-07-04', '2025-12-31'))
        self.assertFalse(overlaps(None, 'never', '2002-07-04', '2025-12-31'))
        with self.assertRaises(ValueError):
            overlaps('2025-01-02', '2025-01-01', '2002-07-04', '2025-12-31')

    def test_requests_keep_flags_gaps_all_sensors_and_precip(self):
        p = request_payload(selection(), 'TEST')
        self.assertEqual(p['variables'], 'Soil Moisture, Precipitation')
        self.assertFalse(p['fill_nan'])
        self.assertFalse(p['g_flag_only'])
        self.assertEqual(p['smTimeseriesList'], [4, 5, 6])
        self.assertEqual(float(p['depth_to']), 300)
        self.assertEqual(p['date_to'], '2025/12/31')
        self.assertEqual(p['format'], 'own')
        with self.assertRaises(ValueError):
            request_payload(selection(), 'UNKNOWN')

    def test_request_rows_keep_links_without_executing_html(self):
        p = Page('<table><tr><th>#</th></tr><tr><td>12</td><td>private user</td>'
                 '<td><a href="/download/12.zip">ZIP</a></td></tr></table>')
        self.assertEqual(p.rows[1]['cells'][0], '12')
        self.assertEqual(p.rows[1]['links'], ['/download/12.zip'])

    def test_standalone_config_preserves_unique_stations_and_sensor_depth(self):
        s = load_config('locks/ismn.json')
        self.assertEqual(len(s['stations']), 1388)
        ids = [x['station_id'] for x in s['stations']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(s['stations_without_sm_in_window']), 7)
        for station in s['stations']:
            for sensor in station['soil_moisture_sensors']:
                self.assertTrue(overlaps(sensor['start_date'], sensor['end_date'], s['start'], s['end']))
                self.assertIn('depth_from', sensor)
                self.assertIn('depth_to', sensor)

    def test_config_controls_variables_and_provider_filters(self):
        s = selection()
        s['requested_variables'] = ['soil_moisture']
        s['provider_options']['climates'] = ['Cold']
        p = request_payload(s, 'TEST')
        self.assertEqual(p['variables'], 'Soil Moisture')
        self.assertEqual(p['climates'], 'Cold')
        self.assertEqual(p['landcovers'], 'Crop land')

    def test_provider_approval_requires_the_exact_lock_hash(self):
        """The provider workflow still checks the SHA-256 that scripts/acquire.py passes from the verified lock."""
        plan = make_plan(Path('locks/ismn.json'))
        with self.assertRaises(ValueError):
            require_approval(plan, 'wrong-hash')
        require_approval(plan, sha256('locks/ismn.json'))

    def test_config_rejects_qc_gap_filling_and_accidental_depth_exclusion(self):
        config = load_config('locks/ismn.json')
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'config.json'
            for key in ('fill_nan', 'g_flag_only'):
                config['provider_options'][key] = True
                write_json(p, config)
                with self.assertRaisesRegex(ValueError, key):
                    load_config(p)
                config['provider_options'][key] = False
            config['provider_options']['depth_range_cm'] = [0, 1]
            write_json(p, config)
            with self.assertRaisesRegex(ValueError, 'exclude a selected sensor'):
                load_config(p)

    def test_changing_row_position_does_not_change_request_identity(self):
        before = {'cells': ['1', 'user', 'date', 'export.zip', '0%', 'queued']}
        after = {'cells': ['2', 'user', 'date', 'export.zip', '100%', 'done']}
        self.assertEqual(row_key(before), row_key(after))

    def test_submission_reuses_server_id_with_or_without_pending_filename(self):
        for request_id, filename in [(123, 'file.zip'), (456, None)]:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as folder:
                client = Mock()
                client.requests.side_effect = [[], [{
                    'cells': ['1', 'user', 'unique creation minute', filename or '', '6.2%', 'started'],
                    'links': []}]]
                client.submit.return_value = {'selection_id': request_id, 'state': 'Q'}
                submit_exports(client, selection(), folder, ['TEST'])
                state = json.loads((Path(folder)/'requests/TEST.json').read_text())
                self.assertEqual(state['status'], 'submitted')
                self.assertEqual(state['request_id'], str(request_id))
                self.assertEqual(state['archive_name'], filename)
                self.assertEqual(state['provider_created'], 'unique creation minute')
                submit_exports(client, selection(), folder, ['TEST'])
                self.assertEqual(client.submit.call_count, 1)

    def test_cache_reuses_valid_zip_and_refetches_corrupt_bytes(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as z:
            z.writestr('station_sm.stm', b'original values and flags\n')
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.iter_content.side_effect = lambda _: [content.getvalue()]
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'raw.zip'
            download(session, 'https://example.test/raw.zip', p, archive=True)
            download(session, 'https://example.test/raw.zip', p, archive=True)
            self.assertEqual(session.get.call_count, 1)
            self.assertEqual(p.read_bytes(), content.getvalue())
            p.write_bytes(b'corrupt')
            download(session, 'https://example.test/raw.zip', p, archive=True)
            self.assertEqual(session.get.call_count, 2)
            self.assertEqual(p.read_bytes(), content.getvalue())

    def test_bad_download_is_not_published(self):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.iter_content.return_value = [b'login page instead of ZIP']
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'raw.zip'
            with self.assertRaises(zipfile.BadZipFile):
                download(session, 'https://example.test/raw.zip', p, archive=True)
            self.assertFalse(p.exists())

    def test_interrupted_submission_is_not_repeated(self):
        client = Mock()
        client.requests.return_value = []
        client.submit.side_effect = TimeoutError('response lost')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(TimeoutError):
                submit_exports(client, selection(), folder, ['TEST'])
            with self.assertRaisesRegex(ValueError, 'prior submission needs review'):
                submit_exports(client, selection(), folder, ['TEST'])
            self.assertEqual(client.submit.call_count, 1)

    def test_matching_upload_is_skipped_and_collision_is_rejected(self):
        store = ObjectStore.__new__(ObjectStore)
        store.bucket, store.prefix, store.client = 'gaia', 'scratch/test', Mock()
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'data.json'; write_json(p, {'raw': True})
            store.client.head_object.return_value = {
                'ContentLength': p.stat().st_size, 'Metadata': {'sha256': sha256(p)}}
            store.upload(p, 'data.json')
            store.client.upload_file.assert_not_called()
            store.client.head_object.return_value['Metadata']['sha256'] = 'different'
            with self.assertRaisesRegex(ValueError, 'Refusing to overwrite'):
                store.upload(p, 'data.json')
            store.client.upload_file.assert_not_called()

    def test_upload_records_correct_media_type_for_raw_binary_and_metadata(self):
        store = ObjectStore.__new__(ObjectStore)
        store.bucket, store.prefix = 'gaia', 'scratch/test'
        with tempfile.TemporaryDirectory() as folder:
            for suffix, content_type in (
                ('.zip', 'application/zip'), ('.json', 'application/json'),
                ('.nc', 'application/x-netcdf'), ('.nc4', 'application/x-netcdf'),
                ('.bin', 'application/octet-stream'), ('.npz', 'application/octet-stream'),
            ):
                path = Path(folder) / ('raw' + suffix)
                path.write_bytes(b'unchanged source bytes')
                store.client = Mock()
                store.client.head_object.side_effect = [
                    ClientError({'Error': {'Code': '404'}}, 'HeadObject'),
                    {'ContentLength': path.stat().st_size, 'Metadata': {'sha256': sha256(path)}},
                ]
                store.upload(path, path.name)
                extra = store.client.upload_file.call_args.kwargs['ExtraArgs']
                self.assertEqual(extra['ContentType'], content_type)
                self.assertEqual(extra['Metadata']['sha256'], sha256(path))


class FakeS3:
    """In-memory bucket carrying the SHA-256 metadata that ObjectStore.upload records."""
    def __init__(self):
        self.objects, self.downloads = {}, 0

    def put(self, key, body, digest=None):
        self.objects['scratch/test/' + key] = (body, digest or hashlib.sha256(body).hexdigest())

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
        body, digest = self.objects[Key]
        return {'ContentLength': len(body), 'Metadata': {'sha256': digest}}

    def download_file(self, bucket, key, path):
        self.downloads += 1
        Path(path).write_bytes(self.objects[key][0])


def fake_store():
    store = ObjectStore.__new__(ObjectStore)
    store.bucket, store.prefix, store.client = 'gaia', 'scratch/test', FakeS3()
    return store


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # One real ARM station keeps the selection valid without any download.
        config = load_config('locks/ismn.json')
        config['stations'] = [next(s for s in config['stations'] if s['network'] == 'ARM')]
        config['networks'] = ['ARM']
        self.config = self.root / 'selection.json'; write_json(self.config, config)
        self.collection = config['collection_id']
        body = io.BytesIO()
        with zipfile.ZipFile(body, 'w') as z:
            z.writestr('ARM/station/sm.stm', b'raw observation\r\n-9999 U\r\n')
        self.body = body.getvalue()
        # Produce the sidecar exactly as collect_exports() would on first retrieval.
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.headers, response.iter_content.return_value = {}, iter([self.body])
        original = self.root / 'original' / 'ARM.zip'
        meta = download(session, 'https://ismn.earth/export.zip', original, archive=True)
        self.sidecar = original.with_name('ARM.zip.provenance.json').read_bytes()
        prefix = 'ismn/' + self.collection
        record = self.root / 'record.json'
        write_json(record, {**meta, 'network': 'ARM', 'request_id': '1', 'request': {},
                            'requested_at': meta['retrieved_at'], 'processing': 'unchanged',
                            'selection_sha256': sha256(self.config),
                            'archive_uri': f's3://gaia/scratch/test/{prefix}/raw/ARM.zip'})
        self.store = fake_store()
        self.store.client.put(f'{prefix}/raw/ARM.zip', self.body)
        self.store.client.put(f'{prefix}/provenance/{sha256(self.config)}/ARM.json', record.read_bytes())
        self.archive = self.root / 'cache' / self.collection / 'archives' / 'ARM.zip'

    def test_restored_sidecar_is_byte_identical_and_verified_cache_is_reused(self):
        restore_ismn(self.store, self.config, self.root / 'cache')
        self.assertEqual(self.archive.read_bytes(), self.body)
        self.assertEqual(self.archive.with_name('ARM.zip.provenance.json').read_bytes(), self.sidecar)
        log = self.archive.parents[1] / 'restore.json'
        first = log.read_text()
        self.assertEqual(self.store.client.downloads, 2)
        restore_ismn(self.store, self.config, self.root / 'cache')
        self.assertEqual(self.store.client.downloads, 2)
        self.assertEqual(log.read_text(), first)
        # Same-size corruption must be detected by hash and repaired from Kopah.
        self.archive.write_bytes(b'x' * len(self.body))
        restore_ismn(self.store, self.config, self.root / 'cache')
        self.assertEqual(self.store.client.downloads, 3)
        self.assertEqual(self.archive.read_bytes(), self.body)

    def test_corrupt_object_and_edited_selection_are_rejected_without_publishing(self):
        prefix = 'ismn/' + self.collection
        self.store.client.put(f'{prefix}/raw/ARM.zip', self.body, digest='0' * 64)
        with self.assertRaisesRegex(ValueError, 'do not match recorded SHA-256'):
            restore_ismn(self.store, self.config, self.root / 'cache')
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.archive.with_name('ARM.zip.part').exists())
        self.assertFalse(self.archive.with_name('ARM.zip.provenance.json').exists())
        edited = json.loads(self.config.read_text()); edited['selection_note'] = 'edited'
        write_json(self.config, edited)
        with self.assertRaisesRegex(FileNotFoundError, 'Missing object'):
            restore_ismn(self.store, self.config, self.root / 'cache')
        with self.assertRaisesRegex(ValueError, 'Unknown network'):
            restore_ismn(self.store, self.config, self.root / 'cache', ['SCAN'])

    def test_extracts_restore_both_files_and_reject_npz_mismatching_its_sidecar(self):
        config = self.root / 'chirps.json'
        write_json(config, {'dataset': 'chirps', 'collection_id': 'c', 'assets': [{'asset_id': '2002'}]})
        npz = b'station-cell arrays'
        self.store.client.put('chirps/c/extracts/2002.npz', npz)
        self.store.client.put('chirps/c/extracts/2002.json',
                              json.dumps({'sha256': hashlib.sha256(npz).hexdigest()}).encode())
        files = restore_extracts(self.store, config, self.root / 'ancillary')
        self.assertEqual([Path(f['path']).name for f in files], ['2002.npz', '2002.json'])
        self.assertEqual((self.root / 'ancillary/c/extracts/2002.npz').read_bytes(), npz)
        self.assertTrue((self.root / 'ancillary/c/restore.json').exists())
        self.store.client.put('chirps/c/extracts/2002.json', json.dumps({'sha256': '0' * 64}).encode())
        with self.assertRaisesRegex(ValueError, 'does not match its metadata sidecar'):
            restore_extracts(self.store, config, self.root / 'ancillary')


class PublishedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);csv=b'SITE_ID,q_q\nA,0.8\nB,NaN\n'
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:archive.writestr('events.csv',csv)
        self.payloads={'data/events.csv.zip':stream.getvalue(),'README.md':b'Published study metadata\n'}
        config={'schema_version':1,'role':'alternate_drydown_events_and_labels','homogenize':False,
            'dataset_id':'fixture','commit':'a'*40,'github_repository':'fixture/study',
            'source_repository':'https://github.com/fixture/study','archive':'data/events.csv.zip',
            'csv_member':'events.csv','csv_sha256':hashlib.sha256(csv).hexdigest(),
            'files':[{'path':name,'url':'https://raw.githubusercontent.com/fixture/study/'+'a'*40+'/'+name,
                      'sha256':hashlib.sha256(value).hexdigest()} for name,value in self.payloads.items()],
            'table_description':{'rows':2},'storage':{}}
        self.config=config;self.csv=csv;self.path=self.root/'selection.json';self.save()
        self.client=MagicMock();self.client.__enter__.return_value=self.client
        self.client.get.side_effect=self.get
    def save(self):self.path.write_text(json.dumps(self.config))
    def get(self,url,**kwargs):
        name=next(name for name in self.payloads if url.endswith('/'+name))
        return HTTPResponse(self.payloads[name])
    def fetch(self,store=None):
        with patch.object(published,'session',return_value=self.client):
            return published.fetch_and_stage(self.path,self.root/'cache',store or MemoryStore(),sha256(self.path))
    def test_unchanged_csv_and_source_files_idempotent_upload_and_cache_refetch(self):
        store=MemoryStore();first=self.fetch(store);second=self.fetch(store)
        self.assertEqual(first,second);self.assertEqual(self.client.get.call_count,2)
        self.assertFalse(first['homogenize']);self.assertEqual(first['table']['rows'],2)
        csv=self.root/'cache'/('a'*40)/'extracted/events.csv'
        self.assertEqual(csv.read_bytes(),self.csv)
        self.assertEqual(store.objects['published_drydowns/fixture/'+'a'*40+'/data/events.csv'],self.csv)
        # Source corruption triggers retrieval; extracted corruption triggers decompression only.
        raw=self.root/'cache'/('a'*40)/'raw/README.md';raw.write_bytes(b'corrupt')
        csv.write_bytes(b'corrupt');self.fetch(store)
        self.assertEqual(self.client.get.call_count,3);self.assertEqual(csv.read_bytes(),self.csv)
        self.assertTrue(all('daily/' not in key for key in store.objects))
    def test_approval_scratch_and_unexpected_archive_contents_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'approval'):
            published.fetch_and_stage(self.path,self.root,MemoryStore(),'wrong')
        scratch=MemoryStore();scratch.prefix='scratch/project'
        with self.assertRaisesRegex(ValueError,'non-scratch'):self.fetch(scratch)
        # A pinned but structurally different archive must never become a label CSV.
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:archive.writestr('../events.csv',self.csv)
        self.payloads['data/events.csv.zip']=stream.getvalue()
        self.config['files'][0]['sha256']=hashlib.sha256(stream.getvalue()).hexdigest();self.save()
        with self.assertRaisesRegex(ValueError,'Unexpected contents'):self.fetch()
    def test_published_policy_paths_and_source_digests_must_be_explicit(self):
        for field,value in [('homogenize',True),('dataset_id','../bad'),('csv_sha256','x'*64)]:
            prior=self.config[field];self.config[field]=value;self.save()
            with self.assertRaises(ValueError):published.load_config(self.path)
            self.config[field]=prior


if __name__ == '__main__':
    unittest.main()
