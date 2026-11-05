"""Shared HTTPS sessions; retry idempotent reads, never export submissions."""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def session():
    s = requests.Session()
    s.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1, allowed_methods=['GET', 'HEAD'],
        status_forcelist=[429, 500, 502, 503, 504])))
    return s
