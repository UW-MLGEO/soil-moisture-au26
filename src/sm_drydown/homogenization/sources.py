"""Read verified acquisition caches for daily homogenization; no remote access."""

import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

import numpy as np
import pandas as pd

from sm_drydown.homogenization.daily import Sensor, decode_values
from sm_drydown.formats.ismn import parse_header, station_key
from sm_drydown.formats.cf import decode_cf_values
from sm_drydown.formats.soil import TEXTURE_CLASS_TOLERANCE, texture_class
from sm_drydown.storage.files import sha256


def configuration_provenance(config_path, config, *, dataset=None):
    """Carry source declarations and portable references to the complete frozen config."""
    name = config.get('dataset', dataset)
    digest = sha256(config_path)
    relative = f"{name}/{config['collection_id']}/config/{digest}.json"
    destination = config.get('storage', {}).get('destination')
    urls = list(config.get('source_urls', []))
    for entry in config.get('sources', {}).values():
        if entry.get('url') and entry['url'] not in urls:
            urls.append(entry['url'])
    doi_urls = [url for url in urls if 'doi.org/' in url]
    return {'dataset': name, 'product': config.get('product'),
            'provider_collection': config.get('collection'), 'version': config.get('version'),
            'collection_id': config['collection_id'], 'source_urls': urls,
            'license': config.get('license'),
            'license_status': 'declared_in_configuration' if config.get('license') else 'not_declared_in_configuration',
            'citation': config.get('citation', doi_urls or None),
            'citation_status': 'declared_in_configuration' if config.get('citation') or doi_urls else 'not_declared_in_configuration',
            'configuration_reference': {'filename': Path(config_path).name, 'sha256': digest,
                'object_uri': destination.rstrip('/') + '/' + relative if destination else None},
            'sampling': config.get('sampling'), 'coordinate_reference': config.get('coordinate_reference'),
            'source_grid': config.get('source_grid', config.get('grid')),
            'source_depth_note': config.get('source_depth_note', config.get('source_depth_coverage')),
            'source_inventory': config.get('sources'),
            'selection_start': config['start'], 'selection_end_inclusive': config['end']}


class ExtractCatalog:
    """Load hash-verified NPZ assets once, retaining native coordinates and source metadata."""

    def __init__(self, config_path, cache_dir):
        self.config_path = Path(config_path)
        self.config = json.loads(self.config_path.read_text())
        self.assets = []
        self.provenance = {'configuration_sha256': sha256(config_path),
                           'source_description': configuration_provenance(config_path, self.config),
                           'assets': []}
        for asset in self.config['assets']:
            file = Path(cache_dir) / self.config['collection_id'] / 'extracts' / (asset['asset_id'] + '.npz')
            if not file.exists() or not file.with_suffix('.json').exists():
                raise FileNotFoundError(f'Missing configured acquisition extract: {file}')
            meta = json.loads(file.with_suffix('.json').read_text())
            if sha256(file) != meta['sha256']:
                raise ValueError(f'Corrupt acquisition extract: {file}')
            with np.load(file, allow_pickle=False) as content:
                arrays = {key: content[key] for key in content.files}
            self.assets.append((asset, arrays, meta))
            relative = f"{self.config.get('dataset')}/{self.config['collection_id']}/extracts/{file.name}"
            destination = self.config.get('storage', {}).get('destination')
            self.provenance['assets'].append({'asset_id': asset['asset_id'], 'sha256': meta['sha256'],
                'source_asset_specification': asset,
                'metadata_sha256': sha256(file.with_suffix('.json')),
                'metadata_reference': {'cache_relative_path': f"{self.config['collection_id']}/extracts/{file.with_suffix('.json').name}",
                    'object_uri': destination.rstrip('/') + '/' + str(Path(relative).with_suffix('.json')) if destination else None},
                'extraction_metadata': meta})

    def _station(self, arrays, station_id):
        matches = np.flatnonzero(arrays['station_ids'] == station_id)
        if len(matches) != 1:
            raise ValueError('Station is missing or ambiguous in extract')
        return int(matches[0])

    def coordinates(self, station_id):
        result = []
        for asset, arrays, meta in self.assets:
            index = self._station(arrays, station_id)
            row = {'asset_id': asset['asset_id']}
            for name in ('native_latitudes', 'native_longitudes', 'latitude_indices', 'longitude_indices'):
                if name in arrays:
                    value = arrays[name][index]
                    row[name] = value.item() if hasattr(value, 'item') else value
            result.append(row)
        return result

    def chirps(self, station_id):
        """Return values and distinct provider-null/absent-row masks without collapsing their meanings."""
        pieces = []
        for _, arrays, _ in self.assets:
            index = self._station(arrays, station_id)
            values = arrays['values'][:, index].astype(float)
            null = arrays['null_mask'][:, index]
            absent = arrays['absent_row_mask'][:, index]
            masks = null | absent
            if null.dtype != bool or absent.dtype != bool or np.any(null & absent):
                raise ValueError('CHIRPS null/absent masks must be disjoint boolean arrays')
            if not np.array_equal(np.isnan(values), masks):
                raise ValueError('CHIRPS values and explicit missingness masks disagree')
            pieces.append(pd.DataFrame({'value': values, 'source_present': ~absent,
                'provider_null': null, 'provider_absent_row': absent}, index=pd.DatetimeIndex(arrays['dates'])))
        return pd.concat(pieces).sort_index()

    def gldas(self, station_id):
        """Return (static field values, decoding audit); texture class is an int or None."""
        values, audit = {}, {}
        aliases = {'soil_texture': 'soil_texture_class', 'soil_texture_class': 'soil_texture_class'}
        for asset, arrays, meta in self.assets:
            index = self._station(arrays, station_id)
            field = aliases.get(asset['field'], asset['field'])
            if field in values:
                raise ValueError('Multiple GLDAS assets map to one static field')
            attrs = meta['source_attributes']
            decoded, _ = decode_cf_values(arrays['values'][:, index], attrs,
                                           creation_properties=meta.get('source_creation_properties'))
            if len(decoded) != 1:
                raise ValueError('Static GLDAS extract requires exactly one value per station and field')
            value = float(decoded[0])
            if field == 'soil_texture_class':
                raw = None if np.isnan(value) else value
                value, snapped = texture_class(raw)
                audit[field] = {'source_value': raw, 'class': value, 'float_noise_removed': snapped,
                                'rule': f'nearest integer within {TEXTURE_CLASS_TOLERANCE}'}
            else:
                units = str(attrs.get('units', '')).strip().lower()
                if units in ('%', 'percent', 'percentage'):
                    value /= 100
                elif units not in ('1', '-', 'fraction', 'm3/m3', 'm^3/m^3', 'cm3/cm3', 'cm^3/cm^3'):
                    raise ValueError(f'Unresolved GLDAS units for {field}: {units!r}')
                if np.isfinite(value) and not 0 <= value <= 1:
                    raise ValueError(f'GLDAS fraction outside [0,1]: {field}')
            values[field] = value
        return values, audit

    def modis(self, station_id, band, qc_bands):
        pieces = []
        audit = []
        for asset, arrays, meta in self.assets:
            index = self._station(arrays, station_id)
            bands = list(arrays['bands'].astype(str))
            if len(set(bands)) != len(bands):
                raise ValueError('Repeated MODIS band identity')
            frame = pd.DataFrame({'start': arrays['interval_start'], 'end': arrays['interval_end']})
            for source_band, target in [(band, 'value')] + [(b, 'qc_' + b) for b in qc_bands]:
                b = bands.index(source_band)
                raw = arrays['values'][:, index, b].copy().astype(float)
                mask = arrays['null_mask'][:, index, b] | arrays['absent_row_mask'][:, index]
                raw[mask] = np.nan
                attrs = meta['source_attributes'][source_band]
                codes = np.atleast_1d(attrs.get('_FillValue', [])).tolist()
                if 'missing_value' in attrs:
                    codes += np.atleast_1d(attrs['missing_value']).tolist()
                bounds = attrs.get('valid_range')
                if bounds is None and 'valid_min' in attrs and 'valid_max' in attrs:
                    bounds = [attrs['valid_min'], attrs['valid_max']]
                decoded, missing = decode_values(raw, missing_codes=codes,
                    scale_factor=attrs.get('scale_factor', 1), add_offset=attrs.get('add_offset', 0),
                    valid_range=bounds)
                frame[target] = decoded.to_numpy()
                audit.append({'asset_id': asset['asset_id'], 'band': source_band,
                              'missing_values': missing, 'attributes': attrs})
            pieces.append(frame)
        return pd.concat(pieces, ignore_index=True), audit


