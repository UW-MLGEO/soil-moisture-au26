"""Conservative CF decoding that preserves raw arrays and explicit missingness."""

import re

import numpy as np

NETCDF_FILL_REFERENCE = 'https://docs.unidata.ucar.edu/netcdf-c/4.9.2/netcdf_8h.html'
NETCDF_ATTRIBUTE_REFERENCE = 'https://docs.unidata.ucar.edu/netcdf-c/current/attribute_conventions.html'


def verified_default_fill(attributes, creation):
    """Return a verified floating NetCDF default, never a generic HDF5 zero.

    Require absent CF missing-code attributes, a NetCDF file marker, a
    user-defined HDF5 creation fill with filling enabled, float32/64 dtype, and
    exact agreement with NC_FILL_FLOAT/NC_FILL_DOUBLE. Other types, nofill mode
    and unrecognized/custom fills are left unchanged rather than guessed.
    """
    if '_FillValue' in attributes or 'missing_value' in attributes or not creation:
        return None
    markers = creation.get('netcdf_file_attributes', {})
    properties = markers.get('_NCProperties', '')
    classic = np.asarray(markers.get('_nc3_strict', 0))
    marker_verified = (isinstance(properties, str) and
                       bool(re.search(r'(?:^|[,|])(?:netcdf|netcdflibversion)=', properties)))
    marker_verified |= classic.size == 1 and classic.item() == 1
    if not marker_verified or creation.get('fill_value_status') != 'user_defined':
        return None
    if creation.get('fill_time') not in ('allocation', 'if_set'):
        return None
    dtype = np.dtype(creation['dtype'])
    if dtype.kind != 'f' or dtype.itemsize not in (4, 8):  # not-science: NetCDF float32/64 default fill (NC_FILL_FLOAT/DOUBLE)
        return None
    expected = np.asarray(9.9692099683868690e36, dtype=dtype).item()  # not-science: NetCDF float32/64 default fill (NC_FILL_FLOAT/DOUBLE)
    observed = np.asarray(creation.get('fill_value'))
    if observed.size != 1 or float(observed.item()) != expected:
        return None
    return float(expected)


def _scalar_attribute(attributes, name, default):
    """CF scalars may be encoded as one-element NetCDF attribute arrays."""
    value = np.asarray(attributes.get(name, default))
    if value.size != 1:
        raise ValueError(f'CF {name} must contain exactly one value')
    return float(value.item())


def decode_cf_values(values, attributes, *, creation_properties=None):
    """Decode declared CF codes or a verified NetCDF default, with counts."""
    raw = np.asarray(values)
    decoded = raw.astype(float, copy=True)
    mask = np.zeros(raw.shape, dtype=bool)
    codes = []
    for key in ('_FillValue', 'missing_value'):
        if key not in attributes:
            continue
        for code in np.atleast_1d(attributes[key]):
            code = float(code)
            mask |= np.isnan(raw) if np.isnan(code) else raw == code
            codes.append('nan' if np.isnan(code) else code)
    declared_count = int(mask.sum())
    default_fill = verified_default_fill(attributes, creation_properties)
    implicit_mask = np.zeros(raw.shape, dtype=bool)
    if default_fill is not None:
        if raw.dtype != np.dtype(creation_properties['dtype']):
            raise ValueError('Raw station dtype differs from source creation metadata')
        implicit_mask = raw == default_fill
        mask |= implicit_mask
    decoded[mask] = np.nan
    scale = _scalar_attribute(attributes, 'scale_factor', 1)
    offset = _scalar_attribute(attributes, 'add_offset', 0)
    decoded = decoded * scale + offset
    return decoded, {'declared_missing_codes': codes, 'declared_missing_count': declared_count,
                     'implicit_netcdf_default_fill': default_fill,
                     'implicit_netcdf_default_fill_count': int(implicit_mask.sum()),
                     'total_missing_code_count': int(mask.sum()),
                     'default_fill_provenance': ({
                         'source_creation_properties': creation_properties,
                         'reference_urls': [NETCDF_ATTRIBUTE_REFERENCE, NETCDF_FILL_REFERENCE],
                         'rule': 'Absent CF missing codes; verified NetCDF float default in creation properties'}
                         if default_fill is not None else None),
                     'input_nan_count': int(np.isnan(raw).sum()), 'scale_factor': scale,
                     'add_offset': offset, 'values_filled': 0,
                     'decoded_units': attributes.get('units')}

