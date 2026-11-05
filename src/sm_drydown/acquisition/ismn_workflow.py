"""Resumable export/download/upload orchestration, separate from the CLI."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from sm_drydown.storage.files import download, sha256, utc_now, write_json
from sm_drydown.acquisition.ismn import load_config, request_payload


def make_plan(config_path, networks=None):
    """An offline, explicit request plan; no credentials, API, or audit access."""
    config = load_config(config_path)
    networks = sorted(set(networks or config['networks']))
    if not set(networks) <= set(config['networks']):
        raise ValueError('Unknown network in configuration')
    stations = [s for s in config['stations'] if s['network'] in networks]
    return {
        'config_sha256': sha256(config_path), 'collection_id': config['collection_id'],
        'start': config['start'], 'end': config['end'],
        'variables': config['requested_variables'], 'provider_options': config['provider_options'],
        'storage': config['storage'], 'station_count': len(stations),
        'sm_sensor_count': sum(len(s['soil_moisture_sensors']) for s in stations),
        'stations_advertising_precipitation': sum('precipitation' in s['available_variables'] for s in stations),
        'requests': {n: request_payload(config, n) for n in networks},
    }


def require_approval(plan, approved_sha256):
    if approved_sha256 != plan['config_sha256']:
        raise ValueError('Explicit approval is required before submit/collect. Review the plan, '
                         'then supply --approved-config-sha256 with the approved configuration hash.')


def row_key(row):
    """Table row numbers are positions, not stable server selection IDs."""
    return tuple(row['cells'][2:4])


def submit_exports(client, selection, run_dir, networks):
    run_dir = Path(run_dir)
    for network in networks:
        state_path = run_dir / 'requests' / f'{network}.json'
        payload = request_payload(selection, network)
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state['payload'] != payload:
                raise ValueError('Existing export has different settings; use a new cache directory')
            if state['status'] == 'submitting' or not state.get('request_id'):
                raise ValueError(f'{network}: prior submission needs review on the ISMN requests page; '
                                 'not submitting a possible duplicate')
            print(f"{network}: reusing request {state['request_id']}", flush=True)
            continue
        # The provider withholds archive names while processing and exposes
        # creation times only to the minute. Space submissions so each can be
        # unambiguously matched, even when table rows are reordered later.
        created = [datetime.fromisoformat(json.loads(p.read_text())['submitted_at'])
                   for p in (run_dir / 'requests').glob('*.json')]
        if created:
            delay = 61 - (datetime.now(timezone.utc) - max(created)).total_seconds()
            while delay > 0:
                pause = min(delay, 30)
                print(f'{network}: waiting {pause:.0f}s to keep export identifiers distinct', flush=True)
                time.sleep(pause)
                delay = 61 - (datetime.now(timezone.utc) - max(created)).total_seconds()
        before = {r['cells'][2] for r in client.requests()}
        state = {'network': network, 'status': 'submitting', 'payload': payload,
                 'submitted_at': utc_now()}
        write_json(state_path, state)
        result = client.submit(payload)
        state['response'] = result
        state['request_id'] = str(result['selection_id'])
        write_json(state_path, state)
        after = client.requests()
        new = [r for r in after if r['cells'][2] not in before]
        if len(new) != 1:
            write_json(state_path, state)
            raise ValueError(f'{network}: cannot identify a unique export creation time; '
                             'submission is saved and must be reviewed before retrying')
        state.update(status='submitted', provider_created=new[0]['cells'][2],
                     archive_name=new[0]['cells'][3] if '.zip' in new[0]['cells'][3] else None)
        write_json(state_path, state)
        print(f"{network}: submitted request {state['request_id']}", flush=True)


def collect_exports(client, store, selection_path, run_dir, object_prefix, networks):
    """Fetch only ready exports. Pending requests remain resumable checkpoints."""
    rows = [r for r in client.requests() if len(r['cells']) >= 4]
    run_dir = Path(run_dir)
    selection = load_config(selection_path)
    pending = []
    completed = []
    config_digest = sha256(selection_path)
    selection_uri = store.upload(selection_path, object_prefix + f'/config/{config_digest}.json')
    for network in networks:
        state_path = run_dir / 'requests' / f'{network}.json'
        if not state_path.exists():
            pending.append(network)
            print(f'{network}: not yet submitted', flush=True)
            continue
        state = json.loads(state_path.read_text())
        if not state.get('request_id'):
            raise ValueError(f'{network}: submission needs review')
        if state['payload'] != request_payload(selection, network):
            raise ValueError(f'{network}: saved request differs from current configuration')
        archive = run_dir / 'archives' / f'{network}.zip'
        # Keep completed exports usable after ISMN expires the original URL.
        url = state.get('archive_url')
        row = None
        if not url:
            matches = [r for r in rows if (
                (state.get('archive_name') and r['cells'][3] == state['archive_name']) or
                (state.get('provider_created') and r['cells'][2] == state['provider_created']))]
            if len(matches) > 1:
                raise ValueError(f'{network}: ambiguous provider request rows; review before downloading')
            row = matches[0] if matches else None
            url = client.archive_url(row) if row else None
        if not url:
            pending.append(network)
            status = ' / '.join(row['cells'][-2:]) if row else 'not listed (possibly expired)'
            print(f"{network}: export {state['request_id']} pending: {status}", flush=True)
            continue
        print(f'{network}: downloading or validating native archive', flush=True)
        meta = download(client.session, url, archive, archive=True)
        state['archive_url'] = url
        state['archive_sha256'] = meta['sha256']
        state['archive_bytes'] = meta['response_bytes']
        state['status'] = 'downloaded'
        write_json(state_path, state)
        key = object_prefix + f'/raw/{network}.zip'
        uri = store.upload(archive, key)
        provenance = {**meta, 'network': network, 'request_id': state['request_id'],
                      'requested_at': state['submitted_at'], 'request': state['payload'],
                      'selection_sha256': sha256(selection_path), 'archive_uri': uri,
                      'processing': 'Unchanged provider ZIP; no quality filtering or preprocessing'}
        provenance_path = run_dir / 'provenance' / f'{network}.json'
        write_json(provenance_path, provenance)
        store.upload(provenance_path, object_prefix + f'/provenance/{config_digest}/{network}.json')
        state.update(status='uploaded', archive_uri=uri)
        write_json(state_path, state)
        completed.append({'network': network, 'uri': uri, 'sha256': meta['sha256'],
                          'bytes': meta['response_bytes']})
        print(f'{network}: verified upload {uri}', flush=True)
    report = {'selection_uri': selection_uri, 'archives': completed, 'pending': pending}
    write_json(run_dir / 'status.json', report)
    if not pending:
        # A subset invocation must not falsely claim all networks are complete.
        all_networks = {s['network'] for s in selection['stations']}
        if set(networks) == all_networks:
            manifest_path = run_dir / 'manifest.json'
            write_json(manifest_path, report)
            store.upload(manifest_path, object_prefix + f'/manifests/{config_digest}.json')
    return report
