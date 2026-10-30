"""Check environment imports and a small calculation without downloads or files.

Run from the repository root with `pixi run smoke`; requires Pixi.
"""

import importlib


def test_environment():
    """Verify direct dependencies import and NumPy, pandas, and xarray interoperate."""
    for module in (
        "matplotlib",
        "numpy",
        "pandas",
        "ipykernel",
        "geopandas",
        "pooch",
        "obspy",
        "jupyterlab",
        "xarray",
    ):
        importlib.import_module(module)

    import numpy as np
    import pandas as pd
    import xarray as xr

    values = xr.DataArray(pd.Series([1.0, np.nan, 3.0], name="values"))
    np.testing.assert_allclose(values.mean(skipna=True).item(), 2.0)


if __name__ == "__main__":
    test_environment()
    print("Smoke test passed: dependency imports and a small calculation succeeded.")
