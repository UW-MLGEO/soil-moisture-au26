"""Load, merge and validate the configuration.

Every scientific choice lives in ``config/pipeline.yml``. A run file
(``config/runs/<name>.yml``) overrides only the keys it lists. Machine-specific
locations are in ``config/paths.yml`` (git-ignored; copy ``paths.example.yml``),
object storage in ``config/storage.yml``, and provider facts and provenance in
``config/sources/<source>.yml``. Merge order: pipeline -> run -> overrides.

The dataclasses below are the schema: one frozen dataclass per section, with
field types but no default values (defaults live only in ``pipeline.yml``).
Unknown keys, missing keys, wrong types and values outside a ``Literal`` are
rejected. This module holds no scientific numbers.
"""

import dataclasses
import hashlib
import json
import types
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / 'config'
SELECTIONS_DIR = REPO_ROOT / 'data' / 'selections'
LOCKS_DIR = REPO_ROOT / 'locks'
SRC_DIR = REPO_ROOT / 'src' / 'sm_drydown'

Number = int | float
SOURCES = ('ismn', 'chirps', 'gldas', 'modis_lai', 'modis_land_cover', 'morgan_2025')


@dataclass(frozen=True)
class Study:
    start: str
    end: str
    day_definition: Literal['utc']
    crs: str


@dataclass(frozen=True)
class Region:
    kind: Literal['global', 'named_boundary', 'bbox', 'polygon_file']
    name: str | None
    bbox: list[Number] | None
    polygon_file: str | None
    buffer_deg: Number


@dataclass(frozen=True)
class Selection:
    name: str
    inventory_url: str
    inventory_snapshot: str | None
    region: Region
    station_crs: str
    networks: Literal['all'] | list[str]
    exclude_networks: list[str]
    require_variable: Literal['soil_moisture']
    min_overlap_days: int
    depth_range_m: list[Number]


@dataclass(frozen=True)
class Subset:
    networks: Literal['all'] | list[str]
    station_ids: Literal['all'] | list[int]


@dataclass(frozen=True)
class ProviderOptions:
    format: Literal['own']
    fill_nan: Literal[False]
    g_flag_only: Literal[False]
    depth_range_cm: list[Number]
    climates: list[str]
    landcovers: list[str]


@dataclass(frozen=True)
class Ismn:
    request_variables: list[Literal['soil_moisture', 'precipitation', 'soil_temperature']]
    provider_options: ProviderOptions
    daily_variables: list[Literal['soil_moisture', 'soil_temperature']]
    flags_kept: list[str]
    temperature_flags_kept: list[str]
    missing_codes: dict[str, list[Number]]
    daily_statistic: Literal['mean', 'median']
    min_obs_per_day: int
    duplicate_timestamps: Literal['error']
    sensor_day_span: Literal['observed', 'study_window']
    soil_moisture_units: Literal['m3/m3']
    temperature_units: Literal['degC']


@dataclass(frozen=True)
class Chirps:
    collection: str
    band: str
    sampling: Literal['native_cell']
    null_is_zero_rain: bool
    absent_row_is_zero_rain: bool


@dataclass(frozen=True)
class Gldas:
    sampling: Literal['native_cell']
    tie_break: Literal['greater_coordinate']
    depth_assignment: Literal['top_layer_through_50cm']
    max_sensor_depth_m: Number
    default_fill: Literal['verified_only']
    percent_to_fraction: bool
    fraction_bounds: list[Number]
    out_of_bounds: Literal['error']
    texture_snap_tolerance: Number


@dataclass(frozen=True)
class QcRule:
    column: str
    offset: int
    mask: int
    allowed: list[int]


@dataclass(frozen=True)
class ModisLai:
    collection: str
    cadence_days: int
    cut_composites_at_year_end: Literal[True]
    bands: list[str]
    sampling: Literal['native_cell']
    band: str
    qc_bands: list[str]
    mask_fill_before_scale: Literal[True]
    qc_mode: Literal['bit_rules', 'retain_flagged']
    qc_rules: list[QcRule]
    missing_qc_is_good: bool
    max_carry_days: Literal[0]


@dataclass(frozen=True)
class ModisLandCover:
    collection: str
    bands: list[str]
    sampling: Literal['native_cell']
    scheme: str
    qc_bands: list[str]
    qc_mode: Literal['retain_flagged']


@dataclass(frozen=True)
class Inspection:
    flatline_min_days: int
    chirps_extreme_quantile: Number
    gldas_fraction_sum_tolerance: Number
    gldas_texture_codes: list[int]
    summary_quantiles: list[Number]
    seasons: dict[str, list[int]]
    histogram_bins: int


