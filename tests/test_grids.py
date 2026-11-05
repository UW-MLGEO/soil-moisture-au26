"""Native NetCDF/HDF grids, conservative CF decoding and direct MODIS acquisition.

Run from the repository root: pixi run test
All fixtures are synthetic; no credentials, network or existing datasets.
"""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

import h5py
import numpy as np
from pyhdf.SD import SD, SDC
import requests

from fixtures import HTTPResponse, MemoryStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sm_drydown.acquisition import lpdaac
from sm_drydown.acquisition.gldas import fetch_and_stage
from sm_drydown.acquisition.grid import extract_native_cells, native_indices
from sm_drydown.formats.cf import decode_cf_values
from sm_drydown.formats.soil import texture_class
from sm_drydown.homogenization.sources import ExtractCatalog
from sm_drydown.storage.files import sha256, write_json


def netcdf_fixture(path):
    with h5py.File(path, 'w') as f:
        f['latitude'] = np.array([34., 35., 36.])
        f['longitude'] = np.array([-91., -90., -89.])
        f['time'] = np.array([0, 1, 2])
        f['time'].attrs['units'] = 'days since 2025-01-01'
        values = np.arange(27).reshape(3, 3, 3).astype('i2')
        values[1, 1, 1] = -9999
        d = f.create_dataset('precip', data=values, chunks=(2, 2, 2), compression='gzip')
        d.attrs.update(units='mm/day', _FillValue=-9999, scale_factor=0.1, add_offset=1.)


def decode_extract(path):
    import json
    metadata = json.loads(path.with_suffix('.json').read_text())
    with np.load(path, allow_pickle=False) as raw:
        return decode_cf_values(raw['values'][:,0], metadata['source_attributes'],
            creation_properties=metadata.get('source_creation_properties'))


def gldas_config():
    return {'schema_version': 1, 'dataset': 'gldas', 'collection_id': 'test',
            'start': '2025-01-01', 'end': '2025-01-03', 'sampling': 'native_cell',
            'strategy': 'http_files', 'storage': {},
            'stations': [{'station_id': 4, 'network': 'TEST', 'station_name': 'One',
                          'latitude': 35., 'longitude': -90.}],
            'variables': {'value': 'precip', 'time': 'time', 'latitude': 'latitude', 'longitude': 'longitude'},
            'assets': [{'asset_id': '2025', 'filename': '2025.nc', 'url': 'https://example.test/2025.nc'}]}


