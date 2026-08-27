#!/usr/bin/env bash

set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root (for example: sudo $0)." >&2
  exit 1
fi

agent_venv="${FREEZER_AGENT_VENV:-/opt/freezer-agent}"
freezer_version="${FREEZER_VERSION:-17.0.0}"
freezerclient_version="${FREEZERCLIENT_VERSION:-6.1.0}"
openstack_release="${OPENSTACK_RELEASE:-2025.2}"
constraints_url="https://releases.openstack.org/constraints/upper/${openstack_release}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
image_dir="${script_dir}/../kolla-ansible/freezer-images"

python3 -m venv "${agent_venv}"
"${agent_venv}/bin/python" -m pip install --upgrade pip
"${agent_venv}/bin/python" -m pip install \
  -c "${constraints_url}" \
  "freezer==${freezer_version}" \
  "python-freezerclient==${freezerclient_version}"

# These entry points add application-credential support and bind the
# scheduler to its explicitly registered client ID. They run the upstream
# Freezer code installed in this virtualenv.
install -m 0755 "${image_dir}/freezer_agent.py" \
  "${agent_venv}/bin/freezer-agent"
install -m 0755 "${image_dir}/freezer_scheduler.py" \
  "${agent_venv}/bin/freezer-scheduler"

# The checked-in entry points use /usr/bin/env for the Kolla image. Point the
# self-managed copies at the virtualenv interpreter so they work without
# requiring the caller to activate the virtualenv first.
for entry_point in freezer-agent freezer-scheduler; do
  sed -i "1c#!${agent_venv}/bin/python" "${agent_venv}/bin/${entry_point}"
done

echo "Freezer agent installed in ${agent_venv}"
echo "Set OS_* credentials, then run:"
echo "  ${agent_venv}/bin/freezer-scheduler --client-id \"\${OS_PROJECT_ID}_\$(hostname -s)\" start"
