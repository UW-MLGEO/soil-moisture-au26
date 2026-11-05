"""Inventory validated daily bundles without processing published label datasets."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sm_drydown.homogenization.workflow import validate_station_bundle
from sm_drydown.storage.files import sha256,write_json


def daily_manifest(output_dir,selection_path,*,network=None,station_id=None):
    """Require exactly one verified bundle for every selected station before publishing."""
    root=Path(output_dir)
    selection=json.loads(Path(selection_path).read_text())
    expected={(s['network'],s['station_id']) for s in selection['stations']
        if (network is None or s['network']==network) and (station_id is None or s['station_id']==station_id)}
    if not expected:raise ValueError('No stations selected for daily publication')
    found={};bundles=[];quality_masked=0
    for path in sorted(root.glob('*/*/*/description.json')):
        doc=validate_station_bundle(path.parent)
        key=(doc['station']['network'],doc['station']['station_id'])
        if key not in expected:continue
        if key in found:raise ValueError(f'Multiple daily bundle versions for station {key}; select one output directory')
        if (doc['inputs']['selection_sha256']!=sha256(selection_path) or
                doc['start']!=selection['start'] or doc['end_inclusive']!=selection['end']):
            raise ValueError('Daily bundle differs from the selected source/window')
        files=[]
        for name in ('daily.parquet','description.json'):
            file=path.with_name(name)
            files.append({'path':file.relative_to(root).as_posix(),'sha256':sha256(file),'bytes':file.stat().st_size})
        bundles.append({'network':key[0],'station_id':key[1],'input_signature':doc['input_signature'],
            'rows':doc['rows'],'files':files})
        found[key]=True
        quality_masked+=sum(s['values_masked_by_quality'] for s in doc['sensors'].values())
    if set(found)!=expected:
        raise ValueError(f'Incomplete daily publication: {len(found)}/{len(expected)} selected stations')
    return {'schema_version':1,'artifact_role':'daily_pre_drydown_station_tables',
        'selection_sha256':sha256(selection_path),'start':selection['start'],'end_inclusive':selection['end'],
        'timezone':'UTC calendar days','coordinate_reference':'EPSG:4326',
        'stations':len(bundles),'rows':sum(b['rows'] for b in bundles),'bundles':bundles,
        'values_masked_by_ISMN_quality':quality_masked,'values_filled':0,
        'complete_for_selected_stations':True,'full_selection':network is None and station_id is None,
        'published_label_sources_processed':False}


def publish_daily_manifest(output_dir,manifest,store):
    """Write a content-addressed completion inventory in non-scratch daily storage."""
    if 'scratch' in store.prefix.strip('/').split('/') or store.bucket=='scratch':
        raise ValueError('Daily manifests belong in non-scratch project storage')
    path=Path(output_dir)/'manifest.json'
    write_json(path,manifest)
    return store.upload(path,'daily/manifests/'+sha256(path)+'.json')


def upload_daily_files(output_dir,manifest,store,*,workers=4):
    """Transfer only manifest-listed daily files, with bounded parallelism and hash checks."""
    if 'scratch' in store.prefix.strip('/').split('/') or store.bucket=='scratch':
        raise ValueError('Daily tables belong in non-scratch project storage')
    if not 1<=workers<=16:raise ValueError('Upload workers must be 1-16')
    entries=[item for bundle in manifest['bundles'] for item in bundle['files']]
    def transfer(item):
        path=Path(output_dir)/item['path']
        if path.stat().st_size!=item['bytes'] or sha256(path)!=item['sha256']:
            raise ValueError('Daily file changed after manifest validation')
        return store.upload(path,'daily/'+item['path'])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(transfer,entries))
