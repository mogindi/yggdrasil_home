#!/bin/bash

set -xeuo pipefail

dir=$(dirname "$0")
out_dir="workspace/etc/kolla/config/prometheus"

mkdir -p "$out_dir"
find "$dir" -iname "*.rules" | xargs -I % cp % "$out_dir"