@dataclass(frozen=True)
class ExportDrydownQ:
    ai_window_days: int
    ai_window_min_valid_days: int
    max_sensor_depth_m: Number | None
    earth_radius_km: Number


@dataclass(frozen=True)
class Runtime:
    log_level: Literal['DEBUG', 'INFO', 'WARNING']


@dataclass(frozen=True)
class Paths:
    ismn_cache: str
    ancillary_cache: str
    morgan_cache: str
    selection_cache: str
    outputs_dir: str
    figure_dir: str
    drydown_q_prepared_dir: str | None


@dataclass(frozen=True)
class Config:
    run_name: str
    study: Study
    selection: Selection
    subset: Subset
    ismn: Ismn
    chirps: Chirps
    gldas: Gldas
    modis_lai: ModisLai
    modis_land_cover: ModisLandCover
    features: list[str]
    inspection: Inspection
    export_drydown_q: ExportDrydownQ
    runtime: Runtime
    paths: Paths
    storage: dict
    sources: dict
    regions: dict

    def section(self, name):
        return getattr(self, name)


SECTIONS = {
    'study': Study, 'selection': Selection, 'subset': Subset, 'ismn': Ismn, 'chirps': Chirps, 'gldas': Gldas,
    'modis_lai': ModisLai, 'modis_land_cover': ModisLandCover, 'features': list[str],
    'inspection': Inspection, 'export_drydown_q': ExportDrydownQ, 'runtime': Runtime,
}


class ConfigError(ValueError):
    """Raised for unknown keys, missing keys or values of the wrong type."""


def _matches(value, annotation) -> bool:
    """True if ``value`` (parsed from YAML) is an instance of ``annotation``."""
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if annotation is type(None):
        return value is None
    if origin in (typing.Union, types.UnionType):
        return any(_matches(value, a) for a in args)
    if origin is Literal:
        return any(value == a and type(value) is type(a) for a in args)
    if origin is list:
        return isinstance(value, list) and all(_matches(v, args[0]) for v in value)
    if origin is dict:
        return isinstance(value, dict) and all(
            _matches(k, args[0]) and _matches(v, args[1]) for k, v in value.items())
    if dataclasses.is_dataclass(annotation):
        return isinstance(value, dict)
    if annotation is int:  # YAML booleans are ints in Python; reject them here
        return isinstance(value, int) and not isinstance(value, bool)
    if annotation is float:
        return isinstance(value, float)
    return isinstance(value, annotation)


def _build(cls, values, where: str):
    """Build a (possibly nested) dataclass from a dict, rejecting any mismatch."""
    if not dataclasses.is_dataclass(cls):
        if not _matches(values, cls):
            raise ConfigError(f'{where} = {values!r} does not match type {cls}')
        return values
    if not isinstance(values, dict):
        raise ConfigError(f'{where} must be a mapping')
    fields = {f.name: f.type for f in dataclasses.fields(cls)}
    unknown, missing = set(values) - set(fields), set(fields) - set(values)
    if unknown:
        raise ConfigError(f'{where}: unknown key(s) {sorted(unknown)}')
    if missing:
        raise ConfigError(f'{where}: missing key(s) {sorted(missing)}')
    built = {}
    for name, annotation in fields.items():
        value = values[name]
        if not _matches(value, annotation):
            raise ConfigError(f'{where}.{name} = {value!r} does not match type {annotation}')
        if dataclasses.is_dataclass(annotation):
            value = _build(annotation, value, f'{where}.{name}')
        elif typing.get_origin(annotation) is list and dataclasses.is_dataclass(typing.get_args(annotation)[0]):
            value = [_build(typing.get_args(annotation)[0], v, f'{where}.{name}[{i}]') for i, v in enumerate(value)]
        built[name] = value
    return cls(**built)


def read_yaml(path: Path):
    with open(path) as f:
        data = yaml.safe_load(f)
    return {} if data is None else data


def merge(base: dict, override: dict, where: str) -> dict:
    """Return ``base`` with the keys of ``override`` replaced, recursing into mappings.

    An override may only name keys that exist in ``base``.
    """
    merged = dict(base)
    for key, value in override.items():
        if key not in base:
            raise ConfigError(f'{where}: unknown key {key!r}')
        if isinstance(base[key], dict) and isinstance(value, dict):
            merged[key] = merge(base[key], value, f'{where}.{key}')
        else:
            merged[key] = value
    return merged


def _resolve(path):
    path = Path(path).expanduser()
    return str(path if path.is_absolute() else REPO_ROOT / path)


def load_paths(paths_file=None) -> Paths:
    """Machine-specific paths; relative paths are resolved against the repository root."""
    paths_file = Path(paths_file) if paths_file else CONFIG_DIR / 'paths.yml'
    if not paths_file.exists():
        paths_file = CONFIG_DIR / 'paths.example.yml'
    values = read_yaml(paths_file)
    values = {k: (_resolve(v) if isinstance(v, str) else v) for k, v in values.items()}
    return _build(Paths, values, str(paths_file))


