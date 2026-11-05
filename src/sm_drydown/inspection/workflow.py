"""Inspect existing primary-data artifacts; never rewrite inputs or retrieve pixels."""
from collections import Counter, defaultdict
import json
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from sm_drydown.homogenization.workflow import validate_station_bundle, json_safe
from sm_drydown.formats.cf import decode_cf_values
from sm_drydown.storage.files import sha256, write_json, utc_now
from sm_drydown.inspection.checks import (Samples, flatlines, precipitation_extremes,
    seasonal_precipitation, gldas_checks)
from sm_drydown.inspection.plots import distribution_plot

def _cell(meta, dataset):
    cells = meta['source_coordinates'].get(dataset, [])
    coordinates = [(c.get('native_latitudes'), c.get('native_longitudes')) for c in cells]
    known = [(round(float(y),7),round(float(x),7)) for y,x in coordinates
             if y is not None and x is not None and np.isfinite(y) and np.isfinite(x)]
    if not known:
        return None
    if len(set(known)) != 1:
        raise ValueError(f'{dataset}: station changes native cell; separate grid versions before pooling')
    return known[0]


def load_gldas(config_path, cache_dir, stations):
    """Decode source missing codes/units while retaining physical outliers for reporting."""
    config = json.loads(Path(config_path).read_text())
    frame = pd.DataFrame(stations)[['station_id','network','station_name','latitude','longitude']].set_index('station_id')
    source_hashes = {}
    for asset in config['assets']:
        path = Path(cache_dir)/config['collection_id']/'extracts'/(asset['asset_id']+'.npz')
        meta = json.loads(path.with_suffix('.json').read_text())
        if sha256(path) != meta['sha256']:
            raise ValueError(f'Corrupt GLDAS extract: {path}')
        source_hashes[asset['asset_id']] = meta['sha256']
        with np.load(path, allow_pickle=False) as data:
            ids = data['station_ids'].tolist()
            if len(ids) != len(set(ids)) or data['values'].shape[0] != 1:
                raise ValueError('GLDAS requires one static value per unique station')
            lookup = {sid:j for j,sid in enumerate(ids)}
            indices = [lookup[s] for s in frame.index]
            decoded, _ = decode_cf_values(data['values'][0, indices], meta['source_attributes'],
                creation_properties=meta.get('source_creation_properties'))
            field = {'soil_texture':'soil_texture_class'}.get(asset['field'],asset['field'])
            if field in frame:
                raise ValueError('Duplicate GLDAS field')
            if field != 'soil_texture_class':
                units = str(meta['source_attributes'].get('units','')).strip().lower()
                if units in ('%','percent','percentage'):
                    decoded /= 100
                elif units not in ('1','-','fraction','m3/m3','m^3/m^3','cm3/cm3','cm^3/cm^3'):
                    raise ValueError(f'Unknown GLDAS units for {field}: {units!r}')
            frame[field] = decoded
            frame[field+'_cell'] = [f"{data['latitude_indices'][j]}:{data['longitude_indices'][j]}" for j in indices]
    expected = {'soil_texture_class','sand_fraction','silt_fraction','clay_fraction','porosity'}
    if not expected.issubset(frame):
        raise ValueError('All five GLDAS fields are required')
    return frame.reset_index(), source_hashes


def _write_csv(path, rows, columns):
    pd.DataFrame.from_records(rows, columns=columns).to_csv(path, index=False)


