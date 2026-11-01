"""Audit public metadata and estimate storage without bulk science downloads.

Reproduce the frozen estimate, offline:
    pixi run python scripts/estimate_data_volume.py
Collect current metadata into an ignored directory (requires internet and curl):
    pixi run python scripts/estimate_data_volume.py --collect \
        --cache-dir output/data_volume/cache --output-dir output/data_volume

Uses the existing Pixi environment; collection also uses geopandas for Census
boundaries. Cached responses require a matching SHA-256, URL, and format check.
Only missing/invalid cache entries are fetched. Use a new cache directory for a
new dated audit. The study window and remote-response samples stay fixed.
Output JSON contains full-file and remote-station GB estimates for both regions,
plus the report's sensitivity range. No bulk observations are downloaded.
Checks: pixi run python tests/test_data_volume.py; pixi run smoke.
See docs/data_volume_audit.md for assumptions and interpretation.
"""

import argparse
import csv
import hashlib
import io
import json
import re
import statistics
import subprocess
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

START = date(2002, 7, 4)
END = date(2025, 12, 31)
CMR = "https://cmr.earthdata.nasa.gov/search/"
COLLECTIONS = {"MCD12Q1": "C2484079608-LPCLOUD", "MCD15A2H": "C2222147000-LPCLOUD"}
CHIRPS = "https://data.chc.ucsb.edu/products/CHIRPS/v2.0/global_daily/netcdf/p05/"
DPET = "https://data.bris.ac.uk/datasets/qb8ujazzda0s2aykkv0oq0ctp/"
SOILS = "https://ldas.gsfc.nasa.gov/sites/default/files/ldas/gldas/SOILS/"

# Response-body sizes measured 2026-11-01; one location/date per request.
# CHIRPS: ClimateSERV datatype=0, operationtype=5, 2025-07-12, a polygon
# inside the cell at (-84.275, 35.975). MODIS: ORNL TESViS /subset, CSV,
# kmAboveBelow=kmLeftRight=0, all bands, (35.958767, -84.287433),
# MCD12Q1 A2024001 and MCD15A2H A2025193. Extrapolations, not byte minima.
REMOTE_SUBSET_BYTES_PER_STATION_DATE = {"CHIRPS": 576, "MCD12Q1": 1587, "MCD15A2H": 755}


def overlap_days(first, last, start=START, end=END):
    """Inclusive span, not the number of valid observations inside that span."""
    if not first or not last or first == "never" or last == "never":
        return 0
    first, last = date.fromisoformat(first[:10]), date.fromisoformat(last[:10])
    if last < first:
        raise ValueError("Reversed source date interval")
    return max(0, (min(last, end) - max(first, start)).days + 1)


def composite_count(start=START, end=END):
    """MODIS 8-day starts reset on January 1; the final period is shorter."""
    return sum(start <= date(y, 1, 1) + timedelta(days=d) <= end
               for y in range(start.year, end.year + 1) for d in range(0, 365, 8))


def content_length(data):
    """Read the final HTTP response's positive file size, ignoring redirects."""
    final_headers = re.split(rb"(?im)^HTTP/\S+ \d{3}[^\r\n]*", data)[-1]
    match = re.search(rb"(?im)^content-length:\s*(\d+)\s*$", final_headers)
    if not match or int(match[1]) <= 0:
        raise ValueError("Final HTTP response has no positive Content-Length")
    return int(match[1])


def validate(data, kind):
    """Reject malformed responses before caching or parsing provider metadata."""
    if kind == "json":
        json.loads(data)
    elif kind == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if z.testzip() is not None:
                raise ValueError("Invalid ZIP CRC")
    elif kind == "head":
        content_length(data)
    elif kind == "html":
        if b"<html" not in data.lower():
            raise ValueError("Expected HTML listing")
    else:
        raise ValueError(f"Unknown response format: {kind}")


