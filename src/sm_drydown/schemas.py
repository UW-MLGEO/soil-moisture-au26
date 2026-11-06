"""Table schemas, asserted whenever a table is written. Units are in column names.

Types: ``str``, ``int`` (int64), ``Int64`` (nullable integer), ``float``,
``date`` (timezone-naive UTC midnight), ``bool``, ``boolean`` (nullable bool).
``missing_reason`` vocabularies are documented in sm_drydown.features.common.
Columns starting with ``qc_`` (raw QC words, nullable integers) are allowed
wherever ``qc_columns`` is true.
"""

import pandas as pd

SENSOR_DAY_COUNTS = {'n_obs': 'int', 'n_good': 'int', 'n_valid': 'int'}
INTERVAL = {'source_start': 'date', 'source_end_exclusive': 'date'}

SCHEMAS = {
    # ── Feature tables (stage 2) ──────────────────────────────────────────────
    'theta_m3m3': {'columns': {'sensor_id': 'str', 'station_id': 'int', 'date': 'date', **SENSOR_DAY_COUNTS,
                               'theta_m3m3': 'float', 'missing_reason': 'str'}},
    'soil_temp_degc': {'columns': {'sensor_id': 'str', 'station_id': 'int', 'date': 'date', **SENSOR_DAY_COUNTS,
                                   'soil_temp_degc': 'float', 'missing_reason': 'str'}},
    'ismn_qc_audit': {'columns': {'sensor_id': 'str', 'date': 'date', 'ismn_flag_counts': 'str',
                                  'provider_flag_counts': 'str'}},
    'ismn_sensors': {'columns': {'sensor_id': 'str', 'station_id': 'int', 'network': 'str', 'variable': 'str',
                                 'depth_from_m': 'float', 'depth_to_m': 'float', 'instrument': 'str',
                                 'source_units': 'str', 'member': 'str', 'member_sha256': 'str',
                                 'native_latitude': 'float', 'native_longitude': 'float', 'elevation_m': 'float',
                                 'rows': 'int', 'observations_in_window': 'int',
                                 'observations_outside_window': 'int', 'decoded_missing_values': 'int',
                                 'values_masked_by_quality': 'int', 'days_with_value': 'int'}},
    'precip_chirps_mm_day': {'columns': {'station_id': 'int', 'date': 'date', 'precip_chirps_mm_day': 'float',
                                         'provider_null': 'boolean', 'provider_absent_row': 'boolean',
                                         'missing_reason': 'str'}},
    'gldas_static': {'columns': {'station_id': 'int', 'gldas_texture_class': 'Int64',
                                 'gldas_sand_frac': 'float', 'gldas_silt_frac': 'float',
                                 'gldas_clay_frac': 'float', 'gldas_porosity': 'float',
                                 'gldas_texture_source_value': 'float',
                                 'gldas_texture_float_noise_removed': 'bool',
                                 **{f'{c}__{s}': 'str' for c in ('gldas_texture_class', 'gldas_sand_frac',
                                                                 'gldas_silt_frac', 'gldas_clay_frac',
                                                                 'gldas_porosity')
                                    for s in ('missing_reason', 'cell')}}},
    'lai_m2m2': {'columns': {'station_id': 'int', 'date': 'date', 'lai_m2m2': 'float', 'missing_reason': 'str',
                             **INTERVAL}, 'qc_columns': True},
    'igbp_class': {'columns': {'station_id': 'int', 'date': 'date', 'igbp_class': 'Int64', 'missing_reason': 'str',
                               **INTERVAL}, 'qc_columns': True},
    # ── Assembled tables (stage 3) ────────────────────────────────────────────
    'stations': {'columns': {'station_id': 'int', 'network': 'str', 'station_name': 'str', 'lat_deg': 'float',
                             'lon_deg': 'float'}, 'extra_columns': True},
    'sensors': {'columns': {'sensor_id': 'str', 'station_id': 'int', 'network': 'str', 'variable': 'str',
                            'depth_from_m': 'float', 'depth_to_m': 'float'}, 'extra_columns': True},
    'sensor_day': {'columns': {'sensor_id': 'str', 'station_id': 'int', 'network': 'str', 'date': 'date',
                               **SENSOR_DAY_COUNTS}, 'extra_columns': True},
}

KINDS = {
    'str': lambda s: pd.api.types.is_string_dtype(s) or (s.dtype == object and s.dropna().map(type).eq(str).all()),
    'int': lambda s: pd.api.types.is_integer_dtype(s) and not isinstance(s.dtype, pd.Int64Dtype),
    'Int64': lambda s: isinstance(s.dtype, pd.Int64Dtype) or s.isna().all(),
    'float': lambda s: pd.api.types.is_float_dtype(s) or s.isna().all(),
    'date': lambda s: pd.api.types.is_datetime64_dtype(s) and getattr(s.dt, 'tz', None) is None,
    'bool': lambda s: pd.api.types.is_bool_dtype(s),
    'boolean': lambda s: isinstance(s.dtype, pd.BooleanDtype) or s.isna().all(),
}


def assert_schema(table: pd.DataFrame, name: str) -> None:
    """Raise if ``table`` lacks a column, has a wrong type or an undeclared column."""
    schema = SCHEMAS[name]
    for column, kind in schema['columns'].items():
        if column not in table:
            raise ValueError(f'{name}: missing column {column}')
        if not KINDS[kind](table[column]):
            raise ValueError(f'{name}.{column}: expected {kind}, found {table[column].dtype}')
    extra = [c for c in table if c not in schema['columns']]
    if schema.get('qc_columns'):
        extra = [c for c in extra if not c.startswith('qc_')]
    if extra and not schema.get('extra_columns'):
        raise ValueError(f'{name}: undeclared column(s) {extra}')