def inspect_data(daily_dir, selection_path, gldas_config, ancillary_cache, output_dir, *,
                 flatline_min_days=3, chirps_positive_quantile=.999, gldas_sum_tolerance=.01,
                 network=None, station_id=None, figure_dir='docs/figures'):
    """Write diagnostic tables/figures for selected complete daily bundles, without filtering data."""
    if flatline_min_days < 2 or not 0 < chirps_positive_quantile <= 1 or not 0 <= gldas_sum_tolerance < 1:
        raise ValueError('Invalid diagnostic thresholds')
    daily_dir, output, figures = Path(daily_dir), Path(output_dir), Path(figure_dir)
    input_roots = [daily_dir.resolve(), Path(ancillary_cache).resolve()]
    if any(folder.resolve().is_relative_to(source) for folder in [output, figures] for source in input_roots):
        raise ValueError('Inspection outputs must be outside acquisition and daily-input directories')
    selection = json.loads(Path(selection_path).read_text())
    stations = [s for s in selection['stations'] if (network is None or s['network']==network)
                and (station_id is None or s['station_id']==station_id)]
    selected = {s['station_id']:s for s in stations}
    if not selected:
        raise ValueError('No stations match inspection selection')
    output.mkdir(parents=True,exist_ok=True)
    figures.mkdir(parents=True,exist_ok=True)
    masked, flat, negative, extreme, seasons, station_summaries = [],[],[],[],[],[]
    samples = {key:Samples() for key in ['ismn_sm_daily_sensor_days','chirps_station_days','chirps_native_cell_days',
        'lai_station_composites','lai_native_cell_composites','land_cover_station_years','land_cover_native_cell_years']}
    seen = set()
    seen_cells = defaultdict(set)
    inputs = []
    selection_hash, gldas_hash = sha256(selection_path), sha256(gldas_config)
    for metadata in sorted(daily_dir.glob('*/*/*/description.json')):
        if network is not None and metadata.parent.parent.parent.name != quote(network,safe=''):
            continue
        if station_id is not None and metadata.parent.parent.name != str(station_id):
            continue
        meta = validate_station_bundle(metadata.parent)
        sid = meta['station']['station_id']
        if sid not in selected:
            continue
        if sid in seen:
            raise ValueError('Multiple daily versions for a selected station; choose one input directory')
        if (meta['inputs']['selection_sha256']!=selection_hash or
            meta['inputs']['sources']['gldas']['configuration_sha256']!=gldas_hash or
            meta['start']!=selection['start'] or meta['end_inclusive']!=selection['end']):
            raise ValueError('Daily bundle differs from the configured inputs/date window')
        seen.add(sid)
        sensors = meta['sensors']
        sm = [c for c,s in sensors.items() if s['variable']=='soil_moisture']
        columns = ['date','chirps_precip_mm_day','modis_lai_m2_m2','modis_lai_m2_m2__source_start',
            'modis_land_cover','modis_land_cover__source_start',*sm]
        frame = pq.read_table(metadata.with_name('daily.parquet'), columns=columns).to_pandas()
        dates = pd.DatetimeIndex(frame.date)
        expected = pd.date_range(selection['start'],selection['end'],freq='D')
        if not np.array_equal(dates.to_numpy(dtype='datetime64[ns]'),expected.to_numpy(dtype='datetime64[ns]')):
            raise ValueError('Daily input does not contain the complete configured calendar grid')
        identity = {'network':meta['station']['network'],'station_id':sid,'station_name':meta['station']['station_name']}
        station_flat = 0
        for column,sensor in sensors.items():
            finite_source = sensor['observations_in_window']-sensor['decoded_missing_values']
            masked.append({**identity,'sensor_column':column,'variable':sensor['variable'],
                'depth_from_m':sensor.get('depth_from'),'depth_to_m':sensor.get('depth_to'),
                'source_observations':sensor['observations_in_window'],'source_missing_values':sensor['decoded_missing_values'],
                'non_g_finite_values_masked':sensor['values_masked_by_quality'],
                'g_finite_values_accepted':finite_source-sensor['values_masked_by_quality'],
                'source_member':sensor['member']})
            if sensor['variable']!='soil_moisture':
                continue
            samples['ismn_sm_daily_sensor_days'].add(frame[column])
            for run in flatlines(frame[column],dates,flatline_min_days):
                flat.append({**identity,'sensor_column':column,'depth_from_m':sensor.get('depth_from'),
                    'depth_to_m':sensor.get('depth_to'),'source_member':sensor['member'],**run})
                station_flat += 1
        precip = frame.chirps_precip_mm_day.to_numpy(dtype=float)
        samples['chirps_station_days'].add(precip)
        chirps_cell = _cell(meta,'chirps')
        if chirps_cell is not None and chirps_cell not in seen_cells['chirps']:
            seen_cells['chirps'].add(chirps_cell);samples['chirps_native_cell_days'].add(precip)
        neg = np.flatnonzero(np.isfinite(precip)&(precip<0))
        for j in neg:
            negative.append({**identity,'date':dates[j],'precip_mm_day':precip[j]})
        flags, threshold, positive_days = precipitation_extremes(precip,chirps_positive_quantile)
        for j in np.flatnonzero(flags):
            extreme.append({**identity,'date':dates[j],'precip_mm_day':precip[j],
                'station_positive_quantile_threshold_mm_day':threshold,'positive_days_for_threshold':positive_days,
                'neighbor_corroboration':'not_performed_by_user_request'})
        for season in seasonal_precipitation(precip,dates).to_dict('records'):
            seasons.append({**identity,**season})
        for dataset,value,start,station_key,cell_key in [
            ('lai','modis_lai_m2_m2','modis_lai_m2_m2__source_start','lai_station_composites','lai_native_cell_composites'),
            ('land_cover','modis_land_cover','modis_land_cover__source_start','land_cover_station_years','land_cover_native_cell_years')]:
            intervals = frame.loc[frame[start].notna(),[start,value]].drop_duplicates(start)
            values = intervals[value].to_numpy(dtype=float,na_value=np.nan)
            samples[station_key].add(values)
            cell = _cell(meta,dataset)
            if cell is not None and cell not in seen_cells[dataset]:
                seen_cells[dataset].add(cell);samples[cell_key].add(values)
        station_summaries.append({**identity,'sm_flatline_runs':station_flat,'chirps_negative_days':len(neg),
            'chirps_extreme_days':int(flags.sum()),'chirps_positive_days':positive_days,
            'chirps_positive_quantile_threshold_mm_day':threshold,'chirps_missing_days':int(np.isnan(precip).sum())})
        inputs.append({'station_id':sid,'bundle_signature':meta['input_signature'],
                       'table_sha256':meta['table_sha256'],'description_sha256':sha256(metadata)})
        if len(seen)%100==0:
            print(f'Inspected {len(seen)}/{len(selected)} stations',flush=True)
    if seen!=set(selected):
        raise ValueError(f'Incomplete daily inspection inputs: {len(seen)}/{len(selected)} selected stations')
    gldas, gldas_sources = load_gldas(gldas_config,ancillary_cache,stations)
    gldas = gldas_checks(gldas,gldas_sum_tolerance)
    gldas.to_csv(output/'gldas_checks.csv',index=False)
    for name,rows,columns in [
        ('ismn_masked_counts',masked,['network','station_id','station_name','sensor_column','variable','depth_from_m','depth_to_m',
            'source_observations','source_missing_values','non_g_finite_values_masked','g_finite_values_accepted','source_member']),
        ('ismn_flatlines',flat,['network','station_id','station_name','sensor_column','depth_from_m','depth_to_m','source_member',
            'start','end','consecutive_days','value']),
        ('chirps_negative',negative,['network','station_id','station_name','date','precip_mm_day']),
        ('chirps_extremes',extreme,['network','station_id','station_name','date','precip_mm_day',
            'station_positive_quantile_threshold_mm_day','positive_days_for_threshold','neighbor_corroboration']),
        ('chirps_seasonal_totals',seasons,['network','station_id','station_name','season_year','season','season_start',
            'season_end_exclusive','calendar_days','days_in_selection','observed_days','missing_days_in_selection',
            'coverage_fraction','negative_days','nonfinite_days','observed_sum_mm','complete_season_total_mm','complete_season']),
        ('station_checks',station_summaries,['network','station_id','station_name','sm_flatline_runs','chirps_negative_days',
            'chirps_extreme_days','chirps_positive_days','chirps_positive_quantile_threshold_mm_day','chirps_missing_days'])]:
        _write_csv(output/(name+'.csv'),rows,columns)
    masking = defaultdict(Counter)
    by_network = defaultdict(Counter)
    for row in masked:
        for key in ['source_observations','source_missing_values','non_g_finite_values_masked','g_finite_values_accepted']:
            masking[row['variable']][key] += row[key]
        by_network[row['network']][row['variable']] += row['non_g_finite_values_masked']
    distributions = {name:value.summary(categorical=name.startswith('land_cover')) for name,value in samples.items()}
    distribution_plot({'G-only daily sensor means':samples['ismn_sm_daily_sensor_days'].plot_sample()},
        figures/'ismn_sm_distribution.png','ISMN soil moisture (G-only daily means)','SM (m³/m³)')
    distribution_plot({key:samples[key].plot_sample() for key in
        ['chirps_station_days','chirps_native_cell_days']}, figures/'chirps_precip_distribution.png',
        'CHIRPS precipitation (zero and tails retained)','Precipitation (mm/day)')
    summary = {'created_at':utc_now(),'scope':'Our ISMN/CHIRPS/GLDAS/MODIS features; published Morgan table excluded',
        'stations':len(seen),'start':selection['start'],'end_inclusive':selection['end'],'timezone':'UTC calendar days',
        'rules':{'flatline_min_consecutive_days':flatline_min_days,'flatline_comparison':'exact numeric equality; missing/gaps break runs',
            'chirps_positive_quantile':chirps_positive_quantile,'chirps_extreme_comparison':'strictly greater than station positive-value quantile',
            'neighbor_corroboration':'disabled_by_user','gldas_sum_tolerance':gldas_sum_tolerance,
            'gldas_fraction_and_porosity_bounds':[0,1],'gldas_texture_codes':[1,16],
            'gldas_texture_reference':'https://ldas.gsfc.nasa.gov/gldas/soils',
            'seasonal_rule':'DJF/MAM/JJA/SON; DJF year is January year; finite observed sum (negative values retained and flagged); complete total only with all calendar days finite'},
        'g_masking_by_variable':dict(masking),'g_masking_by_network':dict(by_network),'flatline_candidate_runs':len(flat),
        'chirps_negative_station_days':len(negative),'chirps_extreme_station_days':len(extreme),
        'gldas':{'physical_range_failure_stations':int(gldas.any_physical_range_failure.sum()),
            'closure_failure_stations':int(gldas.closure_outside_tolerance.sum()),
            'closure_unevaluable_stations':int((~gldas.closure_evaluable).sum())},
        'distributions':distributions,'sampling_note':'SM sensor-days; other plots show station weighting and unique native-cell weighting; MODIS uses source intervals, not repeated daily values; no independence/population claim',
        'inputs':{'ismn_selection_sha256':selection_hash,'gldas_config_sha256':gldas_hash,'gldas_extracts':gldas_sources,
                  'daily_bundles':inputs},
        'software_sha256':{p.name:sha256(p) for p in sorted(Path(__file__).parent.glob('*.py'))},
        'source_data_modified':False,'additional_measurement_masking':False,'values_filled':0,'network_access':False}
    write_json(output/'summary.json',json_safe(summary))
    return summary