def fetch(cache, name, url, kind="json"):
    """Reuse verified bytes; otherwise fetch and record URL, time and SHA-256."""
    path = cache / name
    meta_path = path.with_name(path.name + ".provenance.json")
    try:
        data, meta = path.read_bytes(), json.loads(meta_path.read_text())
        if not isinstance(meta, dict) or meta.get("url") != url or meta.get("sha256") != hashlib.sha256(data).hexdigest():
            raise ValueError("Cache provenance mismatch")
        validate(data, kind)
        return data, meta
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        pass
    cache.mkdir(parents=True, exist_ok=True)
    # curl is used so HTTP failures are explicit, including redirects and timeouts.
    command = ["curl", "-fsSL", "--retry", "2", "--max-time", "60"]
    if kind == "head":
        command.append("--head")
    data = subprocess.check_output(command + [url])
    validate(data, kind)
    meta = {"url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "sha256": hashlib.sha256(data).hexdigest(), "response_bytes": len(data)}
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as f:
        f.write(data)
        tmp = Path(f.name)
    tmp.replace(path)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return data, meta


def entries(data):
    """Require a nonempty CMR page below the collector's 2,000-entry limit."""
    result = json.loads(data)["feed"]["entry"]
    if not result or len(result) >= 2000:
        raise ValueError("Empty or possibly truncated CMR response")
    return result


def granule_bytes(items):
    # CMR legacy ECHO granule_size is in MiB despite often being labelled MB.
    return round(sum(float(x["granule_size"]) * 2**20 for x in items))


def tile(item):
    return re.search(r"h\d{2}v\d{2}", item["producer_granule_id"])[0]


def summarize_stations(networks, conus_ids):
    """Count all stations, but accrue sensor-days only within the study window.

    Ancillary histories are unavailable: temperature uses soil-moisture sensor
    spans; precipitation uses one stream per station reporting that variable.
    """
    results = {}
    by_network = []
    for region in ("global", "conus", "chirps_domain"):
        totals = {"all_stations": 0, "overlap_stations": 0, "sm_overlap_stations": 0,
                  "missing_station_dates": 0,
                  "station_days": 0, "sm_sensor_days": 0,
                  "temperature_proxy_sensor_days": 0, "precipitation_proxy_station_days": 0}
        all_nets, overlap_nets = set(), set()
        for net in networks:
            rows = []
            for s in net["Stations"]:
                if region == "conus" and s["stationID"] not in conus_ids:
                    continue
                if region == "chirps_domain" and not -50 <= s["lat"] <= 50:
                    continue
                days = overlap_days(s.get("minimum"), s.get("maximum"))
                sm_days = sum(overlap_days(t["start_date"], t["end_date"])
                              for t in s["Timeseries"])
                variables = s.get("variableText", "")
                row = {"all_stations": 1, "overlap_stations": int(days > 0),
                       "missing_station_dates": int(s.get("minimum") in (None, "", "never") or
                                                    s.get("maximum") in (None, "", "never")),
                       "sm_overlap_stations": int(sm_days > 0), "station_days": days,
                       "sm_sensor_days": sm_days,
                       # Ancillary sensor histories are not exposed in this inventory.
                       "temperature_proxy_sensor_days": sm_days if "soil temperature" in variables else 0,
                       "precipitation_proxy_station_days": days if "precipitation" in variables else 0}
                rows.append(row)
                all_nets.add(net["networkID"])
                if sm_days:
                    overlap_nets.add(net["networkID"])
                for key in totals:
                    totals[key] += row[key]
            by_network.append({"region": region, "network": net["networkID"],
                               "status": net.get("network_status"),
                               "country_label": net.get("network_country"),
                               "operation_start": net.get("network_op_start"),
                               "operation_end": net.get("network_op_end"),
                               **{key: sum(r[key] for r in rows) for key in totals}})
        results[region] = {**totals, "all_networks": len(all_nets),
                           "sm_overlap_networks": len(overlap_nets)}
    return results, by_network


def collect(cache):
    """Refresh public metadata for the fixed study window; preserve provenance."""
    import geopandas as gpd

    sources = {}

    def get(name, url, kind="json"):
        data, meta = fetch(cache, name, url, kind)
        sources[name] = meta
        return data

    networks = json.loads(get("ismn-stations.json",
        "https://ismn.earth/static/dataviewer/network_station_details.json"))["Networks"]
    if len(networks) < 80 or sum(len(n["Stations"]) for n in networks) < 3000:
        raise ValueError("Unexpectedly incomplete ISMN inventory")
    ids = [s["stationID"] for n in networks for s in n["Stations"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate ISMN station IDs")
    get("conus-states.zip", "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_5m.zip", "zip")
    states = gpd.read_file(cache / "conus-states.zip")
    states = states[~states.STUSPS.isin(["AK", "HI", "PR", "GU", "VI", "MP", "AS"])]
    if len(states) != 49 or states.crs.to_epsg() != 4269:
        raise ValueError("Expected 48 states plus DC in NAD83 geographic coordinates")
    # For this rough count, treat WGS84-style station lon/lat as NAD83 lon/lat.
    # A 0.005-degree tolerance handles generalized coastal boundaries (two sites).
    boundary = states.geometry.union_all().buffer(0.005)
    all_stations = [s for n in networks for s in n["Stations"]]
    points = gpd.points_from_xy([s["lng"] for s in all_stations], [s["lat"] for s in all_stations])
    conus_ids = {s["stationID"] for s, point in zip(all_stations, points) if boundary.covers(point)}
    station_summary, network_rows = summarize_stations(networks, conus_ids)

    def cmr(name, product, **params):
        query = {"collection_concept_id": COLLECTIONS[product], "page_size": 2000, **params}
        return entries(get(name, CMR + "granules.json?" + urlencode(query)))

    collections = {}
    for product in COLLECTIONS:
        c = entries(get(product + "-collection.json", CMR + "collections.json?" +
                        urlencode({"short_name": product, "version": "061"})))[0]
        if c["id"] != COLLECTIONS[product]:
            raise ValueError("CMR collection ID changed; review the audit")
        latest = cmr(product + "-latest.json", product, sort_key="-start_date", page_size=1)[0]
        collections[product] = {"id": c["id"], "start": c["time_start"],
                                "latest_granule_start": latest["time_start"],
                                "latest_granule_end": latest["time_end"]}
    lc = cmr("mcd12-2025.json", "MCD12Q1", temporal="2025-01-01T00:00:00Z,2025-12-31T23:59:59Z")
    lc_conus = cmr("mcd12-conus.json", "MCD12Q1", temporal="2025-01-01T00:00:00Z,2025-12-31T23:59:59Z",
                   bounding_box="-125,24,-66,50")
    # Compare latest complete-year footprint with previous-year footprint.
    lc_previous = cmr("mcd12-2024.json", "MCD12Q1", temporal="2024-01-01T00:00:00Z,2024-12-31T23:59:59Z")
    if {tile(x) for x in lc} != {tile(x) for x in lc_previous}:
        raise ValueError("2025 land-cover coverage differs from 2024; review common end date")
    conus_tiles = {tile(x) for x in lc_conus}
    samples = []
    for doy in (9, 97, 193, 281):
        day = date(2025, 1, 1) + timedelta(days=doy - 1)
        items = cmr(f"mcd15-{doy:03}.json", "MCD15A2H",
                    temporal=f"{day}T00:00:00Z,{day}T23:59:59Z")
        subset = [x for x in items if tile(x) in conus_tiles]
        if len(subset) != len(conus_tiles):
            raise ValueError("Missing sampled CONUS LAI tile")
        samples.append({"date": str(day), "global_tiles": len(items), "conus_tiles": len(subset),
                        "global_bytes": granule_bytes(items), "conus_bytes": granule_bytes(subset)})

    dpet_html = get("dpet-index.html", DPET, "html").decode()
    dpet_rows = re.findall(r"<tr\b[^>]*>.*?</tr>", dpet_html, flags=re.S)
    dpet_sizes = {}
    for year in range(START.year, END.year + 1):
        row = next((row for row in dpet_rows if f"{year}_daily_pet.nc" in row), None)
        if not row:
            raise ValueError(f"Missing dPET year {year}")
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)
        numbers = [int(x.strip()) for x in cells if x.strip().isdigit()]
        if len(numbers) != 1:
            raise ValueError("Cannot parse dPET byte size")
        dpet_sizes[str(year)] = numbers[0]
    get("chirps-index.html", CHIRPS, "html")
    chirps_sizes = {}
    for year in range(START.year, END.year + 1):
        data = get(f"chirps-{year}.head", CHIRPS + f"chirps-v2.0.{year}.days_p05.nc", "head")
        chirps_sizes[str(year)] = content_length(data)
    soils = {}
    for name in ("GLDASp5_soiltexture_025d.nc4", "GLDASp4_soilfraction_025d.nc4", "GLDASp5_porosity_025d.nc4"):
        data = get(name + ".head", SOILS + name, "head")
        soils[name] = content_length(data)
    snapshot = {"audit_date": datetime.now(timezone.utc).date().isoformat(),
                "start": str(START), "end": str(END), "sources": sources,
                "stations": station_summary, "modis_collections": collections,
                "land_cover": {"global_tiles": len(lc), "conus_tiles": len(lc_conus),
                               "global_bytes_per_year": granule_bytes(lc),
                               "conus_bytes_per_year": granule_bytes(lc_conus)},
                "lai_samples": samples, "dpet_annual_bytes": dpet_sizes,
                "chirps_annual_bytes": chirps_sizes, "gldas_bytes": soils}
    return snapshot, network_rows


def estimate(snapshot):
    """Calculate the report's two storage scenarios without network access.

    Station storage retains source files/responses plus packed predictors.
    ISMN is counted once; dPET and GLDAS retain their source files. The range
    varies ISMN record width (24–40 bytes) and sampled LAI seasonality only.
    """
    start, end = date.fromisoformat(snapshot["start"]), date.fromisoformat(snapshot["end"])
    if end < start:
        raise ValueError("Reversed study window")
    days, years, composites = (end - start).days + 1, end.year - start.year + 1, composite_count(start, end)
    annual_years = {str(y) for y in range(start.year, end.year + 1)}
    for product in ("chirps_annual_bytes", "dpet_annual_bytes"):
        if set(snapshot[product]) != annual_years:
            raise ValueError(f"{product} must cover exactly the study years")
    output = {"start": str(start), "end": str(end), "days": days, "annual_files": years,
              "lai_composites": composites, "units": "decimal GB (10^9 bytes)", "regions": {}}
    for region in ("global", "conus"):
        s = snapshot["stations"][region]
        sensor_days = s["sm_sensor_days"] + s["temperature_proxy_sensor_days"] + s["precipitation_proxy_station_days"]
        ismn = sensor_days * 24 * 32  # Hourly ASCII records, including flags.
        lai_sizes = [x[region + "_bytes"] for x in snapshot["lai_samples"]]
        full = {"ISMN_unpacked": ismn,
                "GLDAS": sum(snapshot["gldas_bytes"].values()),
                "CHIRPS": sum(snapshot["chirps_annual_bytes"].values()),
                "dPET": sum(snapshot["dpet_annual_bytes"].values()),
                "MCD12Q1": snapshot["land_cover"][region + "_bytes_per_year"] * years,
                "MCD15A2H": statistics.mean(lai_sizes) * composites}
        n = s["overlap_stations"]
        # All CONUS stations are in CHIRPS coverage (50S–50N). Out-of-domain
        # global stations retain missing-value slots but generate no requests.
        c = snapshot["stations"]["chirps_domain"]["overlap_stations"] if region == "global" else n
        sizes = REMOTE_SUBSET_BYTES_PER_STATION_DATE
        remote = {"ISMN_unpacked": ismn,
                  "GLDAS": full["GLDAS"] + n * 5 * 4,
                  "CHIRPS": c * days * sizes["CHIRPS"] + n * days * 4,
                  "dPET": full["dPET"] + n * days * 4,
                  "MCD12Q1": n * years * (sizes["MCD12Q1"] + 13),
                  "MCD15A2H": n * composites * (sizes["MCD15A2H"] + 6)}
        fixed = sum(full.values()) - ismn - full["MCD15A2H"]
        output["regions"][region] = {
            "full_native_GB": {k: v / 1e9 for k, v in full.items()},
            "full_native_total_GB": sum(full.values()) / 1e9,
            "station_remote_GB": {k: v / 1e9 for k, v in remote.items()},
            "station_remote_total_GB": sum(remote.values()) / 1e9,
            "full_native_range_GB": [(fixed + sensor_days * 24 * width + f(lai_sizes) * composites) / 1e9
                                     for width, f in ((24, min), (40, max))]}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, default=Path("docs/data_volume_inputs.json"))
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=Path("output/data_volume/cache"))
    parser.add_argument("--output-dir", type=Path, default=Path("output/data_volume"))
    args = parser.parse_args()
    if args.collect:
        snapshot, rows = collect(args.cache_dir)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "data_volume_inputs.json").write_text(json.dumps(snapshot, indent=2) + "\n")
        with (args.output_dir / "ismn_network_inventory.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    else:
        snapshot = json.loads(args.inputs.read_text())
    print(json.dumps(estimate(snapshot), indent=2))


if __name__ == "__main__":
    main()
