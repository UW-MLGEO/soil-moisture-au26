"""Interpret GLDAS categorical values without inventing missing classes."""
import numpy as np
import pandas as pd

# The GLDAS texture NetCDF stores FAO classes as float32 with representation
# noise (e.g. 2.9999998, 4.0000005 for ~10% of CONUS cells). Classes are
# snapped to the nearest integer only within this tolerance; anything further
# from an integer is an error, not a class.
TEXTURE_CLASS_TOLERANCE = 1e-4


def texture_class(value):
    """Return (integer class or None for missing, whether float noise was removed)."""
    if value is None or pd.isna(value):
        return None, False
    value = float(value)
    if not np.isfinite(value):
        raise ValueError(f'Non-finite GLDAS texture class: {value!r}')
    nearest = round(value)
    if abs(value - nearest) > TEXTURE_CLASS_TOLERANCE:
        raise ValueError(f'GLDAS texture class {value!r} is not within '
                         f'{TEXTURE_CLASS_TOLERANCE} of an integer')
    return int(nearest), value != nearest

