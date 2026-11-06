"""Preserve published event/label tables separately from daily homogenization.

All source bytes are pinned by the explicit config. No parsing, filtering,
normalization, relabeling or scientific preprocessing is applied to these files.
"""
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import zipfile

from sm_drydown.providers.http import session
from sm_drydown.storage.files import download,sha256,write_json


def load_config(path):
    c=json.loads(Path(path).read_text())
    if c.get('schema_version')!=1 or c.get('role')!='alternate_drydown_events_and_labels':
        raise ValueError('Expected a published alternate event/label dataset specification')
    if c.get('homogenize') is not False:
        raise ValueError('Published event/label sources must explicitly bypass homogenization')
    if not re.fullmatch(r'[A-Za-z0-9_-]+',c['dataset_id']):
        raise ValueError('Unsafe dataset ID')
    if not re.fullmatch(r'[0-9a-f]{64}',c['csv_sha256']):
        raise ValueError('Extracted CSV must have a pinned SHA-256')
    if len(c['commit'])!=40 or any(ch not in '0123456789abcdef' for ch in c['commit']):
        raise ValueError('Source commit must be pinned')
    paths=[]
    for item in c['files']:
        relative=PurePosixPath(item['path'])
        if relative.is_absolute() or '..' in relative.parts or str(relative)!=item['path']:
            raise ValueError('Unsafe source artifact path')
        expected=f"https://raw.githubusercontent.com/{c['github_repository']}/{c['commit']}/{item['path']}"
        if item['url']!=expected or not re.fullmatch(r'[0-9a-f]{64}',item['sha256']):
            raise ValueError('Every source artifact needs a pinned URL and SHA-256')
        paths.append(item['path'])
    if len(set(paths))!=len(paths) or c['archive'] not in paths:
        raise ValueError('Missing archive or duplicate source artifact')
    member=PurePosixPath(c['csv_member'])
    if member.is_absolute() or '..' in member.parts or len(member.parts)!=1:
        raise ValueError('Unsafe CSV archive member')
    return c


def make_plan(path):
    c=load_config(path)
    return {'config_sha256':sha256(path),'role':c['role'],'homogenize':False,
        'github_repository':c['github_repository'],'commit':c['commit'],
        'source_files':len(c['files']),'csv_member':c['csv_member'],
        'published_events':c['table_description']['rows'],'storage':c['storage']}


def fetch_and_stage(config_path,cache_dir,store,approved_hash):
    plan=make_plan(config_path)
    if approved_hash!=plan['config_sha256']:
        raise ValueError('Explicit download approval must match this configuration SHA-256')
    if 'scratch' in store.prefix.strip('/').split('/') or store.bucket=='scratch':
        raise ValueError('Published label datasets belong in non-scratch project storage')
    c=load_config(config_path)
    root=Path(cache_dir)/c['commit']
    prefix=f"published_drydowns/{c['dataset_id']}/{c['commit']}"
    sources=[]
    with session() as client:
        for item in c['files']:
            path=root/'raw'/item['path']
            record=download(client,item['url'],path,expected_sha256=item['sha256'],
                            archive=item['path']==c['archive'])
            provenance=path.with_name(path.name+'.provenance.json')
            uri=store.upload(path,prefix+'/source/'+item['path'])
            record_uri=store.upload(provenance,prefix+'/provenance/'+item['path']+'/'+sha256(provenance)+'.json')
            sources.append({'path':item['path'],'uri':uri,'sha256':record['sha256'],
                            'bytes':path.stat().st_size,'provenance_uri':record_uri})
    csv=root/'extracted'/c['csv_member']
    if not csv.is_file() or sha256(csv)!=c['csv_sha256']:
        csv.parent.mkdir(parents=True,exist_ok=True)
        temporary=csv.with_suffix('.part')
        with zipfile.ZipFile(root/'raw'/c['archive']) as archive:
            if archive.namelist()!=[c['csv_member']]:
                raise ValueError('Unexpected contents in the published archive')
            with archive.open(c['csv_member']) as source,temporary.open('wb') as output:
                shutil.copyfileobj(source,output)
        if sha256(temporary)!=c['csv_sha256']:
            raise ValueError('Extracted published CSV does not match its pinned SHA-256')
        temporary.replace(csv)
    csv_uri=store.upload(csv,prefix+'/data/'+c['csv_member'])
    config_uri=store.upload(config_path,prefix+'/config/'+plan['config_sha256']+'.json')
    result={'artifact_role':c['role'],'homogenize':False,'commit':c['commit'],
        'source_repository':c['source_repository'],'config_uri':config_uri,'source_files':sources,
        'table':{'uri':csv_uri,'sha256':c['csv_sha256'],'bytes':csv.stat().st_size,**c['table_description']},
        'transformations':['ZIP decompression only; original archive and CSV bytes preserved'],
        'complete':True}
    # Content-addressed manifests and retrieval records permit fresh-clone
    # reproducibility without overwriting provenance from an earlier retrieval.
    temp=root/'publication_manifest.json';write_json(temp,result)
    key=prefix+'/manifests/'+sha256(temp)+'.json'
    result_uri=store.upload(temp,key)
    return {**result,'manifest_uri':result_uri}