def load_sources() -> dict:
    """Provider facts and provenance, one YAML file per source."""
    return {name: read_yaml(CONFIG_DIR / 'sources' / f'{name}.yml') for name in SOURCES}


def load_config(run: str = 'baseline', paths_file=None, overrides: dict | None = None) -> Config:
    """Load pipeline.yml, apply ``config/runs/<run>.yml`` and ``overrides``, then paths."""
    merged = read_yaml(CONFIG_DIR / 'pipeline.yml')
    run_file = CONFIG_DIR / 'runs' / f'{run}.yml'
    if not run_file.exists():
        raise ConfigError(f'no run file {run_file}')
    merged = merge(merged, read_yaml(run_file), str(run_file))
    if overrides:
        merged = merge(merged, overrides, 'overrides')
    unknown = set(merged) - set(SECTIONS)
    if unknown:
        raise ConfigError(f'pipeline.yml: unknown section(s) {sorted(unknown)}')
    sections = {name: _build(cls, merged.get(name), name) for name, cls in SECTIONS.items()}
    cfg = Config(run_name=run, paths=load_paths(paths_file), storage=read_yaml(CONFIG_DIR / 'storage.yml'),
                 sources=load_sources(),
                 regions=read_yaml(CONFIG_DIR / 'regions.yml'), **sections)
    _check(cfg)
    return cfg


def _check(cfg: Config):
    """Cross-section consistency that types alone cannot express."""
    low, high = cfg.selection.depth_range_m
    if low > high or cfg.study.start > cfg.study.end:
        raise ConfigError('reversed selection.depth_range_m or study window')
    if cfg.selection.region.kind == 'named_boundary' and not cfg.selection.region.name:
        raise ConfigError('selection.region.name is required for named_boundary')
    if cfg.selection.region.kind == 'bbox' and len(cfg.selection.region.bbox or []) != 4:  # not-science: a bbox has four bounds
        raise ConfigError('selection.region.bbox needs [lon_min, lat_min, lon_max, lat_max]')
    if not set(cfg.ismn.daily_variables) <= set(cfg.ismn.request_variables):
        raise ConfigError('ismn.daily_variables must be requested in ismn.request_variables')
    for name in ('modis_lai', 'modis_land_cover'):
        section = cfg.section(name)
        primary = section.band if name == 'modis_lai' else section.scheme
        if not {primary, *section.qc_bands} <= set(section.bands):
            raise ConfigError(f'{name}: feature and QC bands must be acquired bands')
    if cfg.ismn.min_obs_per_day < 1:
        raise ConfigError('ismn.min_obs_per_day must be at least 1')


def as_dict(obj):
    """Plain-dict view of a config section (for hashing and writing)."""
    return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else obj


def config_hash(*parts) -> str:
    """SHA-256 of config sections, dicts or strings, in canonical JSON form."""
    canonical = [as_dict(p) for p in parts]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()


def code_hash(*modules: str) -> str:
    """SHA-256 of the named source files under src/sm_drydown (all .py files if none)."""
    files = ([SRC_DIR / m for m in modules] if modules else sorted(SRC_DIR.rglob('*.py')))
    digest = hashlib.sha256()
    for path in files:
        if path.is_dir():
            for item in sorted(path.rglob('*.py')):
                digest.update(item.relative_to(SRC_DIR).as_posix().encode() + item.read_bytes())
        else:
            digest.update(path.relative_to(SRC_DIR).as_posix().encode() + path.read_bytes())
    return digest.hexdigest()


def differences_from_default(cfg: Config) -> dict[str, tuple]:
    """``{"section.key": (default, actual)}`` for every setting that differs from pipeline.yml."""
    default = read_yaml(CONFIG_DIR / 'pipeline.yml')
    diffs = {}

    def walk(prefix, base, actual):
        if isinstance(base, dict) and isinstance(actual, dict):
            for key in base:
                walk(f'{prefix}.{key}' if prefix else key, base[key], actual[key])
        elif base != actual:
            diffs[prefix] = (base, actual)

    for section in SECTIONS:
        walk(section, default[section], as_dict(getattr(cfg, section)))
    return diffs


def write_resolved(cfg: Config, path: Path) -> None:
    """Write the fully merged scientific config and paths as YAML."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = {'run_name': cfg.run_name, **{s: as_dict(getattr(cfg, s)) for s in SECTIONS},
                'paths': as_dict(cfg.paths)}
    with open(path, 'w') as f:
        yaml.safe_dump(resolved, f, sort_keys=False)
