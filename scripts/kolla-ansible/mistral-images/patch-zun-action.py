"""Keep Mistral's Zun action compatible with modern python-zunclient."""

from pathlib import Path

import mistral_extra.actions.openstack.actions as actions


path = Path(actions.__file__)
source = path.read_text()
changes = (
    (
        "            zun_api_versions.MAX_API_VERSION,\n",
        "            zun_api_versions.APIVersion(zun_api_versions.MAX_API_VERSION),\n",
    ),
    (
        "            auth_url=keystone_endpoint.url,\n"
        "            session=session_and_auth['auth']\n",
        "            auth_url=keystone_endpoint.url,\n"
        "            session=session_and_auth['session']\n",
    ),
)

for needle, replacement in changes:
    if needle not in source:
        if replacement in source:
            continue
        raise RuntimeError(f"Expected one Zun compatibility target in {path}")
    if source.count(needle) != 1:
        raise RuntimeError(f"Expected one Zun compatibility target in {path}")
    source = source.replace(needle, replacement, 1)

path.write_text(source)
