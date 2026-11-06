"""ISMN selection metadata and authenticated native export requests.

Provider interface: https://ismn.earth/static/js/ISMN_class.js
Native format: https://ismn.earth/en/data/header-value-files/
No observation filtering, aggregation, unit conversion, or gap filling.
"""

import json
import stat
import tomllib
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .http import session

BASE = 'https://ismn.earth'
VIEWER = BASE + '/en/dataviewer/'
REQUESTS = VIEWER + 'download_requests_list/'
VARIABLE_NAMES = {'soil_moisture': 'Soil Moisture', 'precipitation': 'Precipitation',
                  'soil_temperature': 'Soil Temperature'}


def overlaps(first, last, start, end):
    if not first or not last or 'never' in (first, last):
        return False
    first, last = date.fromisoformat(first[:10]), date.fromisoformat(last[:10])
    if first > last:
        raise ValueError('Reversed metadata date range')
    return first <= date.fromisoformat(end) and last >= date.fromisoformat(start)


def load_config(path):
    """Load the complete selection without external metadata or audit access."""
    config = json.loads(Path(path).read_text())
    if config['schema_version'] != 1:
        raise ValueError('Unsupported config schema')
    if date.fromisoformat(config['start']) > date.fromisoformat(config['end']):
        raise ValueError('Reversed requested date range')
    collection = config['collection_id']
    if not collection or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in collection):
        raise ValueError('Invalid collection_id')
    variables = config['requested_variables']
    if not variables or len(set(variables)) != len(variables) or 'soil_moisture' not in variables or set(variables) - VARIABLE_NAMES.keys():
        raise ValueError('Supported variables: soil_moisture, precipitation, soil_temperature')
    if not config['stations']:
        raise ValueError('Empty station selection')
    if set(config['networks']) != {s['network'] for s in config['stations']}:
        raise ValueError('Networks do not match the explicit station list')
    station_ids, sensor_ids = set(), set()
    export = config['provider_options']
    if export['format'] != 'own':
        raise ValueError('This workflow preserves native header+values ZIP exports')
    for key in ('fill_nan', 'g_flag_only'):
        if export[key] is not False:
            raise ValueError(f'{key} must be false for this unprocessed acquisition workflow')
    low, high = export['depth_range_cm']
    if low > high:
        raise ValueError('Reversed provider depth range')
    for station in config['stations']:
        if station['station_id'] in station_ids:
            raise ValueError('Duplicate station ID')
        station_ids.add(station['station_id'])
        if not station['soil_moisture_sensors']:
            raise ValueError('Station has no selected SM sensors')
        for sensor in station['soil_moisture_sensors']:
            if sensor['timeseries_id'] in sensor_ids:
                raise ValueError('Duplicate sensor ID')
            sensor_ids.add(sensor['timeseries_id'])
            if not overlaps(sensor['start_date'], sensor['end_date'], config['start'], config['end']):
                raise ValueError('Selected sensor does not overlap the requested dates')
            for key in ('depth_from', 'depth_to'):
                if sensor.get(key) is not None and not low <= sensor[key] * 100 <= high:
                    raise ValueError('Provider depth range would exclude a selected sensor')
    return config


def request_payload(selection, network):
    stations = [s for s in selection['stations'] if s['network'] == network]
    if not stations:
        raise ValueError(f'No selected stations for network {network}')
    sensors = [t for s in stations for t in s['soil_moisture_sensors']]
    export = selection['provider_options']
    return {
        'networks': [network], 'format': export['format'],
        'stationsList': [s['station_id'] for s in stations],
        'smTimeseriesList': [t['timeseries_id'] for t in sensors],
        'date_from': selection['start'].replace('-', '/'),
        'date_to': selection['end'].replace('-', '/'),
        'variables': ', '.join(VARIABLE_NAMES[v] for v in selection['requested_variables']),
        'climates': ', '.join(export['climates']), 'landcovers': ', '.join(export['landcovers']),
        'depth_from': str(export['depth_range_cm'][0]),
        'depth_to': str(export['depth_range_cm'][1]),
        'fill_nan': export['fill_nan'], 'g_flag_only': export['g_flag_only'],
    }


class Page(HTMLParser):
    """Read server-rendered request rows; never execute provider JavaScript."""
    def __init__(self, text):
        super().__init__()
        self.inputs, self.links, self.rows = [], [], []
        self.row = self.cell = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'input':
            self.inputs.append(a)
        if tag == 'tr':
            self.row = {'cells': [], 'links': []}
        if tag in ('td', 'th'):
            self.cell = []
        if tag == 'a' and a.get('href'):
            self.links.append(a['href'])
            if self.row is not None:
                self.row['links'].append(a['href'])

    def handle_data(self, text):
        if self.cell is not None:
            self.cell.append(text)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.row is not None and self.cell is not None:
            self.row['cells'].append(' '.join(''.join(self.cell).split()))
            self.cell = None
        if tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None


class Client:
    def __init__(self, credential_file):
        path = Path(credential_file).expanduser()
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError('Credential file must be private: chmod 600 PATH')
        try:
            creds = tomllib.loads(path.read_text())
        except Exception:
            raise ValueError('Cannot parse credential TOML; contents suppressed') from None
        if not all(isinstance(creds.get(k), str) and creds[k] for k in ('username', 'password')):
            raise ValueError('Credential file needs nonempty username and password strings')
        self.session = session()
        url = BASE + '/en/accounts/login/'
        page = self.session.get(url, timeout=30)
        page.raise_for_status()
        inputs = Page(page.text).inputs
        csrf = next(i['value'] for i in inputs if i.get('name') == 'csrfmiddlewaretoken')
        result = self.session.post(url, data={
            'login': creds['username'], 'password': creds['password'],
            'csrfmiddlewaretoken': csrf}, headers={'Referer': url}, timeout=60)
        result.raise_for_status()
        viewer = self.session.get(VIEWER, timeout=30)
        viewer.raise_for_status()
        if not any('download_requests_list' in link for link in Page(viewer.text).links):
            raise ValueError('ISMN login did not enable downloads; check account verification')

    def requests(self):
        r = self.session.get(REQUESTS, timeout=60)
        r.raise_for_status()
        if 'login' in urlparse(r.url).path:
            raise ValueError('ISMN session expired')
        return [row for row in Page(r.text).rows
                if row['cells'] and row['cells'][0].isdigit()]

    def submit(self, payload):
        # Intentionally never retry a POST: an interrupted response may still
        # have created a request. Preserve the submitting checkpoint for review.
        headers = {'Referer': VIEWER, 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}
        csrf = self.session.cookies.get('csrftoken')
        if csrf:
            headers['X-CSRFToken'] = csrf
        response = self.session.post(VIEWER + 'download_selection/',
                                     data=json.dumps(payload), headers=headers, timeout=120)
        try:
            value = response.json()
        except ValueError:
            raise ValueError(f'ISMN returned a non-JSON response (HTTP {response.status_code})') from None
        if not response.ok or value.get('message') or value.get('errors'):
            raise ValueError(f'ISMN export rejected: {value}')
        return value

    def archive_url(self, row):
        links = [urljoin(REQUESTS, u) for u in row['links'] if '.zip' in u]
        if len(links) > 1:
            raise ValueError('Ambiguous download links in request row')
        if links:
            u = urlparse(links[0])
            if u.scheme != 'https' or u.hostname != 'ismn.earth':
                raise ValueError('Unexpected archive host; review before downloading')
            return links[0]
        return None
