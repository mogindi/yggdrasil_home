#!/usr/bin/env python3
"""Insert the out-of-tree guard in Nova's authenticated v2.1 pipelines."""

from pathlib import Path
import re
import sys


path = Path(sys.argv[1])
contents = path.read_text()
lines = contents.splitlines(keepends=True)
pipeline_indexes = [
    index
    for index, line in enumerate(lines)
    if re.match(r"^\s*keystone\s*=", line) and "keystonecontext" in line
]
if len(pipeline_indexes) != 2:
    raise SystemExit(
        f"Expected two authenticated Nova API pipelines, found {len(pipeline_indexes)}; refusing to patch"
    )

for index in pipeline_indexes:
    lines[index] = lines[index].replace(
        "keystonecontext",
        "keystonecontext yggdrasil_rgpu_guard",
        1,
    )
contents = "".join(lines)
if "[filter:yggdrasil_rgpu_guard]" in contents:
    raise SystemExit("Nova api-paste.ini already contains the Yggdrasil filter")
contents += (
    "\n\n[filter:yggdrasil_rgpu_guard]\n"
    "paste.filter_factory = yggdrasil_nova_guard.middleware:filter_factory\n"
)
path.write_text(contents)
