#!/bin/bash

set -euo pipefail

dir=$(dirname "$0")
out_dir="workspace/etc/kolla/config/prometheus"

mkdir -p "$out_dir"
find "$dir" -iname "*.rules" -print0 | xargs -0 -I % cp % "$out_dir"