def read_station_sensors(archive, station, *, missing_codes, archive_sha256=None):
    """Read every SM/temperature instrument, preserving flags and bytes in the source archive."""
    sensors = []
    prefix = station['network'] + '/' + station_key(station['station_name']) + '/'
    digest = archive_sha256 or sha256(archive)
    variables = {'sm': ('soil_moisture', 'm3/m3'), 'ts': ('soil_temperature', 'degC')}
    with zipfile.ZipFile(archive) as source:
        for member in sorted(source.namelist()):
            if not member.startswith(prefix) or not member.endswith('.stm'):
                continue
            match = re.search(r'_(sm|ts)_-?\d+(?:\.\d+)?_-?\d+(?:\.\d+)?_', member)
            if not match:
                continue  # ISMN precipitation and nonrequested variables never enter the table.
            variable, units = variables[match[1]]
            payload = source.read(member)
            stream = io.StringIO(payload.decode('utf-8'))
            header = stream.readline().strip()
            metadata = parse_header(member, header)
            try:
                rows = pd.read_csv(stream, sep=r'\s+', header=None, keep_default_na=False,
                                   dtype={0: str, 1: str, 3: str, 4: str})
            except pd.errors.EmptyDataError:
                rows = pd.DataFrame(columns=range(5))
            if len(rows.columns) != 5:
                raise ValueError('Native records require date,time,value,ISMN flag,provider flag')
            dates = pd.to_datetime(rows[0] + ' ' + rows[1], format='%Y/%m/%d %H:%M', utc=True)
            observations = pd.DataFrame({'value': np.array([float(value) for value in rows[2]], dtype=float),
                'ismn_flag': rows[3].to_numpy(), 'provider_flag': rows[4].to_numpy()}, index=pd.DatetimeIndex(dates))
            metadata.update({'units': units, 'variable': variable,
                             'archive_sha256': digest, 'member': member,
                             'member_sha256': hashlib.sha256(payload).hexdigest(), 'native_header': header})
            sensors.append(Sensor(member, variable, observations, units, missing_codes[variable], metadata))
    if not sensors:
        raise ValueError('No native SM or temperature members match station identity')
    return sensors
