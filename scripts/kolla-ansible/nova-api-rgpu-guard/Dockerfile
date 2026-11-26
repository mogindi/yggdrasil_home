ARG BASE_IMAGE=quay.io/openstack.kolla/nova-api:2025.2-ubuntu-noble
FROM ${BASE_IMAGE}

COPY yggdrasil_nova_guard /tmp/yggdrasil_nova_guard
COPY yggdrasil-approved-templates/ /etc/nova/yggdrasil-approved-templates/
COPY patch_api_paste.py /tmp/patch_api_paste.py

RUN site_packages="$(/var/lib/kolla/venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && install -d "$site_packages/yggdrasil_nova_guard" \
    && cp -a /tmp/yggdrasil_nova_guard/. "$site_packages/yggdrasil_nova_guard/" \
    && /var/lib/kolla/venv/bin/python /tmp/patch_api_paste.py /etc/nova/api-paste.ini \
    && rm -rf /tmp/yggdrasil_nova_guard /tmp/patch_api_paste.py
