"""Feature registry: which source builds which feature table, from which inputs.

A *feature group* is one pass over one acquisition (one lock). Its ``build``
function returns ``{table: DataFrame}`` for the stations of one ISMN network.
Every feature named in ``pipeline.yml features:`` must appear in FEATURES with
its group, table and level:

- ``sensor``: one row per sensor per day (joined on sensor_id);
- ``station_day``: one row per station per day (joined on station_id, date);
- ``static``: one row per station (broadcast to every day);
- ``standalone``: written but not joined into sensor_day (e.g. soil temperature,
  whose sensors are separate instruments from the soil-moisture sensors).

To add a feature (README "Adding a feature"): a config section in pipeline.yml,
a build function in its own module, entries in GROUPS and FEATURES, a schema in
sm_drydown.schemas, and one step block in scripts/homogenize.py. A group's cache
key covers only its config sections, inputs and own modules, so adding a group
never recomputes the others.
"""

from dataclasses import dataclass, field
from typing import Callable

from sm_drydown.features import gridded, ismn


@dataclass(frozen=True)
class Group:
    name: str
    request: str                      # lock (locks/<request>.json) the group reads
    sections: tuple                   # pipeline.yml sections in the cache key
    code: tuple                       # src/sm_drydown files in the cache key
    build: Callable                   # build(cfg, inputs, stations, dates) -> {table: DataFrame}
    tables: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Feature:
    group: str
    table: str
    level: str
    unit: str


COMMON = ('features/common.py', 'formats', 'schemas.py')
GRIDDED = COMMON + ('features/gridded.py',)
GROUPS = {
    'ismn': Group('ismn', 'ismn', ('study', 'ismn'), COMMON + ('features/ismn.py',), ismn.build,
                  {'theta_m3m3': 'sensor', 'soil_temp_degc': 'standalone', 'ismn_sensors': 'meta',
                   'ismn_qc_audit': 'audit'}),
    'chirps': Group('chirps', 'chirps', ('study', 'chirps'), GRIDDED, gridded.build_chirps,
                    {'precip_chirps_mm_day': 'station_day'}),
    'gldas': Group('gldas', 'gldas', ('gldas',), GRIDDED, gridded.build_gldas, {'gldas_static': 'static'}),
    'modis_lai': Group('modis_lai', 'modis_lai', ('study', 'modis_lai'), GRIDDED, gridded.build_lai,
                       {'lai_m2m2': 'station_day'}),
    'modis_land_cover': Group('modis_land_cover', 'modis_land_cover', ('study', 'modis_land_cover'), GRIDDED,
                              gridded.build_land_cover, {'igbp_class': 'station_day'}),
}
FEATURES = {
    'theta_m3m3': Feature('ismn', 'theta_m3m3', 'sensor', 'm3 m-3'),
    'soil_temp_degc': Feature('ismn', 'soil_temp_degc', 'standalone', 'degC'),
    'precip_chirps_mm_day': Feature('chirps', 'precip_chirps_mm_day', 'station_day', 'mm d-1'),
    'gldas_texture_class': Feature('gldas', 'gldas_static', 'static', 'FAO class 1-16'),
    'gldas_sand_frac': Feature('gldas', 'gldas_static', 'static', '-'),
    'gldas_silt_frac': Feature('gldas', 'gldas_static', 'static', '-'),
    'gldas_clay_frac': Feature('gldas', 'gldas_static', 'static', '-'),
    'gldas_porosity': Feature('gldas', 'gldas_static', 'static', '-'),
    'igbp_class': Feature('modis_land_cover', 'igbp_class', 'station_day', 'IGBP class'),
    'lai_m2m2': Feature('modis_lai', 'lai_m2m2', 'station_day', 'm2 m-2'),
}