class NativeGridTests(unittest.TestCase):
    def test_asset_specific_variable_preserves_raw_values(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'soil.nc'; netcdf_fixture(source)
            with h5py.File(source, 'a') as f:
                f['sand'] = np.full((3,3,3), .25)
                f['sand'].attrs['units'] = '-'
            c = gldas_config(); asset = {'variables': {**c['variables'], 'value': 'sand'}}
            out = Path(folder)/'sand.npz'
            report = extract_native_cells(source, c, out, asset=asset)
            self.assertEqual(report['source_variable'], 'sand')
            with np.load(out) as raw:
                np.testing.assert_array_equal(raw['values'][:,0], [.25,.25,.25])
    def test_missing_cf_attributes_use_verified_netcdf_fill(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder); source = folder / 'native.nc'; netcdf_fixture(source)
            default_fill = np.float32(9.969209968386869e36)
            with h5py.File(source, 'a') as f:
                del f['precip']
                values = np.zeros((3, 3, 3), dtype='f4')
                values[1, 1, 1], values[2, 1, 1] = default_fill, 2.5
                f.create_dataset('precip', data=values, chunks=(2, 2, 2), fillvalue=default_fill)
                f.attrs['_NCProperties'] = 'version=1|netcdflibversion=4.6.1|hdf5libversion=1.10.2'
                f.attrs['_nc3_strict'] = 1
            source_hash = sha256(source)
            c = gldas_config()
            path = folder / 'test' / 'extracts' / '2025.npz'
            metadata = extract_native_cells(source, c, path)
            self.assertEqual(metadata['source_attributes'], {})
            self.assertEqual(metadata['source_creation_properties']['fill_value_status'], 'user_defined')
            with np.load(path) as raw:
                np.testing.assert_array_equal(raw['values'][:, 0], [0., default_fill, 2.5])
            decoded, report = decode_extract(path)
            np.testing.assert_allclose(decoded, [0., np.nan, 2.5], equal_nan=True)
            self.assertEqual(report['declared_missing_count'], 0)
            self.assertEqual(report['implicit_netcdf_default_fill_count'], 1)
            self.assertEqual(report['values_filled'], 0)
            self.assertEqual(sha256(source), source_hash)

    def test_generic_hdf5_zero_and_unverified_magic_numbers_remain_values(self):
        raw = np.array([0., 9.969209968386869e36], dtype='f4')
        base = {'dtype': '<f4', 'fill_value_status': 'user_defined', 'fill_time': 'if_set',
                'fill_value': float(raw[1]), 'netcdf_file_attributes': {}}
        for creation in (base,
                         {**base, 'fill_value_status': 'default', 'fill_value': 0.,
                          'netcdf_file_attributes': {'_nc3_strict': 1}},
                         {**base, 'fill_time': 'never', 'netcdf_file_attributes': {'_nc3_strict': 1}}):
            decoded, report = decode_cf_values(raw, {}, creation_properties=creation)
            np.testing.assert_array_equal(decoded, raw.astype(float))
            self.assertEqual(report['implicit_netcdf_default_fill_count'], 0)
        # An explicit CF missing code suppresses implicit-default inference.
        decoded, report = decode_cf_values(raw, {'missing_value': -9999.},
                                           creation_properties={**base, 'netcdf_file_attributes': {'_nc3_strict': 1}})
        np.testing.assert_array_equal(decoded, raw.astype(float))
        self.assertIsNone(report['implicit_netcdf_default_fill'])


    def test_existing_explicit_cf_sidecar_does_not_change_when_resumed(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder); source = folder / 'raw.nc'; netcdf_fixture(source)
            path = folder / 'test.npz'
            prior = extract_native_cells(source, gldas_config(), path)
            prior.pop('source_creation_properties')
            write_json(path.with_suffix('.json'), prior)
            before = path.with_suffix('.json').read_bytes()
            report = extract_native_cells(source, gldas_config(), path)
            self.assertEqual(report, prior)
            self.assertEqual(path.with_suffix('.json').read_bytes(), before)

    def test_cf_scalar_attributes_accept_native_single_element_arrays(self):
        raw = np.array([1., 6., -9999.])
        decoded, report = decode_cf_values(raw, {
            '_FillValue': [-9999.], 'missing_value': [-9999.],
            'scale_factor': [1.], 'add_offset': [0.], 'units': 'none',
        })
        np.testing.assert_allclose(decoded, [1., 6., np.nan], equal_nan=True)
        np.testing.assert_array_equal(raw, [1., 6., -9999.])
        self.assertEqual(report['declared_missing_count'], 1)
        self.assertEqual(report['values_filled'], 0)
        with self.assertRaisesRegex(ValueError, 'exactly one value'):
            decode_cf_values(raw, {'scale_factor': [1., 2.]})


    def test_native_cell_edges_and_ties_are_deterministic(self):
        np.testing.assert_array_equal(native_indices([36., 35., 34.], [34.5, 35.5]), [1, 0])
        with self.assertRaises(ValueError):
            native_indices([34., 35.], [40.])


    def test_native_extract_preserves_packed_values_and_decoder_masks_declared_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder); source = folder / 'raw.nc'; netcdf_fixture(source)
            c = gldas_config(); path = folder / 'test' / 'extracts' / '2025.npz'
            report = extract_native_cells(source, c, path)
            with np.load(path) as result:
                self.assertEqual(result['values'][1, 0], -9999)
            series, provenance = decode_extract(path)
            self.assertAlmostEqual(series[0], 1.4)
            self.assertTrue(np.isnan(series[1]))
            self.assertEqual(provenance['declared_missing_count'], 1)
            self.assertEqual(provenance['values_filled'], 0)
            again = extract_native_cells(source, c, path)
            self.assertEqual(report, again)


    def test_missing_source_date_is_reported_without_inserting_values(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder); source = folder / 'raw.nc'; netcdf_fixture(source)
            with h5py.File(source, 'a') as f:
                f['time'][:] = [0, 2, 3]
            c = gldas_config(); c['end'] = '2025-01-04'
            path = folder / 'test' / 'extracts' / '2025.npz'
            report = extract_native_cells(source, c, path)
            self.assertEqual(report['date_count'], 3)
            self.assertEqual(report['expected_date_count'], 4)
            self.assertEqual(report['missing_source_dates'], ['2025-01-02'])
            series, _ = decode_extract(path)
            self.assertEqual(len(series), 3)

    def test_nan_fill_attributes_roundtrip_as_explicit_json_strings(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder); source = folder / 'raw.nc'; netcdf_fixture(source)
            with h5py.File(source, 'a') as f:
                f['precip'].attrs['_FillValue'] = np.float32(np.nan)
                f['precip'].attrs['missing_value'] = np.array([np.nan, -9999.], dtype='f4')
            c = gldas_config(); path = folder / 'test' / 'extracts' / '2025.npz'
            meta = extract_native_cells(source, c, path)
            self.assertEqual(meta['source_attributes']['_FillValue'], 'nan')
            self.assertEqual(meta['source_attributes']['missing_value'], ['nan', -9999.])
            series, report = decode_extract(path)
            self.assertTrue(np.isnan(series[1]))
            self.assertEqual(report['declared_missing_count'], 1)

    def test_missing_station_asset_is_rejected_without_filling(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'
            write_json(path, gldas_config())
            with self.assertRaises(FileNotFoundError):
                ExtractCatalog(path, folder)
        values, report = decode_cf_values(np.array([-9999., 2.]), {})
        self.assertEqual(values[0], -9999.)
        self.assertEqual(report['declared_missing_count'], 0)


class TextureClassTests(unittest.TestCase):
    def test_float32_noise_snaps_to_integer_class(self):
        # Values observed in GLDASp5_soiltexture_025d.nc4 at CONUS stations.
        for raw, expected in ((np.float32(2.9999998), 3), (np.float32(4.0000005), 4), (6.0, 6)):
            value, snapped = texture_class(raw)
            self.assertEqual(value, expected)
            self.assertIsInstance(value, int)
            self.assertEqual(snapped, float(raw) != expected)

    def test_missing_is_none_and_non_integer_is_rejected(self):
        self.assertEqual(texture_class(None), (None, False))
        self.assertEqual(texture_class(np.nan), (None, False))
        with self.assertRaisesRegex(ValueError, 'not within'):
            texture_class(3.4)
        with self.assertRaisesRegex(ValueError, 'Non-finite'):
            texture_class(np.inf)

class GLDASAcquisitionTests(unittest.TestCase):
    def test_static_raw_upload_noisy_texture_decoding_and_cache_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / 'texture.nc'
            with h5py.File(source, 'w') as f:
                f['latitude'] = [34., 35., 36.]
                f['longitude'] = [-91., -90., -89.]
                f['time'] = [0]
                f['time'].attrs['units'] = 'days since 2000-01-01'
                f['texture'] = np.full((1, 3, 3), 2.9999998, dtype='f4')
                f['texture'].attrs['_FillValue'] = -9999
            cfg = gldas_config()
            cfg.update(dataset='gldas', static=True, sensor_depth_assignment='top_layer_through_50cm')
            cfg['variables']['value'] = 'texture'
            cfg['assets'][0]['field'] = 'soil_texture'
            path = folder / 'config.json'
            write_json(path, cfg)
            session, store = Mock(), MemoryStore()
            session.get.return_value = HTTPResponse(source.read_bytes())
            with self.assertRaisesRegex(ValueError, 'approval'):
                fetch_and_stage(path, folder / 'cache', session, store, 'wrong')
            session.get.assert_not_called()
            fetch_and_stage(path, folder / 'cache', session, store, sha256(path))
            objects = dict(store.objects)
            fetch_and_stage(path, folder / 'cache', session, store, sha256(path))
            self.assertEqual(session.get.call_count, 1)
            self.assertEqual(store.objects, objects)
            with np.load(folder/'cache/test/extracts/2025.npz') as raw:
                self.assertEqual(raw['values'][0,0], np.float32(2.9999998))
            values, audit = ExtractCatalog(path, folder/'cache').gldas(4)
            self.assertEqual(values['soil_texture_class'], 3)
            self.assertIsInstance(values['soil_texture_class'], int)
            self.assertTrue(audit['soil_texture_class']['float_noise_removed'])
            raw = folder / 'cache/test/raw/2025/2025.nc'
            raw.write_bytes(b'corrupt')
            fetch_and_stage(path, folder / 'cache', session, store, sha256(path))
            self.assertEqual(session.get.call_count, 2)


PROJECTION = {'crs':'SR-ORG:6974', 'transform':[463.3127165279165,0,-20015109.354,0,-463.3127165279167,10007554.677003]}


def land_cover_selection():
    filename='MCD12Q1.A2025001.h09v04.061.2026293135448.hdf'
    return {'schema_version':1,'dataset':'modis_land_cover','strategy':'lpdaac_https_hdf4',
        'collection':'MCD12Q1.061','collection_id':'fixture','cadence':'annual','sampling':'native_cell',
        'start':'2025-07-04','end':'2025-12-31','bands':['LC_Type1','QC'],'source_projection':PROJECTION,
        'storage':{}, 'stations':[{'station_id':1,'latitude':45.,'longitude':-120.},
            {'station_id':2,'latitude':45.,'longitude':-120.},
            {'station_id':3,'latitude':45.,'longitude':-119.}],
        'assets':[{'asset_id':'2025','granules':[{'tile':'h09v04','filename':filename,'size_estimate_bytes':1,
            'url':'https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/MCD12Q1.061/test/'+filename}]}]}


def hdf4_fixture(path,c):
    cells=lpdaac.station_cells(c)
    a,_,x,_,e,y=PROJECTION['transform']; h,v=9,4; n=2400
    source=SD(str(path),SDC.WRITE|SDC.CREATE)
    source.attr('StructMetadata.0').set(SDC.CHAR8,
        'GridName="MCD12Q1"\nProjection=GCTP_SNSOID\nGridOrigin=HDFE_GD_UL\n'
        f'UpperLeftPointMtrs=({x+h*n*a:.6f},{y+v*n*e:.6f})\n'
        f'LowerRightMtrs=({x+(h+1)*n*a:.6f},{y+(v+1)*n*e:.6f})\n')
    for band in c['bands']:
        field=source.create(band,SDC.UINT8,(n,n));field.setcompress(SDC.COMP_DEFLATE,6)
        values=np.full((n,n),1,dtype=np.uint8)
        for j,cell in enumerate(cells):
            values[cell['line'],cell['sample']] = (255 if j==2 else 10) if band=='LC_Type1' else 7
        field[:,:]=values
        field.attr('_FillValue').set(SDC.UINT8,255)
        field.attr('valid_range').set(SDC.UINT8,[1,17] if band=='LC_Type1' else [0,254])
        field.attr('long_name').set(SDC.CHAR8,band+'\x00\x00')
        field.endaccess()
    source.end()


class LPDAACTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.c=land_cover_selection()
        self.file=self.root/'tile.hdf';hdf4_fixture(self.file,self.c)
        self.g=self.c['assets'][0]['granules'][0];self.g['size_estimate_bytes']=self.file.stat().st_size-4
        self.path=self.root/'selection.json';self.path.write_text(json.dumps(self.c))

    def test_native_samples_fill_flags_and_shared_pixels_preserved(self):
        values,attrs=lpdaac.read_tile(self.file,self.g,self.c,lpdaac.station_cells(self.c))
        np.testing.assert_array_equal(values,[[10,7],[10,7],[255,7]])
        self.assertEqual(attrs['LC_Type1']['valid_range'],[1,17])
        self.assertEqual(attrs['LC_Type1']['long_name'],'LC_Type1')
        self.assertEqual(values.dtype,np.uint8)
        self.assertEqual(lpdaac.valid_values(values[:,0],attrs['LC_Type1']).tolist(),[True,True,False])

    def test_grid_mismatch_and_non_hdf_payload_rejected(self):
        wrong={**self.g,'tile':'h10v04'}
        with self.assertRaisesRegex(ValueError,'lattice'):
            lpdaac.read_tile(self.file,wrong,self.c,lpdaac.station_cells(self.c))
        self.file.write_bytes(b'<html>login</html>')
        with self.assertRaisesRegex(ValueError,'not HDF4'):
            lpdaac.read_tile(self.file,self.g,self.c,lpdaac.station_cells(self.c))

    def test_frozen_inventory_rejects_missing_duplicate_and_wrong_year_granules(self):
        self.assertEqual(lpdaac.make_plan(self.path)['granule_count'],1)
        with self.assertRaisesRegex(ValueError,'approval'):
            lpdaac.require_approval(self.path,'wrong')
        for mutation in ('missing','duplicate','year','url'):
            c=copy.deepcopy(self.c);granules=c['assets'][0]['granules']
            if mutation=='missing':granules.clear()
            elif mutation=='duplicate':granules.append(granules[0])
            elif mutation=='year':c['assets'][0]['asset_id']='2024'
            else:granules[0]['url']='https://example.org/'+granules[0]['filename']
            self.path.write_text(json.dumps(c))
            with self.assertRaises(ValueError):lpdaac.load_config(self.path)

    def test_credentials_private_and_redirect_auth_only_sent_to_login(self):
        credentials=self.root/'credentials';credentials.write_text('USER=test\nPASS=hidden\n');credentials.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'private'):lpdaac.read_credentials(credentials)
        credentials.chmod(0o600);client=lpdaac.EarthdataSession(credentials)
        previous=Mock(request=requests.Request('GET','https://example.org').prepare())
        for host in ['urs.earthdata.nasa.gov','data.lpdaac.earthdatacloud.nasa.gov','bucket.s3.amazonaws.com']:
            prepared=requests.Request('GET','https://'+host+'/',headers={'Authorization':'Bearer old'}).prepare()
            client.rebuild_auth(prepared,previous)
            self.assertEqual('Authorization' in prepared.headers,host=='urs.earthdata.nasa.gov')

    def test_discovery_requires_one_complete_native_tile_inventory(self):
        entry={'title':self.g['filename'][:-4],'id':'G-test','collection_concept_id':'C-test',
            'updated':'2026-11-04T00:00:00Z','granule_size':str(self.g['size_estimate_bytes']/2**20),
            'links':[{'href':self.g['url']}]}
        client=Mock();client.get.return_value.json.return_value={'feed':{'entry':[entry]}}
        resolved,_=lpdaac.discover_year(self.c,'2025',client)
        self.assertEqual(resolved[0]['size_estimate_bytes'],self.g['size_estimate_bytes'])
        for rows in [[],[entry,entry]]:
            client.get.return_value.json.return_value={'feed':{'entry':rows}}
            with self.assertRaises(ValueError):lpdaac.discover_year(self.c,'2025',client)

    def test_empty_primary_year_refused_but_individual_missing_station_preserved(self):
        values,attrs=lpdaac.read_tile(self.file,self.g,self.c,lpdaac.station_cells(self.c))
        output=self.root/'extracts'/'2025.npz'
        report=lpdaac.write_extract(self.c,self.c['assets'][0],values,attrs,{'tile':'hash'},output,'config')
        self.assertEqual(report['primary_valid_stations'],2)
        self.assertEqual(report['primary_missing_station_ids'],[3])
        with np.load(output) as a:
            self.assertEqual(a['values'][0,2,0],255)
            self.assertFalse(a['null_mask'].any());self.assertFalse(a['absent_row_mask'].any())
            self.assertEqual(str(a['interval_start'][0])[:10],'2025-01-01')
        values[:,0]=255
        with self.assertRaisesRegex(ValueError,'empty year'):
            lpdaac.write_extract(self.c,self.c['assets'][0],values,attrs,{},output,'different')

    def test_fetch_cache_integrity_upload_and_daily_reader_compatibility(self):
        payload=self.file.read_bytes();response=HTTPResponse(payload)
        client=Mock();client.get.return_value=response
        store=Mock();store.upload.return_value='s3://test/raw'
        cache=self.root/'cache'
        first=lpdaac.fetch_and_stage(self.path,cache,client,store,sha256(self.path))
        second=lpdaac.fetch_and_stage(self.path,cache,client,store,sha256(self.path))
        self.assertTrue(first['complete']);self.assertEqual(client.get.call_count,1)
        self.assertEqual(first,second)
        self.assertEqual(second['assets'][0]['primary_valid_stations'],2)
        catalog=ExtractCatalog(self.path,cache)
        frame,_=catalog.modis(3,'LC_Type1',['QC'])
        self.assertTrue(np.isnan(frame['value'].iloc[0]));self.assertEqual(frame['qc_QC'].iloc[0],7)
        raw=cache/'fixture'/'raw'/'2025'/self.g['filename'];raw.write_bytes(b'corrupt')
        lpdaac.fetch_and_stage(self.path,cache,client,store,sha256(self.path))
        self.assertEqual(client.get.call_count,2)
        self.assertEqual(raw.read_bytes(),payload)
        with self.assertRaisesRegex(ValueError,'Unknown'):
            lpdaac.fetch_and_stage(self.path,cache,client,store,sha256(self.path),asset_ids=['2024'])


if __name__ == '__main__':
    unittest.main()
