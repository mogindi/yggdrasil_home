#!/usr/bin/env bash
set -euo pipefail

# Create public remote-GPU billing flavors with the same compute sizing as
# m1.small. The actual GPU is provisioned separately by the RGPU service.

command -v openstack >/dev/null || {
  echo "ERROR: OpenStack CLI is not available on PATH." >&2
  exit 127
}
command -v python3 >/dev/null || {
  echo "ERROR: python3 is required to inspect Nova flavor data." >&2
  exit 127
}

small_json="$(openstack flavor show m1.small -f json)"
mapfile -t small_fields < <(
  python3 -c '
import json, sys
flavor = json.load(sys.stdin)
normalized = {str(key).lower(): value for key, value in flavor.items()}
aliases = {
    "vcpus": ("vcpus",),
    "ram": ("ram",),
    "disk": ("disk",),
    "ephemeral": ("os-flv-ext-data:ephemeral", "ephemeral"),
    "swap": ("swap",),
    "rxtx_factor": ("rxtx_factor", "rxtx factor"),
}
for field, names in aliases.items():
    value = next((normalized[name] for name in names if name in normalized), None)
    if value is None:
        raise SystemExit(f"m1.small is missing the {field} flavor field")
    print(0 if value == "" else value)
print(json.dumps(flavor.get("properties") or {}, sort_keys=True))
' <<<"$small_json"
)

if [[ "${#small_fields[@]}" -ne 7 ]]; then
  echo "ERROR: unable to read the complete m1.small flavor specification." >&2
  exit 1
fi

vcpus="${small_fields[0]}"
ram="${small_fields[1]}"
disk="${small_fields[2]}"
ephemeral="${small_fields[3]}"
swap="${small_fields[4]}"
rxtx_factor="${small_fields[5]}"
properties_json="${small_fields[6]}"
mapfile -t property_args < <(
  python3 -c '
import json, sys
for key, value in json.loads(sys.argv[1]).items():
    print("--property")
    print(f"{key}={value}")
' "$properties_json"
)

assert_matches_m1_small() {
  local flavor_name="$1" flavor_json
  flavor_json="$(openstack flavor show "$flavor_name" -f json)"
  python3 -c '
import json, sys
reference, actual = (json.loads(value) for value in sys.argv[1:3])
def normalize(flavor):
    values = {str(key).lower(): value for key, value in flavor.items()}
    return {
        "vcpus": int(values["vcpus"]),
        "ram": int(values["ram"]),
        "disk": int(values["disk"]),
        "ephemeral": int(values.get("os-flv-ext-data:ephemeral", values.get("ephemeral", 0)) or 0),
        "swap": int(values.get("swap", 0) or 0),
        "rxtx_factor": float(values.get("rxtx_factor", 1)),
        "properties": {
            str(key): str(value)
            for key, value in (flavor.get("properties") or {}).items()
        },
    }
expected, found = normalize(reference), normalize(actual)
if expected != found:
    raise SystemExit(f"{sys.argv[3]} does not match m1.small sizing: {found} != {expected}")
' "$small_json" "$flavor_json" "$flavor_name"
}

ensure_public() {
  local flavor_name="$1" flavor_json is_public
  flavor_json="$(openstack flavor show "$flavor_name" -f json)"
  is_public="$(python3 -c '
import json, sys
flavor = json.load(sys.stdin)
print(next((value for key, value in flavor.items()
            if str(key).lower() in {"is_public", "os-flavor-access:is_public"}), False))
' <<<"$flavor_json")"
  if [[ "${is_public,,}" != "true" ]]; then
    openstack flavor set --public "$flavor_name"
    flavor_json="$(openstack flavor show "$flavor_name" -f json)"
    is_public="$(python3 -c '
import json, sys
flavor = json.load(sys.stdin)
print(next((value for key, value in flavor.items()
            if str(key).lower() in {"is_public", "os-flavor-access:is_public"}), False))
' <<<"$flavor_json")"
  fi
  if [[ "${is_public,,}" != "true" ]]; then
    echo "ERROR: $flavor_name could not be made public." >&2
    return 1
  fi
}

for tier in T1 T2 T3 T4 T5; do
  flavor_name="${tier,,}.rgpu"
  if openstack flavor show "$flavor_name" -f value -c id >/dev/null 2>&1; then
    echo "Checking public flavor $flavor_name"
    assert_matches_m1_small "$flavor_name"
    ensure_public "$flavor_name"
    continue
  fi

  echo "Creating public flavor $flavor_name with m1.small sizing"
  openstack flavor create \
    --id auto \
    --vcpus "$vcpus" \
    --ram "$ram" \
    --disk "$disk" \
    --ephemeral "$ephemeral" \
    --swap "$swap" \
    --rxtx-factor "$rxtx_factor" \
    --public \
    "${property_args[@]}" \
    "$flavor_name" >/dev/null
  assert_matches_m1_small "$flavor_name"
  ensure_public "$flavor_name"
done
