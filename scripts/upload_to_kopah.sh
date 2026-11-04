#!/usr/bin/env bash
# Run on Hyak: bash upload_to_kopah.sh SOURCE_URL S3_DESTINATION CACHE_FILE
set -euo pipefail

source_url=$1
destination=$2
cache_file=$3

mkdir -p "$(dirname "$cache_file")"
curl --fail --location --output "$cache_file" "$source_url"
AWS_PROFILE=kopah S3_ENDPOINT_URL=https://s3.kopah.uw.edu \
  s5cmd --retry-count 0 cp "$cache_file" "$destination"
