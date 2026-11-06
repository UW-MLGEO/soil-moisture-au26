"""Native ISMN header and station-directory identities."""
import shlex

def station_key(name):
    """Normalize ISMN export folder names without guessing sensor identities."""
    return name.replace(" ", "").replace("_", "-")


def parse_header(member, header):
    parts = member.split('/')
    fields = shlex.split(header)
    if len(fields) != 9:  # not-science: ISMN native header has 9 fields
        raise ValueError(f'Unsupported native ISMN header: expected 9 fields, found {len(fields)}')
    campaign, network, native_station = fields[:3]  # not-science: ISMN native header has 9 fields
    if network != parts[0]:
        raise ValueError('Header network and archive member network disagree')
    latitude, longitude, elevation, depth_from, depth_to = map(float, fields[3:8])  # not-science: ISMN native header has 9 fields
    return {'campaign': campaign, 'network': network, 'native_station': native_station,
            'station_directory': parts[1], 'latitude': latitude, 'longitude': longitude,
            'elevation_m': elevation, 'depth_from': depth_from, 'depth_to': depth_to,
            'instrument': fields[8], 'units': 'm3/m3', 'timezone': 'UTC'}  # not-science: ISMN native header has 9 fields
